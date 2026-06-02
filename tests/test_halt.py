"""Tests for src/alpha_factory/execution/halt.py (Phase C stage [6]).

Covers the P0 kill-flag primitives, the book flattener (long/short/flat/
unmarkable), ``execute_halt`` event emission for manual + crash, the
crash-guard context manager (fires halt + re-raises), and the
correctness invariant that ``unwind_simulated`` events are folded by
replay so a post-halt restart sees a flat book. The end-to-end test
drives stage [4] (build an active book) -> stage [6] (kill -> flat) ->
stage [5] (reconcile counts the halt).
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID as CARRY_V3_ID,
)
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
)
from alpha_factory.execution.event_log import (
    make_event,
    read_events,
)
from alpha_factory.execution.fill_sim import (
    Position,
    replay_position_book,
    step_paper_fill_sim,
)
from alpha_factory.execution.halt import (
    HALT_REASON_CRASH,
    HALT_REASON_MANUAL,
    HaltDecision,
    arm_kill_flag,
    check_kill_flag,
    clear_kill_flag,
    compute_unwind_fills,
    crash_halt_guard,
    execute_halt,
    kill_flag_present,
)
from alpha_factory.execution.strategy import CarryV3Adapter, RollingWindow

VERSIONING = dict(
    strategy_id=CARRY_V3_ID,
    data_version="2026-01-01T00:00+nocorr",
    git_commit_hash="deadbee",
    clock_drift_ms=10.0,
)

BTC_MARKS = {
    ("BTC-USDT", "spot"): 100_000.0,
    ("BTC-USDT", "perp_usdt"): 100_010.0,
}


# ── Fixtures ──────────────────────────────────────────────────────────────


def _long_short_book() -> dict:
    return {
        ("BTC-USDT", "spot"): Position(
            "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
        ),
        ("BTC-USDT", "perp_usdt"): Position(
            "BTC-USDT", "perp_usdt", -0.005, 100_010.0, -500.05,
        ),
    }


def _seed_active_book(log: Path) -> None:
    """Drive stage [4] to open a 2-leg position so the log has real fills."""
    from datetime import UTC, datetime, timedelta

    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    klines = pl.concat([
        pl.DataFrame({
            "symbol": ["BTC-USDT"] * 5, "market": ["spot"] * 5,
            "open_time": [anchor + timedelta(minutes=i) for i in range(5)],
            "close": [100_000.0 + i for i in range(5)],
        }),
        pl.DataFrame({
            "symbol": ["BTC-USDT"] * 5, "market": ["perp_usdt"] * 5,
            "open_time": [anchor + timedelta(minutes=i) for i in range(5)],
            "close": [100_010.0 + i for i in range(5)],
        }),
    ], how="vertical").with_columns(
        pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")),
    )
    funding = pl.DataFrame({
        "open_time": [anchor + timedelta(hours=8 * i) for i in range(130)],
        "funding_rate": [0.0001] * 130,
    }).with_columns(pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")))
    window = RollingWindow(funding=funding, klines=klines)
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    target = adapter.compute_target_position(window)
    step_paper_fill_sim(
        target, window,
        data_version=VERSIONING["data_version"],
        git_commit_hash=VERSIONING["git_commit_hash"],
        clock_drift_ms=VERSIONING["clock_drift_ms"],
        event_log_path=log,
    )


# ── P0 kill-flag primitives ───────────────────────────────────────────────


def test_kill_flag_absent_by_default(tmp_path: Path):
    assert kill_flag_present(tmp_path / "KILL") is False


def test_arm_then_present_then_clear(tmp_path: Path):
    flag = tmp_path / "KILL"
    arm_kill_flag(flag, reason="unit test")
    assert kill_flag_present(flag) is True
    clear_kill_flag(flag)
    assert kill_flag_present(flag) is False


def test_clear_absent_flag_is_noop(tmp_path: Path):
    clear_kill_flag(tmp_path / "KILL")   # must not raise


def test_check_kill_flag_no_flag_returns_no_halt(tmp_path: Path):
    decision = check_kill_flag(tmp_path / "KILL")
    assert decision == HaltDecision(should_halt=False, reason=None, detail="")


def test_check_kill_flag_present_returns_manual_halt(tmp_path: Path):
    flag = tmp_path / "KILL"
    arm_kill_flag(flag)
    decision = check_kill_flag(flag)
    assert decision.should_halt is True
    assert decision.reason == HALT_REASON_MANUAL


# ── compute_unwind_fills ──────────────────────────────────────────────────


def test_unwind_long_position_is_a_sell():
    book = {("BTC-USDT", "spot"): Position(
        "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
    )}
    fills, left_open = compute_unwind_fills(book, BTC_MARKS)
    assert left_open == []
    assert len(fills) == 1
    assert fills[0].side == "sell"
    assert fills[0].quantity_base == pytest.approx(0.005)


def test_unwind_short_position_is_a_buy():
    book = {("BTC-USDT", "perp_usdt"): Position(
        "BTC-USDT", "perp_usdt", -0.005, 100_010.0, -500.05,
    )}
    fills, left_open = compute_unwind_fills(book, BTC_MARKS)
    assert len(fills) == 1
    assert fills[0].side == "buy"


def test_unwind_uses_perp_fee_for_perp_leg():
    book = {("BTC-USDT", "perp_usdt"): Position(
        "BTC-USDT", "perp_usdt", -0.01, 100_000.0, -1_000.0,
    )}
    fills, _ = compute_unwind_fills(book, {("BTC-USDT", "perp_usdt"): 100_000.0})
    # perp taker 5bp on $1000 notional = $0.50
    assert fills[0].fee_bp == 5.0
    assert fills[0].fee_paid_quote == pytest.approx(0.50)


def test_unwind_flat_book_returns_nothing():
    fills, left_open = compute_unwind_fills({}, BTC_MARKS)
    assert fills == []
    assert left_open == []


def test_unwind_missing_mark_leaves_position_open(caplog):
    book = {("BTC-USDT", "spot"): Position(
        "BTC-USDT", "spot", +0.005, 100_000.0, +500.0,
    )}
    with caplog.at_level("ERROR"):
        fills, left_open = compute_unwind_fills(book, {})   # no marks
    assert fills == []
    assert left_open == [("BTC-USDT", "spot")]
    assert "LEFT OPEN" in caplog.text


def test_unwind_skips_dust_position():
    book = {("BTC-USDT", "spot"): Position(
        "BTC-USDT", "spot", 1e-15, 100_000.0, 1e-10,
    )}
    fills, left_open = compute_unwind_fills(book, BTC_MARKS)
    assert fills == []
    assert left_open == []


# ── execute_halt ──────────────────────────────────────────────────────────


def test_execute_halt_manual_emits_kill_then_unwinds_then_action(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _seed_active_book(log)
    result = execute_halt(
        HALT_REASON_MANUAL, mark_prices=BTC_MARKS, **VERSIONING,
        event_log_path=log,
    )
    kinds = [e["kind"] for e in read_events(log)]
    # ... 2 opening fills, then kill_flag_observed, 2 unwinds, halt_action_fired
    assert kinds[-4:] == [
        "kill_flag_observed", "unwind_simulated", "unwind_simulated",
        "halt_action_fired",
    ]
    assert result.n_positions_closed == 2
    assert result.n_positions_left_open == 0


def test_execute_halt_leaves_book_flat_after_replay(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _seed_active_book(log)
    assert len(replay_position_book(log)) == 2   # active before halt
    result = execute_halt(
        HALT_REASON_MANUAL, mark_prices=BTC_MARKS, **VERSIONING,
        event_log_path=log,
    )
    # The KEY correctness invariant: unwind events fold into replay, so a
    # post-halt restart sees a flat book.
    assert result.book_after == {}
    assert replay_position_book(log) == {}


def test_execute_halt_crash_has_no_kill_flag_observed(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _seed_active_book(log)
    execute_halt(
        HALT_REASON_CRASH, mark_prices=BTC_MARKS, detail="boom", **VERSIONING,
        event_log_path=log,
    )
    kinds = [e["kind"] for e in read_events(log)]
    assert "kill_flag_observed" not in kinds
    action = [e for e in read_events(log) if e["kind"] == "halt_action_fired"][0]
    assert action["data"]["reason"] == HALT_REASON_CRASH
    assert action["data"]["detail"] == "boom"


def test_execute_halt_records_left_open_when_unmarkable(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _seed_active_book(log)
    # Only mark the spot leg; perp leg cannot be priced.
    partial_marks = {("BTC-USDT", "spot"): 100_000.0}
    result = execute_halt(
        HALT_REASON_MANUAL, mark_prices=partial_marks, **VERSIONING,
        event_log_path=log,
    )
    assert result.n_positions_closed == 1
    assert result.n_positions_left_open == 1
    # The unmarkable perp position is still open after halt.
    book_after = replay_position_book(log)
    assert set(book_after) == {("BTC-USDT", "perp_usdt")}
    action = [e for e in read_events(log) if e["kind"] == "halt_action_fired"][0]
    assert action["data"]["left_open"] == [["BTC-USDT", "perp_usdt"]]


def test_execute_halt_flat_book_closes_nothing(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    result = execute_halt(
        HALT_REASON_MANUAL, mark_prices=BTC_MARKS, **VERSIONING,
        event_log_path=log,
    )
    assert result.n_positions_closed == 0
    kinds = [e["kind"] for e in read_events(log)]
    assert kinds == ["kill_flag_observed", "halt_action_fired"]


# ── replay folds unwind (correctness invariant, also tested via halt) ─────


def test_replay_folds_standalone_unwind_event(tmp_path: Path):
    """A bare unwind_simulated event must move the book (not be ignored)."""
    log = tmp_path / "events.jsonl"
    from alpha_factory.execution.event_log import append_event

    open_fill = make_event(
        "fill_simulated", strategy_id=CARRY_V3_ID, symbol="BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
        data={
            "symbol": "BTC-USDT", "market": "spot", "side": "buy",
            "quantity_base": 0.005, "fill_price": 100_000.0,
            "notional_quote": 500.0, "fee_paid_quote": 0.375, "fee_bp": 7.5,
            "target_delta_notional": 500.0,
        },
    )
    append_event(open_fill, path=log)
    assert len(replay_position_book(log)) == 1

    close = make_event(
        "unwind_simulated", strategy_id=CARRY_V3_ID, symbol="BTC-USDT",
        data_version="x", git_commit_hash="y",
        process_clock_drift_vs_binance_ms=None,
        data={
            "symbol": "BTC-USDT", "market": "spot", "side": "sell",
            "quantity_base": 0.005, "fill_price": 101_000.0,
            "notional_quote": 505.0, "fee_paid_quote": 0.378, "fee_bp": 7.5,
            "target_delta_notional": -500.0,
        },
    )
    append_event(close, path=log)
    assert replay_position_book(log) == {}   # unwind folded -> flat


# ── crash_halt_guard ──────────────────────────────────────────────────────


def test_crash_guard_reraises_and_fires_halt(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _seed_active_book(log)

    with pytest.raises(RuntimeError, match="cycle boom"), crash_halt_guard(
        BTC_MARKS, event_log_path=log, **VERSIONING,
    ):
        raise RuntimeError("cycle boom")

    # Halt fired: book flat, halt_action_fired records the crash.
    assert replay_position_book(log) == {}
    action = [e for e in read_events(log) if e["kind"] == "halt_action_fired"][0]
    assert action["data"]["reason"] == HALT_REASON_CRASH
    assert "cycle boom" in action["data"]["detail"]


def test_crash_guard_no_exception_is_passthrough(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    with crash_halt_guard(BTC_MARKS, event_log_path=log, **VERSIONING):
        pass
    assert read_events(log) == []   # nothing fired


# ── End-to-end: stage [4] active -> [6] kill -> flat -> [5] reconcile ────


def test_skeleton_active_then_kill_then_reconcile(tmp_path: Path):
    from datetime import UTC, datetime

    from alpha_factory.execution.reconcile import compute_daily_reconcile

    log = tmp_path / "events.jsonl"
    _seed_active_book(log)
    assert len(replay_position_book(log)) == 2

    arm_kill_flag(tmp_path / "KILL")
    decision = check_kill_flag(tmp_path / "KILL")
    assert decision.should_halt

    execute_halt(
        decision.reason, mark_prices=BTC_MARKS, detail=decision.detail,
        **VERSIONING, event_log_path=log,
    )
    assert replay_position_book(log) == {}

    # Reconcile the day the events were stamped (wall clock via make_event).
    today = datetime.now(tz=UTC).date()
    row = compute_daily_reconcile(today, BTC_MARKS, event_log_path=log)
    assert row.n_halts_today == 1
    assert row.n_simulated_fills_today == 2   # opening fills (unwind counted separately)
