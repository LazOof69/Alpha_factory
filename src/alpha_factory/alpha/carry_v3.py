"""Carry V3 — V2 + funding-compression detector + ratchet guard.

V3 closes the CLAUDE.md red-line gap that V2 only partially met. V2's
hysteresis state machine reacts to ABSOLUTE NEGATIVE funding (caught
COVID, FTX, ETH-unwind episodes correctly). V2 is BLIND to the
post-ETF threat: funding compresses TOWARD ZERO without ever turning
negative, and the carry slowly bleeds round-trip fees against thin
income. V3 adds a second exit path that detects this regime.

ECONOMIC STORY:

    Identical to V2 (long spot + short perp delta-neutral pair on the
    funding cash flow). The economic value is unchanged; only the
    risk-management gate that decides when to be in or out of the
    trade is tightened.

V3 ADDITIONS to V2 state machine:

    Every settlement bar:
        7dma      = rolling_mean(funding_rate, 21 settlements)
                                                      [V2 inherited]
        30dma_abs = abs(rolling_mean(funding_rate, 90 settlements))
                                                      [V3 NEW]

    Transition logic:
        if active:
            exit if 7dma < EXIT_FUNDING_7DMA (-1 bp/8h)        [V2]
            OR exit if 30dma_abs < EXIT_COMPRESSION_30DMA      [V3]
        if exited:
            re-enter only if BOTH:
                7dma > REENTRY_FUNDING_7DMA (+0.5 bp/8h)       [V2]
                AND 30dma_abs > REENTRY_COMPRESSION_30DMA      [V3]

    Ratchet guard:
        no transition within `min_state_duration_settlements`
        (default 21 = 7 days) of the previous transition. Prevents
        7d/30d boundary oscillation from causing rapid in/out churn.

    Initial state:
        exited (NOT active like V2). Warmup conservatism — the strategy
        is out of the market until 30dma_abs is computable AND its
        re-entry condition passes. Cost: forfeits ~30 days of carry
        at the start of any backtest (< 1.2% of a 6.6-year sample).

THRESHOLD DERIVATION (1-round adversarial critique adjustments):

    Phase A audit caveat suggested 0.5 bp/8h. Critique re-derived:

        break_even_per_settlement = round_trip_fee / expected_hold
                                  = 16 bp / 180 settlements (60d)
                                  = 0.089 bp/8h
        Threshold should be ABOVE break-even (so V3 exits BEFORE
        EV turns negative) but not so high that it exits in
        marginally-profitable regimes. Initial = 0.3 bp/8h:
            - well above 0.089 break-even (3.4x cushion)
            - below the original 0.5 bp suggestion (less aggressive)
            - subject to PBO sweep over {0.1, 0.3, 0.5, 0.8} bp
              in Phase B before the threshold is locked.

    Asymmetric hysteresis: 0.6 bp re-entry vs 0.3 bp exit. The 0.3 bp
    gap matches V2's [-1, +0.5] absolute hysteresis spread; both
    asymmetries push the strategy toward staying flat when uncertain.

PORTFOLIO POSITION (per validated_alphas.yaml convention):

    V3 is intended to SUPERSEDE V2 once it passes L3. After validation:
        - V2 status -> "superseded_by: carry_v3" (registry tagged)
        - L4 HRP only sees V3 (V2/V3 correlation expected >= 0.9 since
          they share the long-spot/short-perp legs and the V2 exit path;
          the actual figure is to be measured in Phase B portfolio
          construction work and recorded in validated_alphas.yaml).
        - V2 backtest record retained for audit / reproducibility.

ARTIFACTS:

    Returns CarryArtifacts (same dataclass as V2). The new params
    fields are encoded in `params` dict (canonical_params_hash
    auto-differentiates V2 vs V3 at the registry level).

REUSED HELPERS (from carry.py):

    join_klines_funding, count_transitions, build_legs, build_contrib,
    build_equity_curve. Only the regime state machine is rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from alpha_factory.alpha.carry import (
    CarryArtifacts,
    build_contrib,
    build_equity_curve,
    build_legs,
    count_transitions,
    join_klines_funding,
)
from alpha_factory.validation.costs import SlippageModel, apply_costs
from alpha_factory.validation.schema import (
    BACKTEST_LEG_SCHEMA,
    EQUITY_CURVE_SCHEMA,
    PER_SYMBOL_CONTRIB_SCHEMA,
)

__all__ = [
    "STRATEGY_ID",
    "CarryV3Params",
    "run_carry_v3_backtest",
]


# ── Constants ────────────────────────────────────────────────────────────


# Strategy registry id; matches docs/alphas/carry_v3.md when validated.
STRATEGY_ID = "carry_v3"


# ── Params ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CarryV3Params:
    """V3 carry parameters; V2 fields preserved + 4 new V3 fields.

    `as_dict()` produces a canonical-JSON-safe dict suitable for
    `validation.contracts.canonical_params_hash`. The new V3 fields
    (`exit_compression_30dma`, `reentry_compression_30dma`,
    `compression_lookback_settlements`, `min_state_duration_settlements`)
    cause params_hash to differ from V2 even when V2-inherited fields
    happen to match — registry de-dups V2 vs V3 cleanly.

    Default thresholds are the post-1-round-critique values; subject
    to PBO sweep in Phase B before being treated as final.
    """

    # V2-inherited (semantics unchanged)
    fee_bps: float = 16.0
    capital_per_leg: float = 500.0
    exit_funding_7dma: float = -0.0001       # -1 bp/8h: V2 exit threshold
    reentry_funding_7dma: float = 0.00005    # +0.5 bp/8h: V2 re-entry
    lookback_settlements: int = 21           # 7d (3 settlements/day)

    # V3 NEW (initial; PBO sweep pending)
    exit_compression_30dma: float = 0.00003       # 0.3 bp/8h ABS
    reentry_compression_30dma: float = 0.00006    # 0.6 bp/8h ABS (0.3 bp gap)
    compression_lookback_settlements: int = 90    # 30d
    min_state_duration_settlements: int = 21      # 7d ratchet guard

    def as_dict(self) -> dict:
        """Return a canonical-JSON-safe dict for params_hash + log_trial."""
        return {
            "fee_bps": self.fee_bps,
            "capital_per_leg": self.capital_per_leg,
            "exit_funding_7dma": self.exit_funding_7dma,
            "reentry_funding_7dma": self.reentry_funding_7dma,
            "lookback_settlements": self.lookback_settlements,
            "exit_compression_30dma": self.exit_compression_30dma,
            "reentry_compression_30dma": self.reentry_compression_30dma,
            "compression_lookback_settlements": (
                self.compression_lookback_settlements
            ),
            "min_state_duration_settlements": (
                self.min_state_duration_settlements
            ),
        }


# ── Public entry point ───────────────────────────────────────────────────


def run_carry_v3_backtest(
    symbol: str,
    run_id: str,
    params: CarryV3Params,
    *,
    klines_df: pl.DataFrame,
    funding_df: pl.DataFrame,
    fee_tier: str = "regular",
    slippage_model: SlippageModel | None = None,
) -> CarryArtifacts:
    """Run V3 carry on `symbol`; produce L3-compliant artifacts.

    Mirrors `run_carry_backtest` (V2) signature exactly. Only difference
    is `params` type (`CarryV3Params` vs V2 `CarryParams`). Output
    `CarryArtifacts.params` dict carries the 4 extra V3 fields, so the
    L3 registry's canonical_params_hash distinguishes V2 from V3 even
    when V2-inherited fields share values.

    Empty input (no overlapping spot+perp bars) returns conforming
    empty artifacts; n_transitions = 0.
    """
    bars = join_klines_funding(symbol, klines_df, funding_df)

    if bars.height == 0:
        return CarryArtifacts(
            legs=pl.DataFrame(schema=BACKTEST_LEG_SCHEMA),
            contrib=pl.DataFrame(schema=PER_SYMBOL_CONTRIB_SCHEMA),
            equity_curve=pl.DataFrame(schema=EQUITY_CURVE_SCHEMA),
            n_transitions=0,
            params=params.as_dict(),
        )

    bars = _compute_regime_states_v3(bars, params)
    n_transitions = count_transitions(bars)

    legs_pre = build_legs(
        run_id, symbol, bars, capital_per_leg=params.capital_per_leg,
    )
    legs = apply_costs(
        legs_pre, fee_tier=fee_tier, slippage_model=slippage_model,
    )

    contrib = build_contrib(
        run_id, symbol, legs, capital_per_leg=params.capital_per_leg,
    )
    equity_curve = build_equity_curve(
        run_id, legs, capital_per_leg=params.capital_per_leg,
    )

    return CarryArtifacts(
        legs=legs.select(list(BACKTEST_LEG_SCHEMA.keys())).cast(BACKTEST_LEG_SCHEMA),
        contrib=contrib.cast(PER_SYMBOL_CONTRIB_SCHEMA),
        equity_curve=equity_curve.cast(EQUITY_CURVE_SCHEMA),
        n_transitions=n_transitions,
        params=params.as_dict(),
    )


# ── V3 state machine ─────────────────────────────────────────────────────


def _compute_regime_states_v3(
    bars: pl.DataFrame, params: CarryV3Params,
) -> pl.DataFrame:
    """V3 hysteresis state machine + funding-compression detector.

    State = 1 (active, positions held) or 0 (exited, notional 0).

    Initial state = 0 (exited). Differs from V2 (initial active);
    reflects V3's warmup conservatism — strategy is OUT of the market
    until 30dma_abs is computable AND its re-entry condition passes.

    Per settlement bar (rows where funding_rate is not null):

        7dma      = rolling_mean(funding_rate, lookback_settlements)
        30dma_abs = abs(rolling_mean(funding_rate,
                                     compression_lookback_settlements))

        if current == 1 (active):
            new = 0 if 7dma < EXIT_FUNDING_7DMA            [V2 path]
                   OR 30dma_abs < EXIT_COMPRESSION_30DMA   [V3 NEW]
        elif current == 0 (exited):
            new = 1 only if BOTH:
                  7dma > REENTRY_FUNDING_7DMA              [V2 path]
                  AND 30dma_abs > REENTRY_COMPRESSION_30DMA [V3 NEW]

        Ratchet guard: if (new != current) AND (settlements since last
        transition < min_state_duration_settlements), block the
        transition (force new = current).

    When 7dma or 30dma_abs is None (warmup), the if-branch is skipped
    and state holds. The ratchet counter still ticks during warmup, so
    the first eligible transition after warmup is not artificially
    blocked.

    Forward-fills state to non-settlement bars (state can only change
    at settlements).
    """
    settle = (
        bars.filter(pl.col("funding_rate").is_not_null())
        .with_columns(
            funding_7dma=pl.col("funding_rate").rolling_mean(
                window_size=params.lookback_settlements,
                min_samples=params.lookback_settlements,
            ),
            funding_30dma=pl.col("funding_rate").rolling_mean(
                window_size=params.compression_lookback_settlements,
                min_samples=params.compression_lookback_settlements,
            ),
        )
        .select(["open_time", "funding_7dma", "funding_30dma"])
    )

    state_at_settle: dict = {}
    current = 0   # V3 warmup-exited (V2 used 1; V3 chose conservatism)
    settlements_since_last_transition = 0
    min_dur = params.min_state_duration_settlements

    for row in settle.iter_rows(named=True):
        ma7 = row["funding_7dma"]
        ma30 = row["funding_30dma"]
        ma30_abs = abs(ma30) if ma30 is not None else None
        new = current
        settlements_since_last_transition += 1

        # Both MAs available => evaluate state machine; otherwise hold.
        if ma7 is not None and ma30_abs is not None:
            if current == 1:
                exit_negative = ma7 < params.exit_funding_7dma
                exit_compression = ma30_abs < params.exit_compression_30dma
                if exit_negative or exit_compression:
                    new = 0
            else:  # current == 0
                reenter_funding = ma7 > params.reentry_funding_7dma
                reenter_compression = ma30_abs > params.reentry_compression_30dma
                if reenter_funding and reenter_compression:
                    new = 1

        # Ratchet guard: block transitions too close to the prior one.
        if new != current and settlements_since_last_transition < min_dur:
            new = current

        if new != current:
            settlements_since_last_transition = 0

        state_at_settle[row["open_time"]] = new
        current = new

    # Forward-fill to all bars; warmup bars before first settlement = 0.
    times = bars["open_time"].to_list()
    states: list[int] = []
    cur = 0   # V3 warmup-exited
    for t in times:
        if t in state_at_settle:
            cur = state_at_settle[t]
        states.append(cur)

    return bars.with_columns(regime_state=pl.Series(states, dtype=pl.Int8))
