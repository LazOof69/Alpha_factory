"""Polars schemas + path roots for the L3 validation layer.

Mirrors `alpha_factory.data.schema` pattern: schemas as
`dict[str, pl.DataType]` for direct `polars.cast`; pure path constants
(no `Path(__file__)` magic) so the caller controls CWD.

Single source of truth for the 8 validation-layer parquet schemas.
Callables (`validate_equity_curve`, `make_rng`, `infer_periodicity`,
re-exported `get_archive_state`) live in `contracts.py`. Higher-level
modules write/read these schemas:

    A.4.2 metrics.py     reads EQUITY_CURVE + PER_SYMBOL_CONTRIB
    A.4.3 cv.py          generates folds (consumed via params_hash join
                         to TRIAL_LOG)
    A.4.4 dsr.py         reads TRIAL_LOG (n_trials = COUNT distinct
                         params_hash) + EQUITY_CURVE for bootstrap
    A.4.5 pbo.py         consumes wide returns matrix (pivot from
                         OOS_RETURNS_PANEL with alpha_active mask)
    A.4.6 regime.py      writes REGIME_METRICS rows
    A.4.7 costs.py       writes the friction columns of BACKTEST_LEG
                         (fees / funding_paid / slippage)
    A.4.8 registry.py    manages RUN_REGISTRY (lifecycle + atomic
                         status transitions) + BACKTEST_RESULT +
                         appends to TRIAL_LOG
    A.4.9 integration    end-to-end via synthetic alphas

Storage layout (under repo-root `data/validation/`, gitignored):

    backtests/result/strategy={id}/data.parquet           (BACKTEST_RESULT)
    backtests/leg/year=YYYY/strategy={id}/data.parquet    (BACKTEST_LEG)
    backtests/contrib/year=YYYY/strategy={id}/data.parquet (PER_SYMBOL_CONTRIB)
    backtests/equity/year=YYYY/strategy={id}/data.parquet (EQUITY_CURVE)
    panels/oos_returns/data.parquet                       (OOS_RETURNS_PANEL)
    backtests/regimes/strategy={id}/data.parquet          (REGIME_METRICS)
    registry/runs.parquet                                 (RUN_REGISTRY)
    registry/trial_log/strategy={id}/data.parquet         (TRIAL_LOG)

Design decisions from the A.4.1 1-round adversarial critique
(strategy-validation canon, "spine module"):

* RUN_REGISTRY does NOT denormalize scalar metric columns. The cached
  fields (dsr/pbo/sharpe/max_dd/n_obs/n_trials) drift from the
  authoritative source after any re-validation triggered by a
  corrections-diff. Replaced with a single `metrics_summary_json: Utf8
  nullable` populated atomically in the `complete -> validated`
  transition. Single-source-of-truth, atomic update; consumers parse
  JSON.

* RUN_REGISTRY status lifecycle is documented as a state machine; all
  transitions go through the (yet-to-land) `registry.transition_status`
  function which uses parquet-rewrite-then-rename (mirrors L1
  corrections sidecar). Direct in-place mutation is forbidden by
  convention.

* TRIAL_LOG drops `prev_chain_hash` and `row_hash` -- hash-chain
  integrity adds no real defense at personal-scale (the threat model
  is self-tampering, which a chain over which the writer also
  controls cannot detect). Trial uniqueness is enforced by
  `(strategy_id, params_hash, data_version, split)`; DSR `n_trials`
  reads `COUNT(distinct params_hash) per strategy_id`. Append-only
  semantics via the writer in A.4.8.

* EQUITY_CURVE splits `period_ret` into `period_ret_gross` (pre-fees,
  pre-funding, pre-slippage) and `period_ret_net` (post all three).
  `validate_equity_curve` (contracts.py) enforces equity continuity
  on `_net`. A.4.2 metrics.py and A.4.6 regime stratification both
  consume `_net` -- otherwise friction discontinuities at fee-tier
  transitions get attributed to the wrong regime.

* OOS_RETURNS_PANEL carries `alpha_active: Boolean` so consumers can
  distinguish "alpha was live, ret=0" from "alpha did not exist yet".
  L4 HRP picks intersection vs union policy explicitly; without this
  flag the L3 -> L4 contract is ambiguous and HRP correlations are
  not reproducible.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from alpha_factory.data.schema import DATA_ROOT, TZ_UTC

# ── Schema version ────────────────────────────────────────────────────────
#
# Bump on any persisted-schema change in this file. Reports record the
# version they were generated under so cross-version comparisons fail
# loud rather than silently joining incompatible columns.
SCHEMA_VERSION = 1


# ── RUN_REGISTRY (run index, lifecycle + atomic JSON metric snapshot) ────
#
# State machine for `status`:
#
#     running    -> complete | failed
#     complete   -> validated  (via registry.validate_run; gates pass)
#                |  running    (via registry.trigger_revalidation;
#                                corrections-diff dirty)
#     failed     -> running    (manual retry only)
#     validated  -> running    (via corrections-diff trigger)
#
# All transitions go through `registry.transition_status(run_id, from_,
# to_)` which uses parquet-rewrite-then-rename atomic pattern (L1
# corrections sidecar mirror). Direct in-place mutation is FORBIDDEN.
#
# `metrics_summary_json` is a canonical JSON object with keys
# {dsr, pbo, sharpe, max_dd, n_obs, n_trials, validated_at}, sorted,
# no whitespace. Null until `status == "validated"`. Updated atomically
# in the `complete -> validated` transition. NEVER updated in-place
# without a status transition.
#
# `corrections_diff_status` reflects the most recent
# `check_corrections_diff` result for this run. "dirty" disqualifies
# the run from `list_validated_alphas` until re-validated.
RUN_REGISTRY_SCHEMA: dict[str, pl.DataType] = {
    # identity (immutable)
    "run_id": pl.Utf8,                                   # UUID v4
    "strategy_id": pl.Utf8,                              # canonical strategy name
    "variant": pl.Utf8,                                  # additional discriminator
    "params_hash": pl.Utf8,                              # SHA-256 hex of canonical-JSON params
    "data_version": pl.Utf8,                             # see L1 get_archive_state
    "archive_max_kline_time": pl.Datetime("us", time_zone=TZ_UTC),
    "qc_audit_run_ts": pl.Datetime("us", time_zone=TZ_UTC),  # nullable
    "registered_at": pl.Datetime("us", time_zone=TZ_UTC),
    # lifecycle
    "status": pl.Utf8,                                   # see state machine above
    "verdict": pl.Utf8,                                  # nullable: pass | fail
    "corrections_diff_status": pl.Utf8,                  # clean | dirty | unchecked
    # snapshot of validation results (atomic JSON)
    "metrics_summary_json": pl.Utf8,                     # nullable until validated
}

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_VALIDATED = "validated"
VALID_STATUSES: tuple[str, ...] = (
    STATUS_RUNNING, STATUS_COMPLETE, STATUS_FAILED, STATUS_VALIDATED,
)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VALID_VERDICTS: tuple[str, ...] = (VERDICT_PASS, VERDICT_FAIL)

CORRECTIONS_CLEAN = "clean"
CORRECTIONS_DIRTY = "dirty"
CORRECTIONS_UNCHECKED = "unchecked"
VALID_CORRECTIONS_STATUSES: tuple[str, ...] = (
    CORRECTIONS_CLEAN, CORRECTIONS_DIRTY, CORRECTIONS_UNCHECKED,
)


# ── BACKTEST_RESULT (run header / audit metadata) ────────────────────────
#
# One row per run. Records what was run, against what data version,
# with what params -- the immutable audit trail tying a run_id to its
# inputs. Distinct from RUN_REGISTRY: BACKTEST_RESULT records "what was
# the run", RUN_REGISTRY records "what is its lifecycle status".
BACKTEST_RESULT_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8,
    "strategy_id": pl.Utf8,
    "variant": pl.Utf8,
    "params_json": pl.Utf8,                              # canonical JSON for grep/audit
    "params_hash": pl.Utf8,                              # SHA-256 hex
    "universe_json": pl.Utf8,                            # JSON list of canonical symbols
    "start_time": pl.Datetime("us", time_zone=TZ_UTC),
    "end_time": pl.Datetime("us", time_zone=TZ_UTC),
    "n_bars": pl.UInt32,
    "data_version": pl.Utf8,
    "archive_max_kline_time": pl.Datetime("us", time_zone=TZ_UTC),
    "qc_audit_run_ts": pl.Datetime("us", time_zone=TZ_UTC),  # nullable
    "engine_version": pl.Utf8,
    "computed_at": pl.Datetime("us", time_zone=TZ_UTC),
}


# ── BACKTEST_LEG (per-bar per-leg, long format) ──────────────────────────
#
# `leg_id` is a strategy-defined ordinal. For carry V2: leg_id=0 is
# long spot, leg_id=1 is short perp. For an N-symbol cross-sectional
# alpha each bar emits N rows (one per symbol leg). `weight` is signed
# (negative = short).
#
# Friction columns (`fees`, `funding_paid`, `slippage`) are populated
# by A.4.7 costs.py; they remain Float64 (default 0.0) for runs without
# explicit cost modeling. `leg_ret` is BEFORE friction; the post-friction
# return on this leg = `leg_ret - (fees + funding_paid + slippage) /
# notional` (when notional > 0).
BACKTEST_LEG_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8,
    "time": pl.Datetime("us", time_zone=TZ_UTC),
    "leg_id": pl.UInt8,
    "symbol": pl.Utf8,                                   # canonical "BTC-USDT"
    "market": pl.Utf8,                                   # "spot" | "perp_usdt"
    "side": pl.Utf8,                                     # "long" | "short"
    "weight": pl.Float64,                                # signed fraction of NAV
    "notional": pl.Float64,                              # USDT
    "leg_ret": pl.Float64,                               # bar return BEFORE friction
    "fees": pl.Float64,                                  # commission this bar
    "funding_paid": pl.Float64,                          # funding paid (perp); 0 for spot
    "slippage": pl.Float64,                              # estimated slippage
}

LEG_SIDE_LONG = "long"
LEG_SIDE_SHORT = "short"
VALID_LEG_SIDES: tuple[str, ...] = (LEG_SIDE_LONG, LEG_SIDE_SHORT)

LEG_MARKET_SPOT = "spot"
LEG_MARKET_PERP_USDT = "perp_usdt"
VALID_LEG_MARKETS: tuple[str, ...] = (LEG_MARKET_SPOT, LEG_MARKET_PERP_USDT)


# ── PER_SYMBOL_CONTRIB (per-bar per-symbol P&L attribution) ──────────────
#
# Aggregates all legs of a symbol into a single contribution scalar per
# bar. For carry V2 (BTC-USDT spot + perp legs combined): one row per
# bar, contrib_pnl = (long_spot_pnl + short_perp_pnl + funding_income).
#
# Used by capacity sizing (A.4.2 turnover / capacity helpers) and by
# regime stratification (A.4.6 needs per-symbol returns to apply
# regime tags consistently across XS positions).
PER_SYMBOL_CONTRIB_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8,
    "time": pl.Datetime("us", time_zone=TZ_UTC),
    "symbol": pl.Utf8,
    "contrib_pnl": pl.Float64,                           # USDT P&L this bar
    "position_notional": pl.Float64,                     # net |long-short| USDT
    "weight_net": pl.Float64,                            # net signed weight
}


# ── EQUITY_CURVE (canonical scalar time series for one run) ──────────────
#
# `period_ret_gross` is pre-friction; `period_ret_net` is post fees +
# funding + slippage. `validate_equity_curve` enforces:
#
#     equity[t] == equity[t-1] * (1 + period_ret_net[t])  within rtol=1e-9
#
# (modulo first row: equity[0] is starting capital, period_ret_net[0] = 0).
#
# `cumulative_pnl[t] = equity[t] - equity[0]` (denorm convenience;
# also validated for consistency).
EQUITY_CURVE_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8,
    "time": pl.Datetime("us", time_zone=TZ_UTC),
    "equity": pl.Float64,                                # > 0 always
    "period_ret_gross": pl.Float64,                      # pre-friction
    "period_ret_net": pl.Float64,                        # post-friction
    "cumulative_pnl": pl.Float64,                        # equity - equity[0]
}


# ── OOS_RETURNS_PANEL (long format for HRP / cross-alpha correlations) ───
#
# Spans MULTIPLE strategies. Wide pivot at consumer side (HRP needs
# T x N matrix). `alpha_active` distinguishes "alpha was live this
# period, return = 0" (e.g. carry V2 in flat regime) from "alpha did
# not exist yet" (e.g. funding_xs has no rows pre-2022). Without this
# flag, pivoting long->wide produces NaN blocks that HRP correlation
# silently mishandles (pairwise-complete vs listwise gives different
# clusters).
#
# A.4.8 `get_oos_panel(alpha_names)` returns long format with mask;
# L4 HRP layer chooses intersection vs union policy explicitly.
OOS_RETURNS_PANEL_SCHEMA: dict[str, pl.DataType] = {
    "alpha_id": pl.Utf8,                                 # = strategy_id (or strategy_id/variant)
    "time": pl.Datetime("us", time_zone=TZ_UTC),
    "oos_ret": pl.Float64,                               # OOS return at this period
    "alpha_active": pl.Boolean,                          # true=alive, false=not yet existed
}


# ── REGIME_METRICS (per-regime stratified summary for one run) ───────────
#
# `regime_kind` is the taxonomy ("vol", "etf", "macro", "funding"); a
# given run_id may emit multiple rows per regime_kind (e.g. one row each
# for regime_kind="vol", regime_label in {"low","mid","high"}).
# regime_kind="funding" is reserved -- the funding regime is the
# alpha's responsibility (see CLAUDE.md "carry strategy in production
# without funding-regime detection layer" red line); the validator
# stratifies what alphas have already classified.
REGIME_METRICS_SCHEMA: dict[str, pl.DataType] = {
    "run_id": pl.Utf8,
    "regime_kind": pl.Utf8,                              # vol | etf | macro | funding
    "regime_label": pl.Utf8,                             # bull | bear | pre_etf | post_etf | ...
    "n_obs": pl.UInt32,
    "sharpe": pl.Float64,
    "ann_return": pl.Float64,
    "max_dd": pl.Float64,
    "hit_rate": pl.Float64,
}

REGIME_KIND_VOL = "vol"
REGIME_KIND_ETF = "etf"
REGIME_KIND_TREND = "trend"
REGIME_KIND_MACRO = "macro"
REGIME_KIND_FUNDING = "funding"
VALID_REGIME_KINDS: tuple[str, ...] = (
    REGIME_KIND_VOL, REGIME_KIND_ETF, REGIME_KIND_TREND,
    REGIME_KIND_MACRO, REGIME_KIND_FUNDING,
)

# Regime label constants (consumed by regime.py + downstream gates).
REGIME_LABEL_PRE_ETF = "pre_etf"
REGIME_LABEL_POST_ETF = "post_etf"
REGIME_LABEL_BULL = "bull"
REGIME_LABEL_BEAR = "bear"
REGIME_LABEL_VOL_LOW = "low"
REGIME_LABEL_VOL_MID = "mid"
REGIME_LABEL_VOL_HIGH = "high"

# BTC spot ETF approval cutoff. Used by regime.py to label pre/post-ETF
# epochs; CLAUDE.md / PROJECT.md "validation" gate "Pre-ETF AND post-ETF
# each Sharpe >= 0" anchors here. Stored as the canonical date and
# tz-aware datetime so callers can pick whichever fits their join key.
ETF_CUTOFF_DATE = date(2024, 1, 11)
ETF_CUTOFF_DATETIME = datetime(2024, 1, 11, tzinfo=UTC)


# ── TRIAL_LOG (append-only ledger; DSR n_trials provenance) ──────────────
#
# Every parameter combination ever evaluated against this archive lands
# here. DSR (A.4.4) reads `n_trials = COUNT(distinct params_hash)` for
# the strategy in question; PBO (A.4.5) `enumerate_trials` walks rows.
#
# Uniqueness is enforced by the writer (A.4.8 `log_trial`) on
# `(strategy_id, params_hash, data_version, split)`. Re-evaluating the
# same (params, data, split) is a no-op (idempotent). Multi-process
# safety: append-only with file-level locks per strategy in
# `data/.state/registry/{strategy}/.trials.lock`.
#
# Hash-chain (`prev_chain_hash`, `row_hash`) intentionally NOT in this
# schema -- at personal scale, the threat model is self-tampering, and
# a chain over which the writer also controls the keys cannot detect
# that. What matters for LdP rigor is correct N_trials counting, which
# uniqueness enforcement provides.
TRIAL_LOG_SCHEMA: dict[str, pl.DataType] = {
    "strategy_id": pl.Utf8,
    "trial_id": pl.UInt32,                               # monotonic per strategy_id
    "params_hash": pl.Utf8,                              # SHA-256 hex of canonical-JSON params
    "params_json": pl.Utf8,                              # human-readable
    "evaluated_at": pl.Datetime("us", time_zone=TZ_UTC),
    "data_version": pl.Utf8,
    "split": pl.Utf8,                                    # in_sample | out_of_sample | live
    "realized_sharpe": pl.Float64,
    "n_obs": pl.UInt32,
}

TRIAL_SPLIT_IN_SAMPLE = "in_sample"
TRIAL_SPLIT_OUT_OF_SAMPLE = "out_of_sample"
TRIAL_SPLIT_LIVE = "live"
VALID_TRIAL_SPLITS: tuple[str, ...] = (
    TRIAL_SPLIT_IN_SAMPLE, TRIAL_SPLIT_OUT_OF_SAMPLE, TRIAL_SPLIT_LIVE,
)


# ── Gate thresholds (single source of truth) ─────────────────────────────
#
# Mirrors the PROJECT.md "Validation" decisions table + the
# strategy-validation skill canon. Consumed by A.4.4 (DSR) / A.4.5 (PBO)
# / A.4.8 (registry.list_validated_alphas).
GATE_DSR_LOWER_BOUND = 0.0           # 95% CI lower bound must exceed this
GATE_PBO_UPPER_BOUND = 0.5
GATE_MAX_DD_BOUND = 0.30             # 30%
GATE_MIN_HISTORY_YEARS = 5           # skill canon: "less than 5 years -> red flag"
GATE_REGIME_NONNEG_SHARPE = 0.0


# ── Forward-Sharpe haircut constants (L4 sizing imports; NOT applied here)
#
# Convention from CLAUDE.md / PROJECT.md: forward Sharpe = backtest
# Sharpe x [HAIRCUT_LOW, HAIRCUT_HIGH]. Kept here for the L4 portfolio
# sizing layer to import. Deliberately NOT applied at L3 -- haircut is
# a sizing decision, not a validation artifact.
FORWARD_HAIRCUT_LOW = 0.30
FORWARD_HAIRCUT_HIGH = 0.50


# ── Source identifiers (audit trail for backtest engine version) ─────────
SOURCE_BACKTEST_ENGINE_V1 = "alpha_factory.backtest.engine.v1"


# ── Storage layout ────────────────────────────────────────────────────────
#
# Path is relative to CWD. The L1 layer keeps the actual archive at
# `data/`; validation outputs land at `data/validation/`. State
# (registry, locks) lives at `data/.state/`.
VALIDATION_ROOT = DATA_ROOT / "validation"

BACKTESTS_ROOT = VALIDATION_ROOT / "backtests"
BACKTESTS_RESULT_ROOT = BACKTESTS_ROOT / "result"
BACKTESTS_LEG_ROOT = BACKTESTS_ROOT / "leg"
BACKTESTS_CONTRIB_ROOT = BACKTESTS_ROOT / "contrib"
BACKTESTS_EQUITY_ROOT = BACKTESTS_ROOT / "equity"
BACKTESTS_REGIMES_ROOT = BACKTESTS_ROOT / "regimes"

PANELS_ROOT = VALIDATION_ROOT / "panels"
OOS_RETURNS_PANEL_PATH = PANELS_ROOT / "oos_returns" / "data.parquet"

REGISTRY_ROOT = DATA_ROOT / ".state" / "registry"
RUN_REGISTRY_PATH = REGISTRY_ROOT / "runs.parquet"
TRIAL_LOG_ROOT = REGISTRY_ROOT / "trial_log"
