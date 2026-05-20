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

THRESHOLD DERIVATION (post Phase B PBO sweep, 2026-05-03):

    Phase A audit caveat originally suggested 0.5 bp/8h. The 1-round
    pre-implementation critique re-derived break-even at ~0.09 bp/8h
    (16 bp / 180 settlements). The Phase B PBO sweep then enumerated
    18 trials = {0.05, 0.1, 0.15, 0.2, 0.3, 0.5} bp x {60, 90, 120}d
    on the 6.6-year BTC archive and found:

      - Best trial (0.05 bp / 120d) ties or beats V2 in all 4 windows
        (full / last-12m / last-3m / post-ETF) within 0.05 noise.
      - PBO = 0.30 < 0.5 (no overfit signal).
      - At lookback=120d, post-ETF Sharpe range across thresholds is
        only 0.27 (FLAT) — pick is robust, not knife-edge.

    Default thresholds locked at the sweep optimum:
      - exit_compression_30dma = 0.05 bp/8h (just above break-even).
      - reentry_compression_30dma = 0.10 bp/8h (2x ratio, 0.05 bp gap).
      - compression_lookback_settlements = 120 (40d).

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

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

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
    "current_regime_state_v3",
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

    # V3 NEW (post-PBO-sweep defaults, 2026-05-03)
    # Sweep over {0.05, 0.1, 0.15, 0.2, 0.3, 0.5} bp x {60, 90, 120}d
    # found (0.05 bp, 120d) ties-or-beats V2 in all 4 windows
    # (full / last-12m / last-3m / post-ETF). PBO = 0.30 < 0.5 PASS.
    # Sharpe surface at lookback=120d is FLAT across exit thresholds
    # (range 4.51-4.78 = 0.27 gap), so the (0.05 bp) pick is robust
    # rather than knife-edge. See scripts/v3_pbo_sweep.py.
    exit_compression_30dma: float = 0.000005      # 0.05 bp/8h ABS
    reentry_compression_30dma: float = 0.00001    # 0.10 bp/8h ABS (0.05 bp gap)
    compression_lookback_settlements: int = 120   # 40d (post-sweep)
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
    settle = _build_settle_frame(bars, params)

    state_at_settle: dict = {}
    for open_time, state in _iter_regime_states(settle, params):
        state_at_settle[open_time] = state

    # Forward-fill to all bars; warmup bars before first settlement = 0.
    times = bars["open_time"].to_list()
    states: list[int] = []
    cur = 0   # V3 warmup-exited
    for t in times:
        if t in state_at_settle:
            cur = state_at_settle[t]
        states.append(cur)

    return bars.with_columns(regime_state=pl.Series(states, dtype=pl.Int8))


# ── State-machine internals (shared by backtest + paper-trade adapter) ───


def _build_settle_frame(
    df: pl.DataFrame, params: CarryV3Params,
) -> pl.DataFrame:
    """Project funding settlements + the two rolling means the SM needs.

    Accepts either the joined ``bars`` frame (backtest path) or a raw
    funding frame (paper-trade adapter path) — both must carry an
    ``open_time`` column and a ``funding_rate`` column. Rows with null
    ``funding_rate`` are dropped (non-settlement bars / warmup).
    """
    return (
        df.filter(pl.col("funding_rate").is_not_null())
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


def _iter_regime_states(
    settle: pl.DataFrame, params: CarryV3Params,
) -> Iterator[tuple[Any, int]]:
    """Walk the V3 state machine over a settle frame; yield (open_time, state).

    ``settle`` must contain ``open_time``, ``funding_7dma``, ``funding_30dma``
    rows in ascending time order, one per funding-settlement bar
    (i.e. rows where ``funding_rate`` was not null, with rolling means
    precomputed via ``_build_settle_frame``).

    Initial state = 0 (V3 warmup-exited; conservative — see module
    docstring). The ratchet counter ticks during warmup, so the first
    post-warmup transition is not artificially blocked.

    Shared by ``_compute_regime_states_v3`` (backtest path) and
    ``current_regime_state_v3`` (paper-trade adapter) so the two paths
    cannot drift apart.
    """
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

        yield row["open_time"], new
        current = new


def current_regime_state_v3(
    funding_df: pl.DataFrame, params: CarryV3Params,
) -> int:
    """Return the V3 regime state at the end of ``funding_df``.

    Public skeleton-pipeline helper (Phase C paper-trade strategy
    adapter calls this per cycle, on the rolling funding window).
    Pure function; deterministic; stateless across calls (the caller
    owns the window).

    ``funding_df`` must carry ``open_time`` and ``funding_rate`` columns.
    Rows with null ``funding_rate`` are dropped (warmup or non-settlement
    bars). Returns 0 when the window contains no settlements at all
    (V3 warmup-exited initial state). Otherwise returns the last
    settlement's state: 0 = exited, 1 = active.

    Walking-skeleton caveat (docs/phase_c_infra_design_v3.md §P7):
        The V3 state machine is sequential — a transition's eligibility
        depends on ``min_state_duration_settlements`` since the LAST
        transition. This helper sees only the rows IN the window; if
        the true last transition happened before window-start, the
        ratchet counter resets at window-start. Self-heals once the
        forward archive accumulates past
        ``compression_lookback_settlements`` (default 120 = 40d).
    """
    # A bare/empty frame is a valid input at cron startup (no fetches
    # have landed yet) — return the V3 warmup-exited initial state
    # rather than letting polars raise on a missing column.
    if funding_df.height == 0 or "funding_rate" not in funding_df.columns:
        return 0
    settle = _build_settle_frame(funding_df, params)
    last_state = 0
    for _open_time, state in _iter_regime_states(settle, params):
        last_state = state
    return last_state
