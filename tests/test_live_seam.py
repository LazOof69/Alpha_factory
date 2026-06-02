"""Tests for src/alpha_factory/execution/live_seam.py (Phase C stage [7]).

The walking-skeleton capstone. Verifies the execution seam (paper vs live
backends are interchangeable behind one Protocol), that LiveBackend places
NO real orders (raises), and that ``run_cycle`` wires the whole inner loop
— fetch -> strategy -> P0 gate -> crash-guarded execute — backend-
agnostically. The full-chain test runs run_cycle(paper) -> reconcile.

FakeBinanceClient mirrors test_forward_fetch's URL-dispatch fake; no real
network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from alpha_factory.alpha.carry_v3 import (
    STRATEGY_ID as CARRY_V3_ID,
)
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
)
from alpha_factory.data.schema import (
    FAPI_FUNDING_URL,
    FAPI_KLINES_URL,
    FAPI_TIME_URL,
    FUNDING_INTERVAL_MS,
    KLINE_INTERVAL_MS,
    SPOT_KLINES_URL,
)
from alpha_factory.execution.event_log import read_events
from alpha_factory.execution.halt import HALT_REASON_MANUAL, arm_kill_flag
from alpha_factory.execution.live_seam import (
    CycleStamp,
    ExecutionBackend,
    LiveBackend,
    PaperBackend,
    make_backend,
    run_cycle,
)
from alpha_factory.execution.strategy import (
    CarryV3Adapter,
    RollingWindow,
    Strategy,
    TargetLeg,
    TargetPosition,
)

_NOW = datetime.now(tz=UTC).replace(microsecond=0, second=0)
_BOUNDARY = _NOW.replace(hour=(_NOW.hour // 8) * 8, minute=0)


# ── Canned Binance responses (mirror test_forward_fetch) ──────────────────


def _funding_row(ms: int, rate: float = 0.0001) -> dict:
    return {
        "symbol": "BTCUSDT", "fundingTime": ms,
        "fundingRate": f"{rate:.8f}", "markPrice": "65000.00000000",
    }


def _kline_row(open_ms: int) -> list:
    close_ms = open_ms + KLINE_INTERVAL_MS - 1
    return [
        open_ms, "100000", "101000", "99000", "100000", "1000",
        close_ms, "100000000", 100, "500", "50000000", "0",
    ]


def _funding_series(n: int, rate: float = 0.0001) -> list[dict]:
    end_ms = int(_BOUNDARY.timestamp() * 1000)
    return [_funding_row(end_ms - i * FUNDING_INTERVAL_MS, rate) for i in range(n)][::-1]


def _kline_series(n: int) -> list[list]:
    end_ms = int(_BOUNDARY.timestamp() * 1000) - KLINE_INTERVAL_MS
    return [_kline_row(end_ms - i * KLINE_INTERVAL_MS) for i in range(n)][::-1]


class FakeBinanceClient:
    def __init__(self, responses: dict[str, list]) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        queue = self.responses.get(url, [])
        return queue.pop(0) if queue else []


def _active_client(n_funding: int = 130, n_klines: int = 1100) -> FakeBinanceClient:
    return FakeBinanceClient({
        FAPI_TIME_URL: [{"serverTime": int(datetime.now(tz=UTC).timestamp() * 1000)}],
        FAPI_FUNDING_URL: [_funding_series(n_funding)],
        SPOT_KLINES_URL: [_kline_series(n_klines)],
        FAPI_KLINES_URL: [_kline_series(n_klines)],
    })


def _adapter() -> CarryV3Adapter:
    return CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())


STAMP = CycleStamp(
    data_version="2026-01-01T00:00+nocorr", git_commit_hash="deadbee",
    clock_drift_ms=10.0,
)


class _BoomStrategy:
    """A strategy whose compute is fine but we force a crash in the backend."""

    def compute_target_position(self, window: RollingWindow) -> TargetPosition:
        return TargetPosition(
            strategy_id=CARRY_V3_ID, as_of=_NOW,
            legs=(TargetLeg("BTC-USDT", "spot", 500.0),), inputs_hash="x" * 64,
        )


class _CrashBackend:
    mode = "paper"

    def execute_target(self, target, window, stamp, *, event_log_path=None,
                       taker_fee_bps=None):
        raise RuntimeError("backend boom")


# ── Backend basics ────────────────────────────────────────────────────────


def test_paper_backend_mode():
    assert PaperBackend().mode == "paper"


def test_live_backend_mode():
    assert LiveBackend().mode == "live"


def test_backends_satisfy_protocol():
    assert isinstance(PaperBackend(), ExecutionBackend)
    assert isinstance(LiveBackend(), ExecutionBackend)


def test_make_backend_selects_paper_and_live():
    assert isinstance(make_backend("paper"), PaperBackend)
    assert isinstance(make_backend("live"), LiveBackend)


def test_make_backend_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown execution mode"):
        make_backend("simulation")


def test_strategy_protocol_smoke():
    """The adapter is a Strategy (seam consumes the stage-[2] protocol)."""
    assert isinstance(_adapter(), Strategy)


# ── PaperBackend / LiveBackend execute ────────────────────────────────────


def test_paper_backend_execute_writes_fills(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    klines = _window_klines()
    target = _adapter().compute_target_position(
        RollingWindow(funding=_window_funding(), klines=klines),
    )
    book, fills = PaperBackend().execute_target(
        target, RollingWindow(funding=_window_funding(), klines=klines), STAMP,
        event_log_path=log,
    )
    assert len(fills) == 2
    assert len(book) == 2
    assert all(e["kind"] == "fill_simulated" for e in read_events(log))


def test_live_backend_execute_raises_and_writes_nothing(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    target = TargetPosition(
        strategy_id=CARRY_V3_ID, as_of=_NOW,
        legs=(TargetLeg("BTC-USDT", "spot", 500.0),), inputs_hash="x" * 64,
    )
    with pytest.raises(NotImplementedError, match="LiveBackend is a stub"):
        LiveBackend().execute_target(
            target, RollingWindow(funding=_window_funding(), klines=_window_klines()),
            STAMP, event_log_path=log,
        )
    assert read_events(log) == []   # no orders, no events


# ── run_cycle ─────────────────────────────────────────────────────────────


def test_run_cycle_paper_happy_path(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    result = run_cycle(
        "BTC-USDT", _active_client(), _adapter(), PaperBackend(),
        event_log_path=log, kill_flag_path=tmp_path / "KILL",
        funding_root=tmp_path / "f", klines_root=tmp_path / "k",
        persist=False, git_commit_hash="deadbee",
    )
    assert result.halted is False
    assert result.mode == "paper"
    assert result.target is not None
    assert result.target.regime_state == 1
    assert len(result.fills) == 2
    assert len(result.book) == 2
    assert [e["kind"] for e in read_events(log)] == [
        "fill_simulated", "fill_simulated",
    ]


def test_run_cycle_p0_kill_gate_halts_before_execute(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    flag = tmp_path / "KILL"
    arm_kill_flag(flag)
    result = run_cycle(
        "BTC-USDT", _active_client(), _adapter(), PaperBackend(),
        event_log_path=log, kill_flag_path=flag,
        funding_root=tmp_path / "f", klines_root=tmp_path / "k",
        persist=False, git_commit_hash="deadbee",
    )
    assert result.halted is True
    assert result.halt_reason == HALT_REASON_MANUAL
    # No routine fills; book is flat (nothing was open to unwind on cycle 1).
    assert result.book == {}
    kinds = [e["kind"] for e in read_events(log)]
    assert "kill_flag_observed" in kinds
    assert "halt_action_fired" in kinds
    assert "fill_simulated" not in kinds


def test_run_cycle_crash_in_backend_fires_p1_and_reraises(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    with pytest.raises(RuntimeError, match="backend boom"):
        run_cycle(
            "BTC-USDT", _active_client(), _BoomStrategy(), _CrashBackend(),
            event_log_path=log, kill_flag_path=tmp_path / "KILL",
            funding_root=tmp_path / "f", klines_root=tmp_path / "k",
            persist=False, git_commit_hash="deadbee",
        )
    # P1 halt fired (book was flat so no unwinds, but the marker is there).
    kinds = [e["kind"] for e in read_events(log)]
    assert "halt_action_fired" in kinds


def test_run_cycle_live_backend_raises_not_implemented(tmp_path: Path):
    """The seam: same pipeline, live backend -> NotImplementedError (safe)."""
    log = tmp_path / "events.jsonl"
    with pytest.raises(NotImplementedError, match="LiveBackend is a stub"):
        run_cycle(
            "BTC-USDT", _active_client(), _adapter(), LiveBackend(),
            event_log_path=log, kill_flag_path=tmp_path / "KILL",
            funding_root=tmp_path / "f", klines_root=tmp_path / "k",
            persist=False, git_commit_hash="deadbee",
        )


def test_run_cycle_persists_forward_archive_when_enabled(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    froot = tmp_path / "f"
    kroot = tmp_path / "k"
    run_cycle(
        "BTC-USDT", _active_client(), _adapter(), PaperBackend(),
        event_log_path=log, kill_flag_path=tmp_path / "KILL",
        funding_root=froot, klines_root=kroot,
        persist=True, git_commit_hash="deadbee",
    )
    assert any(froot.glob("year=*/data.parquet"))
    assert any(kroot.glob("year=*/data.parquet"))


# ── Seam interchangeability ───────────────────────────────────────────────


def test_seam_same_pipeline_different_backend(tmp_path: Path):
    """One run_cycle call shape; paper fills, live raises — proves agnostic."""
    paper_log = tmp_path / "paper.jsonl"
    paper = run_cycle(
        "BTC-USDT", _active_client(), _adapter(), PaperBackend(),
        event_log_path=paper_log, kill_flag_path=tmp_path / "K1",
        funding_root=tmp_path / "fp", klines_root=tmp_path / "kp",
        persist=False, git_commit_hash="deadbee",
    )
    assert paper.mode == "paper" and len(paper.fills) == 2

    with pytest.raises(NotImplementedError):
        run_cycle(
            "BTC-USDT", _active_client(), _adapter(), LiveBackend(),
            event_log_path=tmp_path / "live.jsonl", kill_flag_path=tmp_path / "K2",
            funding_root=tmp_path / "fl", klines_root=tmp_path / "kl",
            persist=False, git_commit_hash="deadbee",
        )


# ── Full chain: run_cycle -> reconcile ────────────────────────────────────


def test_full_chain_run_cycle_then_reconcile(tmp_path: Path):
    from alpha_factory.execution.reconcile import compute_daily_reconcile

    log = tmp_path / "events.jsonl"
    result = run_cycle(
        "BTC-USDT", _active_client(), _adapter(), PaperBackend(),
        event_log_path=log, kill_flag_path=tmp_path / "KILL",
        funding_root=tmp_path / "f", klines_root=tmp_path / "k",
        persist=False, git_commit_hash="deadbee",
    )
    assert len(result.fills) == 2

    # Mark at the fill prices -> gross MTM 0 -> PnL = -fees ; tracking_error 0.
    marks = {
        (f.symbol, f.market): f.fill_price for f in result.fills
    }
    today = datetime.now(tz=UTC).date()
    row = compute_daily_reconcile(today, marks, event_log_path=log)
    assert row.n_simulated_fills_today == 2
    assert row.realized_pnl_quote_24h == row.replay_event_log_pnl_24h
    assert row.tracking_error == pytest.approx(0.0)
    assert row.realized_sharpe_to_date is None   # red line intact


# ── Local fixtures (synthetic window for direct backend tests) ────────────


def _window_funding(n: int = 130):
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    return pl.DataFrame({
        "open_time": [anchor + timedelta(hours=8 * i) for i in range(n)],
        "funding_rate": [0.0001] * n,
    }).with_columns(pl.col("open_time").cast(pl.Datetime("us", time_zone="UTC")))


def _window_klines():
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    return pl.concat([
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
