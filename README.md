# Backtest Lie Detector

**Your backtest is probably lying to you. This runs 6 checks on it in 10 seconds.**

Point it at your trade log (CSV). It tells you whether the result can be trusted —
or which specific bug is inflating it.

No pandas. No dependencies. One file. Python 3.6+.

---

## The problem

You backtest a strategy. It shows +30% a year. You fund it. It loses money.

The strategy was not necessarily bad. **The backtest was lying.** The usual ways:

- A breakout order filled at the trigger price **even though the bar gapped straight past it** — you gave yourself a free gap
- The exit was only checked on bars where the signal fired — a look-ahead leak
- The instrument universe only contains things that **still exist today** — survivorship bias
- Fees were never charged — every "cost" is missing profit
- You tried 500 parameter sets and reported the best one — that is 25 expected false positives at 5%

Each of these has a signature. This tool looks for the signatures.

---

## Quick start

```bash
python backtest_lint.py trades.csv
```

With price bars (enables 2 more checks):

```bash
python backtest_lint.py trades.csv --prices prices.csv
```

### Try it on our demo first

The `demo/` folder contains **a backtest with 5 bugs deliberately planted in it**,
plus a clean control. Run both:

```bash
python make_demo.py            # regenerate the buggy demo
python make_demo.py --clean    # regenerate the clean control

python backtest_lint.py demo/trades.csv       --prices demo/prices.csv
python backtest_lint.py demo/trades_clean.csv --prices demo/prices_clean.csv
```

Expected result — and this is the whole point:

| Sample | Score | Verdict |
|---|---|---|
| `trades.csv` (5 bugs planted) | **1 / 6** | FAIL on 4 checks — every planted bug caught |
| `trades_clean.csv` (control) | **6 / 6** | No hard defects |

**If the tool always said "fail", it would be worthless.** It passes the clean one.

---

## The 6 checks

| # | Check | What it catches | Needs price data |
|---|---|---|---|
| 1 | Record self-consistency | `exit_time <= entry_time` (time travel), bad sizes, unparseable rows | — |
| 2 | **Impossible fills** | Fill price outside the bar's `[low, high]` — the price never traded. This is the gap-fill bug. | ✅ |
| 3 | Cost modeled | No fee column, or fees all zero | — |
| 4 | Statistical significance | `t` value and the sample size you would need for `t≈3`: `n = (3 × sd ÷ mean)²` | — |
| 5 | **Tail dependence** | P&L flips negative when you drop the best 1–3 trades, or one trade is ≥50% of net profit | — |
| 6 | Price data gaps | Irregular bar spacing — is it market closure, or missing data that invents fake gaps? | ✅ |

Exit code is `0` when there is no FAIL, `1` when there is — so you can wire it into CI.

---

## Output

```
1. [ FAIL    ] Impossible fills
        Fill price outside that bar's [low, high] range: 2 trade(s)
            row 17: fill 97.1854 but the bar range was [98.6654, 99.235] — that price never traded
          => classic cause: a breakout order filled at the trigger level even though
             the bar gapped straight through it
          => fix: BUY uses max(trigger, bar_open), SELL uses min(...)
```

Every FAIL comes with **the row number**, **the actual numbers**, and **the fix**.

---

## Input format

Trade log — columns are case-insensitive and have aliases:

| Column | Notes |
|---|---|
| `entry_time` | ISO timestamp or unix seconds (ms auto-detected) |
| `exit_time` | same format |
| `side` | `BUY`/`SELL` (also `long`/`short`/`多`/`空`). Missing => all treated as long |
| `entry_price` | fill price |
| `exit_price` | fill price |
| `size` | quantity > 0 |
| `fee` | **recommended** — total commission per trade. Missing or all-zero => check 3 fails |

Price bars:

| Column |
|---|
| `time, open, high, low, close` |

Renaming your columns is fine — see the alias list at the top of the file.

Chinese output: add `--lang zh`.

---

## What this does NOT do

Being explicit, because tools in this space usually overclaim:

- **It does not tell you a strategy works.** It can only reject, not confirm. A 6/6 score means "this check found nothing", not "you have an edge".
- **It cannot detect look-ahead bias automatically.** It flags the *symptom* (`exit_time <= entry_time`, impossible fills). Whether your code read future data is a code-review question.
- **It does not run your backtest.** You bring the trade log.
- **It does not do Monte Carlo or walk-forward for you.** This tool only checks the record you already have; those are separate exercises.
- It checks a *sample of signatures*, not all of them. Three controls still have to be done by hand: reverse, random, and lock one test set you touch exactly once.

**This is a sieve, not a mine.** It helps you throw out the false. It cannot find the true.

---

## License

MIT. Use it, ship it, fork it.

If it catches something in your backtest, that is the tool doing its job — not a bug.
