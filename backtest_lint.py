#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backtest_lint.py — does your backtest lie to you?

Point it at your trade log (CSV). It runs 6 automatic checks and tells you
whether the result can be trusted.

No pandas. Standard library only. Python 3.6+.

Usage
-----
    # trade log only (4 checks)
    python backtest_lint.py trades.csv

    # trade log + price bars (6 checks, adds impossible-fill and data-gap)
    python backtest_lint.py trades.csv --prices prices.csv

    # Chinese output
    python backtest_lint.py trades.csv --prices prices.csv --lang zh

Trade log columns (case-insensitive)
------------------------------------
    entry_time    ISO timestamp or unix seconds
    exit_time     same format
    side          BUY / SELL  (aliases: long, short, 多, 空)
    entry_price   fill price at entry
    exit_price    fill price at exit
    size          quantity > 0
    fee           [recommended] total commission per trade.
                  Missing or all-zero => "cost not modeled" FAIL.

Price bar columns
-----------------
    time, open, high, low, close

Exit code: 0 = no FAIL, 1 = at least one FAIL.
"""
from __future__ import print_function

import argparse
import bisect
import csv
import io
import os
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- messages
LANG = "en"

M = {
    # status
    "pass": ("PASS", "通过"),
    "warn": ("WARN", "注意"),
    "fail": ("FAIL", "不通过"),
    "skip": ("SKIP", "跳过"),
    # header
    "title": ("Backtest Lie Detector — report", "回测谎言检测报告"),
    "trades_file": ("Trade log", "成交记录"),
    "prices_file": ("Price data", "价格数据"),
    "no_prices": ("(not given — 2 checks skipped)", "（未提供，2 项跳过）"),
    "n_trades": ("Trades", "成交笔数"),
    "no_side": ("NOTE: no 'side' column — every trade treated as LONG. "
                "Add the column for short strategies.",
                "⚠ 没有方向列，已一律按多头处理 —— 空头策略请补上该列"),
    "score": ("Score: %d / %d checks passed", "记分：%d / %d 项通过"),
    "concl_fail": ("VERDICT: hard defects found. This backtest cannot be trusted yet.",
                   "结论：★ 有硬伤 —— 这个回测结果现在不能信。"),
    "concl_pass": ("VERDICT: no hard defects detected.",
                   "结论：这份记录没查出明显硬伤。"),
    "concl_pass2": ("  That only means this check found nothing — it does NOT mean the "
                    "strategy works.",
                    "  但这只说明「没被这份检查抓住」，不等于策略有效 ——"),
    "concl_pass3": ("  Still do by hand: reverse control, random control, and lock one "
                    "test set you touch only once.",
                    "  手工还要做：反向对照、随机对照、锁一块测试集只用一次。"),
    "concl_warn": ("VERDICT: no hard defects, but items above need your judgement.",
                   "结论：没有硬伤，但有需要你人工确认的项。"),
    "sieve": ("This is a sieve, not a mine: it can reject the false, it cannot find the true.",
              "这套检查是筛子，不是矿：它能帮你否掉假的，不能帮你找到真的。"),
    # check names
    "c1": ("Record self-consistency (time / size / price)", "记录自洽（时间/数量/价格）"),
    "c2": ("Impossible fills", "不可能成交价"),
    "c3": ("Cost modeled", "成本已建模"),
    "c4": ("Statistical significance", "统计显著性"),
    "c5": ("Tail dependence", "尾部依赖"),
    "c6": ("Price data gaps", "价格数据缺口"),
    "c2_need_prices": ("No price data given. Add --prices prices.csv to enable.",
                       "未提供价格数据，跳过。加上 --prices prices.csv 可启用本条。"),
    "c6_need_prices": ("No price data given", "未提供价格数据"),
    # c1 details
    "c1_bad_time": ("Unparseable time on %d trade(s)", "%d 笔时间无法解析"),
    "c1_timetravel": ("exit_time <= entry_time on %d trade(s)", "★ 平仓时间 <= 开仓时间：%d 笔"),
    "c1_bad_size": ("Missing or non-positive size on %d trade(s)", "数量缺失或 <=0：%d 笔"),
    "c1_bad_price": ("Missing price on %d trade(s)", "价格缺失：%d 笔"),
    "c1_ok": ("All %d trades parsed", "全部 %d 笔均可解析"),
    # c2 details
    "c2_outside": ("Fill price outside that bar's [low, high] range: %d trade(s)",
                   "★ 成交价落在当根 K 线 [最低,最高] 之外：%d 笔"),
    "c2_row": ("    row %d: fill %.6g but the bar range was [%.6g, %.6g] — "
               "that price never traded",
               "    行 %d：成交 %.6g，但该 K 线区间是 [%.6g, %.6g] —— 这个价位当时根本没成交过"),
    "c2_cause": ("  => classic cause: a breakout order filled at the trigger level even "
                 "though the bar gapped straight through it",
                 "  ⇒ 典型原因：跳空开盘时用挂单价成交（把跳空白送给自己）"),
    "c2_fix": ("  => fix: BUY uses max(trigger, bar_open), SELL uses min(...)",
               "  ⇒ 修法：BUY 用 max(挂单价, 当根开盘价)，SELL 用 min(...)"),
    "c2_sus": ("? Fill better than the bar open on %d trade(s) "
               "(explainable by limit orders — please confirm)",
               "? 成交价优于当根开盘价：%d 笔（限价单可解释，请人工确认）"),
    "c2_sus_row": ("    row %d: fill %.6g vs open %.6g", "    行 %d：成交 %.6g vs 开盘 %.6g"),
    "c2_ok": ("Every fill sits inside its bar's [low, high]", "全部成交价都落在对应 K 线区间内"),
    # c3 details
    "c3_nocol": ("No fee/commission column in the trade log.",
                 "★ 成交记录里没有手续费列"),
    "c3_nocol2": ("  => a backtest without costs is systematically optimistic",
                  "  ⇒ 未建模成本的回测，净利一定偏乐观"),
    "c3_nocol3": ("  => fix: at minimum charge one side of the spread times size",
                  "  ⇒ 修法：至少按 单边点差 × 数量 扣一次"),
    "c3_empty": ("Fee column exists but every value is empty.", "★ 有手续费列，但全部为空"),
    "c3_zero": ("Fee column exists but every value is 0.",
                "★ 手续费列存在但全部为 0"),
    "c3_zero2": ("  => check for missing: commission / spread / slippage / funding",
                 "  ⇒ 检查是否漏扣了：手续费 / 点差 / 滑点 / 资金费"),
    "c3_ok": ("%d trades carry fees, mean %.6g", "有 %d 笔带手续费，平均每笔 %.6g"),
    # c4 details
    "c4_few": ("Too few trades (<2)", "成交笔数太少（<2）"),
    "c4_stat": ("n = %d, mean per trade %.6g, sd %.6g",
                "笔数 n = %d，每笔均值 %.6g，每笔标准差 %.6g"),
    "c4_neg": ("★ Mean per trade is negative — the strategy itself does not make money. "
               "Significance is moot.",
               "★ 每笔均值为负 —— 这个策略本身不赚钱，先别谈显著性"),
    "c4_zero_sd": ("Zero variance across trades — very unnatural, check the data.",
                   "每笔零波动 —— 极不自然，请检查数据"),
    "c4_t": ("t = %.2f   (rule of thumb: |t| < 2 is basically no evidence)",
             "t 值 = %.2f   （经验上 |t| < 2 基本等于没有证据）"),
    "c4_need": ("To reach t ~ 3 you need about %d trades; you have %d.",
                "要拿到 t≈3，你需要约 %d 笔；你现在有 %d 笔"),
    "c4_short": ("★ Sample too small: short by about %d trades (%.1fx).",
                 "★ 样本不足：还差约 %d 笔（%.1f 倍）"),
    "c4_ok": ("Sample size clears the t~3 threshold (necessary, not sufficient).",
              "样本量达到 t≈3 的门槛（但这只是必要条件，不是充分条件）"),
    # c5 details
    "c5_few": ("Too few trades (<3)", "成交笔数太少（<3）"),
    "c5_total": ("Total net P&L %.6g over %d trades", "总净盈亏 %.6g（%d 笔）"),
    "c5_drop": ("Drop best %d -> %.6g", "去掉最好的 %d 笔后：%.6g"),
    "c5_share": ("Best single trade = %.1f%% of total net profit",
                 "最好那一笔占总净利 %.1f%%"),
    "c5_flip": ("★ Flips negative once you drop the best %d — the result rests on a few "
                "extreme events and will not repeat.",
                "★ 去掉最好的 %d 笔就翻负 —— 结果由少数极端事件撑起，不可重复"),
    "c5_conc": ("★ One trade contributes >=50% of net profit — this is one lucky trade, "
                "not a strategy.",
                "★ 单笔贡献 ≥50% 净利 —— 这个结果本质上是「一笔运气」，不是「一个策略」"),
    "c5_conc2": ("  => look at what happened in that trade, then recompute assuming it "
                 "did not happen.",
                 "  ⇒ 去看那一笔发生了什么，并假设它没发生，重新算一遍"),
    "c5_ok": ("Still positive after dropping the best 3, and no single trade dominates.",
              "去掉最好的 3 笔仍为正，且无单笔超半壁"),
    # c6 details
    "c6_few": ("Too few bars", "价格行数太少"),
    "c6_dupe": ("Duplicate timestamps — cannot measure spacing",
                "同一时间戳重复，无法判断间隔"),
    "c6_stat": ("%d bars, normal spacing %.6g s", "K 线共 %d 根，常规间隔 %.6g 秒"),
    "c6_holes": ("★ %d irregular gaps, largest %.6g s (~%.1fx normal)",
                 "★ 发现 %d 处间隔异常，最大 %.6g 秒（约 %.1f 倍常规间隔）"),
    "c6_holes2": ("  => first decide: market closed (fine) or missing data "
                  "(invents fake gaps in your backtest)?",
                  "  ⇒ 先确认是休市（正常），还是丢数据（会让回测凭空多出跳空）"),
    # errors
    "err_no_file": ("File not found: %s", "找不到文件：%s"),
    "err_empty": ("Trade log is empty: %s", "成交记录是空的：%s"),
    "err_cols": ("Missing required column(s): %s\nColumns found: %s\n"
                 "(accepted aliases are documented at the top of this file)",
                 "缺少必需的列：%s\n实际列名：%s\n（可用别名见脚本顶部注释）"),
}


def t(key, *args):
    """Look up a message in the active language."""
    en, zh = M[key]
    s = zh if LANG == "zh" else en
    return s % args if args else s


# ---------------------------------------------------------------- aliases
ALIAS = {
    "entry_time": ["entry_time", "entrytime", "open_time", "entry date",
                   "开仓时间", "入场时间", "time"],
    "exit_time": ["exit_time", "exittime", "close_time", "exit date",
                  "平仓时间", "出场时间"],
    "side": ["side", "direction", "dir", "type", "方向", "多空"],
    "entry_price": ["entry_price", "entryprice", "entry", "open_price",
                    "开仓价", "入场价", "buy_price"],
    "exit_price": ["exit_price", "exitprice", "exit", "close_price",
                   "平仓价", "出场价", "sell_price"],
    "size": ["size", "qty", "quantity", "shares", "amount", "volume",
             "数量", "股数"],
    "fee": ["fee", "fees", "commission", "cost", "costs", "手续费", "费用"],
}


# ---------------------------------------------------------------- helpers
def parse_time(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        v = float(s)
        if v > 1e11:      # milliseconds
            v /= 1000.0
        return v
    except ValueError:
        pass
    s2 = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s2[:26], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return None


def to_float(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm_side(s):
    if s is None:
        return None
    v = str(s).strip().lower()
    if v in ("buy", "b", "long", "l", "多", "做多", "1"):
        return 1
    if v in ("sell", "s", "short", "空", "做空", "-1"):
        return -1
    return None


def read_csv_rows(path):
    with io.open(path, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        if not rd.fieldnames:
            return [], []
        mapping = {real: real.strip().lower() for real in rd.fieldnames}
        rows = [{mapping[k]: v for k, v in raw.items() if k in mapping}
                for raw in rd]
        return list(mapping.values()), rows


def pick_col(header, key):
    for cand in ALIAS[key]:
        if cand in header:
            return cand
    return None


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    if n % 2:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2.0


class Report(object):
    def __init__(self):
        self.items = []

    def add(self, name, status, detail=None):
        self.items.append((name, status, detail or []))

    def score(self):
        checked = [i for i in self.items if i[1] in ("PASS", "WARN", "FAIL")]
        return len([i for i in checked if i[1] == "PASS"]), len(checked)

    def has_fail(self):
        return any(i[1] == "FAIL" for i in self.items)


# ---------------------------------------------------------------- loading
def load_trades(path):
    header, rows = read_csv_rows(path)
    if not rows:
        raise SystemExit(t("err_empty", path))
    cols = {k: pick_col(header, k) for k in ALIAS}
    missing = [k for k in ("entry_time", "exit_time", "entry_price",
                           "exit_price", "size") if not cols.get(k)]
    if missing:
        raise SystemExit(t("err_cols", ", ".join(missing), ", ".join(header)))

    trades = []
    for i, r in enumerate(rows, start=2):
        side = norm_side(r.get(cols["side"])) if cols.get("side") else 1
        trades.append({
            "row": i,
            "entry_t": parse_time(r.get(cols["entry_time"])),
            "exit_t": parse_time(r.get(cols["exit_time"])),
            "entry": to_float(r.get(cols["entry_price"])),
            "exit": to_float(r.get(cols["exit_price"])),
            "size": to_float(r.get(cols["size"])),
            "side": side if side in (1, -1) else 1,
            "fee": to_float(r.get(cols["fee"])) if cols.get("fee") else None,
        })
    return cols, trades


def load_prices(path):
    _, rows = read_csv_rows(path)
    out = []
    for r in rows:
        out.append({
            "t": parse_time(r.get("time")),
            "open": to_float(r.get("open")),
            "high": to_float(r.get("high")),
            "low": to_float(r.get("low")),
            "close": to_float(r.get("close")),
        })
    return [b for b in out if b["t"] is not None]


# ---------------------------------------------------------------- checks
def check_basic(rep, trades):
    bad_time, tt, bad_size, bad_price = [], [], [], []
    for x in trades:
        if x["entry_t"] is None or x["exit_t"] is None:
            bad_time.append(x["row"])
        elif x["exit_t"] <= x["entry_t"]:
            tt.append(x["row"])
        if x["size"] is None or x["size"] <= 0:
            bad_size.append(x["row"])
        if x["entry"] is None or x["exit"] is None:
            bad_price.append(x["row"])

    detail = []
    if bad_time:
        detail.append(t("c1_bad_time", len(bad_time)) + " (rows %s)"
                      % ", ".join(map(str, bad_time[:5])))
    if tt:
        detail.append(t("c1_timetravel", len(tt)) + " (rows %s)"
                      % ", ".join(map(str, tt[:5])))
    if bad_size:
        detail.append(t("c1_bad_size", len(bad_size)))
    if bad_price:
        detail.append(t("c1_bad_price", len(bad_price)))

    if tt:
        rep.add(t("c1"), "FAIL", detail)
    elif detail:
        rep.add(t("c1"), "WARN", detail)
    else:
        rep.add(t("c1"), "PASS", [t("c1_ok", len(trades))])


def check_impossible_fill(rep, trades, prices):
    if not prices:
        rep.add(t("c2"), "SKIP", [t("c2_need_prices")])
        return

    bars = sorted(prices, key=lambda b: b["t"])
    times = [b["t"] for b in bars]

    def bar_at(ts):
        if ts is None:
            return None
        i = bisect.bisect_right(times, ts) - 1
        return bars[i] if i >= 0 else None

    outside, suspicious = [], []
    for x in trades:
        if x["entry"] is None or x["entry_t"] is None:
            continue
        b = bar_at(x["entry_t"])
        if b is None or b["low"] is None or b["high"] is None:
            continue
        lo, hi, op = b["low"], b["high"], b["open"]
        if not (lo - 1e-12 <= x["entry"] <= hi + 1e-12):
            outside.append((x["row"], x["entry"], lo, hi))
        elif op is not None and op > 0:
            thr = op * 0.0005      # ignore sub-5bp noise
            if x["side"] == 1 and x["entry"] < op - thr:
                suspicious.append((x["row"], x["entry"], op))
            elif x["side"] == -1 and x["entry"] > op + thr:
                suspicious.append((x["row"], x["entry"], op))

    detail = []
    if outside:
        detail.append(t("c2_outside", len(outside)))
        for row, px, lo, hi in outside[:8]:
            detail.append(t("c2_row", row, px, lo, hi))
        detail.append(t("c2_cause"))
        detail.append(t("c2_fix"))
    if suspicious:
        detail.append(t("c2_sus", len(suspicious)))
        for row, px, op in suspicious[:5]:
            detail.append(t("c2_sus_row", row, px, op))

    if outside:
        rep.add(t("c2"), "FAIL", detail)
    elif suspicious:
        rep.add(t("c2"), "WARN", detail)
    else:
        rep.add(t("c2"), "PASS", [t("c2_ok")])


def check_cost(rep, trades, cols):
    has = cols.get("fee") is not None
    vals = [x["fee"] for x in trades if x["fee"] is not None]
    nonzero = [v for v in vals if abs(v) > 1e-12]

    if not has:
        rep.add(t("c3"), "FAIL", [t("c3_nocol"), t("c3_nocol2"), t("c3_nocol3")])
    elif not vals:
        rep.add(t("c3"), "FAIL", [t("c3_empty")])
    elif not nonzero:
        rep.add(t("c3"), "FAIL", [t("c3_zero"), t("c3_zero2")])
    else:
        avg = sum(abs(v) for v in vals) / len(vals)
        rep.add(t("c3"), "PASS", [t("c3_ok", len(nonzero), avg)])


def trade_pnls(trades):
    out = []
    for x in trades:
        if None in (x["entry"], x["exit"], x["size"]):
            continue
        out.append((x["exit"] - x["entry"]) * x["size"] * x["side"] - (x["fee"] or 0.0))
    return out


def check_significance(rep, trades):
    net = trade_pnls(trades)
    n = len(net)
    if n < 2:
        rep.add(t("c4"), "SKIP", [t("c4_few")])
        return

    mean = sum(net) / n
    var = sum((v - mean) ** 2 for v in net) / (n - 1)
    sd = var ** 0.5
    detail = [t("c4_stat", n, mean, sd)]

    if mean <= 0:
        detail.append(t("c4_neg"))
        rep.add(t("c4"), "FAIL", detail)
        return
    if sd <= 0:
        detail.append(t("c4_zero_sd"))
        rep.add(t("c4"), "WARN", detail)
        return

    tv = mean / (sd / (n ** 0.5))
    need = (3.0 * sd / mean) ** 2
    detail.append(t("c4_t", tv))
    detail.append(t("c4_need", int(need + 0.5), n))
    if n < need:
        detail.append(t("c4_short", int(need + 0.5) - n, need / n))
        rep.add(t("c4"), "WARN", detail)
    else:
        detail.append(t("c4_ok"))
        rep.add(t("c4"), "PASS", detail)


def check_tail(rep, trades):
    net = trade_pnls(trades)
    if len(net) < 3:
        rep.add(t("c5"), "SKIP", [t("c5_few")])
        return

    net.sort(reverse=True)
    total = sum(net)
    detail = [t("c5_total", total, len(net))]

    flipped = None
    for k in (1, 2, 3):
        if len(net) <= k:
            break
        rest = sum(net[k:])
        detail.append(t("c5_drop", k, rest))
        if rest <= 0 and flipped is None:
            flipped = k

    share = (net[0] / total) if total > 0 else None
    if share is not None:
        detail.append(t("c5_share", share * 100.0))

    if flipped is not None:
        detail.append(t("c5_flip", flipped))
        rep.add(t("c5"), "FAIL", detail)
    elif share is not None and share >= 0.5:
        detail.append(t("c5_conc"))
        detail.append(t("c5_conc2"))
        rep.add(t("c5"), "FAIL", detail)
    else:
        detail.append(t("c5_ok"))
        rep.add(t("c5"), "PASS", detail)


def check_price_gaps(rep, prices):
    if not prices:
        rep.add(t("c6"), "SKIP", [t("c6_need_prices")])
        return

    ts = sorted(b["t"] for b in prices if b.get("t") is not None)
    if len(ts) < 3:
        rep.add(t("c6"), "SKIP", [t("c6_few")])
        return

    diffs = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        rep.add(t("c6"), "WARN", [t("c6_dupe")])
        return

    md = median(diffs)
    holes = [d for d in diffs if md and d > md * 1.5 + 1e-9]
    detail = [t("c6_stat", len(ts), md)]
    if holes:
        biggest = max(holes)
        detail.append(t("c6_holes", len(holes), biggest, biggest / md if md else 0))
        detail.append(t("c6_holes2"))
        rep.add(t("c6"), "WARN", detail)
    else:
        rep.add(t("c6"), "PASS", detail)


# ---------------------------------------------------------------- main
def main():
    global LANG

    ap = argparse.ArgumentParser(
        description="Backtest Lie Detector — check whether your backtest is lying to you")
    ap.add_argument("trades", help="trade log CSV")
    ap.add_argument("--prices", help="price bars CSV (optional, enables 2 more checks)")
    ap.add_argument("--lang", choices=["en", "zh"], default="en",
                    help="output language (default: en)")
    a = ap.parse_args()
    LANG = a.lang

    if not os.path.exists(a.trades):
        raise SystemExit(t("err_no_file", a.trades))

    cols, trades = load_trades(a.trades)

    prices = None
    if a.prices:
        if not os.path.exists(a.prices):
            raise SystemExit(t("err_no_file", a.prices))
        prices = load_prices(a.prices)

    rep = Report()
    check_basic(rep, trades)
    check_impossible_fill(rep, trades, prices)
    check_cost(rep, trades, cols)
    check_significance(rep, trades)
    check_tail(rep, trades)
    check_price_gaps(rep, prices)

    print("=" * 72)
    print(t("title"))
    print("=" * 72)
    print("%s: %s" % (t("trades_file"), a.trades))
    print("%s: %s" % (t("prices_file"),
                      a.prices if a.prices else t("no_prices")))
    print("%s: %d" % (t("n_trades"), len(trades)))
    if cols.get("side") is None:
        print(t("no_side"))
    print()

    for idx, (name, status, detail) in enumerate(rep.items, start=1):
        print("%d. [ %-7s ] %s" % (idx, t(status.lower()), name))
        for line in detail:
            print("        " + line)
        print()

    passed, checked = rep.score()
    print("-" * 72)
    print(t("score", passed, checked))
    if rep.has_fail():
        print(t("concl_fail"))
    elif passed == checked:
        print(t("concl_pass"))
        print(t("concl_pass2"))
        print(t("concl_pass3"))
    else:
        print(t("concl_warn"))
    print("-" * 72)
    print(t("sieve"))

    return 1 if rep.has_fail() else 0


if __name__ == "__main__":
    sys.exit(main())
