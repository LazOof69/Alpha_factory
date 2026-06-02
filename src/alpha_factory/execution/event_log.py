"""Phase C paper-trade event log — JSONL append-only spine.

Stage [3] of the Phase C walking-skeleton pipeline
(docs/phase_c_infra_design_v3.md §"Build approach"). Every other stage
(signal compute, fill sim, reconcile, halt) records to / replays from
this log. It is the single source of truth for replay
(docs/phase_c_infra_design_v2.md §3 — event-log-driven, NOT L1 re-query,
because L1 ingest is UPSERT and cannot reconstruct an as-seen view).

INVARIANTS
----------
* SINGLE WRITER (v2 B2): only the signal-compute cron appends events;
  halt-checker and reconcile are READ-ONLY. A future multi-writer fork
  must move to SQLite + advisory lock. Until then `single_writer_lock`
  is a thin fail-fast guard against an accidental concurrent writer.
* APPEND-ONLY: events are never mutated or deleted. A correction is
  itself a new event.
* DURABILITY: each append is flushed + fsync'd, so a crash loses at
  most the final partially-written line, which `read_events` recovers
  from (v2 B1).

PARTIAL-WRITE RECOVERY (v2 B1)
------------------------------
`read_events` skips a malformed FINAL line with a WARN (crash recovery).
A malformed non-final line, or more than one malformed line, is treated
as real corruption -> `EventLogCorruptionError`.

SCHEMA
------
One JSON object per line, conforming to phase_c_infra_design_v2 §2.
`data` is kind-specific and intentionally not validated here — each
producing stage owns its payload shape.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_factory.data.schema import DATA_ROOT

log = logging.getLogger(__name__)

__all__ = [
    "EVENT_KINDS",
    "EVENT_SCHEMA_VERSION",
    "PAPER_EVENTS_PATH",
    "EventLogCorruptionError",
    "SingleWriterViolationError",
    "append_event",
    "git_commit_short",
    "make_event",
    "read_events",
    "single_writer_lock",
]

# Bump on any persisted event-shape change; mirrors v2 §2 spec version.
EVENT_SCHEMA_VERSION = 2

# v2 §2 kind enum + clock_drift_detected (v2 §2 calls it out as the
# NEW P0.5 trigger between manual-kill and crash).
EVENT_KINDS: frozenset[str] = frozenset({
    "signal_compute",
    "fill_simulated",
    "unwind_simulated",
    "funding_received",
    "halt_armed",
    "halt_action_fired",
    "kill_flag_observed",
    "clock_drift_detected",
    "reconcile_complete",
    "data_version_drift_detected",
    "system_start",
    "system_shutdown",
})

PAPER_TRADE_ROOT = DATA_ROOT / "paper_trade"
PAPER_EVENTS_PATH = PAPER_TRADE_ROOT / "paper_events.jsonl"
_WRITER_LOCK_PATH = PAPER_TRADE_ROOT / ".writer.lock"


class EventLogCorruptionError(RuntimeError):
    """More than the final line is malformed — corruption, not crash recovery."""


class SingleWriterViolationError(RuntimeError):
    """A second writer tried to hold the append lock concurrently (v2 B2)."""


# ── versioning helpers ────────────────────────────────────────────────────


def git_commit_short(repo_root: Path | None = None) -> str:
    """Short SHA of HEAD, or 'unknown' if unresolvable.

    Stamped on every event (v2 §2 versioning.git_commit_hash) so a code
    change mid-paper-trade is detectable and replay can pin the matching
    strategy version. Never raises — a missing git is logged, not fatal.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root) if repo_root is not None else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("git_commit_short unresolvable (%s); recording 'unknown'", e)
        return "unknown"


def _now_iso() -> str:
    """tz-aware UTC, millisecond precision, 'Z' suffix (v2 §2 ts format)."""
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_event(
    kind: str,
    strategy_id: str,
    symbol: str,
    *,
    data_version: str,
    git_commit_hash: str,
    process_clock_drift_vs_binance_ms: float | None,
    process_clock_source: str = "systemd-timesyncd",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a v2-§2-shaped event dict. Pure (no I/O); does not append.

    Versioning inputs are caller-supplied (data_version from the L1
    archive fingerprint, clock drift from clock.py) so this stays
    deterministic and unit-testable. event_id/ts are the only
    non-deterministic fields.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(
            f"unknown event kind {kind!r}; allowed: {sorted(EVENT_KINDS)}",
        )
    return {
        "event_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "kind": kind,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "versioning": {
            "git_commit_hash": git_commit_hash,
            "data_version": data_version,
            "process_clock_source": process_clock_source,
            "process_clock_drift_vs_binance_ms": (
                process_clock_drift_vs_binance_ms
            ),
        },
        "data": data if data is not None else {},
    }


# ── single-writer guard (v2 B2) ───────────────────────────────────────────


@contextlib.contextmanager
def single_writer_lock(lock_path: Path = _WRITER_LOCK_PATH) -> Iterator[None]:
    """Fail-fast guard for the single-writer invariant (v2 B2).

    Deliberately thin (not a cross-host mutex): an O_EXCL lock file
    holding the writer pid. A concurrent writer raises
    `SingleWriterViolationError` rather than silently interleaving JSONL
    lines. The cron holds this once for its lifetime (not per append),
    keeping the invariant cheap. A stale lock after a crash is
    intentional — it forces operator acknowledgement before a restart
    re-appends.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as e:
        raise SingleWriterViolationError(
            f"writer lock {lock_path} is held — another paper-trade writer "
            f"is running (single-writer invariant, v2 B2). If this is a "
            f"stale lock from a crashed run, remove it after confirming no "
            f"writer is live.",
        ) from e
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


# ── append / read ─────────────────────────────────────────────────────────


def append_event(event: dict[str, Any], path: Path = PAPER_EVENTS_PATH) -> None:
    """Append one event as a JSON line; flush + fsync for durability.

    The caller must hold `single_writer_lock` for the writer process
    lifetime (the cron does this once, not per append). JSON keys are
    sorted + whitespace-stripped so the line is byte-stable for any
    downstream hashing. fsync bounds crash loss to the final line,
    which `read_events` recovers (v2 B1).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True, default=str)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_events(path: Path = PAPER_EVENTS_PATH) -> list[dict[str, Any]]:
    """Read all events; recover a crash-truncated final line (v2 B1).

    Returns [] if the log does not exist yet. A malformed FINAL line is
    skipped with a WARN (expected after a mid-write crash). A malformed
    non-final line, or more than one malformed line, is real corruption
    and raises `EventLogCorruptionError` (v2 B1: "> 1 malformed line in
    a single session — escalate").
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    malformed_idx: list[int] = []
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            malformed_idx.append(i)

    if not malformed_idx:
        return events

    last_nonblank = max(
        (i for i, raw in enumerate(lines) if raw.strip()),
        default=-1,
    )
    if malformed_idx == [last_nonblank]:
        log.warning(
            "event log %s: malformed final line %d skipped "
            "(crash-recovery, v2 B1)",
            path, last_nonblank,
        )
        return events

    raise EventLogCorruptionError(
        f"event log {path}: malformed line(s) at {malformed_idx} — not "
        f"solely the final line. Real corruption, not crash recovery "
        f"(v2 B1). Manual inspection required before paper-trade resumes.",
    )
