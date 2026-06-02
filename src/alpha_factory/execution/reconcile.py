"""Phase C daily reconcile — A (paper) vs B (replay) tracking-error.

Stage [5] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§"Build approach"; v2 §3 + v3 §3′). Once per UTC day the cron computes the
day's reconcile row and appends it to ``daily_reconcile.parquet``. This is
the health monitor of the whole paper-trade system: it isolates execution
drift (A vs B) from data-correction drift (B vs C).

THE TWO QUANTITIES AT SKELETON (v3 §3′ — 2-quantity start)
----------------------------------------------------------
    A. realized_pnl_24h_paper       — paper position book + simulated fills
    B. replay_pnl_24h_event_log     — re-derive PnL from the event log
    C. replay_pnl_24h_l1_current    — DEFERRED (no L1 archive at go-live;
                                      the column is written NULL until the
                                      forward archive accumulates ≥ window
                                      history, v3 §3′)

    PRIMARY GATE:  tracking_error = (A - B) / |B|
    DEFERRED:      data_correction_effect = (B - C) / |C|   # NULL until C

WHY A ≡ B AT SKELETON (documented, not a bug)
---------------------------------------------
The skeleton fill sim executes target intent EXACTLY (instant, zero
slippage, full fill — fill_sim.py), and B re-derives PnL from the very
same ``fill_simulated`` events. So A and B are computed by the identical
replay over the identical event stream → they are equal by construction
and ``tracking_error`` is structurally 0. This stage proves the
*plumbing* (UTC-day windowing, PnL replay, parquet append, event
counting) end-to-end.

``tracking_error`` becomes informative only once A and B can diverge —
i.e. when the fill sim grows slippage / partial fills / latency (depth
iteration), or once a real cron run makes the executed fill price differ
from the strategy's decision-time price. The structural-zero caveat is
baked into every row's ``notes`` field so the artifact is self-documenting.

RED LINE (CLAUDE.md)
--------------------
Sharpe columns are written NULL. A Sharpe number may NOT be produced
outside the strategy-validation skill (DSR / PBO discipline). The schema
carries the columns so the parquet shape is stable, but this module
never fills them.

SKELETON LIMITATIONS
--------------------
* Intra-window MTM: PnL is marked over positions built from the DAY'S
  OWN fills at caller-supplied end-of-day prices. Multi-day position
  carry-over attribution (start-of-day vs end-of-day marks) needs
  historical price snapshots — deferred.
* B replays from ``fill_simulated`` events. Replaying strategy LOGIC
  from ``signal_compute`` inputs is deferred until the cron emits those
  events (stage [7] integration).
* Single cron writer (v2 B2) — no file lock here; the reconcile job and
  the signal-compute job are the same single process.
* Funding income not yet booked (``funding_received`` events deferred);
  skeleton PnL is therefore MTM − fees only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.data.schema import TZ_UTC
from alpha_factory.execution.event_log import (
    PAPER_EVENTS_PATH,
    PAPER_TRADE_ROOT,
    read_events,
)
from alpha_factory.execution.fill_sim import (
    BOOK_AFFECTING_KINDS,
    PositionBook,
    fill_from_event_data,
    update_book,
)

log = logging.getLogger(__name__)

__all__ = [
    "DAILY_RECONCILE_PATH",
    "RECONCILE_SCHEMA",
    "DailyReconcile",
    "compute_daily_reconcile",
    "replay_pnl_from_fill_events",
    "write_daily_reconcile",
]

DAILY_RECONCILE_PATH = PAPER_TRADE_ROOT / "daily_reconcile.parquet"

# Below this |B| we cannot form a meaningful relative tracking error
# (0/0); tracking_error is written NULL instead of dividing by ~0.
_PNL_EPSILON: float = 1e-9

# v2 §3 daily_reconcile schema + ingested_at audit column. C-quantity and
# Sharpe columns are nullable and written NULL at skeleton (v3 §3′ + the
# CLAUDE.md Sharpe red line).
RECONCILE_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date,
    "realized_pnl_quote_24h": pl.Float64,        # A
    "replay_event_log_pnl_24h": pl.Float64,      # B
    "replay_l1_current_pnl_24h": pl.Float64,     # C — NULL at skeleton
    "tracking_error": pl.Float64,                # (A-B)/|B|; NULL if |B|≈0
    "data_correction_effect": pl.Float64,        # (B-C)/|C|; NULL at skeleton
    "realized_sharpe_to_date": pl.Float64,       # NULL — strategy-validation only
    "replay_sharpe_to_date": pl.Float64,         # NULL — strategy-validation only
    "n_signals_today": pl.Int64,
    "n_simulated_fills_today": pl.Int64,
    "n_halts_today": pl.Int64,
    "n_data_version_drifts": pl.Int64,
    "notes": pl.Utf8,
    "ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
}

_SKELETON_NOTE = (
    "skeleton: A=B by construction (tracking_error structurally 0); "
    "C/data_correction_effect/sharpe null (deferred)"
)


@dataclass(frozen=True)
class DailyReconcile:
    """One day's reconcile row. ``ingested_at`` is stamped at write time.

    Float fields that are deferred at skeleton (C, correction effect,
    both Sharpes) are ``None`` and serialise to parquet null.
    """

    date: date
    realized_pnl_quote_24h: float            # A
    replay_event_log_pnl_24h: float          # B
    replay_l1_current_pnl_24h: float | None  # C
    tracking_error: float | None
    data_correction_effect: float | None
    realized_sharpe_to_date: float | None
    replay_sharpe_to_date: float | None
    n_signals_today: int
    n_simulated_fills_today: int
    n_halts_today: int
    n_data_version_drifts: int
    notes: str


# ── Event-window helpers ──────────────────────────────────────────────────


def _parse_event_ts(ts: str) -> datetime:
    """Parse a v2 §2 event ``ts`` ('...Z') into a tz-aware UTC datetime."""
    # event_log writes ms precision with a 'Z' suffix; normalise to +00:00
    # so fromisoformat is unambiguous across Python builds.
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _events_in_utc_day(events: list[dict], cycle_date: date) -> list[dict]:
    """Filter events to ``[cycle_date 00:00, cycle_date+1 00:00)`` UTC."""
    day_start = datetime(
        cycle_date.year, cycle_date.month, cycle_date.day, tzinfo=UTC,
    )
    day_end = day_start + timedelta(days=1)
    out: list[dict] = []
    for ev in events:
        ts = ev.get("ts")
        if ts is None:
            continue
        t = _parse_event_ts(ts)
        if day_start <= t < day_end:
            out.append(ev)
    return out


def _count_event_kinds(events: list[dict]) -> dict[str, int]:
    """Tally event ``kind`` occurrences (missing kinds default to 0)."""
    counts: dict[str, int] = {}
    for ev in events:
        kind = ev.get("kind", "")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


# ── PnL replay (book is rebuilt from the window's fill events) ────────────


def replay_pnl_from_fill_events(
    fill_events: list[dict],
    mark_prices: dict[tuple[str, str], float],
) -> float:
    """Replay PnL from a list of book-affecting events.

    Folds BOTH ``fill_simulated`` and ``unwind_simulated`` (halt closures)
    — see ``fill_sim.BOOK_AFFECTING_KINDS``. A halt-closed position must
    contribute its closing fees AND zero out its MTM, so the unwind events
    are not optional here. Builds the book via the shared
    ``fill_from_event_data`` + ``update_book`` so this matches live replay
    exactly, marks each surviving position at ``mark_prices[(symbol,
    market)]``, and subtracts fees paid.

        gross_mtm = Σ quantity_base * (mark_price − avg_entry_price)
        pnl       = gross_mtm − Σ fee_paid_quote

    A position whose ``(symbol, market)`` has no mark price contributes 0
    to gross MTM with a WARN — the cron should halt on a missing mark, but
    the skeleton degrades visibly rather than dropping the day silently.
    Sign convention: a short (quantity_base < 0) profits when the mark
    falls below entry, which the signed product captures.
    """
    book: PositionBook = {}
    total_fees = 0.0
    for ev in fill_events:
        if ev.get("kind") not in BOOK_AFFECTING_KINDS:
            continue
        fill = fill_from_event_data(ev["data"])
        book = update_book(book, [fill])
        total_fees += fill.fee_paid_quote

    gross_mtm = 0.0
    for key, pos in book.items():
        mark = mark_prices.get(key)
        if mark is None:
            log.warning(
                "no mark price for %s — position excluded from MTM "
                "(cron should halt on this)", key,
            )
            continue
        gross_mtm += pos.quantity_base * (mark - pos.avg_entry_price)

    return gross_mtm - total_fees


# ── Compose one day's reconcile ───────────────────────────────────────────


def compute_daily_reconcile(
    cycle_date: date,
    mark_prices: dict[tuple[str, str], float],
    *,
    event_log_path: Path | None = None,
    notes: str = "",
) -> DailyReconcile:
    """Compute the reconcile row for ``cycle_date`` from the event log.

    A and B are both ``replay_pnl_from_fill_events`` over the same day's
    ``fill_simulated`` events (A ≡ B at skeleton — see module docstring),
    so ``tracking_error`` is structurally 0 (or NULL when |B| ≈ 0, e.g.
    a no-trade day). C, ``data_correction_effect`` and both Sharpe
    columns are NULL (deferred / red-line).
    """
    path = event_log_path if event_log_path is not None else PAPER_EVENTS_PATH
    events = read_events(path)
    day_events = _events_in_utc_day(events, cycle_date)
    counts = _count_event_kinds(day_events)

    realized_pnl = replay_pnl_from_fill_events(day_events, mark_prices)   # A
    replay_pnl = realized_pnl                                            # B ≡ A

    if abs(replay_pnl) <= _PNL_EPSILON:
        tracking_error: float | None = None
    else:
        tracking_error = (realized_pnl - replay_pnl) / abs(replay_pnl)

    full_notes = f"{_SKELETON_NOTE}; {notes}" if notes else _SKELETON_NOTE

    return DailyReconcile(
        date=cycle_date,
        realized_pnl_quote_24h=realized_pnl,
        replay_event_log_pnl_24h=replay_pnl,
        replay_l1_current_pnl_24h=None,      # C — deferred (no L1 archive)
        tracking_error=tracking_error,
        data_correction_effect=None,         # deferred (needs C)
        realized_sharpe_to_date=None,        # red line — strategy-validation only
        replay_sharpe_to_date=None,          # red line — strategy-validation only
        n_signals_today=counts.get("signal_compute", 0),
        n_simulated_fills_today=counts.get("fill_simulated", 0),
        n_halts_today=counts.get("halt_action_fired", 0),
        n_data_version_drifts=counts.get("data_version_drift_detected", 0),
        notes=full_notes,
    )


# ── Persist (append + dedup-by-date + atomic write) ───────────────────────


def _row_to_frame(row: DailyReconcile, ingested_at: datetime) -> pl.DataFrame:
    """Build a one-row RECONCILE_SCHEMA frame from a DailyReconcile."""
    return pl.DataFrame(
        [{
            "date": row.date,
            "realized_pnl_quote_24h": row.realized_pnl_quote_24h,
            "replay_event_log_pnl_24h": row.replay_event_log_pnl_24h,
            "replay_l1_current_pnl_24h": row.replay_l1_current_pnl_24h,
            "tracking_error": row.tracking_error,
            "data_correction_effect": row.data_correction_effect,
            "realized_sharpe_to_date": row.realized_sharpe_to_date,
            "replay_sharpe_to_date": row.replay_sharpe_to_date,
            "n_signals_today": row.n_signals_today,
            "n_simulated_fills_today": row.n_simulated_fills_today,
            "n_halts_today": row.n_halts_today,
            "n_data_version_drifts": row.n_data_version_drifts,
            "notes": row.notes,
            "ingested_at": ingested_at,
        }],
        schema=RECONCILE_SCHEMA,
    )


def _atomic_write_parquet(df: pl.DataFrame, target: Path) -> None:
    """tmp + os.replace so a crash mid-write cannot truncate the parquet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    df.write_parquet(tmp, compression="zstd", compression_level=3)
    os.replace(tmp, target)


def write_daily_reconcile(
    row: DailyReconcile, target: Path = DAILY_RECONCILE_PATH,
) -> None:
    """Append one reconcile row; dedup by date (keep last); atomic write.

    Re-running a date overwrites that date's row (idempotent re-reconcile),
    mirroring the L1 keep-last UPSERT pattern. ``ingested_at`` is stamped
    at write time for audit.
    """
    ingested_at = datetime.now(tz=UTC)
    new_row = _row_to_frame(row, ingested_at)

    if target.exists():
        existing = pl.read_parquet(target)
        merged = (
            pl.concat([existing, new_row], how="vertical_relaxed")
            .sort(["date", "ingested_at"])
            .unique(subset=["date"], keep="last")
            .sort("date")
        )
    else:
        merged = new_row

    _atomic_write_parquet(merged, target)
    log.info("wrote reconcile row for %s -> %s", row.date, target)
