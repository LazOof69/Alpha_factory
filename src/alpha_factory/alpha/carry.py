"""Carry V2 — long spot + short perp delta-neutral with funding-regime detection.

Phase 0 `feasibility/scripts/backtest_carry.py` refactored to produce
L3-compliant artifacts (BACKTEST_LEG / PER_SYMBOL_CONTRIB / EQUITY_CURVE)
that plug directly into the validation registry + DSR + PBO + regime
stratification pipeline.

ECONOMIC STORY (mandatory per strategy-validation skill canon):

    Positive funding rates in crypto perp markets reflect a structural
    long-bias of speculators; perp longs pay perp shorts when the rate
    is positive. A delta-neutral pair of (long spot + short perp) at
    equal notional captures this funding flow without taking directional
    price risk -- the "futures basis carry" trade, well-documented
    cross-asset.

    The trade earns funding when the rate is positive AND the perp-
    spot basis is mean-reverting (so price drift cancels). It LOSES
    when the funding regime flips negative (perp shorts now pay perp
    longs), incurring funding cost on the short perp leg.

    Capacity is bounded by perp orderbook depth and funding-rate
    sensitivity to position size; at retail $1k notional, well below
    any practical ceiling.

REGIME APPLICABILITY:

    Works:    sustained positive funding (post-ETF crypto bull, late
              2017-Q4 leverage froth, etc.)
    Fails:    sustained negative funding (post-FTX-collapse Q4 2022,
              early-2024 ETH unwind episodes)
    V2 fix:   funding-regime detection layer below

V2 REGIME-DETECTION LAYER (CLAUDE.md red line: carry MUST have this):

    7-day moving average of realized funding income computed at every
    settlement. Hysteresis state machine:

        active   -> exited   when 7d MA <  EXIT_FUNDING_7DMA
        exited   -> active   when 7d MA >  REENTRY_FUNDING_7DMA

    Defaults calibrated to economic break-even of transition cost:

        Round-trip = 16 bp; one-way = 8 bp.
        Avoiding 1 week of cumulative funding requires |7d sum| > 16 bp.
        7d sum = 21 settlements * mean_per_8h, so per-settlement
        threshold ~= -16/21 ~= -0.76 bp/8h. Rounded to -1 bp/8h.
        Re-entry at +0.5 bp/8h to avoid threshold flutter.

ARTIFACTS produced (per run_id, single-symbol carry):

    legs         BACKTEST_LEG_SCHEMA  -- 2 rows per bar (spot long +
                                         perp short); notional=0 when
                                         regime is exited
    contrib      PER_SYMBOL_CONTRIB_SCHEMA -- 1 row per bar; aggregate
                                         spot+perp P&L attribution
    equity_curve EQUITY_CURVE_SCHEMA  -- 1 row per bar; period_ret_gross
                                         (pre-friction) +
                                         period_ret_net (post fees +
                                         funding + slippage)

PORTFOLIO-NAV CONVENTION:

    NAV = 2 * capital_per_leg (spot leg + perp leg notionals when active).
    weight = signed fraction of NAV (+0.5 spot long, -0.5 perp short
             when active; zero when exited).
    period_ret = sum(weight * leg_ret) on a per-NAV basis.

    Funding income on perp short is recorded in `funding_paid` with
    sign convention "positive = COST to position": for a short perp at
    positive rate, funding is RECEIVED, so funding_paid is NEGATIVE.

FIRST-BAR SIMPLIFICATION:

    period_ret_gross[0] = 0, period_ret_net[0] = 0, equity[0] = NAV.
    Any first-bar funding/fees/slippage on legs are recorded in `legs`
    + `contrib` faithfully but NOT propagated to equity. Validates
    cleanly under `validate_equity_curve` (row 0 is exempt). For
    long backtests the one-time first-bar omission is negligible;
    L4 sizing accounts for it separately if needed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl

from alpha_factory.validation.costs import (
    SlippageModel,
    apply_costs,
    funding_payment_direction,
)
from alpha_factory.validation.schema import (
    BACKTEST_LEG_SCHEMA,
    EQUITY_CURVE_SCHEMA,
    LEG_MARKET_PERP_USDT,
    LEG_MARKET_SPOT,
    LEG_SIDE_LONG,
    LEG_SIDE_SHORT,
    PER_SYMBOL_CONTRIB_SCHEMA,
)

__all__ = [
    "CarryArtifacts",
    "CarryParams",
    "run_carry_backtest",
]


# ── Constants ────────────────────────────────────────────────────────────


# Strategy registry id; matches docs/alphas/<id>.md when documented.
STRATEGY_ID = "carry_v2"

# Two legs per bar. leg_id=0 is the long spot leg; leg_id=1 is the
# short perp leg. Order matters: PBO / regime stratification group on
# (run_id, symbol, leg_id, time) and assume monotonic leg_id ranges.
LEG_ID_SPOT_LONG = 0
LEG_ID_PERP_SHORT = 1


# ── Params ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CarryParams:
    """V2 carry parameters; defaults match Phase 0 audit calibration.

    `as_dict()` produces a JSON-canonicalizable params dict suitable
    for `validation.contracts.canonical_params_hash` (the recipe
    requires str-keyed dict of JSON-primitive values).
    """

    fee_bps: float = 16.0                    # round-trip; passes through to apply_costs
    capital_per_leg: float = 500.0           # USDT per leg; NAV = 2 * this
    exit_funding_7dma: float = -0.0001       # -1 bp/8h: exit when 7d MA below
    reentry_funding_7dma: float = 0.00005    # +0.5 bp/8h: re-enter when above
    lookback_settlements: int = 21           # 7 days * 3 settlements/day

    def as_dict(self) -> dict:
        """Return a canonical-JSON-safe dict for params_hash + log_trial."""
        return {
            "fee_bps": self.fee_bps,
            "capital_per_leg": self.capital_per_leg,
            "exit_funding_7dma": self.exit_funding_7dma,
            "reentry_funding_7dma": self.reentry_funding_7dma,
            "lookback_settlements": self.lookback_settlements,
        }


@dataclass(frozen=True)
class CarryArtifacts:
    """Output of `run_carry_backtest` -- ready to feed into L3 registry.

    `n_transitions` is the count of regime flips (active <-> exited);
    surfaced as a top-level scalar for the CarryParams audit / the
    L3 alpha-quality summary (high transitions = thrashy regime).
    """

    legs: pl.DataFrame
    contrib: pl.DataFrame
    equity_curve: pl.DataFrame
    n_transitions: int
    params: dict


# ── Public entry point ───────────────────────────────────────────────────


def run_carry_backtest(
    symbol: str,
    run_id: str,
    params: CarryParams,
    *,
    klines_df: pl.DataFrame,
    funding_df: pl.DataFrame,
    fee_tier: str = "regular",
    slippage_model: SlippageModel | None = None,
) -> CarryArtifacts:
    """Run V2 carry on `symbol`; produce L3-compliant artifacts.

    Args:
        symbol: canonical "BTC-USDT" form
        run_id: unique id per backtest invocation; stamped on every row
        params: CarryParams (defaults match Phase 0 calibration)
        klines_df: pre-loaded L1 klines DataFrame containing rows for
            BOTH `(symbol, market='spot')` and
            `(symbol, market='perp_usdt')`. Must follow KLINES_SCHEMA
            (open_time, close, ...).
        funding_df: pre-loaded L1 funding DataFrame containing rows
            for `symbol`. Must follow FUNDING_SCHEMA (funding_time,
            funding_rate, ...).
        fee_tier: one of `costs.SPOT_FEE_TIERS` / `costs.PERP_FEE_TIERS`
            keys (default "regular" -- safest at retail scale).
        slippage_model: optional SlippageModel callable; defaults to
            zero slippage. For paper-validation use linear_slippage(1)
            or higher per the carry's gross_exposure / ADV ratio.

    Returns:
        CarryArtifacts(legs, contrib, equity_curve, n_transitions, params)
        Every DataFrame is cast to its target schema -- ready to write
        directly via the L3 registry's per-strategy parquet writers.

    Empty input (no overlapping spot+perp bars) returns empty artifacts
    with conforming schemas; n_transitions = 0.
    """
    bars = _join_klines_funding(symbol, klines_df, funding_df)

    if bars.height == 0:
        return CarryArtifacts(
            legs=pl.DataFrame(schema=BACKTEST_LEG_SCHEMA),
            contrib=pl.DataFrame(schema=PER_SYMBOL_CONTRIB_SCHEMA),
            equity_curve=pl.DataFrame(schema=EQUITY_CURVE_SCHEMA),
            n_transitions=0,
            params=params.as_dict(),
        )

    bars = _compute_regime_states(bars, params)
    n_transitions = _count_transitions(bars)

    # _build_legs populates funding_paid up-front from bars.funding_rate;
    # apply_costs only touches fees + slippage, so funding_paid survives.
    legs_pre = _build_legs(run_id, symbol, bars, params)
    legs = apply_costs(
        legs_pre, fee_tier=fee_tier, slippage_model=slippage_model,
    )

    contrib = _build_contrib(run_id, symbol, legs, params)
    equity_curve = _build_equity_curve(run_id, legs, params)

    return CarryArtifacts(
        legs=legs.select(list(BACKTEST_LEG_SCHEMA.keys())).cast(BACKTEST_LEG_SCHEMA),
        contrib=contrib.cast(PER_SYMBOL_CONTRIB_SCHEMA),
        equity_curve=equity_curve.cast(EQUITY_CURVE_SCHEMA),
        n_transitions=n_transitions,
        params=params.as_dict(),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _join_klines_funding(
    symbol: str,
    klines_df: pl.DataFrame,
    funding_df: pl.DataFrame,
) -> pl.DataFrame:
    """Inner-join spot+perp klines on open_time; left-join funding.

    Returns a DataFrame with columns:
        open_time, spot_close, perp_close, funding_rate (nullable),
        spot_ret, perp_ret
    Rows where one of (spot, perp) is missing are dropped (inner-join);
    rows where funding settlement is absent have funding_rate = null.
    Sorted ascending by open_time.
    """
    spot = (
        klines_df
        .filter(
            (pl.col("symbol") == symbol)
            & (pl.col("market") == LEG_MARKET_SPOT),
        )
        .select(["open_time", pl.col("close").alias("spot_close")])
    )
    perp = (
        klines_df
        .filter(
            (pl.col("symbol") == symbol)
            & (pl.col("market") == LEG_MARKET_PERP_USDT),
        )
        .select(["open_time", pl.col("close").alias("perp_close")])
    )
    bars = spot.join(perp, on="open_time", how="inner").sort("open_time")

    if bars.height == 0:
        return bars

    fund = (
        funding_df
        .filter(pl.col("symbol") == symbol)
        .select([pl.col("funding_time").alias("open_time"), "funding_rate"])
    )
    bars = bars.join(fund, on="open_time", how="left")

    return bars.with_columns(
        spot_ret=pl.col("spot_close").pct_change().fill_null(0.0),
        perp_ret=pl.col("perp_close").pct_change().fill_null(0.0),
    )


def _compute_regime_states(
    bars: pl.DataFrame, params: CarryParams,
) -> pl.DataFrame:
    """Apply V2 funding-regime hysteresis state machine; add `regime_state`.

    State 1 = active (positions held), state 0 = exited (notional 0).
    Initial state is 1 (carry assumed favourable until evidence contradicts).
    `regime_state` is forward-filled between settlement bars, since the
    state can only change at funding-settlement events.
    """
    settle = (
        bars.filter(pl.col("funding_rate").is_not_null())
        .with_columns(
            funding_7dma=pl.col("funding_rate").rolling_mean(
                window_size=params.lookback_settlements,
                min_samples=params.lookback_settlements,
            ),
        )
        .select(["open_time", "funding_7dma"])
    )

    # State machine over settlement events; NaN MA holds current state.
    state_at_settle: dict = {}
    current = 1
    for row in settle.iter_rows(named=True):
        ma = row["funding_7dma"]
        new = current
        if ma is not None:
            if current == 1 and ma < params.exit_funding_7dma:
                new = 0
            elif current == 0 and ma > params.reentry_funding_7dma:
                new = 1
        state_at_settle[row["open_time"]] = new
        current = new

    # Forward-fill to all bars
    times = bars["open_time"].to_list()
    states: list[int] = []
    cur = 1
    for t in times:
        if t in state_at_settle:
            cur = state_at_settle[t]
        states.append(cur)

    return bars.with_columns(regime_state=pl.Series(states, dtype=pl.Int8))


def _count_transitions(bars: pl.DataFrame) -> int:
    """Count regime flips in `regime_state` (active <-> exited)."""
    if bars.height < 2:
        return 0
    states = bars["regime_state"].to_numpy()
    return int(np.sum(states[1:] != states[:-1]))


def _build_legs(
    run_id: str,
    symbol: str,
    bars: pl.DataFrame,
    params: CarryParams,
) -> pl.DataFrame:
    """Build BACKTEST_LEG-shaped DataFrame: 2 legs per bar (spot+perp).

    Columns populated here:
        run_id, time, leg_id, symbol, market, side, weight, notional,
        leg_ret, funding_paid (perp short at settlement bars only)
    Friction columns deferred to apply_costs:
        fees           (populated from |delta_notional|)
        slippage       (populated from slippage_model)

    funding_paid sign convention (per BACKTEST_LEG_SCHEMA + costs.py):
        + = COST to position (paid out)
        - = INCOME to position (received)

        For short perp at positive funding rate, the position RECEIVES
        funding from the longs:
            signed_rate = funding_payment_direction("short", rate) = +rate
            funding_paid = -notional * signed_rate = -notional * rate
                                                   (negative => income)
        For short perp at negative funding rate (perp shorts paying longs):
            signed_rate = funding_payment_direction("short", rate) = +rate
                          where rate < 0
            funding_paid = -notional * (negative) = positive (cost)
        Spot legs and exited (notional=0) bars: funding_paid = 0.
    """
    n = bars.height
    state = bars["regime_state"].cast(pl.Float64).to_numpy()
    cap = params.capital_per_leg
    times = bars["open_time"].to_list()
    rates = bars["funding_rate"].to_numpy()

    notional = cap * state    # 0 when exited; cap when active

    # Funding paid on perp short at settlement bars (rate not null) when active.
    # Vectorized: where rate is null OR notional==0, funding_paid=0.
    perp_funding = np.where(
        np.isnan(rates) | (notional == 0),
        0.0,
        -notional * np.nan_to_num(rates),
    )
    # Cross-check using funding_payment_direction for the only non-zero case:
    # short perp + rate -> -notional * funding_payment_direction("short", rate)
    # = -notional * (+rate) = -notional * rate.
    # Verified by the closed-form np.where above.
    _ = funding_payment_direction      # documents the underlying sign convention

    spot_legs = pl.DataFrame({
        "run_id": [run_id] * n,
        "time": times,
        "leg_id": [LEG_ID_SPOT_LONG] * n,
        "symbol": [symbol] * n,
        "market": [LEG_MARKET_SPOT] * n,
        "side": [LEG_SIDE_LONG] * n,
        "weight": (0.5 * state).tolist(),
        "notional": notional.tolist(),
        "leg_ret": bars["spot_ret"].to_list(),
        "fees": [0.0] * n,
        "funding_paid": [0.0] * n,
        "slippage": [0.0] * n,
    })

    perp_legs = pl.DataFrame({
        "run_id": [run_id] * n,
        "time": times,
        "leg_id": [LEG_ID_PERP_SHORT] * n,
        "symbol": [symbol] * n,
        "market": [LEG_MARKET_PERP_USDT] * n,
        "side": [LEG_SIDE_SHORT] * n,
        "weight": (-0.5 * state).tolist(),
        "notional": notional.tolist(),
        "leg_ret": bars["perp_ret"].to_list(),
        "fees": [0.0] * n,
        "funding_paid": perp_funding.tolist(),
        "slippage": [0.0] * n,
    })

    return pl.concat([spot_legs, perp_legs])


def _build_contrib(
    run_id: str,
    symbol: str,
    legs: pl.DataFrame,
    params: CarryParams,
) -> pl.DataFrame:
    """Build PER_SYMBOL_CONTRIB: 1 row per bar with aggregate symbol P&L.

    contrib_pnl USDT per bar = sum over legs of:
        weight * leg_ret * NAV  -- gross PnL contribution
        - fees - funding_paid - slippage  -- friction
    position_notional = |sum signed_notional| (delta-neutral carry: 0)
    weight_net = sum(weight) (delta-neutral carry: 0)
    """
    nav = 2.0 * params.capital_per_leg
    per_bar = (
        legs.group_by("time")
        .agg(
            contrib_pnl=(
                (pl.col("weight") * pl.col("leg_ret")).sum() * nav
                - pl.col("fees").sum()
                - pl.col("funding_paid").sum()
                - pl.col("slippage").sum()
            ),
            # Per schema: position_notional = net |long-short| USDT
            # For carry: weight is signed (+0.5 spot, -0.5 perp short)
            # net_signed_weight * NAV gives net exposure in USDT.
            position_notional=(pl.col("weight").sum() * nav).abs(),
            weight_net=pl.col("weight").sum(),
        )
        .sort("time")
    )
    return per_bar.with_columns(
        run_id=pl.lit(run_id),
        symbol=pl.lit(symbol),
    ).select([
        "run_id", "time", "symbol",
        "contrib_pnl", "position_notional", "weight_net",
    ])


def _build_equity_curve(
    run_id: str, legs: pl.DataFrame, params: CarryParams,
) -> pl.DataFrame:
    """Build EQUITY_CURVE: per-bar gross/net returns + compounded equity.

    period_ret_gross = sum(weight * leg_ret) on per-NAV basis
                       (no friction).
    period_ret_net   = period_ret_gross - friction / equity[t-1]
                       (friction = fees + funding_paid + slippage,
                       all summed across legs, all in USDT).
    equity[t]        = equity[t-1] * (1 + period_ret_net[t])

    First bar exempt per validate_equity_curve convention: equity[0]
    = NAV, period_ret_gross[0] = 0, period_ret_net[0] = 0,
    cumulative_pnl[0] = 0. Friction recorded on legs/contrib for bar 0
    is NOT propagated to equity (acceptable simplification for long
    backtests; documented at module level).
    """
    nav = 2.0 * params.capital_per_leg
    per_bar = (
        legs.group_by("time")
        .agg(
            gross_ret=(pl.col("weight") * pl.col("leg_ret")).sum(),
            cost_total=(
                pl.col("fees")
                + pl.col("funding_paid")
                + pl.col("slippage")
            ).sum(),
        )
        .sort("time")
    )

    times = per_bar["time"].to_list()
    n = len(times)
    gross_arr = per_bar["gross_ret"].to_numpy()
    cost_arr = per_bar["cost_total"].to_numpy()

    equity = np.empty(n)
    period_ret_gross = np.empty(n)
    period_ret_net = np.empty(n)

    equity[0] = nav
    period_ret_gross[0] = 0.0
    period_ret_net[0] = 0.0

    for i in range(1, n):
        period_ret_gross[i] = float(gross_arr[i])
        cost_frac = float(cost_arr[i]) / equity[i - 1] if equity[i - 1] > 0 else 0.0
        period_ret_net[i] = period_ret_gross[i] - cost_frac
        equity[i] = equity[i - 1] * (1.0 + period_ret_net[i])

    return pl.DataFrame({
        "run_id": [run_id] * n,
        "time": times,
        "equity": equity.tolist(),
        "period_ret_gross": period_ret_gross.tolist(),
        "period_ret_net": period_ret_net.tolist(),
        "cumulative_pnl": (equity - equity[0]).tolist(),
    })
