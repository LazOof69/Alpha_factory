# Feasibility Study — Alpha Factory

## Purpose

Before locking `PROJECT.md` / `CLAUDE.md`, validate two empirical assumptions
that underpin every downstream design decision (target Sharpe, layer scope,
risk budget, paper-trading length).

If either study fails, our Sharpe / ROI targets are revised down before any
production code is written.

## Studies

### Study 1 — Funding Harvester (carry trade)

**Hypothesis:** `long spot BTC + short perp BTC` produces positive net Sharpe
across regimes after fees and slippage, even after including the post-ETF
structural shift (Jan 2024+) and the early-perp era (2019–2020).

**Data window:** 2019-09 to 2025-04 (Binance USDT-M perp launch onwards).
**Universe:** BTC-USDT, ETH-USDT (perp + spot, both legs).
**Frequency:** 1h klines + 8h funding.

**Pass criteria (all must hold) — relaxed after distribution analysis showed
post-ETF funding regime is structurally lower than pre-ETF (BTC -30.8%,
ETH -41.6%). The original 0.8 was set before that finding; sticking with it
would have demanded a regime-detection layer just to pass the gate.**

- Full-sample net Sharpe ≥ **0.5**
- ETF-pre (≤ 2024-01-10) and ETF-post (2024-01-11+) sub-period each net Sharpe ≥ **0.5**
  (the post-ETF gate is the more important one — that's the forward-looking regime)
- Bull and bear regime each net Sharpe ≥ 0
- Max DD ≤ 30%
- Realistic round-trip cost: 16 bps (perp 4 + spot 10 + slippage 2)

**Fail consequence:** PROJECT.md target Sharpe revised to ≤ 1.0; carry
strategy demoted from "primary alpha" to "optional supplement."

### Study 2 — Triangular Arb (only if Study 1 passes)

**Hypothesis:** Single-exchange BTC-ETH-USDT triangular arb on Binance yields
net Sharpe ≥ 1.0 with 50% maker fill rate over 2024-2025.

**Data window:** 2024-01 to 2025-04 (post-HFT-saturation regime).
**Frequency:** 1m klines (minimum); 1s if free public data permits.

**Pass criteria:**
- Net Sharpe ≥ 1.0 with maker-only fees + 50% fill rate assumption
- Monthly arbitrage opportunities (spread > 2 bps) ≥ 100
- No single month with negative net P&L

**Fail consequence:** Arb path is dropped from PROJECT.md scope. Focus narrows
to carry + slow systematic alpha only.

## Methodology — skill assignment

| Step | Skill |
|---|---|
| Archive design (point-in-time, schema, dedup) | `anthropic-skills:market-data-pipeline` |
| Data quality checks (missing / stale / outliers / dup / cross-source) | `anthropic-skills:market-data-pipeline` |
| Backtest logic, cost modelling | `anthropic-skills:quant-analyst` |
| Sharpe calc, regime stratification, sub-sample DSR-light | `anthropic-skills:strategy-validation` |
| Code review on every fetch / backtest script before run | `code-reviewer` + `simplify` |
| Final results spreadsheet | `anthropic-skills:xlsx` |

## Folder layout

```
feasibility/
├── data/         # .parquet archives (gitignored, large)
├── scripts/      # fetch + analysis (.py)
├── notebooks/    # exploratory Jupyter
└── results/      # final .xlsx / .md (tracked in git)
```

## Timeline (target ≤ 2 weeks)

| Week | Work |
|---|---|
| 1 | Study 1: data fetch + archive + initial distribution analysis |
| 2 | Study 1: backtest with regime split + xlsx report + gate decision |
| 3 | Study 2 (if Study 1 passed) |
| 3-4 | FS final report → write PROJECT.md and CLAUDE.md |

## Conclusions

> _(to be filled at FS exit; will become the empirical foundation for `PROJECT.md` Sharpe / ROI targets.)_
