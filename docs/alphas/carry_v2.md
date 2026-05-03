# Carry V2 — BTC-USDT Funding Carry, Regime-Aware

> **Status:** PASS-WITH-CAVEATS (post-3-phase audit, Phase A.5.3)
> **Last revised:** 2026-05-03
> **Implementation:** [`src/alpha_factory/alpha/carry.py`](../../src/alpha_factory/alpha/carry.py)
> **Validation run_id:** `dce76976-e9d7-4905-9b0d-48a27f6ef1bb`

---

## Economic Story

Positive funding rates in crypto perpetual-swap markets reflect a structural
long-bias of speculators: perp longs pay perp shorts when the rate is
positive. A delta-neutral pair of (long spot + short perp) at equal notional
captures this funding flow without taking directional price risk — the
"futures basis carry" trade, well-documented cross-asset.

The trade earns funding when the rate is positive AND the perp-spot basis is
mean-reverting (so price drift cancels). It LOSES when the funding regime
flips negative (perp shorts pay perp longs), incurring funding cost on the
short perp leg.

Capacity is bounded by perp orderbook depth and funding-rate sensitivity to
position size; at retail $1k notional, well below any practical ceiling.

### Why funding is structurally positive

- Crypto retail flow is dominantly long-biased on perps.
- Market-makers shorting against retail longs hedge by buying spot →
  long-spot demand props perp basis above spot, mechanically pulling
  funding positive.
- The 8-hour funding settlement is the price retail pays to keep that
  unhedged long exposure.

The carry alpha is essentially **paid to be the market-maker counterparty
to retail longs**, on a delta-neutral book.

---

## V2 Regime-Detection Layer

CLAUDE.md red line: carry strategy in production must have a funding-regime
detection layer. V2 implements a 7-day MA hysteresis state machine over
realized funding settlements:

```
active   -> exited   when 7d_MA(funding_rate) <  EXIT_FUNDING_7DMA  (-1 bp/8h)
exited   -> active   when 7d_MA(funding_rate) >  REENTRY_FUNDING_7DMA (+0.5 bp/8h)
```

Thresholds calibrated to economic break-even of transition cost:

- Round-trip = 16 bp; one-way = 8 bp.
- Avoiding 1 week of cumulative funding requires |7d sum| > 16 bp.
- 7d sum = 21 settlements * mean_per_8h, so per-settlement
  threshold ≈ -16/21 ≈ -0.76 bp/8h. Rounded to -1 bp/8h.
- Re-entry at +0.5 bp/8h to avoid threshold flutter.

### V2 firing record on real BTC archive (2019-09-25 → 2026-05-03)

| transition | time (UTC) | event |
|---|---|---|
| active → exited | 2020-03-14 16:00 | COVID crash; funding briefly broke negative |
| exited → active | 2020-04-29 08:00 | funding recovered to positive regime |
| active → exited | 2021-07-25 00:00 | mid-bull volatility; 7d MA slipped negative |
| exited → active | 2021-08-02 00:00 | resumed positive funding |
| active → exited | 2022-11-11 00:00 | FTX collapse; sustained negative funding |
| exited → active | 2022-12-18 00:00 | funding stabilized |

**Total: 6 transitions over 6.6 years; 96.24% of bars active.**

V2 fires correctly during real prolonged negative-funding regimes (3 out of
3 known crises). It does NOT detect "funding compression toward zero" — the
threat profile of 2025-2026 (see Caveat #1 below).

---

## Validation Results — Full Sample

Backtest produced by [`runner.run_carry_validation`](../../src/alpha_factory/runner.py)
on the L1 archive (top-5 universe smoke test, A.5.3).

### Headline metrics

| Metric | Value | Notes |
|---|---|---|
| n_obs (hourly bars) | 57,858 | 6.6 years |
| Sharpe (annualized) | **3.37** | Backtest only — see forward anchor below |
| Sortino | 4.86 | Downside-deviation-adjusted |
| Calmar | 2.71 | Annualized return / |max DD| |
| Active-only Sharpe | 3.46 | Diagnostic — not headline |
| Max DD | -1.89% | Shallow vs typical alphas |
| Max DD duration | 141 days | |
| Hit rate (per-bar) | 50.1% | Coin flip — edge is in tail asymmetry |
| Profit factor | 1.18 | Modest |
| Skew | -3.06 | Strong negative skew |
| Kurtosis (excess) | 1576 | Driven by 2020-03 / 2021-02 / 2023-03 events |
| VaR (5%) | -0.0138% | Per-bar |
| CVaR (5%) | -0.0255% | Per-bar |
| Turnover (per year) | 0.7272 | Low |
| Capacity estimate | $78M | At 1% of ADV; far above retail scale |
| n_transitions | 6 | V2 regime-detector firings |

### DSR (Deflated Sharpe Ratio)

- Per-period SR: 0.0361
- z-score (=SR × sqrt(n_obs)): 8.69
- DSR (Phi-prob), n_trials=1: **1.0000** (CI [1.0, 1.0])
- DSR with n_trials=10: 1.0000 (full-sample dominates)
- DSR with n_trials=1000: 0.9998 (still passes)

The 6.6-year window has too much signal to deflate via DSR alone. **DSR is
not the binding constraint here.** See Caveat #4 for the recent-12m DSR.

### PBO (Probability of Backtest Overfitting)

- N/A for single-params runner invocation (CSCV requires N ≥ 8 trials).
- Phase B parameter sweeps will compute PBO at the sweep level via
  `pbo.enumerate_trials` + `pbo.pbo` and pass through `runner.pbo_override`.

### Friction sensitivity

| Slippage assumption | Sharpe |
|---|---|
| Default fees (10bp spot taker + 4.5bp perp taker), zero slip | 3.3748 |
| Default fees + 2 bp linear slip | 3.3642 |
| Default fees + 5 bp linear slip | 3.3476 |

Friction is **not material** at V2's transition cadence (6 transitions /
6.6 years means fees are dominated by funding income, not entry/exit costs).

---

## Regime Stratification

| Regime | Kind | n_obs | Sharpe | ann_ret | max_dd | hit |
|---|---|---|---|---|---|---|
| pre_etf | etf | 37,620 | 3.49 | 6.25% | -1.89% | 0.494 |
| post_etf | etf | 20,238 | **4.63** | 2.70% | -0.14% | 0.515 |
| trend bear | trend | 25,913 | 2.22 | 3.21% | -1.72% | 0.501 |
| trend bull | trend | 31,945 | 4.27 | 6.47% | -0.99% | 0.501 |
| vol low | vol | 20,781 | 3.71 | 2.85% | -0.53% | 0.499 |
| vol mid | vol | 21,491 | 5.10 | 4.91% | -0.59% | 0.504 |
| vol high | vol | 15,586 | 3.25 | 8.03% | -1.89% | 0.499 |

### Pre/Post-ETF anomaly

post_etf Sharpe (4.63) is HIGHER than pre_etf (3.49), which contradicts
PROJECT.md decision #5 ("post-ETF compression observed"). Explanation:

- post_etf **mean** is 3.08e-6/hr (≈ 2.70%/yr) — **half** of pre_etf 7.14e-6/hr (6.25%/yr)
- post_etf **std** is 0.62e-4 — **one-third** of pre_etf 1.91e-4

Lower mean + much lower variance → higher Sharpe ratio, but **dollar return
dropped by half**. The Sharpe rise is a misleading signal driven by std
collapse. Honest reading: post-ETF carry has economically halved.

### Year-by-year decay

| year | n_obs | ann_ret | Sharpe |
|---|---|---|---|
| 2019 (partial) | 2,348 | 3.58% | 2.32 |
| 2020 | 8,766 | 8.13% | 3.46 |
| 2021 | 8,747 | **13.10%** | **5.81** |
| 2022 | 8,760 | 1.51% | 1.88 |
| 2023 | 8,759 | 2.95% | 2.17 |
| 2024 | 8,784 | 4.52% | **6.51** |
| 2025 | 8,760 | 1.88% | 3.81 |
| 2026 (YTD, 4 mo) | 2,934 | **0.14%** | **0.29** |

2021 (bull-market leverage froth) and 2024 (post-ETF launch frenzy) are
clear outliers. Excluding them: residual Sharpe = **2.41** (n=40,327).

### Recent rolling Sharpe (the relevant forward anchor)

| Window | n | ann_ret | Sharpe |
|---|---|---|---|
| Last 3 months | 2,161 | -0.48% | **-0.95** |
| Last 6 months | 4,321 | +0.68% | 1.45 |
| Last 12 months | 8,641 | +1.38% | 2.88 |
| Last 18 months | 12,961 | +1.87% | 3.44 |
| Last 24 months | 17,281 | +1.98% | 3.61 |
| Last 36 months | 25,921 | +2.83% | 4.67 |

**The last 3-month window is negative.** Strategy may be in a structurally
unfavorable regime right now — see Caveat #1.

---

## Forward Expectation (post-haircut)

Per CLAUDE.md red line: backtest Sharpe MUST be haircut × 0.3-0.5 before use
as forward anchor.

| Anchor | Backtest Sharpe | × 0.3 | × 0.5 |
|---|---|---|---|
| 6.6y full sample | 3.37 | 1.01 | 1.69 |
| Excluding 2021 + 2024 | 2.41 | 0.72 | 1.21 |
| **Last 12 months** (recommended) | **2.88** | **0.86** | **1.44** |
| Last 24 months | 3.61 | 1.08 | 1.81 |

**Recommended forward anchor: 0.86 - 1.44 Sharpe** (last 12m × haircut).

This is **above** PROJECT.md target of 0.75 for carry alone, but BELOW the
naive headline 3.37 implied. The 12m window incorporates the recent
compression and excludes 2021 leverage froth + 2024 post-ETF spike.

Annualized return forward expectation: **0.5% - 1.5% / year** at $1k notional
(consistent with PROJECT.md "Forward Expectation Anchors" table — 3-5%/yr
upper bound was for richer regimes).

---

## 3-Phase Adversarial Audit (A.5.3)

Following CLAUDE.md `adversarial-debate` protocol after the verdict landed.
6 attacks surveyed; quantitative verification of each.

### Attack tally

| # | Attack | Verdict | Evidence |
|---|---|---|---|
| 1 | Forward Sharpe is decaying | **PARTIAL CONCEDE** | 3m=-0.95, 6m=1.45 — real recent weakness, but 12m=2.88 still passes gate |
| 2 | 2021/2024 outliers carry the backtest | **CONCEDE** | Excluding them: Sharpe drops 3.37 → 2.41 (29% inflation) |
| 3 | DSR n_trials=1 is dishonest | **DEFEND on full / CONCEDE on recent** | full-sample z=8.69 survives any deflation; recent-12m DSR drops to 0.625 at n_trials=100 |
| 4 | V2 regime layer is theatre | **PARTIAL CONCEDE** | V2 fired correctly at 3/3 real crises (COVID, 2021-07, FTX), so not theatre — but blind to compression-toward-zero, which is the current threat |
| 5 | Construction biases | **DEFEND** | 7d MA causality OK (settlement at T is known at T); friction at +5bp slip drops Sharpe by 0.03 only |
| 6 | Stress test breakpoint | **PARTIAL CONCEDE** | 2022 Sharpe 1.88 + retail fees still ann_ret ~1%; but compression + lag creates a path to negative real return |

### Caveats — required before L4 portfolio entry

1. **Recent-window decay watch.** 3m Sharpe is -0.95. If Q3-2026 continues
   negative, the V2 alpha is dying in real time and the verdict must be
   revoked. **Track rolling 3m Sharpe monthly** until next L4 review.

2. **V3 funding-compression detector required.** V2 only catches absolute
   negative-funding episodes. The structural threat in 2025-2026 is funding
   *compressing* toward zero (still positive but too weak to clear friction).
   V3 should add a layer: e.g. 30-day MA < 0.5 bp/8h → exit. Estimated 1-2
   day implementation in Phase B.

3. **Forward-anchor honesty.** Documentation, dashboards, and L4 sizing
   inputs MUST use last-12m Sharpe (2.88) × haircut, NOT 6.6y (3.37). Any
   "carry V2 produces Sharpe 3+" claim downstream is misleading.

4. **DSR computation pinned to recent window.** Phase B param sweeps should
   compute PBO + DSR on a rolling 18-24m window, not full sample. Full-sample
   DSR is misleadingly close to 1.0 even with realistic n_trials.

5. **Sample-bias-corrected Sharpe.** Quote 2.41 (excluding 2021/2024) as
   the "robust" Sharpe in any cross-strategy comparison; full 3.37 reflects
   two specific market windows that may not recur.

### CLAUDE.md red-line check

| Red line | Status |
|---|---|
| DSR > 0 (95% CI) | ✓ pass (0.6+ on recent 12m, 1.0 on full sample) |
| PBO ≤ 0.5 | N/A (single param set; deferred to Phase B sweep) |
| Bull AND bear regime each Sharpe ≥ 0 (post-friction) | ✓ pass (4.27 / 2.22) |
| Pre-ETF AND post-ETF each Sharpe ≥ 0 | ✓ pass (3.49 / 4.63) |
| Max DD ≤ 30% | ✓ pass (1.89%) |
| Forward Sharpe haircut × 0.3-0.5 applied | ✓ in this doc |
| Funding-regime detection layer | ⚠️ partially met — V2 catches negative regimes; V3 needed for compression |
| 3-month paper trading before live | N/A (Phase D requirement, not yet reached) |
| Code-reviewer audit | ✓ runner.py audited (Phase A.5.2 commits) |

**Net:** all hard gates pass. Soft gate ("funding-regime detection layer")
is partially met; a V3 upgrade is recommended before Phase D live capital,
but is not required for Phase B portfolio combination experiments.

---

## Capacity & Friction

- **Capacity (1% ADV bottleneck):** $78M
- **Round-trip transition cost (regular fee tier, BTC-USDT):** ≈ 29 bp
  - Spot taker (10 bp) × 2 + perp taker (4.5 bp) × 2 = 29 bp
- **Total friction over 6.6 years:** ≈ 1.74% (6 transitions × 29 bp)
- **Per-year friction:** ≈ 0.26% / year
- Capacity is irrelevant at retail; friction is dominated by funding income,
  not by entry/exit cost.

---

## Open Questions

1. **2026 Q3 rolling 3m Sharpe**: does it return to positive? If not,
   strategy is structurally broken.
2. **V3 funding-compression detector**: design + backtest in Phase B.
3. **Cross-symbol carry**: does ETH / SOL carry behave similarly? Universe
   expansion in Phase B will test.
4. **Universe-level capacity**: top-20 perps × $50 capacity each = ?
5. **Maker-rebate variant**: switching to limit orders may net positive
   fee structure; non-trivial implementation in L5.

---

## References

- Phase 0 V1 backtest: `feasibility/scripts/backtest_carry.py` (commit 27ebcc4)
- Phase 0 V2 backtest: same file, regime-aware (commit bbd94b5)
- Phase A.5.1 port: `src/alpha_factory/alpha/carry.py` (commit 70f891f)
- Phase A.5.2 runner: `src/alpha_factory/runner.py` (commit a4f381d)
- Phase A.5.3 live archive run: this document
- López de Prado (2018), *Advances in Financial Machine Learning*
- Bailey & López de Prado (2014), "The Deflated Sharpe Ratio"
