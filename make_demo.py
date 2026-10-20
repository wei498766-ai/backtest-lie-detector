#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_demo.py — 生成一份「故意埋了 bug」的示例回测，用来验证 backtest_lint.py 真能抓到东西。

埋的 5 个坑：
  1. 平仓时间早于开仓时间（未来函数/时间错位）
  2. 成交价落在当根 K 线区间之外（跳空白送）
  3. 手续费全部为 0（未建模成本）
  4. 利润全靠 1 笔神话单（尾部依赖）
  5. 样本量远不够（统计上无意义）

用法：
    python make_demo.py            # 生成 demo/ 下两个 CSV
    python make_demo.py --out .    # 指定输出目录
"""
from __future__ import print_function

import argparse
import csv
import io
import os
import random
from datetime import datetime, timedelta

try:
    sys_stdout_ok = True
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo", help="输出目录（默认 demo）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--clean", action="store_true",
                    help="生成一份「干净」的对照样例（不埋坑），用来确认检测器能判通过")
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    os.makedirs(a.out, exist_ok=True)
    suffix = "_clean" if a.clean else ""

    # ---------- 1. 造 200 根 5 分钟 K 线 ----------
    t0 = datetime(2026, 1, 2, 0, 0, 0)
    bars = []
    px = 100.0
    for i in range(200):
        t = t0 + timedelta(minutes=5 * i)
        op = px
        # 每根随机小幅波动；每 40 根插一个跳空
        if i > 0 and i % 40 == 0:
            px = px * (1 + rnd.choice([-1, 1]) * rnd.uniform(0.004, 0.010))
            op = px
        hi = op * (1 + abs(rnd.gauss(0, 0.0012)))
        lo = op * (1 - abs(rnd.gauss(0, 0.0012)))
        cl = rnd.uniform(lo, hi)
        bars.append({"time": t, "open": op, "high": hi, "low": lo, "close": cl})
        px = cl

    prices_path = os.path.join(a.out, "prices%s.csv" % suffix)
    with io.open(prices_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close"])
        for b in bars:
            w.writerow([b["time"].strftime("%Y-%m-%dT%H:%M:%S"),
                        "%.4f" % b["open"], "%.4f" % b["high"],
                        "%.4f" % b["low"], "%.4f" % b["close"]])

    # ---------- 2. 造成交记录（大部分在 K 线内合法） ----------
    # 干净模式用更多笔数（150）来演示「样本够 + 利润分散」的正常情况
    n_trades = 150 if a.clean else 40
    step = 1 if a.clean else 4
    trades = []
    for k in range(n_trades):
        i = 5 + k * step
        if i + 3 >= len(bars):
            break
        eb, xb = bars[i], bars[i + 3]
        if a.clean:
            # 干净模式：模拟一个「有真实微弱优势」的策略 —— 方向判断 80% 正确，
            # 且按市价单建模（在当根开盘价基础上付一点滑点）。
            # 这样净利为正、利润分散在几十笔上，且不会误报「成交价优于开盘价」。
            exit_ = rnd.uniform(xb["low"], xb["high"])
            true_dir = 1 if exit_ >= eb["open"] else -1
            if rnd.random() < 0.80:
                side = "BUY" if true_dir == 1 else "SELL"
            else:
                side = "SELL" if true_dir == 1 else "BUY"
            slip = 0.0001
            entry = eb["open"] * (1 + slip) if side == "BUY" else eb["open"] * (1 - slip)
            entry = min(max(entry, eb["low"]), eb["high"])  # 不出区间
        else:
            entry = rnd.uniform(eb["low"], eb["high"])
            exit_ = rnd.uniform(xb["low"], xb["high"])
            side = rnd.choice(["BUY", "SELL"])
        size = 1.0
        trades.append({
            "entry_time": eb["time"].strftime("%Y-%m-%dT%H:%M:%S"),
            "exit_time": xb["time"].strftime("%Y-%m-%dT%H:%M:%S"),
            "side": side,
            "entry_price": "%.4f" % entry,
            "exit_price": "%.4f" % exit_,
            "size": "%.4f" % size,
            # 干净模式：扣真实量级的手续费；埋坑模式：0
            "fee": ("%.4f" % (entry * size * 0.0002)) if a.clean else "0",
        })

    if not a.clean:
        # 坑 1：平仓早于开仓（时间错位 / 未来函数）
        bad = dict(trades[7])
        bad["entry_time"] = bars[60]["time"].strftime("%Y-%m-%dT%H:%M:%S")
        bad["exit_time"] = bars[58]["time"].strftime("%Y-%m-%dT%H:%M:%S")
        trades[7] = bad

        # 坑 2：成交价落在当根 K 线之外（跳空白送）
        bad2 = dict(trades[15])
        low_of_bar = bars[5 + 15 * 4]["low"]
        bad2["entry_price"] = "%.4f" % (low_of_bar * 0.985)   # 比最低价还低 1.5%
        trades[15] = bad2

        # 坑 4：塞一笔神话单（让利润全靠它）
        myth = dict(trades[20])
        myth["side"] = "BUY"
        myth["entry_price"] = "%.4f" % bars[85]["low"]
        myth["exit_price"] = "%.4f" % (bars[85]["low"] * 1.6)
        trades[20] = myth

    trades_path = os.path.join(a.out, "trades%s.csv" % suffix)
    prices_path = os.path.join(a.out, "prices%s.csv" % suffix)
    with io.open(trades_path, "w", encoding="utf-8", newline="") as f:
        cols = ["entry_time", "exit_time", "side", "entry_price", "exit_price", "size", "fee"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for t in trades:
            w.writerow(t)

    print("已生成：")
    print("  %s   （%d 根 K 线）" % (prices_path, len(bars)))
    print("  %s   （%d 笔成交）" % (trades_path, len(trades)))
    print()
    print("这份示例故意埋了 5 个坑：")
    print("  1) 有一笔平仓时间早于开仓时间")
    print("  2) 有一笔成交价低于当根 K 线的最低价")
    print("  3) 手续费全部为 0")
    print("  4) 利润全靠一笔神话单")
    print("  5) 样本量远不够")
    print()
    print("跑一下看能不能抓出来：")
    print("  python backtest_lint.py %s --prices %s"
          % (trades_path, prices_path))


if __name__ == "__main__":
    main()
