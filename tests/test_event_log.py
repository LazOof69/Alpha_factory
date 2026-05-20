"""Tests for src/alpha_factory/execution/event_log.py (Phase C spine).

Covers the v2 §2 schema shape, append/read round-trip, the v2 B1
partial-write-recovery contract, and the v2 B2 single-writer guard.
All I/O is isolated to tmp_path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_factory.execution.event_log import (
    EVENT_KINDS,
    EventLogCorruptionError,
    SingleWriterViolationError,
    append_event,
    git_commit_short,
    make_event,
    read_events,
    single_writer_lock,
)


def _event(kind: str = "signal_compute") -> dict:
    return make_event(
        kind,
        "carry_v3",
        "BTC-USDT",
        data_version="2026-05-13T00:00+nocorr",
        git_commit_hash="deadbee",
        process_clock_drift_vs_binance_ms=47.0,
        data={"target": "long_spot_short_perp"},
    )


# ── make_event ────────────────────────────────────────────────────────────


def test_make_event_has_v2_schema_shape():
    ev = _event()
    assert set(ev) == {
        "event_id", "ts", "kind", "strategy_id", "symbol",
        "versioning", "data",
    }
    assert ev["kind"] == "signal_compute"
    assert ev["strategy_id"] == "carry_v3"
    assert ev["symbol"] == "BTC-USDT"
    assert ev["ts"].endswith("Z")  # tz-aware UTC, ms, Z suffix
    assert set(ev["versioning"]) == {
        "git_commit_hash", "data_version",
        "process_clock_source", "process_clock_drift_vs_binance_ms",
    }
    assert ev["versioning"]["process_clock_drift_vs_binance_ms"] == 47.0
    assert ev["data"] == {"target": "long_spot_short_perp"}


def test_make_event_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown event kind"):
        make_event(
            "not_a_kind", "carry_v3", "BTC-USDT",
            data_version="x", git_commit_hash="y",
            process_clock_drift_vs_binance_ms=None,
        )


def test_make_event_data_defaults_empty_and_ids_unique():
    a = make_event(
        "system_start", "carry_v3", "BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
    )
    b = make_event(
        "system_start", "carry_v3", "BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
    )
    assert a["data"] == {}
    assert a["event_id"] != b["event_id"]


def test_all_kinds_constructible():
    for k in EVENT_KINDS:
        ev = make_event(
            k, "carry_v3", "BTC-USDT",
            data_version="x", git_commit_hash="y",
            process_clock_drift_vs_binance_ms=None,
        )
        assert ev["kind"] == k


# ── append / read round-trip ──────────────────────────────────────────────


def test_append_then_read_round_trip(tmp_path: Path):
    log_path = tmp_path / "paper_events.jsonl"
    evs = [_event("system_start"), _event("signal_compute"), _event("system_shutdown")]
    for e in evs:
        append_event(e, path=log_path)
    got = read_events(path=log_path)
    assert [g["event_id"] for g in got] == [e["event_id"] for e in evs]
    assert [g["kind"] for g in got] == [
        "system_start", "signal_compute", "system_shutdown",
    ]


def test_read_missing_file_returns_empty(tmp_path: Path):
    assert read_events(path=tmp_path / "nope.jsonl") == []


def test_append_lines_are_byte_stable_sorted_keys(tmp_path: Path):
    log_path = tmp_path / "e.jsonl"
    append_event(_event(), path=log_path)
    line = log_path.read_text(encoding="utf-8").strip()
    # sort_keys + no whitespace -> deterministic for downstream hashing
    assert line == json.dumps(json.loads(line), separators=(",", ":"), sort_keys=True)


# ── v2 B1 partial-write recovery ──────────────────────────────────────────


def test_b1_malformed_final_line_recovered(tmp_path: Path, caplog):
    log_path = tmp_path / "e.jsonl"
    append_event(_event("system_start"), path=log_path)
    append_event(_event("signal_compute"), path=log_path)
    # Simulate a crash mid-write: append a truncated JSON fragment, no newline.
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write('{"event_id":"truncated","kin')
    with caplog.at_level("WARNING"):
        got = read_events(path=log_path)
    assert len(got) == 2  # the two good events survive
    assert "crash-recovery" in caplog.text


def test_b1_more_than_one_malformed_raises(tmp_path: Path):
    log_path = tmp_path / "e.jsonl"
    append_event(_event("system_start"), path=log_path)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("garbage-1\n")
        fh.write('{"good":true}\n')
        fh.write("garbage-2\n")
    with pytest.raises(EventLogCorruptionError, match="not\n?.*solely the final"):
        read_events(path=log_path)


def test_b1_malformed_non_final_raises(tmp_path: Path):
    log_path = tmp_path / "e.jsonl"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("not-json\n")
        fh.write(json.dumps(_event()) + "\n")  # valid final line
    with pytest.raises(EventLogCorruptionError):
        read_events(path=log_path)


# ── v2 B2 single-writer guard ─────────────────────────────────────────────


def _acquire(lock_path: Path) -> None:
    """Acquire + release the single-writer lock (helper for raise-tests)."""
    with single_writer_lock(lock_path=lock_path):
        pass


def test_single_writer_lock_blocks_concurrent_acquire(tmp_path: Path):
    lock = tmp_path / ".writer.lock"
    with single_writer_lock(lock_path=lock), pytest.raises(
        SingleWriterViolationError, match="single-writer invariant",
    ):
        _acquire(lock)


def test_single_writer_lock_releases_on_exit(tmp_path: Path):
    lock = tmp_path / ".writer.lock"
    with single_writer_lock(lock_path=lock):
        pass
    assert not lock.exists()
    # Re-acquire after clean release must succeed.
    with single_writer_lock(lock_path=lock):
        assert lock.exists()


# ── git helper smoke ──────────────────────────────────────────────────────


def test_git_commit_short_returns_str():
    out = git_commit_short()
    assert isinstance(out, str) and out  # real short SHA or 'unknown'
