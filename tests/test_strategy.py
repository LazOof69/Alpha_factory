"""Tests for src/alpha_factory/execution/strategy.py (Phase C stage [2]).

Covers the v3 §9 protocol contract (TargetPosition / TargetLeg /
RollingWindow shape + Strategy structural-typing), the v2 §2
``signal_inputs_hash`` byte-stability properties, and the
``CarryV3Adapter`` placeholder's state -> legs mapping. All synthetic;
deterministic; tz-aware UTC.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID as CARRY_V3_ID,
)
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
    current_regime_state_v3,
)
from alpha_factory.execution.strategy import (
    CarryV3Adapter,
    RollingWindow,
    Strategy,
    TargetLeg,
    TargetPosition,
    compute_inputs_hash,
)

# 8h Binance funding cadence (= 3 settlements per UTC day).
SETTLE_STEP = timedelta(hours=8)
KLINE_STEP = timedelta(minutes=1)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _settle_funding_df(
    rates: list[float], start: datetime | None = None,
) -> pl.DataFrame:
    """One row per settlement; tz-aware UTC ``open_time``."""
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    times = [start + SETTLE_STEP * i for i in range(len(rates))]
    return pl.DataFrame({"open_time": times, "funding_rate": rates}).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )


def _klines_df(n_bars: int, start: datetime | None = None) -> pl.DataFrame:
    """``n`` 1-minute bars with a monotonic close; tz-aware UTC."""
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    times = [start + KLINE_STEP * i for i in range(n_bars)]
    return pl.DataFrame({
        "open_time": times,
        "close": [100.0 + i * 0.01 for i in range(n_bars)],
    }).with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )


def _window(funding: pl.DataFrame, klines: pl.DataFrame) -> RollingWindow:
    return RollingWindow(funding=funding, klines=klines)


# ── TargetLeg / TargetPosition shape ──────────────────────────────────────


def test_target_leg_holds_signed_notional():
    long_leg = TargetLeg("BTC-USDT", "spot", +500.0)
    short_leg = TargetLeg("BTC-USDT", "perp_usdt", -500.0)
    assert long_leg.target_notional_quote > 0
    assert short_leg.target_notional_quote < 0
    assert long_leg.symbol == short_leg.symbol == "BTC-USDT"


def test_target_leg_is_frozen():
    leg = TargetLeg("BTC-USDT", "spot", 500.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        leg.target_notional_quote = 1000.0  # type: ignore[misc]


def test_target_position_is_frozen():
    tp = TargetPosition(
        strategy_id="carry_v3",
        as_of=datetime(2026, 5, 13, tzinfo=UTC),
        legs=(),
        inputs_hash="0" * 64,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        tp.legs = (TargetLeg("BTC-USDT", "spot", 1.0),)  # type: ignore[misc]


def test_target_position_default_regime_state_is_none():
    tp = TargetPosition(
        strategy_id="carry_v3",
        as_of=datetime(2026, 5, 13, tzinfo=UTC),
        legs=(),
        inputs_hash="0" * 64,
    )
    assert tp.regime_state is None


# ── Strategy protocol ─────────────────────────────────────────────────────


def test_carry_v3_adapter_satisfies_strategy_protocol():
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    # runtime_checkable Protocol -> isinstance checks method presence only.
    assert isinstance(adapter, Strategy)


# ── compute_inputs_hash ───────────────────────────────────────────────────


def test_compute_inputs_hash_is_deterministic():
    w1 = _window(_settle_funding_df([0.0001] * 10), _klines_df(5))
    w2 = _window(_settle_funding_df([0.0001] * 10), _klines_df(5))
    assert compute_inputs_hash(w1) == compute_inputs_hash(w2)


def test_compute_inputs_hash_changes_on_funding_edit():
    w1 = _window(_settle_funding_df([0.0001] * 10), _klines_df(5))
    w2 = _window(_settle_funding_df([0.0001] * 9 + [0.0002]), _klines_df(5))
    assert compute_inputs_hash(w1) != compute_inputs_hash(w2)


def test_compute_inputs_hash_changes_on_klines_edit():
    funding = _settle_funding_df([0.0001] * 10)
    klines = _klines_df(5)
    klines_b = klines.with_columns(
        pl.col("close") + pl.lit(0.5),
    )
    assert compute_inputs_hash(_window(funding, klines)) != compute_inputs_hash(
        _window(funding, klines_b),
    )


def test_compute_inputs_hash_separates_funding_from_klines():
    """Section prefixes prevent funding/klines content collision."""
    funding_a = _settle_funding_df([0.0001] * 3)
    klines_empty = pl.DataFrame()
    funding_empty = pl.DataFrame()
    klines_a = _klines_df(3)
    # Different layout, identical "total content" if prefixes were absent.
    h1 = compute_inputs_hash(_window(funding_a, klines_empty))
    h2 = compute_inputs_hash(_window(funding_empty, klines_a))
    assert h1 != h2


def test_compute_inputs_hash_empty_window_does_not_raise():
    w = _window(pl.DataFrame(), pl.DataFrame())
    h = compute_inputs_hash(w)
    assert len(h) == 64  # SHA-256 hex


# ── _window_as_of (via adapter — covers the public path) ──────────────────


def test_adapter_raises_on_completely_empty_window():
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    with pytest.raises(ValueError, match="no usable open_time"):
        adapter.compute_target_position(_window(pl.DataFrame(), pl.DataFrame()))


def test_adapter_as_of_uses_latest_open_time_across_frames():
    """as_of = max(funding.last, klines.last) — klines extends past funding here.

    Funding has 3 settlements at 0h / 8h / 16h (last = 16h = 960 min).
    Klines runs 1100 1-min bars (last = 1099 min ≈ 18.3h), so klines
    is later and must win.
    """
    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    funding = _settle_funding_df([0.0001] * 3, start=start)
    klines = _klines_df(1100, start=start)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    tp = adapter.compute_target_position(_window(funding, klines))
    expected_klines_last = start + KLINE_STEP * 1099
    assert tp.as_of == expected_klines_last


def test_adapter_as_of_picks_funding_when_funding_is_later():
    """Inverse of the previous test: funding extends past klines."""
    start = datetime(2026, 1, 1, 0, tzinfo=UTC)
    funding = _settle_funding_df([0.0001] * 5, start=start)   # last = 32h
    klines = _klines_df(100, start=start)                      # last = 99 min
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    tp = adapter.compute_target_position(_window(funding, klines))
    expected_funding_last = start + SETTLE_STEP * 4
    assert tp.as_of == expected_funding_last


# ── CarryV3Adapter: state -> legs mapping ─────────────────────────────────


def test_adapter_warmup_window_returns_flat():
    """< compression_lookback (120) settlements -> 30dma null -> state 0."""
    params = CarryV3Params()
    funding = _settle_funding_df([0.0001] * 50)  # well under 120
    klines = _klines_df(100)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=params)
    tp = adapter.compute_target_position(_window(funding, klines))
    assert tp.regime_state == 0
    assert tp.legs == ()
    # Adapter is supposed to agree with the underlying helper.
    assert current_regime_state_v3(funding, params) == 0


def test_adapter_active_regime_returns_two_legs():
    """Positive funding for > 120 settlements + past ratchet -> state 1.

    With 130 settlements at 1 bp/8h (rate=0.0001), at row 120 both 7dma
    and 30dma_abs are computable and well above the re-entry thresholds
    (0.5 bp/8h funding and 0.1 bp/8h compression). The ratchet counter
    is 120 at that point, easily past the 21-settle guard, so the
    transition fires. State stays 1 through to the last row.
    """
    params = CarryV3Params()
    funding = _settle_funding_df([0.0001] * 130)
    klines = _klines_df(50)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=params)
    tp = adapter.compute_target_position(_window(funding, klines))

    assert tp.regime_state == 1
    assert len(tp.legs) == 2
    cap = params.capital_per_leg
    assert tp.legs[0] == TargetLeg("BTC-USDT", "spot", +cap)
    assert tp.legs[1] == TargetLeg("BTC-USDT", "perp_usdt", -cap)
    # legs must net to zero notional (delta-neutral carry pair).
    assert sum(leg.target_notional_quote for leg in tp.legs) == pytest.approx(0.0)


def test_adapter_records_strategy_id_and_inputs_hash():
    params = CarryV3Params()
    funding = _settle_funding_df([0.0001] * 130)
    klines = _klines_df(50)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=params)
    w = _window(funding, klines)
    tp = adapter.compute_target_position(w)
    assert tp.strategy_id == CARRY_V3_ID
    assert tp.inputs_hash == compute_inputs_hash(w)


def test_adapter_uses_constructor_symbol():
    params = CarryV3Params()
    funding = _settle_funding_df([0.0001] * 130)
    klines = _klines_df(50)
    adapter = CarryV3Adapter(symbol="ETH-USDT", params=params)
    tp = adapter.compute_target_position(_window(funding, klines))
    assert all(leg.symbol == "ETH-USDT" for leg in tp.legs)


def test_adapter_capital_per_leg_drives_notional():
    """Constructor param flows through to leg sizing."""
    params = CarryV3Params(capital_per_leg=1234.5)
    funding = _settle_funding_df([0.0001] * 130)
    klines = _klines_df(50)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=params)
    tp = adapter.compute_target_position(_window(funding, klines))
    assert tp.legs[0].target_notional_quote == +1234.5
    assert tp.legs[1].target_notional_quote == -1234.5
