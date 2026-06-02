"""Tests for src/alpha_factory/execution/reconcile.py (Phase C stage [5]).

Covers PnL replay from fill events, the skeleton A≡B identity (so
tracking_error is structurally 0), UTC-day event windowing, event-kind
counts, the deferred-column NULL contract (C / data_correction_effect /
Sharpe — the last being a CLAUDE.md red line), and parquet append/dedup.
The end-to-end test runs stage [2] adapter -> stage [4] fill sim ->
stage [5] reconcile so the walking-skeleton claim is empirically closed
for the inner loop.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID as CARRY_V3_ID,
)
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
)
from alpha_factory.execution.event_log import (
    append_event,
    make_event,
)
from alpha_factory.execution.fill_sim import (
    latest_close_by_market,
    step_paper_fill_sim,
)
from alpha_factory.execution.reconcile import (
    RECONCILE_SCHEMA,
    DailyReconcile,
    compute_daily_reconcile,
    replay_pnl_from_fill_events,
    write_daily_reconcile,
)
from alpha_factory.execution.strategy import (
    CarryV3Adapter,
    RollingWindow,
    TargetLeg,
    TargetPosition,
)

CYCLE_DATE = date(2026, 1, 1)
SETTLE_STEP = timedelta(hours=8)
KLINE_STEP = timedelta(minutes=1)
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _fill_event(
    *, symbol: str, market: str, side: str, quantity_base: float,
    fill_price: float, notional_quote: float, fee_bp: float = 7.5,
    ts: datetime = ANCHOR,
) -> dict:
    ev = make_event(
        "fill_simulated", strategy_id=CARRY_V3_ID, symbol=symbol,
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
        data={
            "symbol": symbol, "market": market, "side": side,
            "quantity_base": quantity_base, "fill_price": fill_price,
            "notional_quote": notional_quote,
            "fee_paid_quote": notional_quote * fee_bp / 10_000.0,
            "fee_bp": fee_bp,
            "target_delta_notional": (
                notional_quote if side == "buy" else -notional_quote
            ),
        },
    )
    # Override the auto ts with a deterministic one for window tests.
    ev["ts"] = ts.isoformat().replace("+00:00", "Z")
    return ev


def _market_klines(
    symbol: str, n_bars: int, *, market: str, close_base: float,
) -> pl.DataFrame:
    times = [ANCHOR + KLINE_STEP * i for i in range(n_bars)]
    return pl.DataFrame({
        "symbol": [symbol] * n_bars,
        "market": [market] * n_bars,
        "open_time": times,
        "close": [close_base + i * 0.01 for i in range(n_bars)],
    }).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )


def _klines_both(
    symbol: str = "BTC-USDT", n: int = 10,
    *, spot: float = 100_000.0, perp: float = 100_010.0,
) -> pl.DataFrame:
    return pl.concat([
        _market_klines(symbol, n, market="spot", close_base=spot),
        _market_klines(symbol, n, market="perp_usdt", close_base=perp),
    ], how="vertical")


def _funding_active(n: int = 130) -> pl.DataFrame:
    times = [ANCHOR + SETTLE_STEP * i for i in range(n)]
    return pl.DataFrame({
        "open_time": times, "funding_rate": [0.0001] * n,
    }).with_columns(pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")))


# ── replay_pnl_from_fill_events ───────────────────────────────────────────


def test_replay_pnl_empty_is_zero():
    assert replay_pnl_from_fill_events([], {}) == 0.0


def test_replay_pnl_marks_long_position_gain():
    """Long 0.01 BTC @ 100k, mark 110k -> +100 gross, minus 7.5bp*1000 fee."""
    events = [_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
    )]
    marks = {("BTC-USDT", "spot"): 110_000.0}
    pnl = replay_pnl_from_fill_events(events, marks)
    # gross = 0.01*(110000-100000)=100 ; fee = 1000*7.5/10000 = 0.75
    assert pnl == pytest.approx(100.0 - 0.75)


def test_replay_pnl_short_position_profits_when_mark_falls():
    events = [_fill_event(
        symbol="BTC-USDT", market="perp_usdt", side="sell",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
        fee_bp=5.0,
    )]
    marks = {("BTC-USDT", "perp_usdt"): 90_000.0}
    pnl = replay_pnl_from_fill_events(events, marks)
    # short qty=-0.01; gross = -0.01*(90000-100000)= +100 ; fee = 1000*5/10000=0.5
    assert pnl == pytest.approx(100.0 - 0.5)


def test_replay_pnl_delta_neutral_pair_marks_to_minus_fees_at_entry():
    """Spot long + perp short at entry price -> gross MTM 0 -> pnl = -fees."""
    events = [
        _fill_event(
            symbol="BTC-USDT", market="spot", side="buy",
            quantity_base=0.005, fill_price=100_000.0, notional_quote=500.0,
        ),
        _fill_event(
            symbol="BTC-USDT", market="perp_usdt", side="sell",
            quantity_base=0.005, fill_price=100_010.0, notional_quote=500.0,
            fee_bp=5.0,
        ),
    ]
    marks = {
        ("BTC-USDT", "spot"): 100_000.0,
        ("BTC-USDT", "perp_usdt"): 100_010.0,
    }
    pnl = replay_pnl_from_fill_events(events, marks)
    # gross 0 ; fees = 500*7.5/10000 + 500*5/10000 = 0.375 + 0.25 = 0.625
    assert pnl == pytest.approx(-0.625)


def test_replay_pnl_missing_mark_excludes_position_with_warn(caplog):
    events = [_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
    )]
    with caplog.at_level("WARNING"):
        pnl = replay_pnl_from_fill_events(events, {})   # no marks
    # gross excluded, only the fee remains
    assert pnl == pytest.approx(-0.75)
    assert "no mark price" in caplog.text


# ── UTC-day windowing ─────────────────────────────────────────────────────


def test_compute_reconcile_filters_to_utc_day(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    # In-window fill (Jan 1) + out-of-window fill (Jan 2).
    append_event(_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
        ts=datetime(2026, 1, 1, 8, tzinfo=UTC),
    ), path=log)
    append_event(_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
        ts=datetime(2026, 1, 2, 8, tzinfo=UTC),
    ), path=log)
    row = compute_daily_reconcile(
        CYCLE_DATE, {("BTC-USDT", "spot"): 100_000.0}, event_log_path=log,
    )
    assert row.n_simulated_fills_today == 1   # only the Jan-1 fill counted


def test_compute_reconcile_excludes_day_boundary_next_midnight(tmp_path: Path):
    """Event at exactly cycle_date+1 00:00 belongs to the NEXT day."""
    log = tmp_path / "events.jsonl"
    append_event(_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
        ts=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    ), path=log)
    row = compute_daily_reconcile(
        CYCLE_DATE, {("BTC-USDT", "spot"): 100_000.0}, event_log_path=log,
    )
    assert row.n_simulated_fills_today == 0


# ── A ≡ B + tracking_error ────────────────────────────────────────────────


def test_compute_reconcile_a_equals_b_tracking_error_zero(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    append_event(_fill_event(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.01, fill_price=100_000.0, notional_quote=1_000.0,
        ts=datetime(2026, 1, 1, 8, tzinfo=UTC),
    ), path=log)
    row = compute_daily_reconcile(
        CYCLE_DATE, {("BTC-USDT", "spot"): 110_000.0}, event_log_path=log,
    )
    assert row.realized_pnl_quote_24h == row.replay_event_log_pnl_24h
    assert row.tracking_error == pytest.approx(0.0)


def test_compute_reconcile_tracking_error_none_on_zero_pnl_day(tmp_path: Path):
    """No fills -> B == 0 -> tracking_error is NULL, not a 0/0 division."""
    log = tmp_path / "events.jsonl"
    append_event(make_event(
        "system_start", strategy_id=CARRY_V3_ID, symbol="BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
    ), path=log)
    row = compute_daily_reconcile(CYCLE_DATE, {}, event_log_path=log)
    assert row.replay_event_log_pnl_24h == 0.0
    assert row.tracking_error is None


# ── Deferred-column NULL contract ─────────────────────────────────────────


def test_compute_reconcile_c_and_correction_null_at_skeleton(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    row = compute_daily_reconcile(CYCLE_DATE, {}, event_log_path=log)
    assert row.replay_l1_current_pnl_24h is None
    assert row.data_correction_effect is None


def test_compute_reconcile_sharpe_columns_null_red_line(tmp_path: Path):
    """CLAUDE.md red line: no Sharpe outside strategy-validation."""
    log = tmp_path / "events.jsonl"
    row = compute_daily_reconcile(CYCLE_DATE, {}, event_log_path=log)
    assert row.realized_sharpe_to_date is None
    assert row.replay_sharpe_to_date is None


def test_compute_reconcile_notes_document_skeleton_constraint(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    row = compute_daily_reconcile(CYCLE_DATE, {}, event_log_path=log)
    assert "A=B by construction" in row.notes


def test_compute_reconcile_appends_caller_notes(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    row = compute_daily_reconcile(
        CYCLE_DATE, {}, event_log_path=log, notes="manual recheck",
    )
    assert "manual recheck" in row.notes
    assert "A=B by construction" in row.notes


# ── Event-kind counts ─────────────────────────────────────────────────────


def test_compute_reconcile_counts_each_kind(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    for kind in ("signal_compute", "fill_simulated", "fill_simulated",
                 "halt_action_fired", "data_version_drift_detected"):
        ev = make_event(
            kind, strategy_id=CARRY_V3_ID, symbol="BTC-USDT",
            data_version="x", git_commit_hash="y",
            process_clock_drift_vs_binance_ms=None,
            data={
                "symbol": "BTC-USDT", "market": "spot", "side": "buy",
                "quantity_base": 0.0, "fill_price": 1.0,
                "notional_quote": 0.0, "fee_paid_quote": 0.0, "fee_bp": 7.5,
                "target_delta_notional": 0.0,
            } if kind == "fill_simulated" else {},
        )
        ev["ts"] = datetime(2026, 1, 1, 8, tzinfo=UTC).isoformat().replace(
            "+00:00", "Z",
        )
        append_event(ev, path=log)
    row = compute_daily_reconcile(CYCLE_DATE, {}, event_log_path=log)
    assert row.n_signals_today == 1
    assert row.n_simulated_fills_today == 2
    assert row.n_halts_today == 1
    assert row.n_data_version_drifts == 1


# ── write_daily_reconcile ─────────────────────────────────────────────────


def _row(d: date, *, a: float = -0.6) -> DailyReconcile:
    return DailyReconcile(
        date=d, realized_pnl_quote_24h=a, replay_event_log_pnl_24h=a,
        replay_l1_current_pnl_24h=None, tracking_error=0.0,
        data_correction_effect=None, realized_sharpe_to_date=None,
        replay_sharpe_to_date=None, n_signals_today=1,
        n_simulated_fills_today=2, n_halts_today=0, n_data_version_drifts=0,
        notes="t",
    )


def test_write_creates_new_parquet(tmp_path: Path):
    target = tmp_path / "daily_reconcile.parquet"
    write_daily_reconcile(_row(date(2026, 1, 1)), target=target)
    assert target.exists()
    df = pl.read_parquet(target)
    assert df.height == 1
    assert df.schema == RECONCILE_SCHEMA


def test_write_appends_second_day(tmp_path: Path):
    target = tmp_path / "daily_reconcile.parquet"
    write_daily_reconcile(_row(date(2026, 1, 1)), target=target)
    write_daily_reconcile(_row(date(2026, 1, 2)), target=target)
    df = pl.read_parquet(target)
    assert df.height == 2
    assert df["date"].to_list() == [date(2026, 1, 1), date(2026, 1, 2)]


def test_write_rerun_same_date_dedups_keep_last(tmp_path: Path):
    target = tmp_path / "daily_reconcile.parquet"
    write_daily_reconcile(_row(date(2026, 1, 1), a=-0.6), target=target)
    write_daily_reconcile(_row(date(2026, 1, 1), a=-0.9), target=target)
    df = pl.read_parquet(target)
    assert df.height == 1   # deduped by date
    assert df["realized_pnl_quote_24h"][0] == pytest.approx(-0.9)  # last wins


def test_write_no_tmp_files_after_success(tmp_path: Path):
    target = tmp_path / "daily_reconcile.parquet"
    write_daily_reconcile(_row(date(2026, 1, 1)), target=target)
    assert not list(tmp_path.glob("*.tmp.*"))


# ── End-to-end: stage [2] -> [4] -> [5] ──────────────────────────────────


def test_skeleton_stages_2_4_5_compose(tmp_path: Path):
    """Run adapter -> fill sim -> reconcile and verify a coherent row.

    Positive funding -> state 1 -> 2 fills booked. Marking at the same
    last-close the fills used -> gross MTM 0 -> PnL = -fees (negative),
    tracking_error 0 (A≡B), and the reconcile parquet lands.
    """
    log = tmp_path / "events.jsonl"
    recon = tmp_path / "daily_reconcile.parquet"
    klines = _klines_both(spot=100_000.0, perp=100_010.0)
    window = RollingWindow(funding=_funding_active(130), klines=klines)

    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    target = adapter.compute_target_position(window)
    step_paper_fill_sim(
        target, window,
        data_version="2026-01-01T00:00+nocorr", git_commit_hash="deadbee",
        clock_drift_ms=10.0, event_log_path=log,
    )

    # Mark at the same last close the fills used -> gross MTM 0.
    prices = latest_close_by_market(klines, "BTC-USDT")
    marks = {("BTC-USDT", m): p for m, p in prices.items()}
    # step_paper_fill_sim stamps real wall-clock ts via make_event, so the
    # reconcile day is "today" (the realistic cron call), not the synthetic
    # ANCHOR used by the ts-overriding fixtures above.
    today = datetime.now(tz=UTC).date()
    row = compute_daily_reconcile(today, marks, event_log_path=log)

    assert row.n_simulated_fills_today == 2
    assert row.realized_pnl_quote_24h < 0          # only fees this skeleton cycle
    assert row.realized_pnl_quote_24h == row.replay_event_log_pnl_24h
    assert row.tracking_error == pytest.approx(0.0)
    assert row.replay_l1_current_pnl_24h is None
    assert row.realized_sharpe_to_date is None

    write_daily_reconcile(row, target=recon)
    assert recon.exists()
    assert pl.read_parquet(recon).height == 1
    assert row.date == today


def test_compute_reconcile_target_position_import_smoke():
    """Guard: TargetPosition/TargetLeg stay import-compatible for cron use."""
    tp = TargetPosition(
        strategy_id=CARRY_V3_ID, as_of=ANCHOR,
        legs=(TargetLeg("BTC-USDT", "spot", 1.0),), inputs_hash="x" * 64,
    )
    assert tp.legs[0].market == "spot"
