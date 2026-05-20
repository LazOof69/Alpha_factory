"""Phase C strategy interface — pluggable compute_target_position protocol.

Stage [2] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§9). The paper-trade cron calls ``Strategy.compute_target_position(window)``
each cycle; the returned ``TargetPosition`` feeds:

* the fill sim (stage [4]) which turns target -> simulated fills, and
* the event log (stage [3]) which records the ``signal_compute`` event
  with ``inputs_hash`` for byte-stable replay (v2 §3 — replay is
  event-log-driven, NOT L1 re-query, because L1 ingest is UPSERT).

``carry_v3`` is wrapped here as ONE adapter (a *placeholder* per v3 P7 —
adversarial-debate verdict: the pipeline is the deliverable, not the
strategy). Swapping strategy = swapping the adapter; the rest of the
pipeline does not change.

WALKING-SKELETON LIMITATIONS (documented, not hidden)
-----------------------------------------------------
* ``CarryV3Adapter`` is STATELESS across cycles — each cycle re-runs the
  V3 state machine on the rolling window only. The ratchet guard's
  "settlements since last transition" counter resets at window-start,
  so a transition that happened just before the window enters is
  invisible to the ratchet. Self-heals once forward fetches accumulate
  past ``compression_lookback_settlements`` (default 120 = 40d).
* Single-symbol adapter — multi-symbol = multiple adapter instances.
* ``inputs_hash`` uses polars ``hash_rows()`` which is xxhash-based and
  polars-version-dependent. Byte-stable WITHIN a polars version, NOT
  ACROSS upgrades. Cross-version replay drift is an accepted skeleton
  risk; upgrade to canonical-JSON or parquet-bytes hashing if/when
  drift bites during the 3-month paper window.

LIVE-VS-BACKTEST DRIFT (skill ref: live-trading-execution)
----------------------------------------------------------
``TargetPosition.as_of`` is the LATEST data timestamp in the window,
NOT the wall-clock compute time. This keeps the position deterministic
given the window (wall clock would break byte-stable replay). Cron
latency = ``wall_clock - as_of`` is recorded separately as the event
log's ``ts`` field.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import polars as pl

from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID as CARRY_V3_ID,
)
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
    current_regime_state_v3,
)

__all__ = [
    "CarryV3Adapter",
    "RollingWindow",
    "Strategy",
    "TargetLeg",
    "TargetPosition",
    "compute_inputs_hash",
]


# ── Data model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TargetLeg:
    """One leg of a ``TargetPosition`` — per-(symbol, market) line item.

    ``target_notional_quote`` is SIGNED: positive = long, negative = short.
    Units are quote currency (USDT for Binance USDT-M perp + spot USDT
    pairs).
    """

    symbol: str           # e.g. "BTC-USDT" (hyphenated, per schema convention)
    market: str           # "spot" | "perp_usdt"
    target_notional_quote: float


@dataclass(frozen=True)
class TargetPosition:
    """Intended position state at a given data as-of.

    Convention: ``legs`` is the COMPLETE intent — anything not listed is
    at zero. Empty ``legs`` = flat. Fill sim diffs current book vs target
    legs and emits the deltas as orders.

    ``as_of`` is the LAST data-timestamp the strategy saw (not the wall
    clock); see module docstring for why.
    """

    strategy_id: str
    as_of: datetime                     # tz-aware UTC
    legs: tuple[TargetLeg, ...]
    inputs_hash: str                    # v2 §2 signal_inputs_hash
    regime_state: int | None = None     # strategy-specific debug; carry_v3 emits 0/1


@dataclass
class RollingWindow:
    """Data slice handed to ``Strategy.compute_target_position``.

    Skeleton shape per v3 §3′: rolling 120-settle funding window + the
    matching klines window. Two DataFrames so the adapter owns its own
    column expectations; extending ``RollingWindow`` with more fields
    later does not break existing adapters.

    NOT frozen: polars ``DataFrame`` is not hashable, so a frozen
    dataclass containing one is a footgun. Semantic immutability is by
    convention — adapters must not mutate the window.
    """

    funding: pl.DataFrame
    klines: pl.DataFrame


# ── Protocol ──────────────────────────────────────────────────────────────


@runtime_checkable
class Strategy(Protocol):
    """The pluggable strategy contract (v3 §9).

    Implementations are pure callables: same window in -> same
    ``TargetPosition`` out, with byte-stable ``inputs_hash``. State must
    be encoded in the window (skeleton) or persisted by the adapter
    (depth iteration, not skeleton).
    """

    def compute_target_position(self, window: RollingWindow) -> TargetPosition: ...


# ── Inputs hash (v2 §2 signal_inputs_hash) ────────────────────────────────


def _hash_frame_bytes(df: pl.DataFrame) -> bytes:
    """Bytes of polars row-hashes; empty frame -> sentinel.

    ``DataFrame.hash_rows()`` requires at least one column; an empty or
    zero-column frame returns a stable sentinel instead so the caller
    can always concatenate. Returned bytes are little-endian uint64
    array (numpy default), polars-version-dependent (see module
    docstring).
    """
    if df.height == 0 or df.width == 0:
        return b"<empty>"
    return df.hash_rows().to_numpy().tobytes()


def compute_inputs_hash(window: RollingWindow) -> str:
    """SHA-256 over the funding + klines window contents.

    Used by the cron to populate the event-log ``signal_compute`` event's
    ``data.signal_inputs_hash``. Replay (v2 §3 quantity B) re-executes
    the strategy on data carrying this hash; mismatch -> emit
    ``data_version_drift_detected`` event.

    The ``b"funding:"`` / ``b"klines:"`` section prefixes prevent
    collisions between (funding=X, klines=empty) and (funding=empty,
    klines=X).
    """
    h = hashlib.sha256()
    h.update(b"funding:")
    h.update(_hash_frame_bytes(window.funding))
    h.update(b"klines:")
    h.update(_hash_frame_bytes(window.klines))
    return h.hexdigest()


# ── carry_v3 adapter (placeholder per v3 P7) ──────────────────────────────


@dataclass(frozen=True)
class CarryV3Adapter:
    """Strategy adapter wrapping ``carry_v3`` (placeholder per v3 P7).

    State 1 (active) -> two legs: spot long ``capital_per_leg`` USDT +
    perp short ``capital_per_leg`` USDT — equal & opposite (delta-neutral
    carry pair).
    State 0 (exited / warmup) -> empty legs (flat).

    Stateless across calls; single-symbol per instance (see module
    docstring for skeleton limitations).
    """

    symbol: str
    params: CarryV3Params

    def compute_target_position(self, window: RollingWindow) -> TargetPosition:
        state = current_regime_state_v3(window.funding, self.params)
        as_of = _window_as_of(window)
        inputs_hash = compute_inputs_hash(window)

        if state == 1:
            cap = self.params.capital_per_leg
            legs: tuple[TargetLeg, ...] = (
                TargetLeg(self.symbol, "spot", +cap),
                TargetLeg(self.symbol, "perp_usdt", -cap),
            )
        else:
            legs = ()

        return TargetPosition(
            strategy_id=CARRY_V3_ID,
            as_of=as_of,
            legs=legs,
            inputs_hash=inputs_hash,
            regime_state=state,
        )


# ── Helpers ───────────────────────────────────────────────────────────────


def _window_as_of(window: RollingWindow) -> datetime:
    """Latest ``open_time`` across funding + klines.

    Falls back to whichever frame is non-empty. Raises ``ValueError`` if
    both are empty — a strategy cannot stamp ``as_of`` from no data.
    """
    times: list[datetime] = []
    if window.funding.height > 0 and "open_time" in window.funding.columns:
        times.append(window.funding["open_time"].max())
    if window.klines.height > 0 and "open_time" in window.klines.columns:
        times.append(window.klines["open_time"].max())
    if not times:
        raise ValueError(
            "RollingWindow has no usable open_time — cannot stamp "
            "TargetPosition.as_of",
        )
    return max(times)
