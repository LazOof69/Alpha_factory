"""Tests for src/alpha_factory/alpha/carry_v3.py — funding-compression detector.

The synthetic-data approach mirrors test_carry.py but stresses the V3
state machine specifically: warmup-exited initial state, dual-exit
conditions (V2 negative-funding path AND V3 compression path), AND
re-entry, ratchet guard, and flutter resistance.

Each test holds price constant (50_000.0 spot/perp) so the ONLY
P&L source is funding flow + transition fees. This isolates regime
behavior from price-drift noise.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from alpha_factory.alpha.carry import (
    LEG_ID_SPOT_LONG,
    CarryParams,
    run_carry_backtest,
)
from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID,
    CarryV3Params,
    run_carry_v3_backtest,
)
from alpha_factory.data.schema import FUNDING_SCHEMA, KLINES_SCHEMA
from alpha_factory.validation.contracts import (
    canonical_params_hash,
    validate_equity_curve,
)
from alpha_factory.validation.schema import (
    LEG_MARKET_PERP_USDT,
    LEG_MARKET_SPOT,
)

# ── Constants ────────────────────────────────────────────────────────────


_SYMBOL = "BTC-USDT"
_PRICE = 50_000.0
_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


# ── Synthetic data builders ──────────────────────────────────────────────


def _make_klines(
    market: str, times: list[datetime], closes: list[float],
) -> pl.DataFrame:
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
        "mark_price": [_PRICE] * n,
        "ingested_at": [datetime(2026, 1, 1, tzinfo=UTC)] * n,
        "source": ["test"] * n,
    })
    return df.cast(FUNDING_SCHEMA)


def _build_inputs(
    funding_trajectory: list[float],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Build (klines_df, funding_df) for a synthetic trajectory.

    Length is inferred from `funding_trajectory` (# of settlements).
    Total bars = n_settlements * 8 (hourly cadence). Constant price.
    """
    n_settle = len(funding_trajectory)
    n_hours = n_settle * 8
    times = [_BASE_TIME + timedelta(hours=h) for h in range(n_hours)]
    closes = [_PRICE] * n_hours
    klines = pl.concat([
        _make_klines(LEG_MARKET_SPOT, times, closes),
        _make_klines(LEG_MARKET_PERP_USDT, times, closes),
    ])
    settle_times = [t for t in times if t.hour % 8 == 0]
    assert len(settle_times) == n_settle
    funding = _make_funding(settle_times, funding_trajectory)
    return klines, funding


def _spot_weight_series(art) -> pl.Series:
    """Return the per-bar spot-long weight (0 or 0.5) as a 1-D series."""
    return (
        art.legs
        .filter(pl.col("leg_id") == LEG_ID_SPOT_LONG)
        .sort("time")
        ["weight"]
    )


# ── Tests ────────────────────────────────────────────────────────────────


def test_strategy_id_is_carry_v3():
    """Registry partition key must be 'carry_v3' (separate from V2)."""
    assert STRATEGY_ID == "carry_v3"


def test_v3_params_hash_differs_from_v2_with_same_v2_fields():
    """V3 params dict carries 4 extra fields => params_hash diverges.

    Critical for L3 registry: V2 and V3 must occupy distinct rows even
    when V2-inherited fee/threshold values happen to match exactly.
    """
    v2_params = CarryParams()
    v3_params = CarryV3Params()
    h_v2 = canonical_params_hash(v2_params.as_dict())
    h_v3 = canonical_params_hash(v3_params.as_dict())
    assert h_v2 != h_v3, "V3 params_hash collides with V2 — registry will conflict"


def test_v3_empty_input_returns_empty_artifacts():
    """Graceful empty path — same contract as V2."""
    klines = pl.DataFrame(schema=KLINES_SCHEMA)
    funding = pl.DataFrame(schema=FUNDING_SCHEMA)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_empty", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    assert art.legs.is_empty()
    assert art.contrib.is_empty()
    assert art.equity_curve.is_empty()
    assert art.n_transitions == 0


def test_v3_steady_high_positive_funding_active_after_warmup():
    """+5 bp/8h steady => V3 stays exited during warmup, then re-enters.

    Validates: (a) initial state is exited (V3 warmup-conservatism);
    (b) re-entry triggers once 30d MA is computable AND > 0.6 bp/8h
    threshold.
    """
    funding_traj = [0.0005] * 200  # +5 bp/8h, steady
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_steady_high", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )

    weights = _spot_weight_series(art).to_numpy()
    # First eligible transition is at settlement (compression_lookback - 1)
    # = settlement 89 (the first settle where 30dMA is computable).
    # Convert to hour index: settle 89 starts at hour 89*8 = 712.
    warmup_hours = (CarryV3Params().compression_lookback_settlements - 1) * 8
    assert (weights[:warmup_hours] == 0.0).all(), "warmup window not held exited"
    # By end, V3 has re-entered (steady high funding clears both gates).
    assert weights[-1] == 0.5, "V3 should be active by end of steady-high run"


def test_v3_negative_funding_crater_exits_via_v2_path():
    """+5 bp first 100, then -2 bp => V3 inherits V2 exit path.

    Validates: V2's 7d-MA<-1bp condition still triggers under V3.
    """
    # Need long enough run that V3 first re-enters (post-warmup) before
    # the crater hits — otherwise we're just testing the warmup-stuck case.
    funding_traj = [0.0005] * 150 + [-0.0002] * 100  # 5 bp -> -2 bp
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_crater", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    weights = _spot_weight_series(art).to_numpy()
    # V3 should re-enter by settle 90+ (warmup) when high funding is
    # consistent — verify mid-run is active.
    p = CarryV3Params()
    mid_settle = p.compression_lookback_settlements + 30  # post-warmup buffer
    mid_idx = mid_settle * 8
    assert weights[mid_idx] == 0.5, "V3 should be active mid-run pre-crater"
    # By end, after crater drives 7dMA below -1 bp, V3 should exit.
    assert weights[-1] == 0.0, "V3 should exit after crater (V2 path)"


def test_v3_compression_to_low_positive_exits_v3_new_path():
    """V2 stays active; V3 exits — the whole point of V3.

    Funding compresses from +5 bp to +0.2 bp/8h. V2 sees no
    negative-funding signal (rate stays positive); V3 sees 30d-MA
    drop below 0.3 bp threshold and exits.
    """
    # +5 bp first 60 settles (build V3 active state past warmup);
    # then +0.2 bp for 250 settles (long enough for 30d MA to compress).
    funding_traj = [0.0005] * 60 + [0.000002] * 250  # 0.02 bp/8h tail
    klines, funding = _build_inputs(funding_traj)

    # V3 path
    art_v3 = run_carry_v3_backtest(
        _SYMBOL, "test_v3_compression", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    # V2 path on identical inputs
    art_v2 = run_carry_backtest(
        _SYMBOL, "test_v2_compression", CarryParams(),
        klines_df=klines, funding_df=funding,
    )

    v3_weights = _spot_weight_series(art_v3).to_numpy()
    v2_weights = (
        art_v2.legs
        .filter(pl.col("leg_id") == LEG_ID_SPOT_LONG)
        .sort("time")
        ["weight"]
        .to_numpy()
    )

    # V3 must be exited at end (compression detected).
    assert v3_weights[-1] == 0.0, (
        "V3 failed to detect compression — exit threshold may be too loose"
    )
    # V2 must NOT exit (rate never negative; no V2 signal).
    assert v2_weights[-1] == 0.5, (
        "V2 unexpectedly exited — synthetic input may have triggered V2 path"
    )


def test_v3_recovery_reenters_when_both_conditions_met():
    """Low funding => exit; sustained high funding => re-enter (AND-gate)."""
    # 200 settlements at 0.02 bp/8h (V3 stays exited; way below 0.6 bp gate)
    # + 200 settlements at +2 bp/8h (well above both 0.5 bp 7dMA + 0.6 bp
    # 30dMA gates; eventually re-enters once 30d MA accumulates).
    funding_traj = [0.000002] * 200 + [0.0002] * 200
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_recovery", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    weights = _spot_weight_series(art).to_numpy()
    # First 200 settlements (1600 bars) all exited.
    assert (weights[:1600] == 0.0).all(), (
        "V3 should remain exited under sustained low funding"
    )
    # By end, after 200 high-funding settlements, V3 should re-enter.
    assert weights[-1] == 0.5, (
        "V3 failed to re-enter after sustained recovery — 30d gate too tight"
    )


def test_v3_flutter_zone_does_not_thrash():
    """Funding oscillating in [0.2 bp, 0.8 bp] => few transitions.

    Re-entry threshold (0.6 bp) and ratchet (21-settlement min duration)
    should jointly prevent rapid in/out churn even when 30d MA wobbles
    around the boundaries.
    """
    # Alternating bursts: 30 settlements at 0.8 bp then 30 at 0.2 bp.
    # 30d MA averages ~0.5 bp, dipping below entry threshold most of time.
    funding_traj = []
    for i in range(400):
        funding_traj.append(0.00008 if (i // 30) % 2 == 0 else 0.00002)
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_flutter", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    # Expect very few transitions across 400 settlements (likely 0 — never
    # clears the 0.6 bp re-entry gate). Allow up to 5 to be tolerant of
    # small parameter shifts.
    assert art.n_transitions <= 5, (
        f"V3 thrashed in flutter zone: n_transitions={art.n_transitions}"
    )


def test_v3_validate_equity_curve_passes():
    """V3 produces an equity curve that passes the L3 contract validator."""
    funding_traj = [0.0003] * 200  # 3 bp/8h, well clear of all gates
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_curve_valid", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    # Raises ValueError if curve is invalid (NaN, zero equity, etc.)
    validate_equity_curve(art.equity_curve)


def test_v3_active_fraction_lower_than_v2_in_compression_regime():
    """Behavioral contract: V3 spends LESS time active than V2 when
    funding compresses (NOT a P&L claim — that depends on cost model
    calibration; this test asserts only the regime-detection effect).

    The whole point of V3 is reducing active-time in low-EV regimes.
    Active-fraction is the directly testable consequence.
    """
    # 200 settle high (clears V3 warmup + lets it re-enter) +
    # 300 settle compressed (V3 should detect and exit; V2 stays active).
    funding_traj = [0.0005] * 200 + [0.00001] * 300  # 0.1 bp/8h tail
    klines, funding = _build_inputs(funding_traj)

    art_v2 = run_carry_backtest(
        _SYMBOL, "v2_compress_act", CarryParams(),
        klines_df=klines, funding_df=funding,
    )
    art_v3 = run_carry_v3_backtest(
        _SYMBOL, "v3_compress_act", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )

    v2_w = (
        art_v2.legs.filter(pl.col("leg_id") == LEG_ID_SPOT_LONG)
        .sort("time")["weight"].to_numpy()
    )
    v3_w = _spot_weight_series(art_v3).to_numpy()
    v2_active_frac = (v2_w == 0.5).mean()
    v3_active_frac = (v3_w == 0.5).mean()

    # V3 must be active SUBSTANTIALLY less than V2 (≥10pp lower) in
    # this regime — the regime detector should clear meaningful
    # active-time, not differ by a single boundary bar. V2 stays
    # active essentially 100%; V3 should drop ≥40pp from warmup exit
    # alone in this 500-settlement run.
    assert v3_active_frac < v2_active_frac - 0.10, (
        f"V3 active_frac={v3_active_frac:.3f} not ≥10pp less than "
        f"V2 active_frac={v2_active_frac:.3f}"
    )


def test_v3_warmup_records_zero_funding_paid():
    """During warmup-exited window, perp leg funding_paid must be 0."""
    funding_traj = [0.0005] * 100
    klines, funding = _build_inputs(funding_traj)
    art = run_carry_v3_backtest(
        _SYMBOL, "test_v3_warmup_zero", CarryV3Params(),
        klines_df=klines, funding_df=funding,
    )
    # Filter to perp-short leg, first 80 settlement bars (well within warmup).
    # All funding_paid should be 0 since notional=0 (exited).
    perp_legs_warmup = (
        art.legs
        .filter((pl.col("leg_id") == 1) & (pl.col("funding_paid") != 0.0))
        .filter(pl.col("time") < (_BASE_TIME + timedelta(hours=80 * 8)))
    )
    assert perp_legs_warmup.height == 0, (
        "Warmup-exited window leaked nonzero funding_paid"
    )


# Suppress warnings about polars `cast` in older API; not a V3 concern.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
