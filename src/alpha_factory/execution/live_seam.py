"""Phase C live seam — the paper/live execution boundary (skeleton stage [7]).

Stage [7] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§"Build approach"). The capstone: it proves the pipeline (fetch -> signal
-> execute -> log -> reconcile -> halt) is execution-backend-agnostic.
Going live is a BACKEND SWAP, not a pipeline rewrite.

THE SEAM
--------
``ExecutionBackend`` is the single thing that differs between paper and
live: how a ``TargetPosition`` becomes fills.

    PaperBackend  — delegates to the stage-[4] fill simulator
    LiveBackend   — STUB. Raises NotImplementedError. Places NO real
                    orders. Documents what a real live backend must add.

``run_cycle`` wires the whole inner loop and takes the backend as a
parameter, so the exact same composition runs paper or live.

RED LINE (CLAUDE.md): "永遠不要在沒有 paper trading 階段下直接上實盤"
(never go live without the paper-trade phase). LiveBackend is a stub on
purpose — there is no code path here that can send a real order. Wiring a
real LiveBackend is gated on the 3-month paper-trade re-validation.

CYCLE CONTROL FLOW (run_cycle)
------------------------------
    1. fetch rolling window           (a fetch crash -> P1 halt, best-effort)
    2. derive marks + CycleStamp
    3. strategy.compute_target_position   (pure; emits no events)
    4. P0 kill-flag gate              (flag present -> execute_halt + return)
    5. crash-guarded backend.execute_target   (P1 on any exception)

SKELETON LIMITATIONS (documented)
---------------------------------
* ``run_cycle`` does NOT hold ``event_log.single_writer_lock`` — the CRON
  acquires it once for its whole lifetime (B2 invariant) and calls
  ``run_cycle`` inside it. Tests call ``run_cycle`` directly (single
  threaded), which is safe.
* No scheduling here — cron / systemd-timer at the 8h funding boundaries
  is an ops concern (v3 §8), not skeleton code.
* LiveBackend is entirely a stub (no Binance order placement, no
  cancel-replace, no SIGTERM handler, no cancel-all — v2 §5 live-only
  deferrals).
* P0 still fetches once (to obtain marks to flatten at); a fetch failure
  during P0 leaves positions open (logged) rather than flattening blind.
* No ``signal_compute`` event is emitted yet (reconcile B replays fills,
  not signal logic; n_signals stays 0) — deferred to real-cron wiring.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from alpha_factory.data.binance_client import BinanceClient
from alpha_factory.data.schema import FUNDING_ROOT, KLINES_ROOT
from alpha_factory.execution.event_log import (
    PAPER_EVENTS_PATH,
    git_commit_short,
)
from alpha_factory.execution.fill_sim import (
    PositionBook,
    SimulatedFill,
    latest_close_by_market,
    step_paper_fill_sim,
)
from alpha_factory.execution.forward_fetch import fetch_forward_window
from alpha_factory.execution.halt import (
    HALT_REASON_CRASH,
    HALT_REASON_MANUAL,
    KILL_FLAG_PATH,
    check_kill_flag,
    crash_halt_guard,
    execute_halt,
)
from alpha_factory.execution.strategy import (
    RollingWindow,
    Strategy,
    TargetPosition,
)

log = logging.getLogger(__name__)

__all__ = [
    "CycleResult",
    "CycleStamp",
    "ExecutionBackend",
    "LiveBackend",
    "PaperBackend",
    "make_backend",
    "run_cycle",
]

# strategy_id stand-in when a halt fires before a target exists (fetch crash).
_UNKNOWN_STRATEGY = "unknown"


@dataclass(frozen=True)
class CycleStamp:
    """Per-cycle versioning bundle (v2 §2), produced by the fetch stage."""

    data_version: str
    git_commit_hash: str
    clock_drift_ms: float | None


@dataclass(frozen=True)
class CycleResult:
    """Outcome of one ``run_cycle`` invocation."""

    mode: str                         # backend mode: "paper" | "live"
    halted: bool
    halt_reason: str | None
    target: TargetPosition | None
    book: PositionBook
    fills: list[SimulatedFill] = field(default_factory=list)
    data_version: str | None = None
    clock_drift_ms: float | None = None


# ── The seam ──────────────────────────────────────────────────────────────


@runtime_checkable
class ExecutionBackend(Protocol):
    """How a TargetPosition becomes fills — the only paper/live difference.

    Implementations must expose a ``mode`` string and an
    ``execute_target`` that records fills to the event log and returns
    the resulting (book, fills).
    """

    mode: str

    def execute_target(
        self,
        target: TargetPosition,
        window: RollingWindow,
        stamp: CycleStamp,
        *,
        event_log_path: Path = ...,
        taker_fee_bps: dict[str, float] | None = ...,
    ) -> tuple[PositionBook, list[SimulatedFill]]: ...


class PaperBackend:
    """Paper execution — delegates to the stage-[4] fill simulator."""

    mode = "paper"

    def execute_target(
        self,
        target: TargetPosition,
        window: RollingWindow,
        stamp: CycleStamp,
        *,
        event_log_path: Path = PAPER_EVENTS_PATH,
        taker_fee_bps: dict[str, float] | None = None,
    ) -> tuple[PositionBook, list[SimulatedFill]]:
        return step_paper_fill_sim(
            target,
            window,
            data_version=stamp.data_version,
            git_commit_hash=stamp.git_commit_hash,
            clock_drift_ms=stamp.clock_drift_ms,
            event_log_path=event_log_path,
            taker_fee_bps=taker_fee_bps,
        )


class LiveBackend:
    """Live execution — STUB. Places NO real orders (CLAUDE.md red line).

    A real implementation, gated on the 3-month paper-trade
    re-validation, would:

    * translate each leg delta into Binance USDT-M perp / spot orders
      (market or limit per the fill policy decided at impl time, v2 §5);
    * handle partial fills, cancel-replace / price-chasing, and
      idempotent client order IDs;
    * record ACTUAL fills (price, qty, fee) as ``fill_simulated``'s live
      analogue, so reconcile's A (realized) reflects real execution;
    * wire the live-tier kill switch (SIGTERM handler + Binance
      cancel-all, B3 live tier).

    Until then this raises so an accidental live run cannot trade. Run
    through ``run_cycle``, the NotImplementedError is caught by the
    crash guard, fires a (no-op) P1 halt, and re-raises — a safe stop.
    """

    mode = "live"

    def execute_target(
        self,
        target: TargetPosition,
        window: RollingWindow,
        stamp: CycleStamp,
        *,
        event_log_path: Path = PAPER_EVENTS_PATH,
        taker_fee_bps: dict[str, float] | None = None,
    ) -> tuple[PositionBook, list[SimulatedFill]]:
        raise NotImplementedError(
            "LiveBackend is a stub — real order placement is gated on the "
            "3-month paper-trade re-validation (CLAUDE.md red line). The "
            "pipeline is backend-agnostic; wire a real backend here when "
            "paper-trade tracking-error passes.",
        )


def make_backend(mode: str) -> ExecutionBackend:
    """Select an execution backend by mode string."""
    if mode == "paper":
        return PaperBackend()
    if mode == "live":
        return LiveBackend()
    raise ValueError(f"unknown execution mode {mode!r}; expected 'paper' | 'live'")


# ── Composition root ──────────────────────────────────────────────────────


def _marks_from_window(
    window: RollingWindow, symbol: str,
) -> dict[tuple[str, str], float]:
    """``(symbol, market) -> last close`` from the window's klines."""
    return {
        (symbol, market): price
        for market, price in latest_close_by_market(window.klines, symbol).items()
    }


def run_cycle(
    symbol: str,
    client: BinanceClient,
    strategy: Strategy,
    backend: ExecutionBackend,
    *,
    n_settlements: int = 120,
    event_log_path: Path = PAPER_EVENTS_PATH,
    kill_flag_path: Path = KILL_FLAG_PATH,
    funding_root: Path = FUNDING_ROOT,
    klines_root: Path = KLINES_ROOT,
    persist: bool = True,
    git_commit_hash: str | None = None,
    taker_fee_bps: dict[str, float] | None = None,
) -> CycleResult:
    """Run one backend-agnostic paper-trade cycle.

    The CRON wraps the loop in ``event_log.single_writer_lock`` (B2); this
    function assumes the lock is already held (or that the caller is a
    single-threaded test). See the module docstring for the control flow
    and skeleton limitations.
    """
    git_hash = git_commit_hash or git_commit_short()

    # 1. Fetch. A crash here is a P1 with no marks (best-effort flatten).
    try:
        fetch = fetch_forward_window(
            symbol, client,
            n_settlements=n_settlements,
            funding_root=funding_root,
            klines_root=klines_root,
            persist=persist,
        )
    except Exception as e:
        log.exception("fetch crashed; firing P1 crash halt with no marks")
        execute_halt(
            HALT_REASON_CRASH, mark_prices={}, strategy_id=_UNKNOWN_STRATEGY,
            data_version="unknown", git_commit_hash=git_hash,
            clock_drift_ms=None, detail=f"fetch: {type(e).__name__}: {e}",
            event_log_path=event_log_path, taker_fee_bps=taker_fee_bps,
        )
        raise

    marks = _marks_from_window(fetch.window, symbol)
    stamp = CycleStamp(
        data_version=fetch.data_version,
        git_commit_hash=git_hash,
        clock_drift_ms=fetch.clock_drift_ms,
    )

    # 2. Strategy is pure — no events, safe before the halt gate.
    target = strategy.compute_target_position(fetch.window)

    # 3. P0 manual-kill gate preempts execution.
    decision = check_kill_flag(kill_flag_path)
    if decision.should_halt:
        result = execute_halt(
            HALT_REASON_MANUAL, mark_prices=marks,
            strategy_id=target.strategy_id,
            data_version=stamp.data_version, git_commit_hash=git_hash,
            clock_drift_ms=stamp.clock_drift_ms, detail=decision.detail,
            event_log_path=event_log_path, taker_fee_bps=taker_fee_bps,
        )
        return CycleResult(
            mode=backend.mode, halted=True, halt_reason=HALT_REASON_MANUAL,
            target=target, book=result.book_after, fills=result.unwind_fills,
            data_version=stamp.data_version, clock_drift_ms=stamp.clock_drift_ms,
        )

    # 4. Execute under P1 crash protection (LiveBackend stub raises here).
    with crash_halt_guard(
        marks, strategy_id=target.strategy_id,
        data_version=stamp.data_version, git_commit_hash=git_hash,
        clock_drift_ms=stamp.clock_drift_ms,
        event_log_path=event_log_path, taker_fee_bps=taker_fee_bps,
    ):
        book, fills = backend.execute_target(
            target, fetch.window, stamp,
            event_log_path=event_log_path, taker_fee_bps=taker_fee_bps,
        )

    return CycleResult(
        mode=backend.mode, halted=False, halt_reason=None,
        target=target, book=book, fills=fills,
        data_version=stamp.data_version, clock_drift_ms=stamp.clock_drift_ms,
    )
