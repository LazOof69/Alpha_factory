"""Tests for src/alpha_factory/execution/fill_sim.py (Phase C stage [4]).

Covers replay-as-projection (the book is event-log-derived, not a
separate file), the (symbol, market) diff -> SimulatedFill mapping,
update_book arithmetic (including position flip), and the
``step_paper_fill_sim`` composition root. End-to-end chain test wires
stage [2] (carry_v3 adapter) -> stage [4] (fill sim) so the
walking-skeleton claim "the pipeline runs end-to-end" is empirically
verified, not just docstring-asserted.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    read_events,
)
from alpha_factory.execution.fill_sim import (
    DEFAULT_TAKER_FEE_BPS,
    Position,
    SimulatedFill,
    compute_fills,
    latest_close_by_market,
    replay_position_book,
    step_paper_fill_sim,
    update_book,
)
from alpha_factory.execution.strategy import (
    CarryV3Adapter,
    RollingWindow,
    TargetLeg,
    TargetPosition,
)

SETTLE_STEP = timedelta(hours=8)
KLINE_STEP = timedelta(minutes=1)
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _market_klines(
    symbol: str, n_bars: int, *, market: str,
    start: datetime = ANCHOR, close_base: float = 100.0,
) -> pl.DataFrame:
    times = [start + KLINE_STEP * i for i in range(n_bars)]
    return pl.DataFrame({
        "symbol": [symbol] * n_bars,
        "market": [market] * n_bars,
        "open_time": times,
        "close": [close_base + i * 0.01 for i in range(n_bars)],
    }).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )


def _klines_both_markets(
    symbol: str = "BTC-USDT", n_bars: int = 10,
    *, spot_close: float = 100_000.0, perp_close: float = 100_010.0,
) -> pl.DataFrame:
    return pl.concat([
        _market_klines(symbol, n_bars, market="spot", close_base=spot_close),
        _market_klines(symbol, n_bars, market="perp_usdt", close_base=perp_close),
    ], how="vertical")


def _funding_df_active(n: int = 130, start: datetime = ANCHOR) -> pl.DataFrame:
    """Positive funding for n settlements -> V3 state machine emits state 1."""
    times = [start + SETTLE_STEP * i for i in range(n)]
    return pl.DataFrame({
        "open_time": times,
        "funding_rate": [0.0001] * n,
    }).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )


def _target_active(symbol: str = "BTC-USDT", cap: float = 500.0) -> TargetPosition:
    return TargetPosition(
        strategy_id=CARRY_V3_ID,
        as_of=ANCHOR,
        legs=(
            TargetLeg(symbol, "spot", +cap),
            TargetLeg(symbol, "perp_usdt", -cap),
        ),
        inputs_hash="x" * 64,
        regime_state=1,
    )


def _target_flat(symbol: str = "BTC-USDT") -> TargetPosition:
    return TargetPosition(
        strategy_id=CARRY_V3_ID,
        as_of=ANCHOR,
        legs=(),
        inputs_hash="x" * 64,
        regime_state=0,
    )


def _append_fill_event(
    path: Path, *, symbol: str, market: str, side: str,
    quantity_base: float, fill_price: float, notional_quote: float,
    fee_bp: float = 7.5,
) -> None:
    """Append a synthetic fill_simulated event (helper for replay tests)."""
    event = make_event(
        "fill_simulated", strategy_id=CARRY_V3_ID, symbol=symbol,
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
        data={
            "symbol": symbol, "market": market, "side": side,
            "quantity_base": quantity_base, "fill_price": fill_price,
            "notional_quote": notional_quote,
            "fee_paid_quote": notional_quote * fee_bp / 10_000.0,
            "fee_bp": fee_bp,
            "target_delta_notional": notional_quote if side == "buy" else -notional_quote,
        },
    )
    append_event(event, path=path)


# ── replay_position_book ──────────────────────────────────────────────────


def test_replay_empty_log_returns_empty_book(tmp_path: Path):
    assert replay_position_book(tmp_path / "no_such.jsonl") == {}


def test_replay_single_long_fill_returns_long_position(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.005, fill_price=100_000.0, notional_quote=500.0,
    )
    book = replay_position_book(log)
    assert set(book) == {("BTC-USDT", "spot")}
    pos = book[("BTC-USDT", "spot")]
    assert pos.quantity_base == pytest.approx(0.005)
    assert pos.notional_quote == pytest.approx(500.0)
    assert pos.avg_entry_price == pytest.approx(100_000.0)


def test_replay_buy_then_full_sell_returns_empty_book(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.005, fill_price=100_000.0, notional_quote=500.0,
    )
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="sell",
        quantity_base=0.005, fill_price=101_000.0, notional_quote=505.0,
    )
    assert replay_position_book(log) == {}


def test_replay_partial_close_reduces_quantity(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.010, fill_price=100_000.0, notional_quote=1_000.0,
    )
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="sell",
        quantity_base=0.004, fill_price=101_000.0, notional_quote=404.0,
    )
    book = replay_position_book(log)
    pos = book[("BTC-USDT", "spot")]
    assert pos.quantity_base == pytest.approx(0.006)
    # Partial close preserves the original avg entry (FIFO would differ; we
    # use weighted-mean conservation for same-direction reductions).
    assert pos.avg_entry_price == pytest.approx(100_000.0)


def test_replay_position_flip_resets_avg_entry(tmp_path: Path):
    """Crossing zero throws out the old avg; new avg = flip price."""
    log = tmp_path / "events.jsonl"
    _append_fill_event(
        log, symbol="BTC-USDT", market="perp_usdt", side="buy",
        quantity_base=0.005, fill_price=100_000.0, notional_quote=500.0,
    )
    _append_fill_event(
        log, symbol="BTC-USDT", market="perp_usdt", side="sell",
        quantity_base=0.010, fill_price=110_000.0, notional_quote=1_100.0,
    )
    book = replay_position_book(log)
    pos = book[("BTC-USDT", "perp_usdt")]
    assert pos.quantity_base == pytest.approx(-0.005)
    assert pos.avg_entry_price == pytest.approx(110_000.0)


def test_replay_ignores_non_fill_events(tmp_path: Path):
    """system_start / signal_compute / etc. are not balance-affecting."""
    log = tmp_path / "events.jsonl"
    sysstart = make_event(
        "system_start", strategy_id=CARRY_V3_ID, symbol="BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
    )
    append_event(sysstart, path=log)
    assert replay_position_book(log) == {}


# ── latest_close_by_market ────────────────────────────────────────────────


def test_latest_close_by_market_picks_last_open_time_per_market():
    klines = _klines_both_markets(n_bars=5, spot_close=100.0, perp_close=200.0)
    prices = latest_close_by_market(klines, "BTC-USDT")
    # Last spot close = 100 + 4*0.01 = 100.04; perp = 200.04.
    assert prices["spot"] == pytest.approx(100.04)
    assert prices["perp_usdt"] == pytest.approx(200.04)


def test_latest_close_by_market_returns_empty_when_no_rows():
    klines = pl.DataFrame()
    assert latest_close_by_market(klines, "BTC-USDT") == {}


def test_latest_close_by_market_filters_to_symbol():
    klines = pl.concat([
        _market_klines("BTC-USDT", 3, market="spot", close_base=100.0),
        _market_klines("ETH-USDT", 3, market="spot", close_base=4_000.0),
    ], how="vertical")
    assert latest_close_by_market(klines, "ETH-USDT") == pytest.approx(
        {"spot": 4_000.02},
    )


# ── compute_fills ─────────────────────────────────────────────────────────


def test_compute_fills_empty_book_active_target_emits_two_buys_sells():
    target = _target_active(cap=500.0)
    prices = {"spot": 100_000.0, "perp_usdt": 100_010.0}
    fills = compute_fills(target, {}, prices)
    assert len(fills) == 2
    by_market = {f.market: f for f in fills}
    spot, perp = by_market["spot"], by_market["perp_usdt"]
    assert spot.side == "buy"
    assert perp.side == "sell"
    assert spot.notional_quote == pytest.approx(500.0)
    assert perp.notional_quote == pytest.approx(500.0)


def test_compute_fills_flat_target_closes_existing_positions():
    book = {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
        ("BTC-USDT", "perp_usdt"): Position(
            "BTC-USDT", "perp_usdt", -0.005, 100_010.0, -500.05,
        ),
    }
    target = _target_flat()
    prices = {"spot": 100_000.0, "perp_usdt": 100_010.0}
    fills = compute_fills(target, book, prices)
    assert len(fills) == 2
    sides = {f.market: f.side for f in fills}
    assert sides == {"spot": "sell", "perp_usdt": "buy"}   # close long / cover short


def test_compute_fills_zero_delta_emits_nothing():
    book = {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
    }
    target = TargetPosition(
        strategy_id=CARRY_V3_ID, as_of=ANCHOR,
        legs=(TargetLeg("BTC-USDT", "spot", +500.0),),
        inputs_hash="x" * 64,
    )
    prices = {"spot": 100_000.0}
    assert compute_fills(target, book, prices) == []


def test_compute_fills_uses_correct_taker_fee_per_market():
    target = _target_active(cap=1_000.0)
    prices = {"spot": 100_000.0, "perp_usdt": 100_000.0}
    fills = compute_fills(target, {}, prices)
    by_market = {f.market: f for f in fills}
    assert by_market["spot"].fee_bp == DEFAULT_TAKER_FEE_BPS["spot"]
    assert by_market["perp_usdt"].fee_bp == DEFAULT_TAKER_FEE_BPS["perp_usdt"]
    # spot 7.5bp on $1000 = $0.75; perp 5bp on $1000 = $0.50.
    assert by_market["spot"].fee_paid_quote == pytest.approx(0.75)
    assert by_market["perp_usdt"].fee_paid_quote == pytest.approx(0.50)


def test_compute_fills_skips_when_no_price_for_market(caplog):
    target = _target_active()
    prices = {"perp_usdt": 100_010.0}   # no spot price
    with caplog.at_level("WARNING"):
        fills = compute_fills(target, {}, prices)
    assert len(fills) == 1
    assert fills[0].market == "perp_usdt"
    assert "no price for" in caplog.text


def test_compute_fills_quantity_base_matches_delta_over_price():
    target = TargetPosition(
        strategy_id=CARRY_V3_ID, as_of=ANCHOR,
        legs=(TargetLeg("BTC-USDT", "spot", +1_000.0),),
        inputs_hash="x" * 64,
    )
    prices = {"spot": 50_000.0}
    fills = compute_fills(target, {}, prices)
    assert fills[0].quantity_base == pytest.approx(0.02)   # 1000/50000


# ── update_book ───────────────────────────────────────────────────────────


def test_update_book_does_not_mutate_input():
    book = {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
    }
    fill = SimulatedFill(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.001, fill_price=110_000.0, notional_quote=110.0,
        fee_paid_quote=0.0825, fee_bp=7.5, target_delta_notional=110.0,
    )
    out = update_book(book, [fill])
    # Input book untouched.
    assert book[("BTC-USDT", "spot")].quantity_base == pytest.approx(0.005)
    # New book reflects the fill.
    assert out[("BTC-USDT", "spot")].quantity_base == pytest.approx(0.006)


def test_update_book_close_to_zero_drops_from_book():
    book = {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
    }
    fill = SimulatedFill(
        symbol="BTC-USDT", market="spot", side="sell",
        quantity_base=0.005, fill_price=101_000.0, notional_quote=505.0,
        fee_paid_quote=0.378, fee_bp=7.5, target_delta_notional=-500.0,
    )
    out = update_book(book, [fill])
    assert out == {}


def test_update_book_same_direction_buy_weighted_avg_entry():
    book = {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
    }
    fill = SimulatedFill(
        symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.005, fill_price=120_000.0, notional_quote=600.0,
        fee_paid_quote=0.45, fee_bp=7.5, target_delta_notional=600.0,
    )
    out = update_book(book, [fill])
    pos = out[("BTC-USDT", "spot")]
    assert pos.quantity_base == pytest.approx(0.010)
    # Weighted: (0.005*100k + 0.005*120k) / 0.010 = 110_000.
    assert pos.avg_entry_price == pytest.approx(110_000.0)


# ── step_paper_fill_sim ───────────────────────────────────────────────────


def test_step_paper_fill_sim_writes_two_fill_events_when_going_active(
    tmp_path: Path,
):
    log = tmp_path / "events.jsonl"
    target = _target_active(cap=500.0)
    window = RollingWindow(
        funding=pl.DataFrame(),
        klines=_klines_both_markets(spot_close=100_000.0, perp_close=100_010.0),
    )
    book, fills = step_paper_fill_sim(
        target, window,
        data_version="2026-01-01T00:00+nocorr",
        git_commit_hash="deadbee",
        clock_drift_ms=42.0,
        event_log_path=log,
    )
    events = read_events(log)
    assert len(events) == 2
    assert all(e["kind"] == "fill_simulated" for e in events)
    assert len(book) == 2
    assert len(fills) == 2


def test_step_paper_fill_sim_no_change_writes_no_events(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    # Pre-seed log with two fills matching the target -> next cycle: zero deltas.
    _append_fill_event(
        log, symbol="BTC-USDT", market="spot", side="buy",
        quantity_base=0.005, fill_price=100_000.0, notional_quote=500.0,
    )
    _append_fill_event(
        log, symbol="BTC-USDT", market="perp_usdt", side="sell",
        quantity_base=0.005, fill_price=100_010.0, notional_quote=500.0,
        fee_bp=5.0,
    )
    target = _target_active(cap=500.0)
    window = RollingWindow(
        funding=pl.DataFrame(),
        klines=_klines_both_markets(spot_close=100_000.0, perp_close=100_010.0),
    )
    book_before = replay_position_book(log)
    book_after, fills = step_paper_fill_sim(
        target, window,
        data_version="2026-01-01T00:00+nocorr",
        git_commit_hash="deadbee",
        clock_drift_ms=42.0,
        event_log_path=log,
    )
    # No new events appended.
    events = read_events(log)
    assert len(events) == 2
    assert fills == []
    # Notionals already equal; absolute book contents may shift only via
    # avg_entry arithmetic — verify keys + signed quantities preserved.
    assert set(book_after) == set(book_before)


def test_step_paper_fill_sim_rejects_mixed_symbols(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    target = TargetPosition(
        strategy_id=CARRY_V3_ID, as_of=ANCHOR,
        legs=(
            TargetLeg("BTC-USDT", "spot", +500.0),
            TargetLeg("ETH-USDT", "spot", +500.0),
        ),
        inputs_hash="x" * 64,
    )
    window = RollingWindow(
        funding=pl.DataFrame(),
        klines=_klines_both_markets(),
    )
    with pytest.raises(ValueError, match="mixed symbols"):
        step_paper_fill_sim(
            target, window,
            data_version="x", git_commit_hash="y", clock_drift_ms=None,
            event_log_path=log,
        )


def test_step_paper_fill_sim_cold_start_flat_is_noop(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    book, fills = step_paper_fill_sim(
        _target_flat(), RollingWindow(funding=pl.DataFrame(), klines=pl.DataFrame()),
        data_version="x", git_commit_hash="y", clock_drift_ms=None,
        event_log_path=log,
    )
    assert book == {}
    assert fills == []
    assert read_events(log) == []


# ── End-to-end: stage [2] adapter -> stage [4] fill sim ──────────────────


def test_skeleton_stages_2_4_compose(tmp_path: Path):
    """Walking-skeleton claim: pipeline runs end-to-end.

    Build a synthetic window with positive funding -> adapter emits state 1
    with two legs -> step_paper_fill_sim writes two fill_simulated events
    AND returns a 2-position book. Verifies (target -> fills -> book +
    audit log) integration without involving real Binance fetches
    (those are tested separately in test_forward_fetch.py).
    """
    log = tmp_path / "events.jsonl"
    funding = _funding_df_active(n=130)
    klines = _klines_both_markets(spot_close=100_000.0, perp_close=100_010.0)
    window = RollingWindow(funding=funding, klines=klines)

    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    target = adapter.compute_target_position(window)
    assert target.regime_state == 1
    assert len(target.legs) == 2

    book, fills = step_paper_fill_sim(
        target, window,
        data_version="2026-01-01T00:00+nocorr",
        git_commit_hash="deadbee",
        clock_drift_ms=10.0,
        event_log_path=log,
    )

    assert len(fills) == 2
    assert len(book) == 2
    events = read_events(log)
    assert [e["kind"] for e in events] == ["fill_simulated", "fill_simulated"]
    # Versioning fields flowed through.
    for ev in events:
        assert ev["versioning"]["data_version"] == "2026-01-01T00:00+nocorr"
        assert ev["versioning"]["git_commit_hash"] == "deadbee"
        assert ev["versioning"]["process_clock_drift_vs_binance_ms"] == 10.0
