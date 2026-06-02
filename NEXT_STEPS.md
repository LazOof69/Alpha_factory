# Next Steps — 2026-05-13 (Phase C skeleton — spine landed)

> Handoff for next session. Canonical state on `main`; active work on
> branch `claude/phase-c-skeleton`.

## TL;DR

- Phase C **walking-skeleton** in flight on `claude/phase-c-skeleton`
  (worktree `.claude/worktrees/phase-c-skeleton/`).
- **Stage [3] `event_log.py` + test DONE** — commit `dbe621d`, ruff
  clean, 13/13 pytest green, code-reviewer pass (no Blocking).
- **Resume at:** stage [2] strategy interface
  (`compute_target_position(window) -> TargetPosition` protocol +
  carry_v3 adapter) → [1] thin L1 forward fetch → [4] fill sim →
  [5] reconcile, closing one end-to-end cycle. Then [6] halt skeleton
  + [7] live seam stub.

## Why we're here (read once if cold-starting)

- **carry_v3 = SUSPENDED** (`portfolio_eligible: false`). Provenance
  loss 2026-05-01: production L1 archive + RUN_REGISTRY + TRIAL_LOG
  destroyed in OneDrive→ASCII relocation; verified unrecoverable
  2026-05-13.
- Adversarial-debate **B1/B3 verdict**: do NOT panic-rebuild.
  **Phase C 3-month paper-trade = sole forward re-validation gate**
  (no backtest fallback).
- User reprioritization 2026-05-13: **pipeline end-to-end "通" beats
  any single stage's depth**. Strategy is pluggable; pipeline is the
  deliverable. ("策略之後再想")
- v3 spec patches v2 on 7 points (P1–P7) incl. walking-skeleton +
  strategy-as-interface (§9). See `docs/phase_c_infra_design_v3.md`
  + `_v2.md`.

## Skeleton build status (7 thin stages, v3 §"Build approach")

| # | Stage | Status |
|---|---|---|
| 1 | L1 forward fetch (rolling 120-settle window; persist = forward archive byproduct) | pending |
| 2 | Strategy interface + carry_v3 adapter | pending |
| 3 | **Event log** `event_log.py` | **DONE** `dbe621d` |
| 4 | Paper fill sim (m1 taker, zero slippage) | pending |
| 5 | Daily reconcile (A vs B only; v3 §3′ 2-quantity; C deferred until forward-L1 accumulates) | pending |
| 6 | Halt / kill (flag-file + P0/P1 only at skeleton; P2–P5 later) | pending |
| 7 | Live seam (stub adapter; paper/live share pipeline) | pending |

## Resume protocol

1. `cd C:\Users\butte\projects\Alpha_factory\.claude\worktrees\phase-c-skeleton`
2. `git fetch origin && git status` (clean tree; branch
   `claude/phase-c-skeleton` at `dbe621d`)
3. If cold-starting: skim `docs/phase_c_infra_design_v3.md` +
   `phase_c_infra_design_v2.md` §1–§4
4. Pick up at stage [2]:
   `compute_target_position(window) -> TargetPosition` protocol in
   proposed `src/alpha_factory/execution/strategy.py` + carry_v3
   adapter wrapping `src/alpha_factory/alpha/carry_v3.py`'s state
   machine
5. Per CLAUDE.md after each module: code-reviewer (manual checklist
   pass), deterministic synthetic tests, ruff clean (no committing
   failing lint — red line)

## User-side action items (independent)

- **Q1.a NTP fallback** on quant-1 (5-min SSH; v3 readiness gate; not
  blocking code progress)
- **AMD micro provisioning** (Phase C host): when ready, install repo
  + uv + paper-trade-only deps. v3 §8 details. **Day-1 backup policy
  mandatory** — forward fetch is the only copy of forward data
  (provenance-loss structural lesson)

## Don't relitigate

- Rebuilding L1 archive for backtest validation (B1/B3 ruled out)
- `funding_xs` / `momentum_xs` (3-round adversarial-debate; PARKED;
  next alpha cycle is paper-trade-driven, not design-driven)
- Q1.b multi-symbol script re-run (CANCELLED — no archive, cosmetic)
- carry_v3's economic thesis (validated reasoning intact; only
  empirical track record was lost)

## Key refs

- `validated_alphas.yaml` `registry_provenance` block — global
  ARCHIVE_LOST context
- `docs/alphas/carry_v3.md` — SUSPENDED with provenance admonition;
  logic + locked params intact in code
- `docs/phase_c_infra_design_v2.md` + `_v3.md` — implementation spec
  (v2 base + v3 patches)
- `CLAUDE.md` — red lines (DSR/PBO gate, 3mo paper, ×0.3-0.5 haircut,
  code-reviewer audit, ruff lint)
- `src/alpha_factory/execution/event_log.py` — the spine; v2 §2
  schema + B1 recovery + B2 single-writer
