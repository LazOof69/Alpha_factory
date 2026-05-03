"""End-to-end orchestration: L1 archive snapshot + L2 carry alpha + L3 validation.

Wires `alpha.carry.run_carry_backtest` into the L3 spine
(`validation.registry`) so a single function call produces:

    1. RUN_REGISTRY row (lifecycle running -> complete -> validated)
    2. BACKTEST_RESULT row (audit metadata: data_version, params_hash,
       universe, time-range, engine_version)
    3. BACKTEST_LEG / PER_SYMBOL_CONTRIB / EQUITY_CURVE artifacts on
       disk, year-partitioned (data/validation/backtests/{leg,contrib,
       equity}/year=YYYY/strategy={id}/data.parquet)
    4. REGIME_METRICS rows (data/validation/backtests/regimes/strategy={id}/data.parquet)
    5. TRIAL_LOG append (TRIAL_SPLIT_OUT_OF_SAMPLE; idempotent on
       same params_hash + data_version)
    6. metrics_summary_json snapshot atomically written into
       RUN_REGISTRY.metrics_summary_json on the complete -> validated
       transition

Public surface:

    RunReport                       frozen dataclass returned by run_carry_validation
    run_carry_validation(...)       single-symbol carry V2 end-to-end

VERDICT GATES (PROJECT.md / CLAUDE.md "Validation"):

    Pass requires ALL of:
        DSR CI lower-bound  >  GATE_DSR_LOWER_BOUND   (Phi-prob > 0.5)
        PBO                  <= GATE_PBO_UPPER_BOUND  (skipped if N/A)
        max_dd               >= -GATE_MAX_DD_BOUND    (>= -0.30)
        Each PRESENT regime  >= GATE_REGIME_NONNEG_SHARPE in {bull, bear,
                                                              pre_etf,
                                                              post_etf}

REGIME COVERAGE (per A.5.2 critique #4):

    Distinguish absent vs negative-sharpe. ABSENT regimes (run window
    structurally cannot cover -- e.g. pre_etf for any backtest starting
    after 2024-01-11) are recorded in `verdict_reasons` as
    `regime_absent_<label>` but DO NOT FAIL the verdict. Only PRESENT
    regimes with sharpe < 0 fail. The alternative ("missing -> fail")
    silently rejects every modern run for non-strategy reasons. Tighten
    in Phase B if we want to require multi-regime coverage as a hard
    gate.

CORRECTIONS-DIFF SEMANTICS:

    `check_corrections_diff` compares the run's recorded `data_version`
    (snapshotted at `register_run` and used by the backtest) against
    the archive's current `data_version`. CLEAN means the archive has
    not changed since the run started; DIRTY means a correction
    landed during or after the run. The result is recorded on the
    complete -> validated transition. There is a small race window:
    the archive could change between `check_corrections_diff` and the
    final transition, and a subsequent run on the live tree would
    surface that as DIRTY for the new run -- acceptable at retail scale.

LOG_TRIAL TIMING:

    log_trial is called BEFORE DSR computation so `count_trials` reflects
    the current state of unique-params explored (this run included).
    Idempotent on (strategy_id, params_hash, data_version, split) --
    same-params re-runs do not double-count, so DSR's n_trials is
    stable across re-validation.

CAPACITY ESTIMATE LIMITATION (DEFER):

    `daily_volume_per_symbol` is supplied as a single scalar per symbol
    by the caller (typically the L1 universe snapshot's
    `quote_volume_24h`). This is a forward-looking estimate when applied
    to past bars; the resulting `capacity_estimate_usd` is an
    upper-bound advisory number, not point-in-time. Phase B+ should
    promote ADV to a per-bar time series.

TRIAL_LOG / PBO CONVENTIONS:

    * realized_sharpe in TRIAL_LOG is ANNUALIZED. Cross-strategy
      aggregation requires a stable scale; the schema does not carry
      a scale column, so all sibling alphas porting into this framework
      must annualize before logging.
    * PBO is N/A for single-params runner invocations (CSCV requires
      N >= 8 trials). A Phase B sweep harness running this runner once
      per param combo can pre-compute sweep-level PBO via
      `pbo.enumerate_trials` + `pbo.pbo` and pass it via the
      `pbo_override` kwarg so the PBO gate fires on the per-run
      RUN_REGISTRY validated transition.

IDEMPOTENT RE-RUN:

    `_write_year_partitioned` filters out rows for the same `run_id`
    before appending, so re-running with the same run_id (e.g. crash
    + retry) replaces rather than duplicates. Each `register_run`
    call mints a fresh UUID, however, so this is mainly defensive.

FAILURE MODES:

    Pre-COMPLETE crash (backtest exception, validate_equity_curve
    invariant violation, artifact-write IO error): the broader
    try/except transitions RUNNING -> FAILED (best-effort -- a stale
    StaleStatusError is logged but not propagated) and re-raises the
    original exception. Per-file atomic writes (tmp + rename) mean
    each individual artifact is consistent, but cross-file atomicity
    is absent: a crash between writes 1..5 leaves orphan parquet rows
    tagged with this run_id while the registry says FAILED. Operators
    can identify orphans by joining `run_id` from
    BACKTEST_LEG/CONTRIB/EQUITY/RESULT against
    `RUN_REGISTRY.status == 'failed'` and pruning them.

    Insufficient data (curve.height < 2): runner short-circuits to
    RUNNING -> COMPLETE -> VALIDATED with verdict=fail and
    blocking_reasons=['insufficient_data']. log_trial / regime /
    DSR are skipped; corrections_diff_status stays UNCHECKED.

    Post-COMPLETE crash (very unlikely, since transition_status and
    check_corrections_diff are atomic under registry lock): the row
    sits at COMPLETE indefinitely without VALIDATED. Manual recovery
    via re-run with same params (idempotent through TRIAL_LOG) and
    a new run_id.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from alpha_factory.alpha.carry import (
    STRATEGY_ID as CARRY_STRATEGY_ID,
)
from alpha_factory.alpha.carry import (
    CarryArtifacts,
    CarryParams,
    run_carry_backtest,
)
from alpha_factory.validation.contracts import (
    SALT_DSR_BOOTSTRAP,
    canonical_params_hash,
    canonical_params_json,
    get_archive_state,
    make_rng,
    validate_equity_curve,
)
from alpha_factory.validation.costs import SlippageModel
from alpha_factory.validation.dsr import deflated_sharpe_bootstrap_ci
from alpha_factory.validation.metrics import (
    active_only_sharpe,
    calmar,
    capacity_estimate_usd,
    cvar_quantile,
    full_sample_sharpe,
    hit_rate,
    kurtosis,
    max_drawdown,
    max_drawdown_duration_days,
    periods_per_year_from,
    profit_factor,
    skew,
    sortino,
    turnover_per_year,
    var_quantile,
)
from alpha_factory.validation.regime import (
    classify_market_regime,
    stratify_metrics,
)
from alpha_factory.validation.registry import (
    StaleStatusError,
    check_corrections_diff,
    count_trials,
    log_trial,
    register_run,
    transition_status,
)
from alpha_factory.validation.schema import (
    BACKTEST_LEG_SCHEMA,
    BACKTEST_RESULT_SCHEMA,
    BACKTESTS_CONTRIB_ROOT,
    BACKTESTS_EQUITY_ROOT,
    BACKTESTS_LEG_ROOT,
    BACKTESTS_REGIMES_ROOT,
    BACKTESTS_RESULT_ROOT,
    EQUITY_CURVE_SCHEMA,
    GATE_DSR_LOWER_BOUND,
    GATE_MAX_DD_BOUND,
    GATE_PBO_UPPER_BOUND,
    GATE_REGIME_NONNEG_SHARPE,
    PER_SYMBOL_CONTRIB_SCHEMA,
    REGIME_KIND_ETF,
    REGIME_KIND_TREND,
    REGIME_LABEL_BEAR,
    REGIME_LABEL_BULL,
    REGIME_LABEL_POST_ETF,
    REGIME_LABEL_PRE_ETF,
    REGIME_METRICS_SCHEMA,
    SOURCE_BACKTEST_ENGINE_V1,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_VALIDATED,
    TRIAL_SPLIT_OUT_OF_SAMPLE,
    VERDICT_FAIL,
    VERDICT_PASS,
)

log = logging.getLogger(__name__)

__all__ = [
    "RunReport",
    "run_carry_validation",
]


# ── Public dataclass ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunReport:
    """Output of `run_carry_validation`.

    Fields:
        run_id: UUID v4; matches RUN_REGISTRY.run_id.
        strategy_id: alpha registry id (e.g. "carry_v2").
        verdict: VERDICT_PASS | VERDICT_FAIL.
        metrics: parsed metrics_summary dict (same content as the
            JSON written into RUN_REGISTRY.metrics_summary_json).
            NaN / Inf in raw metrics are replaced with None for
            JSON-canonical encoding.
        regime_metrics: REGIME_METRICS_SCHEMA DataFrame (one row per
            (regime_kind, regime_label) the run window covered;
            empty if curve.height < 2).
        artifacts: CarryArtifacts (legs / contrib / equity_curve /
            n_transitions / params).
    """

    run_id: str
    strategy_id: str
    verdict: str
    metrics: dict[str, Any]
    regime_metrics: pl.DataFrame
    artifacts: CarryArtifacts


# ── Atomic-write helpers ─────────────────────────────────────────────────


def _atomic_write_parquet(df: pl.DataFrame, target: Path) -> None:
    """Write parquet via tmp-then-rename; mirror registry.py pattern."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    df.write_parquet(str(tmp))
    os.replace(tmp, target)


def _write_year_partitioned(
    df: pl.DataFrame,
    root: Path,
    run_id: str,
    strategy_id: str,
    schema: dict[str, pl.DataType],
) -> None:
    """Append `df` to root/year=YYYY/strategy={id}/data.parquet per year.

    Idempotent on `run_id`: existing rows with the same run_id are
    filtered out before append (re-run with same run_id replaces rather
    than duplicates). Other run_ids' rows in the partition pass through.
    """
    if df.height == 0:
        return
    expected_cols = list(schema.keys())
    df = df.select(expected_cols).cast(schema)
    df_with_year = df.with_columns(_year=pl.col("time").dt.year())
    for year_val in df_with_year["_year"].unique().to_list():
        slice_df = (
            df_with_year.filter(pl.col("_year") == year_val).drop("_year")
        )
        path = (
            root / f"year={year_val}" / f"strategy={strategy_id}" / "data.parquet"
        )
        if path.exists():
            existing = pl.read_parquet(str(path))
            existing = existing.filter(pl.col("run_id") != run_id)
            combined = (
                pl.concat([existing, slice_df])
                if existing.height > 0
                else slice_df
            )
        else:
            combined = slice_df
        _atomic_write_parquet(combined.cast(schema), path)


def _write_result_row(
    *,
    run_id: str,
    strategy_id: str,
    variant: str,
    params: CarryParams,
    universe: list[str],
    artifacts: CarryArtifacts,
    archive_state,
) -> None:
    """Append one BACKTEST_RESULT row to result/strategy={id}/data.parquet.

    Idempotent: existing row with the same run_id is dropped before
    append. start_time / end_time are null for empty curves.
    """
    if artifacts.equity_curve.height > 0:
        start_t: datetime | None = artifacts.equity_curve["time"].min()
        end_t: datetime | None = artifacts.equity_curve["time"].max()
        n_bars = artifacts.equity_curve.height
    else:
        start_t = None
        end_t = None
        n_bars = 0

    row = {
        "run_id": run_id,
        "strategy_id": strategy_id,
        "variant": variant,
        "params_json": canonical_params_json(params.as_dict()),
        "params_hash": canonical_params_hash(params.as_dict()),
        "universe_json": json.dumps(universe),
        "start_time": start_t,
        "end_time": end_t,
        "n_bars": n_bars,
        "data_version": archive_state.data_version,
        "archive_max_kline_time": archive_state.archive_max_kline_time,
        "qc_audit_run_ts": archive_state.qc_audit_run_ts,
        "engine_version": SOURCE_BACKTEST_ENGINE_V1,
        "computed_at": datetime.now(UTC),
    }
    new_df = pl.DataFrame([row]).cast(BACKTEST_RESULT_SCHEMA)

    path = BACKTESTS_RESULT_ROOT / f"strategy={strategy_id}" / "data.parquet"
    if path.exists():
        existing = pl.read_parquet(str(path))
        existing = existing.filter(pl.col("run_id") != run_id)
        combined = (
            pl.concat([existing, new_df]) if existing.height > 0 else new_df
        )
    else:
        combined = new_df
    _atomic_write_parquet(combined.cast(BACKTEST_RESULT_SCHEMA), path)


def _write_regimes(
    regime_df: pl.DataFrame, run_id: str, strategy_id: str,
) -> None:
    """Append REGIME_METRICS rows. Idempotent on run_id."""
    if regime_df.height == 0:
        return
    path = (
        BACKTESTS_REGIMES_ROOT
        / f"strategy={strategy_id}"
        / "data.parquet"
    )
    casted = regime_df.cast(REGIME_METRICS_SCHEMA)
    if path.exists():
        existing = pl.read_parquet(str(path))
        existing = existing.filter(pl.col("run_id") != run_id)
        combined = (
            pl.concat([existing, casted]) if existing.height > 0 else casted
        )
    else:
        combined = casted
    _atomic_write_parquet(combined.cast(REGIME_METRICS_SCHEMA), path)


# ── JSON sanitization ────────────────────────────────────────────────────


def _sanitize_for_json(value: Any) -> Any:
    """Recursive coerce a metrics dict to JSON-canonical form.

    NaN / +/-Inf -> None (json.dumps would emit "NaN" / "Infinity"
    which are invalid JSON; downstream `json.loads` accepts them but
    not all consumers do).
    """
    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_json(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ── Metrics computation ──────────────────────────────────────────────────


def _active_mask_for_curve(
    curve: pl.DataFrame, legs: pl.DataFrame,
) -> pl.Series:
    """Bars where any leg has non-zero notional."""
    active_times = (
        legs.filter(pl.col("notional") > 0)["time"].unique()
    )
    # `.implode()` wraps the unique-times Series as a single List-Series so
    # `is_in` is unambiguous (Series-vs-Series was deprecated in polars 1.x
    # since the desired interpretation -- elementwise vs list-membership --
    # is not inferrable from types).
    return curve["time"].is_in(active_times.implode())


def _compute_metrics(
    artifacts: CarryArtifacts,
    daily_volume_per_symbol: dict[str, float],
    periods_per_year: float,
) -> dict[str, Any]:
    """Compute per-run metrics from artifacts. Caller has ensured curve.height >= 2."""
    curve = artifacts.equity_curve
    legs = artifacts.legs
    rets = curve["period_ret_net"]
    equity = curve["equity"]

    active_mask = _active_mask_for_curve(curve, legs)
    return {
        "n_obs": curve.height,
        "sharpe": full_sample_sharpe(curve, periods_per_year=periods_per_year),
        "active_only_sharpe": active_only_sharpe(
            curve, active_mask, periods_per_year=periods_per_year,
        ),
        "sortino": sortino(rets, periods_per_year=periods_per_year),
        "calmar": calmar(curve, periods_per_year=periods_per_year),
        "max_dd": max_drawdown(equity),
        "max_dd_duration_days": max_drawdown_duration_days(curve),
        "hit_rate": hit_rate(rets),
        "profit_factor": profit_factor(rets),
        "skew": skew(rets),
        "kurtosis": kurtosis(rets),
        "var_5pct": var_quantile(rets, q=0.05),
        "cvar_5pct": cvar_quantile(rets, q=0.05),
        "turnover_per_year": turnover_per_year(
            legs, curve, periods_per_year=periods_per_year,
        ),
        "capacity_estimate_usd": capacity_estimate_usd(
            legs, daily_volume_per_symbol,
        ),
        "n_transitions": int(artifacts.n_transitions),
        "periods_per_year": float(periods_per_year),
    }


# ── Verdict gates ────────────────────────────────────────────────────────


def _regime_sharpe(
    regime_metrics: pl.DataFrame, kind: str, label: str,
) -> float | None:
    """Return Sharpe for one (kind, label) regime; None if regime absent in run window."""
    rows = regime_metrics.filter(
        (pl.col("regime_kind") == kind) & (pl.col("regime_label") == label),
    )
    if rows.height == 0:
        return None
    val = rows["sharpe"][0]
    return None if val is None else float(val)


_REGIME_GATES: tuple[tuple[str, str, str], ...] = (
    (REGIME_KIND_TREND, REGIME_LABEL_BULL, "bull"),
    (REGIME_KIND_TREND, REGIME_LABEL_BEAR, "bear"),
    (REGIME_KIND_ETF, REGIME_LABEL_PRE_ETF, "pre_etf"),
    (REGIME_KIND_ETF, REGIME_LABEL_POST_ETF, "post_etf"),
)


def _evaluate_verdict(
    metrics: dict[str, Any],
    regime_metrics: pl.DataFrame,
    dsr_lo: float,
    pbo: float | None,
) -> tuple[str, list[str], list[str]]:
    """Apply gates per PROJECT.md. Returns (verdict, blocking_reasons, warnings).

    Blocking reasons fail the verdict; warnings (absent-regime in the
    run window) are recorded for downstream review but do NOT block
    a pass. See module docstring REGIME COVERAGE.
    """
    blocking: list[str] = []
    warnings: list[str] = []

    if math.isnan(dsr_lo) or not (dsr_lo > GATE_DSR_LOWER_BOUND):
        blocking.append(
            f"dsr_ci_lower={dsr_lo:.4f} not > {GATE_DSR_LOWER_BOUND}",
        )

    if pbo is not None and pbo > GATE_PBO_UPPER_BOUND:
        blocking.append(f"pbo={pbo:.4f} > {GATE_PBO_UPPER_BOUND}")

    max_dd = metrics.get("max_dd")
    if max_dd is None or math.isnan(max_dd) or max_dd < -GATE_MAX_DD_BOUND:
        blocking.append(
            f"max_dd={max_dd} below -{GATE_MAX_DD_BOUND} (or undefined)",
        )

    for kind, label, name in _REGIME_GATES:
        sr = _regime_sharpe(regime_metrics, kind, label)
        if sr is None:
            warnings.append(f"regime_absent:{name}")
            continue
        if math.isnan(sr) or sr < GATE_REGIME_NONNEG_SHARPE:
            blocking.append(
                f"regime_sharpe_negative:{name}={sr:.4f}",
            )

    return (
        VERDICT_PASS if not blocking else VERDICT_FAIL,
        blocking,
        warnings,
    )


# ── Public entry point ───────────────────────────────────────────────────


def run_carry_validation(
    symbol: str,
    variant: str,
    params: CarryParams,
    *,
    klines_df: pl.DataFrame,
    funding_df: pl.DataFrame,
    btc_returns_df: pl.DataFrame,
    daily_volume_per_symbol: dict[str, float],
    fee_tier: str = "regular",
    slippage_model: SlippageModel | None = None,
    trial_split: str = TRIAL_SPLIT_OUT_OF_SAMPLE,
    n_bootstrap: int = 1000,
    pbo_override: float | None = None,
) -> RunReport:
    """Run carry V2 backtest end-to-end and persist L3 validation artifacts.

    Args:
        symbol: canonical "BTC-USDT" form (single-symbol carry V2)
        variant: descriminator stamped into RUN_REGISTRY.variant /
            BACKTEST_RESULT.variant (e.g. "default", "tuned-2026Q1")
        params: CarryParams (defaults match Phase 0 calibration)
        klines_df: pre-loaded L1 klines DataFrame containing rows for
            BOTH (symbol, market='spot') and (symbol, market='perp_usdt')
        funding_df: pre-loaded L1 funding DataFrame for `symbol`
        btc_returns_df: BTC returns for regime classification (long
            format with `time` + `returns` columns; typically loaded
            via `regime.load_btc_returns(start, end)`). Must span
            enough bars for `classify_market_regime` defaults
            (200-bar trend window).
        daily_volume_per_symbol: USDT daily volume per traded symbol;
            consumed by `capacity_estimate_usd`. Caller derives from
            L1 universe snapshot's `quote_volume_24h`.
        fee_tier: passed through to `run_carry_backtest`
        slippage_model: passed through to `run_carry_backtest`;
            None means zero slippage
        trial_split: which TRIAL_SPLIT_* the realized_sharpe is logged
            under. Default OUT_OF_SAMPLE for back-validation runs;
            pass IN_SAMPLE for parameter-sweep runs that should count
            toward search-bias deflation but not toward the OOS ledger.
        n_bootstrap: passed to `deflated_sharpe_bootstrap_ci`
        pbo_override: optional sweep-level PBO computed externally
            (e.g. by a Phase B sweep harness using
            `pbo.enumerate_trials` + `pbo.pbo`). When None (single-run
            mode), the PBO gate is skipped (PBO requires N >= 8 trials
            with comparable returns matrices, which a single-params
            run cannot supply). See module-level "TRIAL_LOG / PBO
            CONVENTIONS" for the planned sweep-harness contract.

    Returns:
        RunReport(run_id, strategy_id, verdict, metrics, regime_metrics,
                  artifacts).

    Side effects:
        Writes to RUN_REGISTRY, BACKTEST_RESULT, BACKTEST_LEG (year-
        partitioned), PER_SYMBOL_CONTRIB (year-partitioned), EQUITY_CURVE
        (year-partitioned), REGIME_METRICS, TRIAL_LOG. All under CWD's
        `data/validation/` and `data/.state/registry/`.

    Failure modes:
        - run_carry_backtest raises -> RUN_REGISTRY transitions
          running -> failed; the original exception propagates.
        - Empty / single-row equity curve -> RUN_REGISTRY transitions
          running -> complete -> validated with verdict=fail and
          reason=insufficient_data. log_trial / DSR / regime
          stratification skipped.
        - Otherwise: full pipeline runs; verdict reflects gates.
    """
    archive_state = get_archive_state()
    run_id = register_run(
        CARRY_STRATEGY_ID,
        variant,
        params.as_dict(),
        [symbol],
        archive_state=archive_state,
    )

    # Backtest + schema-validate + artifact-write all wrapped in a single
    # try/except so any pre-COMPLETE failure transitions to STATUS_FAILED
    # rather than leaving the row stuck at RUNNING (audit critique #1).
    # validate_equity_curve enforcing equity-continuity invariants in
    # particular is a likely failure point and MUST flip status to failed
    # so list_validated_alphas does not silently miss the row.
    try:
        artifacts = run_carry_backtest(
            symbol,
            run_id,
            params,
            klines_df=klines_df,
            funding_df=funding_df,
            fee_tier=fee_tier,
            slippage_model=slippage_model,
        )
        if artifacts.equity_curve.height > 0:
            validate_equity_curve(artifacts.equity_curve)

        # Persist artifacts. Year-partitioned for legs/contrib/equity (long
        # format scaling); single file for result (one row per run_id).
        # Each writer is per-file atomic (tmp + rename), but cross-file
        # atomicity is absent: a crash between writes 1..5 leaves orphan
        # rows tagged with this run_id while the registry still says
        # RUNNING. The except block below transitions to FAILED so a
        # housekeeping sweep can identify orphans by `status=failed`.
        _write_year_partitioned(
            artifacts.legs,
            BACKTESTS_LEG_ROOT,
            run_id,
            CARRY_STRATEGY_ID,
            BACKTEST_LEG_SCHEMA,
        )
        _write_year_partitioned(
            artifacts.contrib,
            BACKTESTS_CONTRIB_ROOT,
            run_id,
            CARRY_STRATEGY_ID,
            PER_SYMBOL_CONTRIB_SCHEMA,
        )
        _write_year_partitioned(
            artifacts.equity_curve,
            BACKTESTS_EQUITY_ROOT,
            run_id,
            CARRY_STRATEGY_ID,
            EQUITY_CURVE_SCHEMA,
        )
        _write_result_row(
            run_id=run_id,
            strategy_id=CARRY_STRATEGY_ID,
            variant=variant,
            params=params,
            universe=[symbol],
            artifacts=artifacts,
            archive_state=archive_state,
        )
    except Exception:
        log.exception(
            "orchestration crashed pre-COMPLETE for run_id=%s symbol=%s",
            run_id, symbol,
        )
        try:
            transition_status(run_id, STATUS_RUNNING, STATUS_FAILED)
        except StaleStatusError:
            log.warning(
                "run_id %s is past RUNNING; cannot mark FAILED", run_id,
            )
        raise

    # Insufficient data: fail-fast path. Status reaches `validated`
    # (validation pipeline completed successfully) with verdict=fail
    # (gates structurally cannot certify on this input). corrections_diff
    # left at CORRECTIONS_UNCHECKED (its register_run default) since
    # there is no validation result to invalidate.
    if artifacts.equity_curve.height < 2:
        log.warning(
            "equity_curve has %d bars (< 2); short-circuiting to "
            "verdict=fail run_id=%s",
            artifacts.equity_curve.height, run_id,
        )
        transition_status(run_id, STATUS_RUNNING, STATUS_COMPLETE)
        blocking_reasons = ["insufficient_data"]
        empty_summary = _sanitize_for_json({
            "n_obs": artifacts.equity_curve.height,
            "verdict_blocking_reasons": blocking_reasons,
            "verdict_warnings": [],
            "verdict_reasons": blocking_reasons,
            "validated_at": datetime.now(UTC).isoformat(),
        })
        transition_status(
            run_id,
            STATUS_COMPLETE,
            STATUS_VALIDATED,
            verdict=VERDICT_FAIL,
            metrics_summary=empty_summary,
        )
        return RunReport(
            run_id=run_id,
            strategy_id=CARRY_STRATEGY_ID,
            verdict=VERDICT_FAIL,
            metrics=empty_summary,
            regime_metrics=pl.DataFrame(schema=REGIME_METRICS_SCHEMA),
            artifacts=artifacts,
        )

    periods_per_year = periods_per_year_from(artifacts.equity_curve)
    metrics = _compute_metrics(
        artifacts, daily_volume_per_symbol, periods_per_year,
    )

    # log_trial first so count_trials reflects current state for DSR.
    # Skip for NaN sharpe (no statistical signal to record). log_trial is
    # idempotent on (strategy_id, params_hash, data_version, split), so
    # re-runs do not double-count toward count_trials.
    #
    # CONVENTION: realized_sharpe in TRIAL_LOG is ANNUALIZED. Sibling
    # alphas porting into this framework MUST follow the same scale; the
    # TRIAL_LOG schema does not carry an explicit scale column and
    # cross-strategy aggregation would silently corrupt comparisons
    # otherwise. DSR internally re-derives per-period SR from raw returns,
    # so this convention does not affect DSR computation.
    sharpe_for_log = metrics["sharpe"]
    if not (isinstance(sharpe_for_log, float) and math.isnan(sharpe_for_log)):
        log_trial(
            CARRY_STRATEGY_ID,
            params.as_dict(),
            evaluated_at=datetime.now(UTC),
            data_version=archive_state.data_version,
            split=trial_split,
            realized_sharpe=float(sharpe_for_log),
            n_obs=int(metrics["n_obs"]),
        )

    # Regime stratification. classify_market_regime runs the trend and
    # vol-tercile passes on the supplied BTC returns; stratify_metrics
    # inner-joins onto the equity curve, so absent regimes simply yield
    # no row -> verdict gate treats as `regime_absent` warning.
    regimes = classify_market_regime(btc_returns_df)
    regime_metrics = stratify_metrics(
        artifacts.equity_curve, regimes, periods_per_year=periods_per_year,
    )
    _write_regimes(regime_metrics, run_id, CARRY_STRATEGY_ID)

    # DSR. n_trials reflects unique parameter combinations explored to
    # date for this strategy_id (current trial included via log_trial
    # above). Bootstrap CI with deterministic RNG salted by run_id +
    # SALT_DSR_BOOTSTRAP for reproducibility.
    n_trials = max(count_trials(CARRY_STRATEGY_ID), 1)
    dsr_rng = make_rng(run_id, SALT_DSR_BOOTSTRAP)
    dsr_point, dsr_lo, dsr_hi = deflated_sharpe_bootstrap_ci(
        artifacts.equity_curve["period_ret_net"],
        n_trials=n_trials,
        rng=dsr_rng,
        n_bootstrap=n_bootstrap,
    )
    metrics["dsr"] = dsr_point
    metrics["dsr_ci_lower"] = dsr_lo
    metrics["dsr_ci_upper"] = dsr_hi
    metrics["n_trials"] = n_trials

    # PBO is N/A for single-params orchestration runs (PBO requires
    # >= 8 trials with comparable returns matrices). The `pbo_override`
    # kwarg lets a Phase B parameter-sweep harness pass through a
    # pre-computed sweep-level PBO so the gate is enforced even though
    # the runner sees one params at a time. Single-run callers leave
    # it None.
    metrics["pbo"] = pbo_override

    # Lifecycle: running -> complete (backtest + metrics done).
    transition_status(run_id, STATUS_RUNNING, STATUS_COMPLETE)

    # corrections_diff: compare run.data_version vs current archive's
    # data_version. CLEAN means archive unchanged since `register_run`;
    # DIRTY means a correction landed during/after the run.
    corrections_status = check_corrections_diff(run_id)

    # Verdict from gates. Blocking reasons fail the verdict; warnings
    # (regime_absent for run windows that structurally cannot cover a
    # regime) are recorded but don't block. `verdict_reasons` is the
    # union (for grep-friendly audit); the split fields let downstream
    # consumers reconstruct what BLOCKED vs what was merely flagged.
    verdict, blocking_reasons, warnings = _evaluate_verdict(
        metrics, regime_metrics, dsr_lo, metrics["pbo"],
    )
    metrics["verdict_blocking_reasons"] = blocking_reasons
    metrics["verdict_warnings"] = warnings
    metrics["verdict_reasons"] = blocking_reasons + warnings
    metrics["validated_at"] = datetime.now(UTC).isoformat()
    metrics["corrections_diff_status"] = corrections_status

    # No `corrections_diff_status=` argument here: `check_corrections_diff`
    # above already wrote the value atomically under the registry lock,
    # making it the single writer for that field. Passing it again would
    # double-rewrite the entire registry parquet.
    summary = _sanitize_for_json(metrics)
    transition_status(
        run_id,
        STATUS_COMPLETE,
        STATUS_VALIDATED,
        verdict=verdict,
        metrics_summary=summary,
    )

    return RunReport(
        run_id=run_id,
        strategy_id=CARRY_STRATEGY_ID,
        verdict=verdict,
        metrics=summary,
        regime_metrics=regime_metrics,
        artifacts=artifacts,
    )
