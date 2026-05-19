# Phase C — Paper-Trade Infra Design v3 (v2 → v3 patch)

> **Status:** DESIGN PATCH (patches `phase_c_infra_design_v2.md` on the
> points below ONLY; v2 unchanged elsewhere)
> **Created:** 2026-05-13
> **Supersedes:** specific v2 sections enumerated in the patch table
> **Trigger:** 2026-05-01 provenance loss (verified unrecoverable
> 2026-05-13) + adversarial-debate B1/B3 verdict + component-1 plan
> review (user-approved 2026-05-13)
> **Governance:** code refers to v2 + this v3 patch. Material further
> deviation → v4 before code lands (same rule v2 set for itself).

---

## Trigger for v3

Three events post-date v2 (2026-05-05) and force a design patch:

1. **Provenance loss (2026-05-01, recorded 2026-05-13).** Production L1
   archive + `data/validation/` + RUN_REGISTRY + TRIAL_LOG destroyed in
   the OneDrive→ASCII relocation; exhaustively verified unrecoverable.
   Only Phase 0 `feasibility/data/` (BTC/ETH, no SOL) survives.
2. **Adversarial-debate B1/B3 verdict.** carry_v3 demoted to
   `suspended` / `portfolio_eligible: false`; numbers frozen as
   unreproducible historical record; **paper-trade is now the SOLE
   forward re-validation gate — there is no backtest fallback.**
3. **Component-1 plan review** (user-approved 2026-05-13): AMD micro
   host decided; feasibility/data sanity harness accepted; this v3
   addendum gating code.

---

## v2 → v3 patch table

| # | v2 design point | v3 revision | Reason |
|---|---|---|---|
| P1 | §3 reconcile is 3-quantity (A paper / B event-log replay / C replay-on-current-L1) | **Start 2-quantity (A/B only).** C (`data_correction_effect`) is uncomputable at go-live — there is no L1 archive. C phases in progressively once the signal layer's own forward fetches accumulate ≥ window history. | L1 archive destroyed |
| P2 | §5 pre-go-live: `trial_log` key fix (~1 hr) | **MOOT — removed.** No archive to register against; Q1.b cancelled (cosmetic, non-critical-path). | provenance loss |
| P3 | §5 pre-go-live: `carry_v3.md` multi-symbol section | **DONE** (PR #4 merged 2026-05). | — |
| P4 | (implicit) backtest record is the validation backstop behind paper-trade | **NONE.** Paper-trade is the sole re-validation gate. The 3-month tracking-error gate is now load-bearing with no fallback. | carry_v3 demoted |
| P5 | (gap, unstated in v2) §3 reconcile A and B both execute the same `carry_v3.py` → a *systematic signal-logic error* is invisible to the reconcile | **Add pre-go-live signal-logic sanity harness** on surviving `feasibility/data` (BTC/ETH). Non-canonical; catches gross logic regression only; NOT a Sharpe/validation claim. | provenance loss removed the backtest oracle |
| P6 | (open) signal-compute host undecided | **DECIDED: AMD micro (1 core / 1 GB).** Backtest/heavy work stays off-host. | user 2026-05-13 |
| P7 | (implicit) build component-1-deep-first | **Walking skeleton:** thinnest viable slice through ALL pipeline stages end-to-end first, then iterate depth. Strategy is a pluggable interface (carry_v3 = placeholder); the *pipeline* is the deliverable, strategy deferred ("策略之後再想"). §7 sanity-harness demoted to pre-go-live "later". | user reprioritization 2026-05-13 |

---

## §3′ — Reconcile under provenance loss

v2 §3 defined three quantities. Post-loss:

```
A. realized_pnl_24h_paper          — paper position book + simulated fills
B. replay_pnl_24h_event_log        — re-execute carry_v3 logic against the
                                     event log's signal_inputs_hash data
C. replay_pnl_24h_l1_current       — DEFERRED. No L1 archive exists at
                                     go-live. Becomes computable only
                                     after the signal layer's own forward
                                     fetches accumulate; then
                                     data_correction_effect is forward-only.

PRIMARY GATE:  tracking_error = (A - B) / |B|     # unchanged from v2
DEFERRED:      data_correction_effect = (B - C) / |C|   # null until C exists
```

**Key consequence — the forward archive is a side effect, not a task.**
The signal-compute layer MUST fetch fresh funding/klines every cycle to
compute the rolling-120-settlement regime state. Persisting those fetches
*is* the forward L1 archive being rebuilt incrementally. There is no
separate "rebuild L1" task — running paper-trade rebuilds it as a
byproduct. `data/corrections/` sidecar logging (v2 locked-fact) applies
to these forward fetches from day 1, so C becomes computable for any
window fully inside the forward-fetch era.

`daily_reconcile.parquet` schema (v2 §3): `replay_l1_current_pnl_24h`
and `data_correction_effect` columns are **nullable**, written null
until forward-L1 covers the reconcile window.

---

## §7 (new) — Signal-logic oracle gap

**The gap.** v2 §3's reconcile catches fill-simulation drift, API gaps,
clock skew, and (later) data corrections. It does **NOT** catch a
*systematic error in `carry_v3.py` itself* — because A (paper) and B
(event-log replay) both execute the same logic. Pre-loss, the canonical
backtest archive was the independent oracle that would have caught a
logic regression. That oracle is gone.

**Mitigation (accepted, non-canonical).** Before paper-trade go-live,
run a one-time signal-logic sanity harness:

- Input: surviving `feasibility/data/` (Phase 0, BTC/ETH, no SOL,
  git-untracked but physically present).
- Run `carry_v3.py` regime state machine over it; emit the position /
  transition timeline.
- **Manual economic-sense review** (not a metric gate): do longs/shorts
  and exit/re-entry transitions occur where the funding-compression
  logic says they should? Any obviously broken behaviour (e.g., never
  exits, flips every bar, ignores compression) is a gross-logic-error
  catch.

**Explicit limits (LdP-honest):**
- This is NOT a re-validation. No Sharpe/DSR/PBO claim is produced.
- BTC/ETH only; SOL signal logic has no independent check at all.
- feasibility/data is a different data_version than the lost archive;
  numbers are not comparable to historical yaml figures.
- It guards against *gross* logic regression only, not subtle edge bugs.
  Subtle bugs surface (if at all) only through 3 months of paper-trade
  tracking-error behaviour — which is exactly why the paper window is
  now load-bearing.

---

## §8 (new) — Host decision: AMD micro

**Decided:** signal-compute + paper-trade run on the AMD micro
(1 core / 1 GB), freshly provisioned. Backtest / validation / any
full-history work stays OFF this host.

**Why it fits (1 GB is adequate for this workload):**
- Per cycle the layer loads only a rolling **120-settlement** funding
  window + matching klines window — kilobytes, not the multi-year
  archive. Deterministic state machine. JSONL append. Idle 8h between
  cycles.
- The OOM risk that disqualified the micro for *backtest* (full-history
  concat + DSR bootstrap) does not exist for *incremental signal compute*.

**Operational notes for impl:**
- cron at funding-settlement boundaries (00:00 / 08:00 / 16:00 UTC,
  Binance 8h cadence) + small post-boundary offset for settlement finality.
- Clock-drift sample at cycle start vs Binance server time (v2 §2);
  > 500 ms → halt P0.5.
- Forward-L1 fetches persist to the micro's disk; **this is now the only
  copy of forward data → host-side snapshot/backup policy is mandatory
  from day 1** (the structural lesson from the provenance loss).

---

## Readiness gate (v2 §6, updated)

- [x] BNB rebate ON / Oracle time source / L1 UPSERT (v2-verified; still true)
- [x] Provenance loss recorded; carry_v3 demoted; yaml + carry_v3.md annotated (2026-05-13)
- [x] v2 → v3 patch written (this doc) ← **the doc-governance gate**
- [ ] v3 reviewed + approved by user ← *gating code*
- [ ] feasibility/data signal-logic sanity harness run + manually reviewed ← *pre go-live*
- [ ] NTP fallback added on the host (Q1.a; independent ops; still open)
- [ ] AMD micro provisioned with repo + uv + paper-trade deps only (no backtest stack)
- [ ] host-side snapshot/backup policy defined (mandatory — single copy of forward data)

---

## Open (decide at impl time, unchanged from v2 §5)

- SQLite vs JSONL-only — component 1 ships JSONL-only; JSONL is source
  of truth regardless.
- Limit-vs-market fill policy — belongs to the fill-simulator component,
  not signal-compute; paper m1 = taker for conservatism (v2 §4).

---

## §9 (new) — Strategy as a pluggable interface

The pipeline does NOT hardcode carry_v3. A thin protocol:

```
compute_target_position(rolling_window) -> TargetPosition
```

carry_v3 is wrapped as ONE adapter implementing this protocol and is a
*placeholder*. Swapping strategy = swapping the adapter; the pipeline
(fetch / event-log / fill-sim / reconcile / halt / live-seam) does not
change. This operationalises "策略之後再想": the pipeline is the
deliverable; which strategy flows through it is deferred and replaceable.

---

## Build approach — walking skeleton (v3-P7)

Priority: **end-to-end pipeline "通" > any single stage's depth.** Build
the thinnest viable version of every stage so data flows ingest → … →
reconcile → live-seam, then iterate depth. **Do NOT mistake "skeleton
runs end-to-end" for "production-ready".**

| # | Stage | Thinnest version (skeleton) |
|---|---|---|
| 1 | L1 forward fetch | rolling 120-settle funding + klines window; persist (= forward-archive byproduct, v3 §3′) |
| 2 | Strategy interface | `compute_target_position(window)->TargetPosition` (§9); carry_v3 adapter as placeholder |
| 3 | Event log | `event_log.py` — JSONL append-only, schema v2 §2, single-writer (B2), partial-write recovery (B1). **SPINE — build first.** |
| 4 | Paper fill sim | target → simulated fills + position book; m1 = taker, zero slippage |
| 5 | Daily reconcile | A vs B only (v3 §3′ 2-quantity); C / data_correction_effect null |
| 6 | Halt / kill | kill flag-file + P0/P1 (crash) only; P2–P5 deferred to depth iteration |
| 7 | Live seam | stub adapter; paper/live share pipeline, live = adapter swap (no real orders) |

**Build order:** `[3] event_log.py` first (the spine everything records
to), then thin `[1][2][4][5]` so the skeleton runs one full cycle
end-to-end, then `[6][7]` stubs, then iterate depth per stage.

`code-reviewer` after each module (CLAUDE.md red line: no production
code without code-reviewer audit). Tests synthetic + deterministic;
no backtest oracle (§7, demoted to pre-go-live "later").

---

*v3 patch ends. v2 remains the base spec for everything not listed here.*
