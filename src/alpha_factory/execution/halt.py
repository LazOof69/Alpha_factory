"""Phase C halt / kill — P0 manual kill + P1 crash (skeleton stage [6]).

Stage [6] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§"Build approach"; v2 §1 halt taxonomy). The kill switch the
live-trading-execution skill calls non-negotiable: "永遠要有 kill switch".

SKELETON SCOPE (v3 build-approach: "kill flag-file + P0/P1 (crash) only")
-------------------------------------------------------------------------
Only the two triggers that need no threshold state:

    P0  manual kill   — a flag file exists on disk -> flat + exit
    P1  crash         — any unhandled exception in the cycle -> flat + exit

Deferred to depth iteration (v2 §1 table; need rolling state / sustained-
breach gates that censor the paper sample if fired early):

    P0.5 clock drift > 500ms   (the sample is already plumbed —
                                forward_fetch.clock_drift_ms — so this is a
                                trivial future add)
    P2   cumulative mtm DD > 5%
    P3   daily DD > 1%
    P4   tracking error > 0.50
    P5   Binance API outage > 4h

PAPER vs LIVE (v2 §1 two-mode action table)
-------------------------------------------
At skeleton we are PAPER mode. P0/P1 flat the book in BOTH modes (the
threshold triggers are the ones that are alert-only in paper). "Flat"
here = emit ``unwind_simulated`` closing fills for every open position,
then the cron exits. Live mode additionally cancels open orders via the
Binance API (B3 live tier) — deferred to live work, not paper scope.

WHY ``unwind_simulated`` (not ``fill_simulated``) FOR HALT CLOSURES
------------------------------------------------------------------
Incident audit: a kill-driven close MUST be distinguishable in the spine
from a strategy-driven close. Both move the book (so both are folded by
``fill_sim.replay_position_book`` via ``BOOK_AFFECTING_KINDS``), but the
``kind`` records WHY the position changed — exactly what post-incident
review needs.

MISSING-MARK HONESTY
--------------------
A kill switch that cannot price a position does NOT fabricate a fill.
The position is LEFT OPEN, logged at ERROR, and counted in the
``halt_action_fired`` event's ``n_positions_left_open`` — manual
intervention required. Faking a flatten would hide an unhedged book.

FLAG LIFECYCLE
--------------
``execute_halt`` does NOT remove the kill flag (v2 §1 re-arm condition:
"flag removed + user ack written to audit log"). The operator clears it
deliberately via ``clear_kill_flag`` after acknowledging — a stale flag
intentionally blocks an automatic restart.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from alpha_factory.execution.event_log import (
    PAPER_EVENTS_PATH,
    PAPER_TRADE_ROOT,
    append_event,
    make_event,
)
from alpha_factory.execution.fill_sim import (
    DEFAULT_TAKER_FEE_BPS,
    DUST_QUANTITY,
    PositionBook,
    SimulatedFill,
    fill_to_event_data,
    replay_position_book,
)

log = logging.getLogger(__name__)

__all__ = [
    "HALT_REASON_CRASH",
    "HALT_REASON_MANUAL",
    "KILL_FLAG_PATH",
    "HaltDecision",
    "HaltResult",
    "arm_kill_flag",
    "check_kill_flag",
    "clear_kill_flag",
    "compute_unwind_fills",
    "crash_halt_guard",
    "execute_halt",
    "kill_flag_present",
]

# P0 kill flag. Presence (not content) is the signal; an operator or an
# external monitor `touch`es it to stop the cron at the next pre-cycle gate.
KILL_FLAG_PATH = PAPER_TRADE_ROOT / "KILL"

HALT_REASON_MANUAL = "manual_kill"   # P0
HALT_REASON_CRASH = "crash"          # P1

# System-level halt markers are not tied to one symbol.
_SYSTEM_SYMBOL = ""


@dataclass(frozen=True)
class HaltDecision:
    """Result of the P0 pre-cycle gate."""

    should_halt: bool
    reason: str | None    # HALT_REASON_* or None
    detail: str


@dataclass(frozen=True)
class HaltResult:
    """Outcome of an executed halt.

    ``book_after`` is re-derived from the spine post-unwind, so it is the
    authoritative residual book — empty iff every position was markable
    and closed. ``n_positions_left_open`` > 0 means some positions could
    not be priced and remain open (manual intervention required).
    """

    reason: str
    book_after: PositionBook
    unwind_fills: list[SimulatedFill]
    n_positions_closed: int
    n_positions_left_open: int


# ── P0 kill-flag primitives ───────────────────────────────────────────────


def kill_flag_present(flag_path: Path = KILL_FLAG_PATH) -> bool:
    """True iff the kill flag file exists."""
    return flag_path.exists()


def arm_kill_flag(flag_path: Path = KILL_FLAG_PATH, *, reason: str = "") -> None:
    """Create the kill flag (ops / test helper). Idempotent.

    Writes an optional human reason as the file body for the audit trail;
    the cron keys only on existence, not content.
    """
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(reason or "manual kill", encoding="utf-8")
    log.warning("kill flag armed at %s (reason=%r)", flag_path, reason)


def clear_kill_flag(flag_path: Path = KILL_FLAG_PATH) -> None:
    """Remove the kill flag (operator re-arm step). No-op if absent."""
    with contextlib.suppress(FileNotFoundError):
        flag_path.unlink()
    log.info("kill flag cleared at %s", flag_path)


def check_kill_flag(flag_path: Path = KILL_FLAG_PATH) -> HaltDecision:
    """P0 pre-cycle gate: should the cron halt before running this cycle?"""
    if kill_flag_present(flag_path):
        return HaltDecision(
            should_halt=True,
            reason=HALT_REASON_MANUAL,
            detail=f"kill flag present at {flag_path}",
        )
    return HaltDecision(should_halt=False, reason=None, detail="")


# ── Flatten (compute closing fills for the whole book) ────────────────────


def compute_unwind_fills(
    book: PositionBook,
    mark_prices: dict[tuple[str, str], float],
    *,
    taker_fee_bps: dict[str, float] | None = None,
) -> tuple[list[SimulatedFill], list[tuple[str, str]]]:
    """Closing fills to flatten every open position; plus the unmarkable keys.

    Returns ``(unwind_fills, left_open_keys)``. A long is closed with a
    sell, a short with a buy. A position with no mark price (or a
    non-positive one) is NOT closed — its key is returned in
    ``left_open_keys`` and logged at ERROR (no fabricated fill).
    """
    fee_bps = taker_fee_bps if taker_fee_bps is not None else DEFAULT_TAKER_FEE_BPS

    fills: list[SimulatedFill] = []
    left_open: list[tuple[str, str]] = []
    for key in sorted(book.keys()):
        pos = book[key]
        if abs(pos.quantity_base) <= DUST_QUANTITY:
            continue
        sym, market = key
        mark = mark_prices.get(key)
        if mark is None or mark <= 0:
            log.error(
                "halt: cannot mark %s — position LEFT OPEN (qty=%.8f); "
                "manual intervention required", key, pos.quantity_base,
            )
            left_open.append(key)
            continue

        side = "sell" if pos.quantity_base > 0 else "buy"
        qty = abs(pos.quantity_base)
        notional = qty * mark
        fee_bp = fee_bps.get(market, max(fee_bps.values()))   # conservative default
        fee = notional * fee_bp / 10_000.0
        fills.append(SimulatedFill(
            symbol=sym,
            market=market,
            side=side,
            quantity_base=qty,
            fill_price=mark,
            notional_quote=notional,
            fee_paid_quote=fee,
            fee_bp=fee_bp,
            # Closing the position drives notional to zero; the delta is
            # the negative of the current signed notional.
            target_delta_notional=-pos.notional_quote,
        ))
    return fills, left_open


# ── Execute a halt (emit events; book is flat afterwards) ─────────────────


def execute_halt(
    reason: str,
    *,
    mark_prices: dict[tuple[str, str], float],
    strategy_id: str,
    data_version: str,
    git_commit_hash: str,
    clock_drift_ms: float | None,
    detail: str = "",
    event_log_path: Path = PAPER_EVENTS_PATH,
    taker_fee_bps: dict[str, float] | None = None,
) -> HaltResult:
    """Flatten the book and record the halt on the spine.

    Event sequence (all on the single-writer spine; the cron must hold
    ``event_log.single_writer_lock``):

        1. (P0 only) ``kill_flag_observed``
        2. one ``unwind_simulated`` per closable position
        3. ``halt_action_fired`` summary (reason, counts, detail)

    Returns a ``HaltResult`` whose ``book_after`` is re-replayed from the
    spine — flat iff every position was markable. Does NOT remove the
    kill flag (operator re-arm step) and does NOT emit ``system_shutdown``
    (the cron owns its lifecycle events).
    """
    book = replay_position_book(event_log_path)

    if reason == HALT_REASON_MANUAL:
        append_event(make_event(
            "kill_flag_observed", strategy_id=strategy_id, symbol=_SYSTEM_SYMBOL,
            data_version=data_version, git_commit_hash=git_commit_hash,
            process_clock_drift_vs_binance_ms=clock_drift_ms,
            data={"detail": detail},
        ), path=event_log_path)

    unwind_fills, left_open = compute_unwind_fills(
        book, mark_prices, taker_fee_bps=taker_fee_bps,
    )

    for fill in unwind_fills:
        append_event(make_event(
            "unwind_simulated", strategy_id=strategy_id, symbol=fill.symbol,
            data_version=data_version, git_commit_hash=git_commit_hash,
            process_clock_drift_vs_binance_ms=clock_drift_ms,
            data=fill_to_event_data(fill),
        ), path=event_log_path)

    append_event(make_event(
        "halt_action_fired", strategy_id=strategy_id, symbol=_SYSTEM_SYMBOL,
        data_version=data_version, git_commit_hash=git_commit_hash,
        process_clock_drift_vs_binance_ms=clock_drift_ms,
        data={
            "reason": reason,
            "n_positions_closed": len(unwind_fills),
            "n_positions_left_open": len(left_open),
            "left_open": [list(k) for k in left_open],
            "detail": detail,
        },
    ), path=event_log_path)

    book_after = replay_position_book(event_log_path)
    log.warning(
        "halt fired: reason=%s closed=%d left_open=%d",
        reason, len(unwind_fills), len(left_open),
    )
    return HaltResult(
        reason=reason,
        book_after=book_after,
        unwind_fills=unwind_fills,
        n_positions_closed=len(unwind_fills),
        n_positions_left_open=len(left_open),
    )


# ── P1 crash guard ────────────────────────────────────────────────────────


@contextlib.contextmanager
def crash_halt_guard(
    mark_prices: dict[tuple[str, str], float],
    *,
    strategy_id: str,
    data_version: str,
    git_commit_hash: str,
    clock_drift_ms: float | None,
    event_log_path: Path = PAPER_EVENTS_PATH,
    taker_fee_bps: dict[str, float] | None = None,
) -> Iterator[None]:
    """Wrap the risky part of a cycle; fire a P1 halt on any exception.

    On an unhandled exception the guard flattens at ``mark_prices``
    (best-effort — the caller passes the most recent marks it has; if the
    crash predates any fetch the map may be empty and positions are left
    open) and then RE-RAISES so the crash is not swallowed. The cron's
    own ``system_shutdown`` / process exit follows.

    Usage::

        with crash_halt_guard(marks, strategy_id=..., data_version=...,
                              git_commit_hash=..., clock_drift_ms=...):
            run_one_cycle(...)
    """
    try:
        yield
    except Exception as e:
        log.exception("paper-trade cycle crashed; firing P1 crash halt")
        with contextlib.suppress(Exception):
            execute_halt(
                HALT_REASON_CRASH,
                mark_prices=mark_prices,
                strategy_id=strategy_id,
                data_version=data_version,
                git_commit_hash=git_commit_hash,
                clock_drift_ms=clock_drift_ms,
                detail=f"{type(e).__name__}: {e}",
                event_log_path=event_log_path,
                taker_fee_bps=taker_fee_bps,
            )
        raise
