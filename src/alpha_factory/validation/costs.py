"""Trading-cost models -- fees + slippage for backtest validation.

This module ONLY models fees and slippage. NOT a friction.py: the
funding_payment_direction helper returns a SIGN ONLY; it does not
mutate any P&L state. Funding P&L is the strategy module's
responsibility (see CLAUDE.md "carry strategy in production without
funding-regime detection layer" red line for why funding logic is
alpha-specific, not framework-specific).

Public API:

    SPOT_FEE_TIERS / PERP_FEE_TIERS
        Hardcoded Binance fee schedule snapshots (as-of comment in
        the constant block). Re-confirm before any live deployment.

    apply_costs(positions, *, fee_tier="regular", slippage_model=None)
        Compute fees + slippage on per-bar absolute changes in
        `notional` within each (run_id, symbol, leg_id) group,
        populate `fees` and `slippage` columns of a BACKTEST_LEG-
        shaped DataFrame.

    funding_payment_direction(side, rate) -> float
        Signed funding rate from the position's perspective. Caller
        multiplies by notional to get USDT flow; this function does
        NOT mutate any P&L state.

    zero_slippage(), linear_slippage(coefficient_bps=1.0)
        Slippage-model factories. SlippageModel is the type alias
        Callable[[float], float] -- |delta_notional| in USDT to
        slippage cost in USDT.

Conservative default: TAKER fee rate. Maker rebates require live
order-type info not preserved in the BACKTEST_LEG schema; assuming
maker would over-state strategy returns. For paper-trading and live
deployment, the L5 layer will consult the actual order type per fill.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

import polars as pl

from alpha_factory.validation.schema import (
    LEG_MARKET_PERP_USDT,
    LEG_MARKET_SPOT,
    LEG_SIDE_LONG,
    LEG_SIDE_SHORT,
)

__all__ = [
    "PERP_FEE_TIERS",
    "SPOT_FEE_TIERS",
    "SlippageModel",
    "apply_costs",
    "funding_payment_direction",
    "linear_slippage",
    "zero_slippage",
]


# ── Type aliases ─────────────────────────────────────────────────────────


# Slippage model: takes |delta_notional| in USDT, returns slippage cost
# in USDT. Models are pure functions of trade size; for size-independent
# rates use linear_slippage.
SlippageModel: TypeAlias = Callable[[float], float]


# ── Binance fee tiers (as of 2026-Q2; re-confirm before live) ────────────
#
# Source: Binance public fee schedule. Spot rates are flat per tier;
# perpetual rates differ between maker and taker. We expose ONLY the
# taker rate via apply_costs because the BACKTEST_LEG schema does not
# carry the order-type discrimination needed to know if a particular
# fill was maker or taker. Maker rebates can be modeled separately at
# L5 paper-trading time when fill type is observed.
#
# Each tier dict has: {"maker": rate_decimal, "taker": rate_decimal}.
# Rates are decimal -- e.g. 0.001 = 10 bp.

# Spot fees (regular tier as of 2026-Q2: 10/10 bp; tiers from
# Binance.com/en/fee/schedule).
SPOT_FEE_TIERS: dict[str, dict[str, float]] = {
    "regular": {"maker": 0.0010, "taker": 0.0010},  # default retail
    "vip_1":   {"maker": 0.00090, "taker": 0.0010},
    "vip_2":   {"maker": 0.00080, "taker": 0.0010},
    "vip_3":   {"maker": 0.00042, "taker": 0.0006},
    "vip_4":   {"maker": 0.00042, "taker": 0.00054},
    "vip_5":   {"maker": 0.00036, "taker": 0.00048},
}

# USDT-M Perpetual fees (regular tier 1.8/4.5 bp; VIP discounts on
# both maker and taker).
PERP_FEE_TIERS: dict[str, dict[str, float]] = {
    "regular": {"maker": 0.00018, "taker": 0.00045},  # default retail
    "vip_1":   {"maker": 0.00016, "taker": 0.00040},
    "vip_2":   {"maker": 0.00014, "taker": 0.00035},
    "vip_3":   {"maker": 0.00012, "taker": 0.00032},
    "vip_4":   {"maker": 0.00010, "taker": 0.00030},
    "vip_5":   {"maker": 0.00008, "taker": 0.00027},
}


# ── Slippage-model factories ─────────────────────────────────────────────


def zero_slippage() -> SlippageModel:
    """Slippage model that always returns 0 USDT (frictionless)."""
    def _model(delta_notional: float) -> float:
        _ = delta_notional   # signature placeholder
        return 0.0
    return _model


def linear_slippage(coefficient_bps: float = 1.0) -> SlippageModel:
    """Slippage model: slippage = coefficient_bps / 1e4 * |delta_notional|.

    1 bp default approximates retail-scale linear impact on liquid
    crypto pairs. For thin pairs at retail size, raise to 3-5 bp.
    """
    if coefficient_bps < 0:
        raise ValueError(
            f"coefficient_bps must be non-negative, got {coefficient_bps}",
        )
    rate = coefficient_bps / 10_000.0

    def _model(delta_notional: float) -> float:
        return abs(delta_notional) * rate
    return _model


# ── apply_costs ──────────────────────────────────────────────────────────


_REQUIRED_COLS: tuple[str, ...] = (
    "run_id", "symbol", "market", "leg_id", "time", "notional",
)


def _taker_fee_rate(market: str, fee_tier: str) -> float:
    if market == LEG_MARKET_SPOT:
        table = SPOT_FEE_TIERS
    elif market == LEG_MARKET_PERP_USDT:
        table = PERP_FEE_TIERS
    else:
        raise ValueError(
            f"market must be {LEG_MARKET_SPOT!r} or {LEG_MARKET_PERP_USDT!r}, "
            f"got {market!r}",
        )
    if fee_tier not in table:
        raise KeyError(
            f"fee_tier {fee_tier!r} not in {market} table; "
            f"available: {sorted(table.keys())}",
        )
    return table[fee_tier]["taker"]


def apply_costs(
    positions: pl.DataFrame,
    *,
    fee_tier: str = "regular",
    slippage_model: SlippageModel | None = None,
) -> pl.DataFrame:
    """Populate `fees` and `slippage` columns based on per-bar notional changes.

    Per (run_id, symbol, leg_id) group, sorted by time, the trade size
    each bar is `|notional[t] - notional[t-1]|`. The first bar of each
    group has trade size 0 by convention (no prior position to diff
    against). For each row:

        fees = trade_size * taker_fee_rate(market, fee_tier)
        slippage = slippage_model(trade_size) if slippage_model else 0

    Both fees and slippage are NON-NEGATIVE costs in USDT. Caller is
    responsible for subtracting them from gross returns when computing
    `period_ret_net` for the EQUITY_CURVE.

    Args:
        positions: DataFrame with at minimum the columns in
            `_REQUIRED_COLS`. Must contain rows from a SINGLE market
            value per (symbol, leg_id) group; mixing spot/perp on the
            same leg_id is not supported (the fee rate is per-row from
            the row's own `market` column).
        fee_tier: key into SPOT_FEE_TIERS / PERP_FEE_TIERS
        slippage_model: callable USDT-trade-size -> USDT-slippage-cost.
            Defaults to zero (frictionless) when None.

    Returns:
        New DataFrame with the same row order as input plus `fees` and
        `slippage` columns overwritten. All other columns pass through.

    Raises:
        ValueError on missing required column or unsupported market
        KeyError on unknown fee_tier
    """
    missing = [c for c in _REQUIRED_COLS if c not in positions.columns]
    if missing:
        raise ValueError(f"positions missing required columns: {missing}")

    if positions.height == 0:
        # Preserve schema; just add the cost columns if not present.
        out = positions
        if "fees" not in out.columns:
            out = out.with_columns(fees=pl.lit(0.0))
        if "slippage" not in out.columns:
            out = out.with_columns(slippage=pl.lit(0.0))
        return out

    slip = slippage_model if slippage_model is not None else zero_slippage()

    sorted_pos = positions.sort(["run_id", "symbol", "leg_id", "time"])
    # Trade size = |notional[t] - notional[t-1]| within group; first row
    # of each group gets 0 (fill_null after diff).
    sorted_pos = sorted_pos.with_columns(
        _trade_size=pl.col("notional")
            .diff()
            .over(["run_id", "symbol", "leg_id"])
            .abs()
            .fill_null(0.0),
    )

    # Compute fee rate per row from market column (vectorized via map_elements
    # over a small set of unique markets is overkill; just use when/then).
    sorted_pos = sorted_pos.with_columns(
        _fee_rate=pl.when(pl.col("market") == LEG_MARKET_SPOT)
            .then(SPOT_FEE_TIERS[fee_tier]["taker"]
                  if fee_tier in SPOT_FEE_TIERS else float("nan"))
            .when(pl.col("market") == LEG_MARKET_PERP_USDT)
            .then(PERP_FEE_TIERS[fee_tier]["taker"]
                  if fee_tier in PERP_FEE_TIERS else float("nan"))
            .otherwise(float("nan")),
    )
    # Reject unsupported markets / unknown tier loud.
    if sorted_pos["_fee_rate"].is_nan().any():
        bad_markets = (
            sorted_pos.filter(pl.col("_fee_rate").is_nan())["market"]
            .unique()
            .to_list()
        )
        raise ValueError(
            f"unsupported market or unknown fee_tier {fee_tier!r}; "
            f"offending markets: {bad_markets}",
        )

    # Compute fees vectorized; slippage requires the user-supplied
    # callable, applied per-row.
    sorted_pos = sorted_pos.with_columns(
        fees=pl.col("_trade_size") * pl.col("_fee_rate"),
    )
    slippage_values = [
        slip(float(t)) for t in sorted_pos["_trade_size"].to_list()
    ]
    sorted_pos = sorted_pos.with_columns(
        slippage=pl.Series("slippage", slippage_values, dtype=pl.Float64),
    )
    return sorted_pos.drop(["_trade_size", "_fee_rate"])


# ── funding_payment_direction ────────────────────────────────────────────


def funding_payment_direction(side: str, rate: float) -> float:
    """Signed funding rate from the position's perspective.

    Convention: funding flows from longs to shorts when rate > 0.

        side="long",  rate > 0  ->  long PAYS  funding -> negative
        side="long",  rate < 0  ->  long EARNS funding -> positive
        side="short", rate > 0  ->  short EARNS funding -> positive
        side="short", rate < 0  ->  short PAYS  funding -> negative

    Returns the signed RATE from the position's perspective (caller
    multiplies by notional to obtain USDT flow). This function does
    NOT mutate any P&L state -- it is a pure sign helper. The strategy
    module computes funding P&L itself (see module docstring rationale).

    Args:
        side: "long" or "short"
        rate: funding rate as a decimal (e.g. 0.0001 for 1 bp/8h)

    Returns:
        +rate if the position earns, -rate if it pays.

    Raises:
        ValueError if side is not "long" or "short"
    """
    if side == LEG_SIDE_LONG:
        return -rate
    if side == LEG_SIDE_SHORT:
        return +rate
    raise ValueError(
        f"side must be {LEG_SIDE_LONG!r} or {LEG_SIDE_SHORT!r}, got {side!r}",
    )
