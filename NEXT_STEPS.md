# Next Steps — 2026-05-21 (Phase C walking-skeleton — COMPLETE, end-to-end)

> Handoff for next session. Active work on branch
> `claude/phase-c-skeleton` (worktree
> `.claude/worktrees/phase-c-skeleton/`), 8 commits ahead of `main`,
> open for merge via PR. Canonical state moves to `main` once merged.

## TL;DR

- Phase C **walking-skeleton is COMPLETE** — all 7 thin stages land
  end-to-end. The inner paper-trade loop runs: `run_cycle()` wires
  fetch → signal → execute → event-log → reconcile, with P0/P1 halt
  gates and a paper/live execution seam.
- **349/349 pytest green, ruff clean, every module code-reviewer'd
  (no Blocking).**
- **Resume at:** NOT another skeleton stage — the skeleton is done.
  Next is **depth iteration** (make `tracking_error` informative) +
  **ops** (AMD micro provisioning, pre-go-live sanity harness) before
  the 3-month paper clock can start. See "What's next" below.

## Why we're here (read once if cold-starting)

- **carry_v3 = SUSPENDED** (`portfolio_eligible: false`). Provenance
  loss 2026-05-01: production L1 archive + RUN_REGISTRY + TRIAL_LOG
  destroyed in OneDrive→ASCII relocation; verified unrecoverable
  2026-05-13.
- Adversarial-debate **B1/B3 verdict**: do NOT panic-rebuild.
  **Phase C 3-month paper-trade = sole forward re-validation gate**
  (no backtest fallback). The tracking-error gate is load-bearing.
- User reprioritization 2026-05-13: **pipeline end-to-end "通" beats
  any single stage's depth**. Strategy is pluggable; pipeline is the
  deliverable. ("策略之後再想") — this is now ACHIEVED.
- v3 spec patches v2 on 7 points (P1–P7) incl. walking-skeleton +
  strategy-as-interface (§9). See `docs/phase_c_infra_design_v3.md`
  + `_v2.md`.

## Skeleton build status (7 thin stages — ALL DONE)

| # | Stage | Module | Status |
|---|---|---|---|
| 3 | Event log (JSONL spine; B1 recovery + B2 single-writer) | `execution/event_log.py` | **DONE** `dbe621d` |
| 2 | Strategy interface + carry_v3 adapter | `execution/strategy.py` | **DONE** `e63d9ea` |
| 1 | L1 forward fetch (rolling 120-settle window; persist = forward-archive byproduct) | `execution/forward_fetch.py` | **DONE** `ed6abbb` |
| 4 | Paper fill sim (m1 taker, zero slippage) | `execution/fill_sim.py` | **DONE** `1ba2f28` |
| 5 | Daily reconcile (A vs B; C/Sharpe NULL) | `execution/reconcile.py` | **DONE** `20b61a5` |
| 6 | Halt / kill (P0 flag-file + P1 crash) | `execution/halt.py` | **DONE** `42130e6` |
| 7 | Live seam (backend Protocol + `run_cycle`) | `execution/live_seam.py` | **DONE** `3164e37` |

Each stage: synthetic deterministic tests + an end-to-end compose test
chaining it to the prior stages. `test_live_seam.py::
test_full_chain_run_cycle_then_reconcile` is the capstone proof the
loop runs.

## What's next (NOT skeleton — depth iteration + ops)

### Depth iteration (makes tracking_error a real signal)
At skeleton, A ≡ B by construction → `tracking_error` is structurally 0
(it only proves plumbing). It becomes informative once the fill sim can
diverge from strategy intent:

1. **Fill sim depth** (`fill_sim.py`): slippage model (v2 §4 sqrt-impact
   betas), partial fills, latency between `target.as_of` and fill price.
   This is THE change that gives `tracking_error` meaning.
2. **`signal_compute` events**: `run_cycle` does not yet emit them, so
   reconcile B replays fills not strategy logic, and `n_signals` is 0.
   Emit them so B can re-derive intent independently of A.
3. **Halt P0.5 + P2–P5**: clock-drift (`clock_drift_ms` already plumbed
   in forward_fetch — trivial add), mtm/daily DD, tracking-error, API
   outage — all need sustained-breach gates (v2 §1). Paper mode keeps
   P2–P4 alert-only (don't censor the sample).
4. **funding_received events**: skeleton PnL = MTM − fees only; book the
   funding cash flow (the actual carry income) so reconcile PnL is real.
5. **Reconcile C-quantity**: `replay_l1_current_pnl_24h` +
   `data_correction_effect` go live once the forward archive accumulates
   ≥ one reconcile window of history (v3 §3′).
6. **Real cron**: schedule `run_cycle` at 00:00/08:00/16:00 UTC inside
   one `single_writer_lock` for the process lifetime (v3 §8).

### Ops / pre-go-live gates (block the 3-month clock starting)
- **AMD micro provisioning** (Phase C host): repo + uv + paper-trade-only
  deps (no backtest stack). v3 §8. **Day-1 backup policy mandatory** —
  forward fetch is the only copy of forward data (provenance-loss lesson).
- **Pre-go-live signal-logic sanity harness** (v3 §7): run carry_v3 state
  machine over surviving `feasibility/data/` (BTC/ETH), manual
  economic-sense review. NOT a Sharpe claim — gross-logic-regression
  catch only (the lost backtest oracle's cheap stand-in).
- **Q1.a NTP fallback** on quant-1 (5-min SSH; v3 readiness gate).

## Resume protocol

1. `cd C:\Users\butte\projects\Alpha_factory\.claude\worktrees\phase-c-skeleton`
2. `git fetch origin && git status` — branch `claude/phase-c-skeleton`
   (8 ahead of `main`; merge via PR if not already merged)
3. If cold-starting: skim `docs/phase_c_infra_design_v3.md` +
   `phase_c_infra_design_v2.md` §1–§4, then read the 7 `execution/`
   modules in stage order [3]→[2]→[1]→[4]→[5]→[6]→[7].
4. Pick the next item from "What's next". Recommended first:
   **fill-sim slippage/partials (#1)** — it is the single change that
   converts `tracking_error` from a structural 0 into a real
   live-vs-intent signal, which is the whole point of the paper window.
5. Per CLAUDE.md after each module: `code-reviewer` (manual checklist
   pass), deterministic synthetic tests, ruff clean (no committing
   failing lint — red line). Skill-route execution work via
   `live-trading-execution`.

## Don't relitigate

- Rebuilding L1 archive for backtest validation (B1/B3 ruled out)
- `funding_xs` / `momentum_xs` (3-round adversarial-debate; PARKED;
  next alpha cycle is paper-trade-driven, not design-driven)
- Q1.b multi-symbol script re-run (CANCELLED — no archive, cosmetic)
- carry_v3's economic thesis (validated reasoning intact; only the
  empirical track record was lost) — and carry_v3 stays SUSPENDED; it
  is the skeleton's placeholder adapter, not a re-blessed alpha
- Skeleton "A ≡ B → tracking_error 0" is BY DESIGN, not a bug — see
  depth-iteration #1; do not "fix" it without adding fill-sim divergence

## Key refs

- `validated_alphas.yaml` `registry_provenance` block — global
  ARCHIVE_LOST context
- `docs/alphas/carry_v3.md` — SUSPENDED with provenance admonition;
  logic + locked params intact in code
- `docs/phase_c_infra_design_v2.md` + `_v3.md` — implementation spec
  (v2 base + v3 patches); each `execution/` module cites the section
  it implements
- `CLAUDE.md` — red lines (DSR/PBO gate, 3mo paper, ×0.3-0.5 haircut,
  code-reviewer audit, ruff lint, Sharpe only via strategy-validation)
- `src/alpha_factory/execution/` — the 7 skeleton modules; start at
  `live_seam.py::run_cycle` to see the whole loop, then read outward
