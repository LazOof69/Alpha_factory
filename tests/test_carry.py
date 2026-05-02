"""Tests for src/alpha_factory/alpha/carry.py — V2 carry on synthetic data.

Synthesizes minimal KLINES_SCHEMA and FUNDING_SCHEMA DataFrames so the
regime-detection state machine, leg construction, funding sign, and
equity-curve continuity can be tested without touching the L1 archive.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from alpha_factory.alpha.carry import (
    LEG_ID_PERP_SHORT,
    LEG_ID_SPOT_LONG,
    CarryArtifacts,
    CarryParams,
    run_carry_backtest,
)
from alpha_factory.data.schema import FUNDING_SCHEMA, KLINES_SCHEMA
from alpha_factory.validation.contracts import validate_equity_curve
from alpha_factory.validation.schema import (
    BACKTEST_LEG_SCHEMA,
    EQUITY_CURVE_SCHEMA,
    PER_SYMBOL_CONTRIB_SCHEMA,
)

# ── Synthetic data builders ──────────────────────────────────────────────


_SYMBOL = "BTC-USDT"


def _make_klines(
    market: str, times: list[datetime], closes: list[float],
) -> pl.DataFrame:
    """Minimal KLINES_SCHEMA-conforming frame; non-relevant cols padded."""
    n = len(times)
    df = pl.DataFrame({
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
    })
    return df.cast(KLINES_SCHEMA)


def _make_funding(times: list[datetime], rates: list[float]) -> pl.DataFrame:
    n = len(times)
    df = pl.DataFrame({
        "symbol": [_SYMBOL] * n,
        "funding_time": times,
        "funding_rate": rates,
        "mark_price": [50000.0] * n,
        "ingested_at": [datetime(2026, 1, 1, tzinfo=UTC)] * n,
        "source": ["test"] * n,
    })
    return df.cast(FUNDING_SCHEMA)


def _hourly_times(start: datetime, n_hours: int) -> list[datetime]:
    return [start + timedelta(hours=h) for h in range(n_hours)]


def _settlement_times(times: list[datetime]) -> list[datetime]:
    """Every 8th hour is a funding settlement (matches Binance perp 8h cadence)."""
    return [t for t in times if t.hour % 8 == 0]


def _build_inputs(
    n_hours: int,
    funding_rate: float | list[float],
    *,
    spot_close: float = 50_000.0,
    perp_close: float = 50_000.0,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Helper: construct (klines_df, funding_df) for `n_hours` of bars with
    constant prices and either constant or varying funding rate."""
    times = _hourly_times(datetime(2026, 1, 1, tzinfo=UTC), n_hours)
    settle_times = _settlement_times(times)

    spot_klines = _make_klines("spot", times, [spot_close] * n_hours)
    perp_klines = _make_klines("perp_usdt", times, [perp_close] * n_hours)
    klines_df = pl.concat([spot_klines, perp_klines])

    if isinstance(funding_rate, (int, float)):
        rates = [float(funding_rate)] * len(settle_times)
    else:
        if len(funding_rate) != len(settle_times):
            raise ValueError(
                f"funding_rate len {len(funding_rate)} != "
                f"settlement count {len(settle_times)}",
            )
        rates = list(funding_rate)
    funding_df = _make_funding(settle_times, rates)
    return klines_df, funding_df


# ── Tests ────────────────────────────────────────────────────────────────


def test_empty_input_returns_empty_artifacts():
    """No overlapping spot/perp bars -> empty artifacts with conforming schemas."""
    empty_klines = pl.DataFrame(schema=KLINES_SCHEMA)
    empty_funding = pl.DataFrame(schema=FUNDING_SCHEMA)
    out = run_carry_backtest(
        _SYMBOL, "test-empty", CarryParams(),
        klines_df=empty_klines, funding_df=empty_funding,
    )
    assert isinstance(out, CarryArtifacts)
    assert out.legs.height == 0
    assert out.contrib.height == 0
    assert out.equity_curve.height == 0
    assert out.n_transitions == 0
    assert out.legs.schema == pl.Schema(BACKTEST_LEG_SCHEMA)
    assert out.contrib.schema == pl.Schema(PER_SYMBOL_CONTRIB_SCHEMA)
    assert out.equity_curve.schema == pl.Schema(EQUITY_CURVE_SCHEMA)


def test_constant_positive_funding_stays_active():
    """Constant +2 bp/8h funding, flat prices: V2 stays active throughout."""
    n_hours = 168 * 2  # 14 days
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=0.0002)

    out = run_carry_backtest(
        _SYMBOL, "test-pos", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )

    # State machine: starts active; 7d MA = +2 bp > re-entry threshold
    # (which is irrelevant since we never exit), so stays active.
    assert out.n_transitions == 0

    # All legs at active state -> non-zero notional after the first bar
    perp_active = out.legs.filter(
        (pl.col("leg_id") == LEG_ID_PERP_SHORT) & (pl.col("notional") > 0)
    )
    assert perp_active.height == n_hours

    # Equity grew (carry harvested across many settlements)
    equity_first = out.equity_curve["equity"][0]
    equity_last = out.equity_curve["equity"][-1]
    assert equity_last > equity_first, (
        f"equity {equity_first:.2f} -> {equity_last:.2f} should grow"
    )

    # Sanity: funding_paid on perp shorts is NEGATIVE (received income)
    perp_settlement = out.legs.filter(
        (pl.col("leg_id") == LEG_ID_PERP_SHORT)
        & (pl.col("funding_paid") != 0.0)
    )
    assert perp_settlement.height > 0
    assert (perp_settlement["funding_paid"] < 0).all(), (
        "short perp at positive rate should receive funding (negative funding_paid)"
    )


def test_constant_negative_funding_eventually_exits():
    """Constant -2 bp/8h funding: V2 detects bad regime after 7d MA fills."""
    n_hours = 168 * 2  # 14 days; lookback=21 settlements = 7 days
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=-0.0002)

    out = run_carry_backtest(
        _SYMBOL, "test-neg", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )

    # State: stays active until 21st settlement (when MA first fills, at -2bp <
    # exit threshold -1bp), then flips to 0. Exactly 1 transition.
    assert out.n_transitions == 1

    # Final state should be exited (notional=0 on both legs)
    last_legs = out.legs.filter(pl.col("time") == out.legs["time"].max())
    assert (last_legs["notional"] == 0).all()


def test_regime_flip_counts_transitions():
    """Negative funding for first 7 days then positive for next 7 days."""
    n_hours = 168 * 2  # 14 days
    times = _hourly_times(datetime(2026, 1, 1, tzinfo=UTC), n_hours)
    settle_times = _settlement_times(times)
    half = len(settle_times) // 2
    rates = [-0.0002] * half + [0.0002] * (len(settle_times) - half)
    klines_df, _ = _build_inputs(n_hours, funding_rate=0.0)   # placeholder
    funding_df = _make_funding(settle_times, rates)

    out = run_carry_backtest(
        _SYMBOL, "test-flip", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )

    # Expected sequence:
    #   1) Start active
    #   2) Once 21-settlement MA fills with negative samples -> exit (1 trans)
    #   3) As positive samples roll into the 21-window the MA crosses the
    #      re-entry threshold -> active (1 more trans)
    # n_transitions ranges 1-2 depending on exact crossing dynamics; assert >=1
    # and the final state is plausible (active or exited).
    assert out.n_transitions >= 1, (
        f"regime flip should produce at least 1 transition, got {out.n_transitions}"
    )


def test_legs_have_correct_long_short_weights_when_active():
    """Active state -> spot leg weight=+0.5, perp short weight=-0.5."""
    n_hours = 168 * 2
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=0.0001)
    out = run_carry_backtest(
        _SYMBOL, "test-weights", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    # Skip first bar (period_ret conventions); pick a later active bar.
    sample = out.legs.filter(pl.col("time") == out.legs["time"][-1])
    spot = sample.filter(pl.col("leg_id") == LEG_ID_SPOT_LONG)
    perp = sample.filter(pl.col("leg_id") == LEG_ID_PERP_SHORT)
    assert spot["weight"][0] == 0.5
    assert perp["weight"][0] == -0.5
    assert spot["side"][0] == "long"
    assert perp["side"][0] == "short"


def test_funding_paid_sign_short_perp_negative_when_rate_positive():
    """Short perp at rate>0 receives funding => funding_paid < 0."""
    n_hours = 24    # 3 settlements
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=0.0003)
    out = run_carry_backtest(
        _SYMBOL, "test-fundsign-pos", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    perp_settle = out.legs.filter(
        (pl.col("leg_id") == LEG_ID_PERP_SHORT)
        & (pl.col("funding_paid") != 0.0)
    )
    assert perp_settle.height > 0
    assert (perp_settle["funding_paid"] < 0).all()
    # Magnitude check: funding_paid = -notional * rate.
    # notional=500, rate=0.0003 -> funding_paid = -0.15
    sample = perp_settle["funding_paid"][0]
    expected = -500.0 * 0.0003
    assert abs(sample - expected) < 1e-9, (
        f"expected {expected}, got {sample}"
    )


def test_funding_paid_sign_short_perp_positive_when_rate_negative():
    """Short perp at rate<0 pays funding => funding_paid > 0."""
    n_hours = 24
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=-0.0003)
    out = run_carry_backtest(
        _SYMBOL, "test-fundsign-neg", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    perp_settle = out.legs.filter(
        (pl.col("leg_id") == LEG_ID_PERP_SHORT)
        & (pl.col("funding_paid") != 0.0)
    )
    assert perp_settle.height > 0
    assert (perp_settle["funding_paid"] > 0).all(), (
        "short perp at negative rate should PAY funding (positive funding_paid)"
    )


def test_apply_costs_charges_transition_fees():
    """Regime transition (notional 500 -> 0) triggers fees on the transition bar."""
    n_hours = 168 * 2
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=-0.0002)
    out = run_carry_backtest(
        _SYMBOL, "test-fees", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    # First exit transition should happen after lookback (~21 settlements).
    # At least one perp leg row should have non-zero fees from |delta_notional|.
    fees_total = float(out.legs["fees"].sum())
    assert fees_total > 0, (
        f"expected fees > 0 on transition bars, got {fees_total}"
    )


def test_equity_curve_passes_validate_equity_curve():
    """Carry's equity_curve output conforms to validate_equity_curve invariants."""
    n_hours = 168 * 2
    klines_df, funding_df = _build_inputs(n_hours, funding_rate=0.0001)
    out = run_carry_backtest(
        _SYMBOL, "test-validate", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    # Should NOT raise.
    validate_equity_curve(out.equity_curve)


def test_carry_params_as_dict_canonical():
    """CarryParams.as_dict() yields a JSON-canonicalizable dict."""
    from alpha_factory.validation.contracts import canonical_params_hash
    p1 = CarryParams()
    p2 = CarryParams(fee_bps=16.0)
    assert canonical_params_hash(p1.as_dict()) == canonical_params_hash(p2.as_dict())
    p3 = CarryParams(fee_bps=20.0)
    assert canonical_params_hash(p1.as_dict()) != canonical_params_hash(p3.as_dict())


def test_artifacts_schemas_match():
    """All output artifacts conform to their declared schemas exactly."""
    klines_df, funding_df = _build_inputs(48, funding_rate=0.0001)
    out = run_carry_backtest(
        _SYMBOL, "test-schema", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    assert out.legs.schema == pl.Schema(BACKTEST_LEG_SCHEMA)
    assert out.contrib.schema == pl.Schema(PER_SYMBOL_CONTRIB_SCHEMA)
    assert out.equity_curve.schema == pl.Schema(EQUITY_CURVE_SCHEMA)


def test_two_legs_per_bar_invariant():
    """Every bar produces exactly 2 legs (spot long + perp short)."""
    klines_df, funding_df = _build_inputs(48, funding_rate=0.0001)
    out = run_carry_backtest(
        _SYMBOL, "test-2legs", CarryParams(),
        klines_df=klines_df, funding_df=funding_df,
    )
    n_bars = out.equity_curve.height
    assert out.legs.height == 2 * n_bars
    leg_counts = out.legs.group_by("time").agg(pl.len().alias("n"))
    assert (leg_counts["n"] == 2).all()
