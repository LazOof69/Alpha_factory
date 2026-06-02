"""Phase C paper fill simulator — TargetPosition + price -> simulated fills.

Stage [4] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§"Build approach"). Composes the per-cycle execution step:

    replay_position_book(event_log) -> book
    compute_fills(target, book, prices) -> fills
    update_book(book, fills) -> next_book
    append fill_simulated events to event_log

The book is NOT a separate persisted file — it is a *projection* of
``fill_simulated`` events in the spine (v2 §3 replay-as-source-of-truth).
This keeps the spine the single audit + recovery surface (one place to
back up; one place to inspect).

SKELETON ORDER MODEL (v2 §4 "m1 paper assumption: taker, recalibrate at
month-2 boundary"; v3 P7 walking-skeleton; live-trading-execution skill)
--------------------------------------------------------------------------
* One synthetic fill per leg-delta — NO partial fills, NO cancel-replace,
  NO order-slicing, NO chasing.
* Zero slippage (v2 §4 ``default_assumption_at_1k_notional_bp: 0``,
  "confirmed empirically post-paper"). Slippage modelling is deferred
  to month-2 calibration with real fill data.
* Taker fees only (m1 conservatism). spot=7.5 bp / perp=5.0 bp per v2 §4
  locked-facts with BNB rebate ON.
* Instant fill — no latency between target compute and fill (the cron's
  wall-clock latency is recorded separately as event ``ts`` minus
  ``target.as_of``).
* Single-symbol per call (matches stage [1] / [2] thinness).
* Fill price = last close in ``window.klines`` for the matching market.
  No order book inspection / mid-price reconstruction at skeleton.

WHY THESE ARE NOT BUGS
----------------------
The point of the 3-month paper window is to MEASURE execution drift.
A naive fill sim with documented assumptions makes the drift
*visible*; a fancy fill sim that pretends to model microstructure
would hide it. Real execution quality is calibrated empirically once
month-1 fills exist (TCA per the execution skill).

UNWIND vs FILL EVENTS
---------------------
The event-log spine reserves a ``unwind_simulated`` kind for
halt-driven closures (stage [6]). Routine flat<->active transitions
use ``fill_simulated`` uniformly — replay treats them identically and
distinguishing at skeleton would add semantic surface without
strategy-level benefit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from alpha_factory.execution.event_log import (
    PAPER_EVENTS_PATH,
    append_event,
    make_event,
    read_events,
)
from alpha_factory.execution.strategy import RollingWindow, TargetPosition

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TAKER_FEE_BPS",
    "Position",
    "PositionBook",
    "SimulatedFill",
    "compute_fills",
    "fill_from_event_data",
    "latest_close_by_market",
    "replay_position_book",
    "step_paper_fill_sim",
    "update_book",
]


# v2 §4 locked-facts with BNB rebate ON. Taker for m1 conservatism;
# m2 boundary will add maker estimation per cost_model.yaml once fills
# accumulate (deferred to depth iteration).
DEFAULT_TAKER_FEE_BPS: dict[str, float] = {
    "spot": 7.5,
    "perp_usdt": 5.0,
}

# Below this absolute quantity_base we treat the position as fully closed
# and drop it from the book — guards against float dust at flip boundaries.
_DUST_QUANTITY: float = 1e-12


# ── Data model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """One row in the paper-trade position book.

    ``quantity_base`` is SIGNED — positive = long, negative = short.
    ``notional_quote`` is SIGNED at the same sign as ``quantity_base``
    (= quantity_base * avg_entry_price). ``avg_entry_price`` is the
    weighted average across all fills that built this position; reset
    to the new fill price when a position flips through zero.
    """

    symbol: str
    market: str                # "spot" | "perp_usdt"
    quantity_base: float
    avg_entry_price: float
    notional_quote: float


PositionBook = dict[tuple[str, str], Position]


@dataclass(frozen=True)
class SimulatedFill:
    """One synthetic fill — emitted as a ``fill_simulated`` event.

    ``side`` reflects the order direction (buy/sell); ``quantity_base``
    and ``notional_quote`` are UNSIGNED (the side carries the sign).
    ``target_delta_notional`` is SIGNED (positive = need to buy more,
    negative = need to sell) and preserved for audit so reconcile can
    diff exactly what intent drove the fill.
    """

    symbol: str
    market: str
    side: str                  # "buy" | "sell"
    quantity_base: float       # unsigned
    fill_price: float
    notional_quote: float      # unsigned
    fee_paid_quote: float
    fee_bp: float
    target_delta_notional: float


# ── Replay (book is a projection of the event log) ────────────────────────


def replay_position_book(
    event_log_path: Path = PAPER_EVENTS_PATH,
) -> PositionBook:
    """Project the current position book from ``fill_simulated`` events.

    Scan-once over the log; left-fold each fill through ``_apply_fill``.
    O(N) per cycle — at 3 months * 3 cycles/day * 2 legs ≈ 540 events
    this is trivially cheap. Ordering: events are appended in cycle
    order by the single-writer cron (event_log B2), so replay sees
    fills in execution order.
    """
    events = read_events(event_log_path)
    book: PositionBook = {}
    for ev in events:
        if ev.get("kind") != "fill_simulated":
            continue
        fill = fill_from_event_data(ev["data"])
        book = _apply_fill(book, fill)
    return book


def fill_from_event_data(data: dict) -> SimulatedFill:
    """Reconstruct SimulatedFill from a fill_simulated event's data payload.

    Shared by ``replay_position_book`` (here) and the daily reconcile
    (stage [5]) so both rebuild fills from the spine identically.
    """
    return SimulatedFill(
        symbol=data["symbol"],
        market=data["market"],
        side=data["side"],
        quantity_base=float(data["quantity_base"]),
        fill_price=float(data["fill_price"]),
        notional_quote=float(data["notional_quote"]),
        fee_paid_quote=float(data["fee_paid_quote"]),
        fee_bp=float(data["fee_bp"]),
        target_delta_notional=float(data["target_delta_notional"]),
    )


# ── Price extraction from RollingWindow ───────────────────────────────────


def latest_close_by_market(
    klines: pl.DataFrame, symbol: str,
) -> dict[str, float]:
    """Map ``market -> last close`` for ``symbol`` from a klines frame.

    Markets with no rows for the symbol are absent from the result;
    ``compute_fills`` treats absence as "no price, skip this leg" with a
    WARN. Real cron should halt under that condition; skeleton degrades
    visibly rather than silently substituting a stale price.
    """
    if klines.is_empty() or "symbol" not in klines.columns:
        return {}
    sub = klines.filter(pl.col("symbol") == symbol)
    if sub.is_empty():
        return {}
    by_market = (
        sub.sort("open_time")
        .group_by("market")
        .agg(pl.col("close").last().alias("last_close"))
    )
    return {
        row["market"]: float(row["last_close"])
        for row in by_market.iter_rows(named=True)
    }


# ── Fill computation (target - book diff) ─────────────────────────────────


def compute_fills(
    target: TargetPosition,
    book: PositionBook,
    prices: dict[str, float],
    *,
    taker_fee_bps: dict[str, float] | None = None,
) -> list[SimulatedFill]:
    """Diff target intent against current book; emit one fill per delta.

    Algorithm:
        for (symbol, market) in keys(target) ∪ keys(book):
            current_notional = book[k].notional_quote if present else 0
            target_notional  = target leg notional if present else 0
            delta = target_notional - current_notional
            if |delta| ~ 0:        skip
            if no price for market: skip + WARN
            else:                  emit one fill at last close

    Skeleton accepts: no slicing, no chasing, no partials (one delta
    -> one fill). Caller (cron / step_paper_fill_sim) is responsible
    for choosing prices via ``latest_close_by_market``.

    ``prices`` is ``market -> last_close``. Single-symbol scope per
    skeleton design — multi-symbol cron orchestration calls this once
    per symbol with that symbol's own price map.
    """
    fee_bps = taker_fee_bps if taker_fee_bps is not None else DEFAULT_TAKER_FEE_BPS

    target_legs = {
        (leg.symbol, leg.market): leg.target_notional_quote
        for leg in target.legs
    }
    all_keys = set(target_legs) | set(book.keys())

    fills: list[SimulatedFill] = []
    for key in sorted(all_keys):
        sym, market = key
        current = book[key].notional_quote if key in book else 0.0
        target_notional = target_legs.get(key, 0.0)
        delta = target_notional - current
        if abs(delta) <= _DUST_QUANTITY:
            continue

        price = prices.get(market)
        if price is None or price <= 0:
            log.warning(
                "no price for %s/%s (delta_notional=%.2f) — skipping fill; "
                "cron should halt on this condition",
                sym, market, delta,
            )
            continue

        fee_bp = fee_bps.get(market, max(fee_bps.values()))   # conservative default
        side = "buy" if delta > 0 else "sell"
        unsigned_notional = abs(delta)
        quantity_base = unsigned_notional / price
        fee_paid = unsigned_notional * fee_bp / 10_000.0

        fills.append(SimulatedFill(
            symbol=sym,
            market=market,
            side=side,
            quantity_base=quantity_base,
            fill_price=price,
            notional_quote=unsigned_notional,
            fee_paid_quote=fee_paid,
            fee_bp=fee_bp,
            target_delta_notional=delta,
        ))
    return fills


# ── Book update (pure; returns a new dict) ────────────────────────────────


def _apply_fill(book: PositionBook, fill: SimulatedFill) -> PositionBook:
    """Apply one fill to the book. Returns a new book (input untouched).

    Crossing-zero handling: when a fill flips the position through
    zero, the post-flip ``avg_entry_price`` resets to the fill price
    (the new position has only one execution behind it — the flip).
    Sub-flip arithmetic would attribute zero-crossing P&L to entry-price
    drift, which is misleading.
    """
    key = (fill.symbol, fill.market)
    signed_qty = fill.quantity_base if fill.side == "buy" else -fill.quantity_base
    signed_notional = fill.notional_quote if fill.side == "buy" else -fill.notional_quote

    new_book = dict(book)
    if key not in book:
        new_qty = signed_qty
        new_avg = fill.fill_price
        new_notional = signed_notional
    else:
        cur = book[key]
        new_qty = cur.quantity_base + signed_qty
        crossing_zero = (cur.quantity_base * new_qty < 0)
        if crossing_zero:
            new_avg = fill.fill_price
            new_notional = new_qty * fill.fill_price
        elif abs(new_qty) <= _DUST_QUANTITY:
            # Fully closed — handled below by removing the key.
            new_avg = 0.0
            new_notional = 0.0
        elif abs(cur.quantity_base) < abs(new_qty):
            # Adding to position in same direction — weighted average.
            weight_existing = abs(cur.quantity_base) * cur.avg_entry_price
            weight_new = abs(signed_qty) * fill.fill_price
            new_avg = (weight_existing + weight_new) / abs(new_qty)
            new_notional = new_qty * new_avg
        else:
            # Partial close (same direction) — avg entry preserved.
            new_avg = cur.avg_entry_price
            new_notional = new_qty * new_avg

    if abs(new_qty) <= _DUST_QUANTITY:
        new_book.pop(key, None)
        return new_book

    new_book[key] = Position(
        symbol=fill.symbol,
        market=fill.market,
        quantity_base=new_qty,
        avg_entry_price=new_avg,
        notional_quote=new_notional,
    )
    return new_book


def update_book(book: PositionBook, fills: list[SimulatedFill]) -> PositionBook:
    """Fold a list of fills into a new book; input book is not mutated."""
    out = dict(book)
    for fill in fills:
        out = _apply_fill(out, fill)
    return out


# ── Composition root (one paper-trade cycle's fill step) ──────────────────


def step_paper_fill_sim(
    target: TargetPosition,
    window: RollingWindow,
    *,
    data_version: str,
    git_commit_hash: str,
    clock_drift_ms: float | None,
    event_log_path: Path = PAPER_EVENTS_PATH,
    taker_fee_bps: dict[str, float] | None = None,
) -> tuple[PositionBook, list[SimulatedFill]]:
    """Run one cycle: replay -> compute -> emit fill_simulated -> updated book.

    The cron must hold ``event_log.single_writer_lock`` for its
    lifetime (B2 invariant). Versioning kwargs flow straight to
    ``make_event`` so the fill events match the signal_compute event
    of the same cycle (replay determinism).

    Returns the post-fill book + the fills emitted this cycle (for
    convenience; the source-of-truth is the appended events).

    Raises:
        ValueError: if target legs and book span more than one symbol
                    in one call — skeleton is single-symbol per cron
                    invocation.
    """
    book = replay_position_book(event_log_path)
    symbols = {leg.symbol for leg in target.legs} | {sym for sym, _market in book}
    if not symbols:
        # Cold start with flat target -> nothing to fill, no book update.
        return book, []
    if len(symbols) > 1:
        raise ValueError(
            f"step_paper_fill_sim got mixed symbols {sorted(symbols)} in one "
            "call; skeleton is single-symbol per call (v3 §'Build approach' "
            "stage [4]). Cron orchestrates multi-symbol via repeated calls.",
        )
    (the_symbol,) = symbols
    prices = latest_close_by_market(window.klines, the_symbol)
    fills = compute_fills(target, book, prices, taker_fee_bps=taker_fee_bps)

    for fill in fills:
        event = make_event(
            "fill_simulated",
            strategy_id=target.strategy_id,
            symbol=fill.symbol,
            data_version=data_version,
            git_commit_hash=git_commit_hash,
            process_clock_drift_vs_binance_ms=clock_drift_ms,
            data=_fill_to_event_data(fill),
        )
        append_event(event, path=event_log_path)

    return update_book(book, fills), fills


def _fill_to_event_data(fill: SimulatedFill) -> dict:
    """Flatten SimulatedFill into the event-log ``data`` payload shape."""
    return {
        "symbol": fill.symbol,
        "market": fill.market,
        "side": fill.side,
        "quantity_base": fill.quantity_base,
        "fill_price": fill.fill_price,
        "notional_quote": fill.notional_quote,
        "fee_paid_quote": fill.fee_paid_quote,
        "fee_bp": fill.fee_bp,
        "target_delta_notional": fill.target_delta_notional,
    }
