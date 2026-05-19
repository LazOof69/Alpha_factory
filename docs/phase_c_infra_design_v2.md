# Phase C — Paper-Trade Infra Design v2

> **Status:** DESIGN (not yet implemented; supersedes v1 implicit-design in
> `phase_c_paper_trade_plan.md`)
> **Created:** 2026-05-05
> **Supersedes:** v1 design embedded in `phase_c_paper_trade_plan.md`
> (informally produced 2026-05-04, before adversarial-debate)
> **Adversarial-debate ref:** PR #3 session 2026-05-05; 9 landed attacks
> on v1, 3 failed; this doc is the v1 → v2 patch covering landed attacks.

---

## Locked facts (verified 2026-05-05)

These facts are inputs to the design; no longer open questions:

```yaml
binance_account:
  bnb_rebate_enabled: true
  effective_fees_bp:
    spot_maker: 7.5
    spot_taker: 7.5
    perp_maker: 2.0
    perp_taker: 5.0
  round_trip_taker_full: 25.0    # spot in+out 7.5+7.5 + perp in+out 5+5
  round_trip_maker_full: 19.0    # if all limit orders fill as maker
  paper_m1_assumption: "use taker for conservatism;
                        recalibrate at month-2 boundary"

oracle_arm_host:
  hostname: quant-1
  timezone: Etc/UTC
  ntp_daemon: systemd-timesyncd
  ntp_primary_server: 169.254.169.254          # Oracle Cloud link-local
  ntp_fallback_pending: true                    # add pool.ntp.org / cloudflare
  measured_drift_vs_binance_ms: 50              # network RTT dominates
  stratum: 3
  validated_at: "2026-05-05"

l1_archive:
  ingest_semantic: UPSERT                       # NOT insert-only
                                                # klines.py:419 + funding.py:279
                                                # use unique(keep='last') over
                                                # ingested_at-sorted merge
  reproducibility_via: data_version fingerprint # max_open_time + corrections
                                                # sidecar SHA-256 first-8-hex
  corrections_logged_in: data/corrections/      # sidecar files; not inline
```

---

## v1 → v2 patch: 9 landed attacks resolved

| # | v1 attack | v2 resolution | Code touch? |
|---|---|---|---|
| A1 | Halt paper vs live config 應分開 | Two halt configs: `paper_halt_policy.yaml` (alert-only) + `live_halt_policy.yaml` (flat). Same triggers, different actions. | new file at infra impl time |
| A2 | mtm DD 5% 與 daily DD 1% 應為兩 trigger | Replace single mtm-DD trigger with: P3 daily DD > 1% (alert) + P2 cumulative mtm DD > 5% (halt-or-alert per mode) | halt config |
| A3 | Halt 需 sustained-breach gate | All threshold triggers (P2/P3) require ≥ 2 consecutive 8h bars beyond gate before action fires | halt state machine |
| A4 | Replay 需 data_version pinning | Every paper-trade event captures `data_version` of L1 at compute time. Replay must use SAME data_version OR explicitly log divergence | event schema |
| A5 | L1 ingest INSERT vs UPSERT | **VERIFIED 2026-05-05: L1 IS UPSERT.** Replay design changed (see §3 below) — relying on `ingested_at <= decision_time` was wrong | replay strategy |
| B1 | JSONL partial-write recovery | Spec: parser skips malformed last line + WARNs. Recovery is silent for paper; logged for live | parser code |
| B2 | Single-writer invariant 文件化 | Doc: only signal-compute cron writes events. halt-checker is read-only. Future fork → must use SQLite + advisory lock | this doc + comment in event-log writer |
| B3 | Kill 多層架構 | paper-trade: flag-file only (sufficient). live-trade pre-flight: flag-file + SIGTERM handler + Binance `cancel-all` API | this doc; live impl deferred |
| B4 | Event log 補 versioning fields | Schema adds: `git_commit_hash`, `data_version`, `process_clock_source`, `signal_inputs_hash` | event schema |

---

## §1. Halt mechanism v2 — two-mode config

### Triggers (priority order)

| Pri | Trigger | Threshold | Sustained gate |
|---|---|---|---|
| P0 | Manual kill flag observed | flag file exists | none (single observation) |
| P1 | Signal-compute crash (any unhandled exception) | exception fires | none |
| P2 | Cumulative mtm DD breached | mtm DD > 5% from peak | ≥ 2 consecutive 8h bars |
| P3 | Daily DD breached | daily realized+mtm DD > 1% | ≥ 2 consecutive UTC days |
| P4 | Tracking error breached | \|TE\| > 0.50 | ≥ 3 consecutive UTC days |
| P5 | Binance API outage | API unreachable continuously | > 4h |

### Two-mode action table

| Pri | paper-mode action | live-mode action | Re-arm condition |
|---|---|---|---|
| P0 | flat + exit | flat + cancel-all + exit | flag removed + user ack written to audit log |
| P1 | flat + exit + crash dump | flat + cancel-all + exit + crash dump + alert | root cause fixed + redeploy + 24h observation |
| P2 | **alert-only** (don't flat) | flat | mtm DD < 3% for 1 week |
| P3 | **alert-only** | flat | daily DD < 0.5% for 3 days |
| P4 | **alert-only** | flat | rolling-3d \|TE\| < 0.30 for 1 week |
| P5 | log + maintain position (fail-static) | log + maintain position (fail-static) | API health for 1h |

**Why paper P2/P3/P4 are alert-only:** paper-trade's purpose is to gather
tracking-error data through 3 months across regimes. Halting censors the
sample. Halt-mechanism testing happens via dry-run (inject synthetic
breach, verify alert fires, verify flat-the-book code path) — not via
real paper-trade triggers. Live mode flats because $$$ is real.

### Sustained-breach gate semantics

For threshold triggers (P2/P3/P4): the trigger fires the moment threshold
is exceeded, but the **action** waits for sustained breach.

```
state at t=0:   mtm DD = -3% (under 5% threshold)
state at t=8h:  mtm DD = -6% (over threshold) → P2 trigger ARMED, count=1
state at t=16h: mtm DD = -7% (over threshold) → count=2 → ACTION FIRES
                (paper: alert; live: flat)
```

Anti-flutter: if mtm DD recovers to under 5% before count=2, counter resets.

---

## §2. Event log schema v2

`data/paper_trade/paper_events.jsonl` — append-only, one event per line.

```json
{
  "event_id": "uuid-v4",
  "ts": "2026-05-15T08:00:00.123Z",
  "kind": "signal_compute"
        | "fill_simulated"
        | "unwind_simulated"
        | "funding_received"
        | "halt_armed"
        | "halt_action_fired"
        | "kill_flag_observed"
        | "reconcile_complete"
        | "system_start"
        | "system_shutdown"
        | "data_version_drift_detected",

  "strategy_id": "carry_v3",
  "symbol": "BTC-USDT",

  "versioning": {
    "git_commit_hash": "9720d17",         # short SHA of HEAD at compute time
    "data_version": "2026-05-15T08:00+nocorr",  # L1 archive fingerprint
    "process_clock_source": "systemd-timesyncd",
    "process_clock_drift_vs_binance_ms": 47
  },

  "data": {
    /* kind-specific payload */
    /* For signal_compute: include `signal_inputs_hash` =
       SHA-256 of (funding_window_120, klines_window_120) used to compute
       the regime state. Replay verifies this hash matches L1 at replay
       time; mismatch → emit data_version_drift_detected event */
  }
}
```

**Versioning rationale:**
- `git_commit_hash`: code change mid-paper-trade detectable; replay can
  use the right `carry_v3.py` version
- `data_version`: L1 archive fingerprint; if changes mid-paper-trade
  (correction landed), replay sees drift
- `process_clock_drift_vs_binance_ms`: sampled at cycle start; if grows
  > 500ms, halt with kind `clock_drift_detected` (NEW; add to triggers
  list above as P0.5 between manual kill and crash)
- `signal_inputs_hash`: cryptographic check that the actual numbers
  fed into the regime state machine are reproducible

**Partial-write recovery (B1):** parser skips malformed last line, emits
WARN, continues. malformed-line count stored in reconcile output for
audit. If > 1 malformed line in a single session, escalate (likely real
corruption, not crash recovery).

---

## §3. Replay strategy v2 — driven by event log, NOT by L1 re-query

### Why v1 was wrong

v1 design said: "replay uses `ingested_at <= decision_time` filter on
L1 klines/funding". This **doesn't work** because L1 is UPSERT
(`klines.py:419` `unique(keep="last")` after sort by ingested_at).
A correction landing at t' > decision_time PHYSICALLY REPLACES the
pre-correction row in the parquet partition. There is no
"as-seen-at-decision_time" view recoverable from L1 alone.

### v2 approach: event log is replay source of truth

Each `signal_compute` event captures `signal_inputs_hash` = hash of the
exact (funding_120_settle_window + klines_120_window) data the strategy
saw at compute time. Replay reads this hash from event log.

**Daily reconcile compares 3 things:**

```
A. realized_pnl_24h_paper  (from paper position book + simulated fills)
B. replay_pnl_24h_event_log  (re-execute strategy logic against event log
                              data only — no L1 re-query needed)
C. replay_pnl_24h_l1_current  (re-execute against current L1 archive)

tracking_error          = (A - B) / |B|         # paper vs replay-self
data_correction_effect  = (B - C) / |C|         # event-log vs current-L1
```

**Interpretation:**
- `tracking_error` = paper trade vs strategy logic on identical inputs.
  Should be ~0 if no fill simulation drift, no API gaps, no clock skew.
- `data_correction_effect` ≠ 0 iff L1 data has been corrected since the
  paper-trade observation. This isolates corrections from execution
  drift. Plotted separately in the daily reconcile output.

### Storage

```
data/paper_trade/daily_reconcile.parquet (append-only):
  date                        DATE
  realized_pnl_quote_24h      REAL  # A
  replay_event_log_pnl_24h    REAL  # B
  replay_l1_current_pnl_24h   REAL  # C
  tracking_error              REAL
  data_correction_effect      REAL
  realized_sharpe_to_date     REAL
  replay_sharpe_to_date       REAL
  n_signals_today             INT
  n_simulated_fills_today     INT
  n_halts_today               INT
  n_data_version_drifts       INT
  notes                       TEXT
```

---

## §4. Cost model v2 — config-file backed

Replace v1's hardcoded fees with a config:

```yaml
# config/cost_model.yaml
binance:
  bnb_rebate: true
  spot_maker_bp: 7.5
  spot_taker_bp: 7.5
  perp_maker_bp: 2.0
  perp_taker_bp: 5.0
  funding_settlement_bp_per_8h: live_observed   # not modeled; from API

slippage_model:
  kind: square_root_impact
  beta_btc: 0.030
  beta_eth: 0.045
  beta_sol: deferred
  default_assumption_at_1k_notional_bp: 0  # confirmed empirically post-paper

assumptions:
  fee_tier_static: true           # VIP-0 throughout 3m paper window
  bnb_balance_sufficient: true    # user maintains BNB > $10 for rebate
                                   # to remain active
```

Loaded by `validation/costs.py` at module import time; same config powers
backtest, paper-trade, and (later) live. Switching BNB rebate off (for
audit reproduction) → flip the bool, re-run; numerical answer changes
predictably.

---

## §5. What's still open / deferred (not blocking infra start)

### Decisions deferred to infra implementation time
- **SQLite vs JSONL-only storage** — adversarial debate landed partial
  attack on dual-storage. Decide at impl time based on:
  - SQLite if halt-checker query rate > 1/min
  - JSONL-only otherwise (replay rebuilds state)
  - Either way, JSONL is source of truth; SQLite (if used) is cache
- **Limit-order fill simulation policy** — when to assume maker fills vs
  taker fills. Most conservative: always taker for paper m1; revisit at
  m2 boundary with empirical maker-fill data.

### Live-only deferrals (not paper-trade scope)
- Kill switch SIGTERM handler (B3 live tier)
- Kill switch Binance `cancel-all` API call (B3 live tier)
- chrony upgrade from systemd-timesyncd (caveat 2 from time-source check)
- chrony peer fairness + statistical sanity check
- BNB-balance auto-monitor (refill if drops below $10)

### Pre-paper-trade-go-live cleanup (still open)
- `trial_log` key fix (~1 hr) — symbol in idempotency key
- `carry_v3.md` multi-symbol section (~30 min)
- Add NTP fallback to systemd-timesyncd config (5 min on Oracle host)

---

## §6. Architecture-level decisions: GO / NO-GO checkpoint

### Survived adversarial-debate (proceed)
- 5-priority halt taxonomy (P0-P5) ✅ revised to two-mode action table
- Event-log-as-source-of-truth ✅ strengthened (versioning fields added)
- Replay strategy ✅ pivoted to event-log-driven (was: L1 re-query)
- Cost model unified across backtest/paper/live ✅ config-file backed
- Kill flag-file pattern for paper ✅ scope-clarified

### Architecture revisions for v2 (vs v1)
1. Halt has two action modes (paper alert-only / live flat) not one
2. Threshold triggers require sustained-breach gate (anti-flutter)
3. Replay reads event log, not L1 archive (UPSERT-aware)
4. Event log captures git hash + data_version + clock drift per cycle
5. Cost model is config-file, not hardcoded constants

### Implementation readiness gate (before writing any infra code)
- [x] BNB rebate confirmed ON
- [x] Oracle ARM time source verified (50ms drift, NTP active)
- [x] L1 ingest semantic verified (UPSERT)
- [ ] NTP fallback added to Oracle host (5 min — recommended pre-go-live)
- [ ] **One more adversarial-debate round on this v2 doc?** (optional;
      v1→v2 delta is itself a critique-driven revision so likely not
      needed; recommend skip and proceed to impl)

---

## Open questions (next session)
1. SQLite vs JSONL-only — pick at infra impl time, not now
2. Limit-vs-market order fill policy — pick at infra impl time
3. Should NTP fallback be added now (5 min on Oracle), or batched with
   pre-go-live cleanup?

---

*This doc is the implementation spec. Subsequent infra code should refer
back to it for architecture decisions. Material deviations require
explicit doc revision (v3) before code lands.*
