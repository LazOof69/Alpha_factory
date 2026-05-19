# Carry V3 — BTC-USDT Funding Carry, Compression-Aware

> ⚠ **PROVENANCE LOST (2026-05-01).** The production archive, backtest
> artifacts, and RUN_REGISTRY / TRIAL_LOG that produced **every number in
> this document** were destroyed in the OneDrive→ASCII relocation and are
> unrecoverable (exhaustively verified 2026-05-13, post adversarial-debate;
> Phase A-B ran on the Windows box, not quant-1). All figures below are
> **frozen historical record, NOT reproducible**. run_id `053ee062` no
> longer resolves to any artifact. Per strategy-validation rigor +
> CLAUDE.md red lines (no verifiable DSR/PBO), carry_v3 is **demoted:
> `suspended`, portfolio_eligible = false**. The economic thesis,
> mechanism, and locked parameters survive intact in code — what is lost
> is the *empirical verification*, not the design. Sole requalification
> path: **Phase C 3-month paper-trade** on a fresh reproducible
> data_version. Numbers retained un-deleted as honest record (deletion
> would itself be tampering). See `validated_alphas.yaml`
> `registry_provenance` block.

> **Status:** SUSPENDED — provenance lost (was PASS-WITH-CAVEATS on a now-destroyed data_version)
> **Last revised:** 2026-05-13 (provenance-loss demotion)
> **Implementation:** [`src/alpha_factory/alpha/carry_v3.py`](../../src/alpha_factory/alpha/carry_v3.py) — intact
> **Validation run_id:** `053ee062-…` — ⚠ ORPHANED (artifact destroyed 2026-05-01)
> **PBO sweep harness:** [`scripts/v3_pbo_sweep.py`](../../scripts/v3_pbo_sweep.py) — code intact; results unreproducible
> **Supersedes V2:** historical only — both V2 and V3 numbers are provenance-lost
> **Multi-symbol scope (Phase B.2):** BTC head-of-family
> (portfolio-eligible); `carry_v3-eth` `on_deck` (Phase C m2 conditional);
> `carry_v3-sol` `deferred` — per cross-symbol PBO sweep + 3rd-round
> adversarial-debate, 2026-05-05. See "Multi-Symbol Family Extension" below.

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

## Empirical Comparison (V2 vs V3 post-sweep, BTC-USDT 2019-04 → 2026-05)

Both runs on identical L1 archive snapshot
(`data_version = 2026-05-03T05:00+nocorr`); identical 6.6-year sample.
**V3 metrics use the post-sweep defaults** (`exit=0.05 bp/8h`,
`lookback=120 settlements = 40d`, `reentry=0.10 bp/8h`).

| Metric            |   V2     |   V3 (post-sweep) | Δ (V3 − V2) |
|-------------------|----------|-------------------|-------------|
| n_obs (hourly bars) | 57,858 | 57,858 | – |
| Sharpe (full sample) | 3.3748 | 3.341 | −0.034 (within noise) |
| **Sharpe (last 12m)** | 2.8503 | **3.307** | **+0.456 ✓** |
| **Sharpe (last 3m)** | −0.9459 | **−0.874** | **+0.072 ✓** (still negative; see caveat) |
| **Sharpe (post-ETF)** | 4.6276 | **4.737** | **+0.109 ✓** |
| max_dd | −1.89% | −1.89% | flat |
| active_frac | 96.2% | 90.0% | −6.2pp |
| n_transitions | 6 | 14 | +8 |
| final_equity (NAV $1k) | $1391.33 | ~$1390 | essentially equal |
| DSR (Bailey & LdP) | 1.0000 | 1.0000 | – |
| **PBO (CSCV, 18 trials)** | n/a | **0.3035** | < 0.5 PASS |
| Verdict (L3 gates) | pass | **pass** | – |

**Interpretation:**

1. **V3 dominates V2 in 4/4 windows** within Sharpe noise tolerance
   (±0.05). Last-12m is the strongest delta (+0.456) — V3 captured
   2025-Q4 / 2026-Q1 better by stepping out of the late-cycle bleed.

2. **V3 last-3m is still −0.87** (improved 0.07 vs V2 but not yet ≥ 0).
   The 3m window is too short for a 30d (120-settlement) compression
   detector to fully react. Acceptable per ratchet design (transitions
   are slow on purpose). Track monthly; if V3 last-3m stays < −0.5 for
   2+ consecutive months, threshold may need re-tuning.

3. **DSR is uninformative for both** at 6.6yr scale (saturates to 1.0).
   V2 caveat `dsr_recent_window_required` inherited; rolling 18-24m DSR
   is the planned Phase B fix.

4. **All L3 gates pass** including the new PBO gate. PBO = 0.30 < 0.5
   means the IS-best trial in the 18-trial sweep would more likely
   than not also outperform OOS — the threshold pick is not just
   in-sample fit.

---

## Phase B PBO Sweep (Decisive)

Harness: [`scripts/v3_pbo_sweep.py`](../../scripts/v3_pbo_sweep.py).
Grid: 6 exit thresholds × 3 lookbacks = **18 trials** (CSCV requires
N ≥ 8 — exceeded with margin).

**Grid:**
- `exit_compression_30dma ∈ {0.05, 0.10, 0.15, 0.20, 0.30, 0.50} bp/8h`
- `compression_lookback_settlements ∈ {60, 90, 120}` (= 20d, 30d, 40d)
- `reentry_compression_30dma = 2 × exit` (locked ratio)

**PBO result:** **0.3035** (CSCV via `pbo.pbo()` with `make_rng` seeded
deterministically). Below 0.5 gate → not overfit.

**Robustness check** (post-ETF Sharpe range across thresholds at each lookback):

| Lookback | Sharpe range | Gap | Verdict |
|----------|--------------|-----|---------|
| 60 settlements (20d) | 4.18 – 4.71 | 0.53 | FLAT |
| 90 settlements (30d) | 4.10 – 4.80 | 0.70 | FLAT |
| **120 settlements (40d)** | **4.51 – 4.78** | **0.27** | **FLAT** ← chosen |

Conclusion: lookback=120 is the most robust (smallest Sharpe gap across
threshold variation). Pick is not knife-edge — even ±50% threshold
movement within the [0.05, 0.5] bp range moves Sharpe by < 0.3.

**Top 5 trials by dominance count vs V2:**

| Rank | Exit (bp) | Lookback | Dom (4) | full | 12m | 3m | post-ETF |
|------|-----------|----------|---------|------|-----|----|----------|
| #1 | **0.05** | **120** | **4/4** | 3.341 | **3.307** | −0.874 | **4.737** |
| #2 | 0.10 | 90 | 3/4 | 3.327 | 3.356 | −1.416 | 4.799 |
| #3 | 0.15 | 120 | 3/4 | 3.324 | 3.386 | −0.908 | 4.781 |
| #4 | 0.05 | 90 | 3/4 | 3.372 | 3.347 | −1.051 | 4.781 |
| #5 | 0.10 | 120 | 3/4 | 3.336 | 3.304 | −1.167 | 4.728 |

**Choice rationale:** #1 (0.05 bp / 120d) is the only trial that
ties-or-beats V2 in **all 4 windows** within ±0.05 noise tolerance.
Robustness check at lookback=120 confirms the pick is not a knife-edge
single-cell winner — neighbouring threshold values give comparable
Sharpe.

**Defaults locked in `CarryV3Params`:**
- `exit_compression_30dma = 0.000005` (0.05 bp/8h)
- `reentry_compression_30dma = 0.00001` (0.10 bp/8h)
- `compression_lookback_settlements = 120`

---

## Multi-Symbol Family Extension (Phase B.2)

The 3rd-round adversarial-debate on 2026-05-04 concluded that a 1-alpha
portfolio (BTC `carry_v3` alone) violates CLAUDE.md's *"single alpha > 50%
portfolio weight"* red line. Option **(c)** — run `carry_v3` as a FAMILY
of symbol-specific instances — was the cheapest mitigation: same mechanism,
different funding-rate / vol regime per symbol.

The family extension was executed in two passes:

1. **Exploratory backtest**
   ([`scripts/run_carry_v3_multi_symbol.py`](../../scripts/run_carry_v3_multi_symbol.py))
   ran the BTC-locked V3 defaults (0.05 bp / 120d) on BTC / ETH / SOL with
   the same archive snapshot. All three symbols passed the L3-gate-approx
   (full / 12m / post-ETF Sharpe ≥ 0, max_dd ≤ 30%).
2. **Cross-symbol PBO sweep**
   ([`scripts/v3_pbo_sweep.py --symbol`](../../scripts/v3_pbo_sweep.py),
   commit `c1cb0b3`) re-ran the 18-trial grid per symbol to test whether
   the threshold pick is robust on each symbol's archive INDEPENDENTLY.

### Cross-symbol PBO results

Each symbol's sweep uses the same 6 × 3 grid (`exit_compression_30dma ∈
{0.05, 0.10, 0.15, 0.20, 0.30, 0.50} bp/8h` × `lookback_settlements ∈
{60, 90, 120}`).

| Symbol | Archive span | PBO (CSCV, 18 trials) | Gate (< 0.5) | Verdict |
|--------|--------------|-----------------------|--------------|---------|
| BTC-USDT | 6.6 yr | **0.3035** | ✓ | **PASS** |
| ETH-USDT | 5.4 yr | **0.6302** | ✗ | **FAIL** |
| SOL-USDT | 3.0 yr | **0.6342** | ✗ | **FAIL** |

**Interpretation.** PBO > 0.5 means the in-sample-best trial is *more
likely than not* to underperform out-of-sample — the threshold pick is
fitting noise on that symbol's data. ETH and SOL fail the same gate BTC
clears, despite using the same mechanism. Two factors explain the
divergence:

1. **Funding regime variability.** ETH (~1 bp/8h base) and SOL (~3 bp/8h
   base, regime-dependent extremes) have more variable funding
   distributions than BTC (~0.5 bp/8h). The compression-detector exit
   threshold has a less stable optimum across windows.
2. **Sample size.** SOL has 3.0 yr of perp archive (listing 2020-09-14);
   ETH has 5.4 yr; BTC has 6.6 yr. Shorter sample gives CSCV more chance
   to score the IS-best as a noise pick.

The exploratory backtests' positive Sharpe **does not contradict** the PBO
fail. PBO is a robustness gauge over hyperparameter selection, not a
profitability gauge. The intended mitigation for the family is to **lock
the threshold from BTC's PBO sweep** (0.05 bp / 120d) and apply it
unchanged to ETH / SOL — not to run a fresh per-symbol PBO sweep to pick
each threshold. The cross-symbol PBO above is a *diagnostic* showing what
*would* have happened if we did pick per-symbol. The risk that remains
even with BTC-locked params is **information leakage from BTC**: the
chosen 0.05/120d came from a sweep, so it carries one round of fitting;
porting unchanged is more conservative than re-tuning, but not
overfit-free. This is recorded in `validated_alphas.yaml` as caveat
`information_leakage_btc_param_port` on the ETH / SOL entries.

### ETH at BTC-locked params (single-trial diagnostic)

For comparison: with the BTC-locked threshold (0.05 bp / 120d) applied
unchanged, ETH-USDT backtest on full archive produces:

| Window | ETH Sharpe (BTC-locked params) |
|--------|-------------------------------|
| full sample | 3.557 |
| last 12m | 2.506 |
| post-ETF | 3.628 |
| max_dd | −1.26% |

The numbers exceed Phase C carry-alone target (0.75) at every window, and
the post-ETF figure is comparable to BTC's 4.737. The PBO fail does NOT
invalidate these — it invalidates the *individually-fit* threshold pick
on ETH data. Whether the BTC-locked threshold is the right call on ETH
forward is what Phase C m2 paper-trade will surface.

### Adversarial-debate scope verdict (3rd round, 2026-05-05)

The cross-symbol PBO results fed back into a 3rd-round adversarial-debate
on whether to ship the 3-alpha family or shrink scope. The debate
concluded option **(a')**: scope-shrink with conditional re-entry. The
verdict is recorded in `validated_alphas.yaml` via commit `9720d17`.

| Family member | Phase C milestone | Status | Rationale |
|---------------|-------------------|--------|-----------|
| `carry_v3` (BTC) | **m1** — included from day 1 | `portfolio_eligible: true` | PBO PASS; only solid alpha; ship the shippable asset. |
| `carry_v3-eth` | **m2** — conditional on BTC m1 paper-trade success | `on_deck` (`portfolio_eligible: false`) | PBO FAIL on per-symbol sweep, but information-leakage-mitigated via BTC-locked threshold. Verify tracking error on paper trade before promoting. |
| `carry_v3-sol` | **deferred indefinitely** | `deferred` (`portfolio_eligible: false`) | PBO FAIL + marginal backtest (full-Sharpe 0.89, last-12m 0.34, max_dd −7.8%) + shortest sample (3.0 yr). Low conviction; revisit only if capital scales or SOL funding regime stabilises. |

This matches CLAUDE.md's *"Suspicion over enthusiasm"* principle. The
cross-symbol PBO was the suspicion-test that downgraded the 3-alpha
family to **1 + conditional**.

### Implications

- **`validated_alphas.yaml`** carries the downgraded statuses (commit
  `9720d17`). Earlier exploratory entries that claimed
  `portfolio_eligible: true` for ETH/SOL have been retracted. New caveats
  `per_symbol_pbo_fail` (severity: blocking_for_portfolio_entry) and
  `information_leakage_btc_param_port` (severity: documentation) recorded
  on the ETH / SOL entries. Status legend gained two values: `on_deck`
  (registered, NOT portfolio-eligible, waiting on a defined trigger) and
  `deferred` (pushed back without a defined trigger; promotion requires
  explicit user decision).
- **1-alpha portfolio red-line still binds.** Until ETH clears m2's
  paper-trade gate, the portfolio remains 1-alpha — violating the
  *"single alpha > 50% portfolio weight"* red line. Mitigation paths
  require Phase C work and are out of scope here; see
  [`docs/phase_c_paper_trade_plan.md`](../phase_c_paper_trade_plan.md)
  (and its v2 successor on branch `claude/reverent-swirles-bc2bff`) for
  the live-money bridge plan.
- **Phase B `funding_xs` and `momentum_xs`** were also explored as
  diversifying alphas but PARKED after 3 rounds of adversarial-debate.
  See the `validated_alphas.yaml` PARKED ALPHAS block for the verdict.

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

**Initial V3 default was 0.3 bp/8h** (between the critique-derived
~0.09 bp break-even and the Phase A audit's 0.5 bp suggestion). The
Phase B PBO sweep above confirmed this initial choice was **too tight**
— V3 exited in regimes where V2 still earned positive carry.

**Post-sweep default = 0.05 bp/8h** at lookback=120d (40d). The new
default sits just above the conservative break-even and below all
other tested thresholds — backed by 18-trial CSCV PBO=0.30 < 0.5 and
robustness-flat Sharpe surface (gap 0.27 across exit thresholds at
the chosen lookback).

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

## Caveats (post-sweep, shrunken)

| ID | Severity | Description |
|---|---|---|
| `last_3m_still_marginally_negative` | documentation | V3 last-3m Sharpe is −0.87 at post-sweep defaults (better than V2's −0.95 but still < 0). The 3m window is too short for the 40d compression detector to fully react; ratchet design intentionally throttles transitions. Track monthly. If V3 last-3m stays < −0.5 for 2+ consecutive months, threshold or lookback may need re-tuning. |
| `dsr_recent_window_required` | pipeline | Inherited from V2: full-sample DSR saturates at 1.0; rolling 18-24m DSR is more informative. Phase B work item. |
| `v3_warmup_handicap` | documentation | V3 default initial state is `exited`, costing roughly 30-60 days of carry at any cold-start backtest. For 6.6yr sample this is < 1.2% impact; for 1yr cold start the handicap can dominate. Mitigation: persist last state to registry across re-runs (Phase B). |
| `v3_v2_correlation_unmeasured` | portfolio | V3/V2 correlation is **asserted ≥ 0.9** (shared legs + V2 exit path inheritance) but NOT measured on aligned per-bar returns. Phase B portfolio-construction step to compute and record. |

**Resolved this PR (post-sweep):**
- ~~`thresholds_untuned`~~ → SWEEP COMPLETE; defaults locked at 0.05 bp / 120d.
- ~~`v3_does_not_supersede_v2_yet`~~ → V3 ties-or-beats V2 in 4/4 windows; supersede gate PASSED.

---

## Phase D Gating (Live Capital)

V3 cannot enter live capital until ALL of:

1. ~~PBO sweep over `{0.05, 0.1, 0.15, 0.2, 0.3, 0.5}` bp shows V3 dominates V2~~
   **DONE 2026-05-03**. V3 (0.05 bp / 120d) ties-or-beats V2 in 4/4 windows;
   PBO=0.30 < 0.5; lookback=120d Sharpe surface gap=0.27 (FLAT).
2. Selected threshold is held for ≥ 1 month of paper trading on Binance
   API; tracking error vs. backtest < 30%. **PENDING**
3. CLAUDE.md red lines all green:
   - DSR > 0 (95% CI) ✓ (formal — full-sample saturates; rolling
     window required for true binding)
   - PBO ≤ 0.5 ✓ (sweep result 0.30)
   - Live cap $1k initially (CLAUDE.md hard limit Phase D)
   - 3-month paper trading minimum
4. Last-3m Sharpe trigger ([`recent_3m_negative` from V2 caveat](carry_v2.md#caveats))
   is now empirically held at ≥ 0 in last-3m windows for at least 2 quarters
   on real archive (the original V2 trigger that motivated V3).
   **PENDING**: V3 last-3m at sweep close was −0.87 (still negative;
   ratchet design throttles transitions; track monthly).

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
