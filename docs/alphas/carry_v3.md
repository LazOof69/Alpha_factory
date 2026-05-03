# Carry V3 — BTC-USDT Funding Carry, Compression-Aware

> **Status:** PASS-WITH-CAVEATS (L3 verdict pass; thresholds pending PBO sweep)
> **Last revised:** 2026-05-03
> **Implementation:** [`src/alpha_factory/alpha/carry_v3.py`](../../src/alpha_factory/alpha/carry_v3.py)
> **Validation run_id:** `929ed503-5a3e-4065-bbe9-933c407c6950`
> **Compared against V2 run_id:** `c7c5aadd-497d-49f4-9ecd-8b236221e397`
> **Supersedes V2:** **NO** (current thresholds underperform V2 in steady-positive regimes;
> waiting on Phase B PBO sweep before V3 takes the carry slot in L4)

---

## Why V3

The Phase A V2 audit identified one blocking caveat:
[`v3_funding_compression_detector_missing`](../../validated_alphas.yaml).

> V2 detects only ABSOLUTE NEGATIVE funding regimes (caught COVID, FTX,
> ETH-unwind correctly). It is BLIND to **funding compression toward zero**,
> which is the dominant post-ETF threat. Post-Jan-2024, funding compressed
> from ~6.5 bp/8h average to ~0.3 bp/8h — V2 stays active because the 7d MA
> never crossed -1 bp/8h, but at 0.3 bp average the post-fee EV is
> approximately negative.

V3 closes that gap by adding a second, ABS-30d-MA exit condition.

This is the same red line the CLAUDE.md states explicitly:

> 🚫 Carry strategy in production without funding-regime detection layer

V2 partially met it (catches absolute-negative regimes); V3 extends the
detection to the compression case the post-ETF era highlighted.

---

## Economic Story

Same as V2 — see [`carry_v2.md`](carry_v2.md) "Economic Story". V3 changes
only the regime gate; the underlying long-spot/short-perp delta-neutral
trade is unchanged.

---

## V3 State Machine

V3 evaluates two thresholds at every settlement, retains V2's hysteresis,
and adds a ratchet guard against rapid in/out churn:

```
Per settlement:
    7dma      = rolling_mean(funding_rate, 21 settlements)        # V2 inherited
    30dma_abs = abs(rolling_mean(funding_rate, 90 settlements))   # V3 NEW

  if active:
    exit if 7dma < EXIT_FUNDING_7DMA       (-1 bp/8h)             # V2 path
    OR exit if 30dma_abs < EXIT_COMPRESSION_30DMA (0.3 bp/8h)     # V3 NEW
  if exited:
    re-enter only if BOTH:
      7dma > REENTRY_FUNDING_7DMA          (+0.5 bp/8h)
      AND 30dma_abs > REENTRY_COMPRESSION_30DMA (0.6 bp/8h)

  Ratchet guard: no transition within min_state_duration_settlements
                  (default 21 = 7 days) of the previous transition.
```

**Initial state = exited (V2 used active).** V3 is conservative during
warmup until 30dma_abs is computable AND its re-entry condition passes
(roughly 30 trading days = 90 settlements).

---

## Empirical Comparison (V2 vs V3, BTC-USDT 2019-04 → 2026-05)

Both runs on identical L1 archive snapshot
(`data_version = 2026-05-03T05:00+nocorr`); identical 6.6-year sample;
default `CarryParams` / `CarryV3Params`.

| Metric            |   V2     |   V3     | Δ (V3 − V2) |
|-------------------|----------|----------|-------------|
| n_obs (hourly bars) | 57,858 | 57,858 | – |
| **Sharpe (full sample)** | 3.3748 | 3.1525 | **−0.222** |
| **Sharpe (last 12m)** | 2.8503 | 1.8174 | **−1.033** |
| **Sharpe (last 3m)** | **−0.9459** | **0.0000** | **+0.946 ✓** |
| Sharpe (post-ETF) | 4.6276 | 4.3956 | −0.232 |
| max_dd | −1.89% | −1.74% | +0.15pp |
| **active_frac** | 96.2% | 64.4% | **−31.9pp** |
| n_transitions | 6 | 22 | +16 |
| final_equity (NAV $1k) | $1391.33 | $1340.89 | −$50.44 |
| **DSR (Bailey & LdP)** | 1.0000 | 1.0000 | – |
| **Verdict (L3 gates)** | pass | **pass** | – |

**Interpretation:**

1. **V3 fixes the original motivating caveat** — last-3m Sharpe is no
   longer negative. V3 sat exited during the Q1-2026 bleed, capturing the
   gap that V2 walked through.

2. **V3 underperforms V2 in steady-positive regimes** — 32pp lower
   active-fraction means V3 misses 1/3 of the carry opportunity. The
   compression-exit threshold (0.3 bp/8h) is too aggressive against the
   true post-fee break-even (~0.09 bp/8h at 60-day expected hold).

3. **DSR is uninformative for both** — 1.0 to floating-point precision
   (same caveat already documented for V2: full-sample DSR with 100+
   nominal trials still saturates because the sample size is large
   relative to the per-period return scale).

4. **Both pass L3 gates** — DSR > 0.5, max_dd < 30%, all 4 regimes
   non-negative-Sharpe.

---

## Threshold Derivation (post 1-round adversarial critique)

**Original Phase A audit suggested 0.5 bp/8h.** The 1-round critique
re-derived break-even using the holding-period attribution:

```
break_even_per_settlement = round_trip_fee / expected_hold_settlements
                          = 16 bp / 180 settlements (60d)
                          ≈ 0.089 bp/8h
```

Under the same logic, the 30-day expected hold gives 0.18 bp; the 90-day
expected hold gives 0.06 bp.

**V3 default = 0.3 bp/8h** sits between the conservative critique-derived
break-even (~0.09 bp) and the original Phase A audit suggestion (0.5 bp).
Empirically this is too tight — the run results above show V3 exits in
regimes where V2 still earns post-fee positive carry.

**Phase B follow-up:** PBO sweep across `{0.05, 0.1, 0.15, 0.2, 0.3, 0.5}` bp
threshold values; verify Sharpe surface is flat (i.e. not knife-edge fit
to one regime); pick the central robust value as the new default. Until
then, V3 thresholds are **interim** and V3 does not supersede V2.

---

## Adversarial Critique Highlights (1-round, pre-implementation)

The full critique with attacks 1-6 is recorded in the implementation PR.
Highlights of how each attack was addressed:

1. **Threshold over-tight** — Acknowledged. Default lowered from
   audit-suggested 0.5 to 0.3 bp; full sweep deferred to Phase B.
2. **Ratchet flutter risk** — Mitigated by `min_state_duration_settlements = 21`
   (7-day minimum between transitions).
3. **Lookback (90 settlements) under-sampled** — Phase B sweep over
   `{60d, 90d, 120d}` planned; current lookback locked.
4. **STRATEGY_ID forking double-weights L4** — V3 uses
   `STRATEGY_ID = "carry_v3"` (registry-distinct from V2). HRP
   guard: V3 will replace V2 in `validated_alphas.yaml` only after PBO
   sweep validates the threshold. Until then, **V2 is the carry default**;
   V3 is a candidate, not an overlay.
5. **V3 strictly worse in stable-low-positive regime** — Confirmed
   empirically (12m Sharpe 1.82 vs 2.85). This is the threshold-tuning
   gap; not a design defect.
6. **Initial-state warmup optimism** — Fixed: V3 default initial state
   is `exited` (not V2's `active`).

---

## Caveats

| ID | Severity | Description |
|---|---|---|
| `thresholds_untuned` | required_for_supersede_V2 | Default thresholds (0.3 / 0.6 bp) are interim, NOT the result of a PBO sweep. V3 underperforms V2 in steady-positive regimes at current thresholds. Phase B PBO sweep over `{0.05, 0.1, 0.15, 0.2, 0.3, 0.5}` bp must complete before V3 enters L4. |
| `dsr_recent_window_required` | pipeline | Inherited from V2: full-sample DSR saturates at 1.0; rolling 18-24m DSR is more informative. Phase B work item. |
| `v3_warmup_handicap` | documentation | V3 default initial state is `exited`, costing roughly 30-60 days of carry at the start of any cold-start backtest. For a 6.6-year sample this is < 1.2% of equity. For a 1-year cold start the handicap can dominate. Mitigation: persist last state to registry across re-runs (Phase B). |
| `v3_does_not_supersede_v2_yet` | structural | The whole point of V3 was to dominate V2 in post-ETF regimes; current thresholds give post-ETF Sharpe 4.40 vs V2's 4.63. V3 is a CANDIDATE, not a REPLACEMENT, until thresholds are tuned. |

---

## Phase D Gating (Live Capital)

V3 cannot enter live capital until ALL of:

1. PBO sweep over `{0.05, 0.1, 0.15, 0.2, 0.3, 0.5}` bp shows V3 dominates
   V2 across `{full, post-ETF, last-12m}` windows at the chosen threshold,
   AND Sharpe surface is robust (not knife-edge).
2. Selected threshold is held for ≥ 1 month of paper trading on Binance
   API; tracking error vs. backtest < 30%.
3. CLAUDE.md red lines all green:
   - DSR > 0 (95% CI) ✓ (formal — full-sample saturates; rolling
     window required for true binding)
   - PBO ≤ 0.5 (Phase B sweep harness will populate)
   - Live cap $1k initially (CLAUDE.md hard limit Phase D)
   - 3-month paper trading minimum
4. Last-3m Sharpe trigger ([`recent_3m_negative` from V2 caveat](carry_v2.md#caveats))
   is now empirically held at ≥ 0 in last-3m windows for at least 2 quarters
   on real archive (the original V2 trigger that motivated V3).

---

## Portfolio-Hint (L4 HRP, deferred)

```yaml
portfolio_hint:
  max_weight: 0.50              # CLAUDE.md red line, same as V2
  forward_vol_estimate: 0.05    # ann vol approx; V3 lower active_frac → lower realized vol
  orthogonality_to_btc_directional: high   # delta-neutral, same as V2
  v3_v2_correlation_estimate: ">= 0.9"   # NOT measured; assertion based on
                                          # shared legs + V2-path inheritance.
                                          # Phase B portfolio construction step
                                          # to measure on aligned per-bar returns.
```

**HRP guidance:** if V3 ever passes the supersede gate, **remove V2 from
HRP universe**. Otherwise the V2/V3 correlation will be ≥ 0.9 (shared
spot/perp legs + shared V2 negative-funding exit path) and HRP would
either dedupe arbitrarily or double-weight carry exposure.

---

## Phase B Follow-Ups

1. **PBO sweep harness** — once `pbo.enumerate_trials` interface is wired
   (Phase B spec'd), enumerate `{exit_compression_30dma, lookback_settle}`
   grid and re-validate via `runner.run_carry_validation(pbo_override=...)`.
2. **Threshold robustness check** — compute V3 Sharpe on
   {full, post-ETF, last-12m, last-3m} across all sweep points; Sharpe
   surface should be flat to ≥ ±25% threshold movement.
3. **V3/V2 correlation measure** — align V3 and V2 `equity_curve` time
   series; compute pearson on `period_ret_net`; record in
   `validated_alphas.yaml` portfolio_hint.
4. **Persisted state across re-runs** — V3 cold-start warmup handicap is
   only acceptable for backtests; live deployment must persist last state
   to registry to avoid re-paying the warmup cost on each restart.

---

## Code Reviewer Audit (post-implementation)

Status: APPROVED-WITH-FIXES. No BLOCKING issues. Fixes integrated:

1. Ratchet counter naming clarity — kept as comment-level note.
2. Magic numbers in tests — replaced with `CarryV3Params()` references.
3. Active-fraction test strengthened from `<` to `< v2 - 0.10`.
4. 0.95 V3/V2 correlation claim re-tagged "expected ≥ 0.9, measure in Phase B".

Deferred NITs: ratchet-isolation unit test (covered indirectly by
`test_v3_flutter_zone_does_not_thrash`); pre-existing dead branches in
`carry.py` left for separate cleanup.
