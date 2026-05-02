"""Tests for src/alpha_factory/runner.py.

Synthesize klines + funding + btc_returns_df for end-to-end orchestration
tests. Each test runs in an isolated tmp_path CWD so registry state and
artifact parquets do not leak between tests or pollute the real
`data/` tree.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest

from alpha_factory.alpha.carry import (
    STRATEGY_ID as CARRY_STRATEGY_ID,
)
from alpha_factory.alpha.carry import (
    CarryParams,
)
from alpha_factory.data.schema import FUNDING_SCHEMA, KLINES_SCHEMA, TZ_UTC
from alpha_factory.runner import RunReport, run_carry_validation
from alpha_factory.validation.registry import count_trials, get_run
from alpha_factory.validation.schema import (
    BACKTEST_RESULT_SCHEMA,
    BACKTESTS_CONTRIB_ROOT,
    BACKTESTS_EQUITY_ROOT,
    BACKTESTS_LEG_ROOT,
    BACKTESTS_REGIMES_ROOT,
    BACKTESTS_RESULT_ROOT,
    CORRECTIONS_CLEAN,
    RUN_REGISTRY_PATH,
    STATUS_FAILED,
    STATUS_VALIDATED,
    VERDICT_FAIL,
    VERDICT_PASS,
)

# ── Synthetic data builders ──────────────────────────────────────────────


_SYMBOL = "BTC-USDT"


def _make_klines(
    market: str, times: list[datetime], closes: list[float],
) -> pl.DataFrame:
    """Minimal KLINES_SCHEMA-conforming frame."""
    n = len(times)
    return pl.DataFrame({
        "symbol": [_SYMBOL] * n,
        "market": [market] * n,
        "open_time": times,
        "close_time": times,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [0.0] * n,
        "quote_volume": [0.0] * n,
        "trades": [0] * n,
        "taker_buy_base": [0.0] * n,
        "taker_buy_quote": [0.0] * n,
        "ingested_at": [datetime(2026, 1, 1, tzinfo=UTC)] * n,
        "source": ["test"] * n,
    }).cast(KLINES_SCHEMA)


def _make_funding(
    times: list[datetime], rates: list[float],
) -> pl.DataFrame:
    n = len(times)
    return pl.DataFrame({
        "symbol": [_SYMBOL] * n,
        "funding_time": times,
        "funding_rate": rates,
        "mark_price": [50000.0] * n,
        "ingested_at": [datetime(2026, 1, 1, tzinfo=UTC)] * n,
        "source": ["test"] * n,
    }).cast(FUNDING_SCHEMA)


def _hourly_times(start: datetime, n_hours: int) -> list[datetime]:
    return [start + timedelta(hours=h) for h in range(n_hours)]


def _settlement_times(times: list[datetime]) -> list[datetime]:
    return [t for t in times if t.hour % 8 == 0]


def _build_carry_inputs(
    n_hours: int = 336,
    funding_rate: float | list[float] = 0.0002,
    seed: int = 42,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Build (klines_df, funding_df, btc_returns_df) for a carry V2 run.

    Random-walk spot/perp prices (so vol-tercile classifier has variance);
    settlement-cadence funding at given rate (constant or per-settlement
    list). btc_returns_df uses the spot pct_change (drops first bar per
    pct_change convention).
    """
    times = _hourly_times(datetime(2026, 1, 1, tzinfo=UTC), n_hours)
    rng = np.random.default_rng(seed)
    spot_rets = rng.normal(0, 0.001, n_hours - 1)
    spot_prices = 50_000.0 * np.cumprod(
        np.concatenate([[1.0], 1.0 + spot_rets]),
    )
    perp_prices = spot_prices.copy()  # zero basis for the test

    spot_klines = _make_klines("spot", times, spot_prices.tolist())
    perp_klines = _make_klines("perp_usdt", times, perp_prices.tolist())
    klines_df = pl.concat([spot_klines, perp_klines])

    settle_times = _settlement_times(times)
    if isinstance(funding_rate, (int, float)):
        rates = [float(funding_rate)] * len(settle_times)
    else:
        rates = list(funding_rate)
    funding_df = _make_funding(settle_times, rates)

    btc_returns = pl.DataFrame({
        "time": times[1:],
        "returns": spot_rets.tolist(),
    }).with_columns(
        time=pl.col("time").cast(pl.Datetime("us", time_zone=TZ_UTC)),
    )

    return klines_df, funding_df, btc_returns


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_cwd(tmp_path, monkeypatch):
    """Chdir into tmp_path so all parquet writes land in an isolated tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── Tests ────────────────────────────────────────────────────────────────


def test_round_trip_writes_all_artifacts(tmp_cwd):
    """End-to-end: backtest -> all artifacts on disk -> RUN_REGISTRY validated."""
    klines_df, funding_df, btc_returns = _build_carry_inputs(
        n_hours=336, funding_rate=0.0002,
    )

    report = run_carry_validation(
        _SYMBOL,
        "default",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,  # cheap for test
    )

    assert isinstance(report, RunReport)
    assert report.strategy_id == CARRY_STRATEGY_ID
    assert report.verdict in (VERDICT_PASS, VERDICT_FAIL)

    # RUN_REGISTRY has the row with status=validated + metrics_summary_json
    row = get_run(report.run_id)
    assert row is not None, f"run_id {report.run_id} not found in registry"
    assert row["status"] == STATUS_VALIDATED
    assert row["verdict"] in (VERDICT_PASS, VERDICT_FAIL)
    assert row["metrics_summary_json"] is not None

    # metrics_summary_json round-trips: the canonical keys are all present
    summary = json.loads(row["metrics_summary_json"])
    canonical_keys = {
        "sharpe", "max_dd", "n_obs", "n_trials", "dsr",
        "dsr_ci_lower", "dsr_ci_upper", "verdict_reasons",
        "verdict_blocking_reasons", "verdict_warnings",
        "validated_at", "corrections_diff_status",
        "active_only_sharpe", "sortino", "calmar",
        "max_dd_duration_days", "hit_rate", "profit_factor",
        "skew", "kurtosis", "var_5pct", "cvar_5pct",
        "turnover_per_year", "capacity_estimate_usd",
        "n_transitions", "periods_per_year", "pbo",
    }
    missing = canonical_keys - set(summary.keys())
    assert not missing, f"metrics_summary missing keys: {missing}"
    # Split invariant: verdict_reasons is the union of warnings + blocking.
    assert (
        set(summary["verdict_reasons"])
        == set(summary["verdict_blocking_reasons"])
        | set(summary["verdict_warnings"])
    )

    # log_trial fired so count_trials >= 1 (assuming sharpe wasn't NaN)
    assert count_trials(CARRY_STRATEGY_ID) >= 1

    # Year-partitioned artifacts on disk
    leg_files = list(BACKTESTS_LEG_ROOT.glob(
        f"year=*/strategy={CARRY_STRATEGY_ID}/data.parquet",
    ))
    assert leg_files, "BACKTEST_LEG parquet not on disk"
    contrib_files = list(BACKTESTS_CONTRIB_ROOT.glob(
        f"year=*/strategy={CARRY_STRATEGY_ID}/data.parquet",
    ))
    assert contrib_files
    equity_files = list(BACKTESTS_EQUITY_ROOT.glob(
        f"year=*/strategy={CARRY_STRATEGY_ID}/data.parquet",
    ))
    assert equity_files

    # BACKTEST_RESULT row written
    result_path = (
        BACKTESTS_RESULT_ROOT
        / f"strategy={CARRY_STRATEGY_ID}"
        / "data.parquet"
    )
    assert result_path.exists()
    result_df = pl.read_parquet(str(result_path))
    assert result_df.height == 1
    assert result_df["run_id"][0] == report.run_id
    assert result_df.schema == pl.Schema(BACKTEST_RESULT_SCHEMA)
    assert result_df["data_version"][0] is not None

    # REGIME_METRICS written iff at least one regime row was emitted
    if report.regime_metrics.height > 0:
        regime_path = (
            BACKTESTS_REGIMES_ROOT
            / f"strategy={CARRY_STRATEGY_ID}"
            / "data.parquet"
        )
        assert regime_path.exists()


def test_short_curve_fails_with_insufficient_data(tmp_cwd):
    """1-bar curve -> verdict=fail with reason=insufficient_data."""
    times = [datetime(2026, 1, 1, tzinfo=UTC)]
    spot = _make_klines("spot", times, [50_000.0])
    perp = _make_klines("perp_usdt", times, [50_000.0])
    klines_df = pl.concat([spot, perp])
    funding_df = _make_funding(times, [0.0001])

    btc_returns = pl.DataFrame({
        "time": [datetime(2026, 1, 1, tzinfo=UTC)],
        "returns": [0.0],
    }).with_columns(
        time=pl.col("time").cast(pl.Datetime("us", time_zone=TZ_UTC)),
    )

    report = run_carry_validation(
        _SYMBOL,
        "short",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
    )

    assert report.verdict == VERDICT_FAIL
    assert "insufficient_data" in report.metrics["verdict_reasons"]

    # Registry consistent: status=validated, verdict=fail
    row = get_run(report.run_id)
    assert row["status"] == STATUS_VALIDATED
    assert row["verdict"] == VERDICT_FAIL


def test_backtest_exception_transitions_to_failed(tmp_cwd):
    """run_carry_backtest raising -> RUN_REGISTRY.status=failed; exception propagates."""
    klines_df, funding_df, btc_returns = _build_carry_inputs()

    with patch(
        "alpha_factory.runner.run_carry_backtest",
        side_effect=RuntimeError("simulated backtest failure"),
    ), pytest.raises(RuntimeError, match="simulated"):
        run_carry_validation(
                _SYMBOL,
                "exception",
                CarryParams(),
                klines_df=klines_df,
                funding_df=funding_df,
                btc_returns_df=btc_returns,
                daily_volume_per_symbol={_SYMBOL: 1e9},
            )

    # The run was registered before the crash so RUN_REGISTRY has one
    # row with status=failed.
    runs = pl.read_parquet(str(RUN_REGISTRY_PATH))
    failed = runs.filter(pl.col("status") == STATUS_FAILED)
    assert failed.height == 1
    assert failed["strategy_id"][0] == CARRY_STRATEGY_ID
    assert failed["metrics_summary_json"][0] is None


def test_replay_idempotent_for_count_trials(tmp_cwd):
    """Same params + same archive_state run twice -> count_trials = 1."""
    klines_df, funding_df, btc_returns = _build_carry_inputs()

    for _ in range(2):
        run_carry_validation(
            _SYMBOL,
            "default",
            CarryParams(),
            klines_df=klines_df,
            funding_df=funding_df,
            btc_returns_df=btc_returns,
            daily_volume_per_symbol={_SYMBOL: 1e9},
            n_bootstrap=50,
        )

    # Idempotent on (strategy_id, params_hash, data_version, split)
    assert count_trials(CARRY_STRATEGY_ID) == 1


def test_corrections_diff_marked_clean_on_stable_archive(tmp_cwd):
    """Single run on stable archive -> corrections_diff_status=clean."""
    klines_df, funding_df, btc_returns = _build_carry_inputs()
    report = run_carry_validation(
        _SYMBOL,
        "clean",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,
    )
    row = get_run(report.run_id)
    assert row["corrections_diff_status"] == CORRECTIONS_CLEAN


def test_post_etf_only_window_records_pre_etf_absent(tmp_cwd):
    """Backtest starting 2026 -> pre_etf absent; verdict_reasons records but doesn't auto-fail.

    Confirms the critique-#4 fix: missing regime row -> warning, not
    blocking failure. (Whether the run ultimately passes depends on
    other gates, but absent-regime alone must not be a blocker.)
    """
    klines_df, funding_df, btc_returns = _build_carry_inputs(
        n_hours=336, funding_rate=0.0002,
    )
    report = run_carry_validation(
        _SYMBOL,
        "post-etf-only",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,
    )

    reasons = report.metrics["verdict_reasons"]
    # 2026-01-* is post-ETF -> pre_etf rows are structurally absent
    assert "regime_absent:pre_etf" in reasons, (
        f"expected pre_etf absent record; got {reasons}"
    )
    # Any actual blocking failure (e.g. dsr/regime-negative) is a
    # SEPARATE entry; absent-regime alone must not blocked a pass.
    blocking = [r for r in reasons if not r.startswith("regime_absent:")]
    if not blocking:
        assert report.verdict == VERDICT_PASS, (
            f"no blocking reasons but verdict={report.verdict}; reasons={reasons}"
        )


def test_pbo_override_enforces_pbo_gate(tmp_cwd):
    """pbo_override=0.7 (> GATE_PBO_UPPER_BOUND=0.5) -> blocking_reasons records PBO failure.

    Confirms the audit fix #6 wiring: a Phase B sweep harness can pass
    pre-computed sweep-level PBO into the per-run runner, and the gate
    fires accordingly (single-run runner has no way to compute PBO
    itself since CSCV requires N >= 8 trials).
    """
    klines_df, funding_df, btc_returns = _build_carry_inputs()
    report = run_carry_validation(
        _SYMBOL,
        "pbo-fail",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,
        pbo_override=0.7,
    )

    blocking = report.metrics["verdict_blocking_reasons"]
    assert any("pbo=" in r for r in blocking), (
        f"expected pbo blocking reason; got {blocking}"
    )
    assert report.verdict == VERDICT_FAIL
    # PBO recorded in metrics_summary
    assert report.metrics["pbo"] == 0.7


def test_corrections_diff_single_writer_no_double_write(tmp_cwd):
    """The validated transition does not re-write corrections_diff_status.

    Audit fix #5: check_corrections_diff is the sole writer; passing
    corrections_diff_status=... to the validated transition would
    double-rewrite the registry parquet. We assert the value still
    persists correctly (semantics preserved) and is also surfaced in
    metrics_summary for downstream consumers.
    """
    klines_df, funding_df, btc_returns = _build_carry_inputs()
    report = run_carry_validation(
        _SYMBOL,
        "corr-single-writer",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,
    )
    row = get_run(report.run_id)
    # check_corrections_diff sets the value pre-transition; it persists.
    assert row["corrections_diff_status"] == CORRECTIONS_CLEAN
    # And it's also in metrics_summary for downstream JSON consumers.
    assert report.metrics["corrections_diff_status"] == CORRECTIONS_CLEAN


def test_metrics_summary_json_no_nan_inf(tmp_cwd):
    """metrics_summary_json must contain only JSON-canonical primitives.

    NaN / Inf values from metrics are sanitized to None before JSON
    encoding; the resulting JSON is a flat dict suitable for
    list_validated_alphas filtering and downstream HRP consumption.
    """
    klines_df, funding_df, btc_returns = _build_carry_inputs()
    report = run_carry_validation(
        _SYMBOL,
        "json",
        CarryParams(),
        klines_df=klines_df,
        funding_df=funding_df,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={_SYMBOL: 1e9},
        n_bootstrap=50,
    )

    row = get_run(report.run_id)
    raw = row["metrics_summary_json"]
    # No literal "NaN" / "Infinity" tokens (Python's json.dumps emits
    # these for non-finite floats; we sanitize to null).
    assert "NaN" not in raw
    assert "Infinity" not in raw

    # Round-trip: the JSON parses back, and recovered dict matches
    # report.metrics for the canonical keys.
    parsed = json.loads(raw)
    for key in ("sharpe", "max_dd", "n_obs", "n_trials", "dsr"):
        if key in report.metrics and report.metrics[key] is not None:
            assert parsed[key] == report.metrics[key], (
                f"key {key!r} drift: parsed={parsed[key]} "
                f"vs report={report.metrics[key]}"
            )
