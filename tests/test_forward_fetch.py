"""Tests for src/alpha_factory/execution/forward_fetch.py (Phase C stage [1]).

Pattern mirrors tests/test_funding.py — a URL-dispatching FakeBinanceClient
feeds canned responses; persistence isolated to tmp_path; no real network.
Covers clock-drift sample, funding column rename (L1->execution boundary),
spot+perp klines concat, persist on/off, data_version format,
n_settlements trimming, and an end-to-end check that the result feeds
``CarryV3Adapter`` cleanly.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alpha_factory.alpha.carry_v3 import CarryV3Params
from alpha_factory.data.binance_client import BinanceAPIError
from alpha_factory.data.schema import (
    FAPI_FUNDING_URL,
    FAPI_KLINES_URL,
    FAPI_TIME_URL,
    FUNDING_INTERVAL_MS,
    KLINE_INTERVAL_MS,
    SPOT_KLINES_URL,
)
from alpha_factory.execution.forward_fetch import (
    ForwardFetchResult,
    fetch_forward_window,
    sample_clock_drift_ms,
)
from alpha_factory.execution.strategy import CarryV3Adapter

# Anchor near the current 8h funding boundary so fetch_funding's
# (start, end) window covers our canned rows regardless of when CI runs.
_NOW = datetime.now(tz=UTC).replace(microsecond=0, second=0)
_BOUNDARY = _NOW.replace(hour=(_NOW.hour // 8) * 8, minute=0)


def _funding_row(ms: int, rate: float = 0.0001, mark: float = 65000.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": ms,
        "fundingRate": f"{rate:.8f}",
        "markPrice": f"{mark:.8f}",
    }


def _kline_row(open_ms: int) -> list:
    """Minimal 12-tuple klines row (matches Binance spot v3 / fapi v1)."""
    close_ms = open_ms + KLINE_INTERVAL_MS - 1
    return [
        open_ms, "100", "101", "99", "100.5", "1000",
        close_ms, "100500", 100, "500", "50250", "0",
    ]


def _funding_series(n: int, rate: float = 0.0001) -> list[dict]:
    """``n`` ascending funding rows ending at the boundary just before _NOW."""
    end_ms = int(_BOUNDARY.timestamp() * 1000)
    return [
        _funding_row(end_ms - i * FUNDING_INTERVAL_MS, rate=rate)
        for i in range(n)
    ][::-1]


def _kline_series(n: int) -> list[list]:
    """``n`` ascending 1h kline rows ending one bar before ``_BOUNDARY``."""
    end_ms = int(_BOUNDARY.timestamp() * 1000) - KLINE_INTERVAL_MS
    return [
        _kline_row(end_ms - i * KLINE_INTERVAL_MS) for i in range(n)
    ][::-1]


class FakeBinanceClient:
    """URL-keyed fake Binance client.

    Each URL has a queue of canned responses; calls pop the head of the
    matching queue and record (url, params). Unknown URL or empty queue
    returns ``[]`` so paginating callers terminate.
    """

    def __init__(self, responses: dict[str, list]) -> None:
        self.responses: dict[str, list] = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        queue = self.responses.get(url, [])
        if not queue:
            return []
        return queue.pop(0)


class FailingTimeClient:
    """Time endpoint raises; everything else returns empty.

    Used to verify ``sample_clock_drift_ms`` swallows failures.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        if url == FAPI_TIME_URL:
            raise BinanceAPIError("simulated time-endpoint failure")
        return []


def _build_client_with_funding(
    n_funding: int = 130, n_klines: int = 1100, rate: float = 0.0001,
) -> FakeBinanceClient:
    """One canned response per URL, sized to cover one paper-trade cycle.

    ``serverTime`` is captured fresh at construction (not at module
    import) so the clock-drift sample, which calls ``datetime.now()``
    inside the production function, sees a server timestamp aligned
    with the test's wall clock. Module-level constants drift by tens
    of seconds while tests run.
    """
    server_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    return FakeBinanceClient({
        FAPI_TIME_URL: [{"serverTime": server_ms}],
        FAPI_FUNDING_URL: [_funding_series(n_funding, rate=rate)],
        SPOT_KLINES_URL: [_kline_series(n_klines)],
        FAPI_KLINES_URL: [_kline_series(n_klines)],
    })


# ── sample_clock_drift_ms ─────────────────────────────────────────────────


def test_sample_clock_drift_ms_returns_float_near_zero():
    """Server stub aligned to local -> drift inside the single-call jitter."""
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    client = FakeBinanceClient({FAPI_TIME_URL: [{"serverTime": now_ms}]})
    drift = sample_clock_drift_ms(client)
    assert drift is not None
    assert abs(drift) < 100   # no real I/O between bracket samples


def test_sample_clock_drift_ms_positive_when_server_ahead():
    """Server stub +5s ahead of local -> drift ≈ +5000."""
    ahead_ms = int(datetime.now(tz=UTC).timestamp() * 1000) + 5_000
    client = FakeBinanceClient({FAPI_TIME_URL: [{"serverTime": ahead_ms}]})
    drift = sample_clock_drift_ms(client)
    assert drift is not None
    assert drift > 4_000   # generous slack for OS clock tick between bracket samples


def test_sample_clock_drift_ms_returns_none_on_failure(caplog):
    with caplog.at_level("WARNING"):
        drift = sample_clock_drift_ms(FailingTimeClient())
    assert drift is None
    assert "clock_drift sample failed" in caplog.text


# ── fetch_forward_window basics ───────────────────────────────────────────


def test_fetch_forward_window_returns_forward_fetch_result(tmp_path: Path):
    client = _build_client_with_funding()
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    assert isinstance(result, ForwardFetchResult)
    assert result.fetched_at.tzinfo is not None  # tz-aware
    assert result.window.funding.height == 120   # trimmed from 130
    assert result.window.klines.height > 0


def test_fetch_forward_window_funding_open_time_renamed(tmp_path: Path):
    """L1->execution boundary contract: funding's time column is ``open_time``."""
    client = _build_client_with_funding()
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    cols = result.window.funding.columns
    assert "open_time" in cols
    assert "funding_time" not in cols
    assert "funding_rate" in cols    # the actual signal column survives


def test_fetch_forward_window_klines_has_both_markets(tmp_path: Path):
    client = _build_client_with_funding()
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    markets = set(result.window.klines["market"].unique().to_list())
    assert markets == {"spot", "perp_usdt"}


def test_fetch_forward_window_persists_when_enabled(tmp_path: Path):
    client = _build_client_with_funding()
    funding_root = tmp_path / "funding"
    klines_root = tmp_path / "klines"
    fetch_forward_window(
        "BTC-USDT", client,
        funding_root=funding_root, klines_root=klines_root,
        persist=True,
    )
    # Year-partitioned parquet landed for both archives.
    assert any(funding_root.glob("year=*/data.parquet"))
    assert any(klines_root.glob("year=*/data.parquet"))


def test_fetch_forward_window_skips_persist_when_disabled(tmp_path: Path):
    client = _build_client_with_funding()
    funding_root = tmp_path / "funding"
    klines_root = tmp_path / "klines"
    fetch_forward_window(
        "BTC-USDT", client,
        funding_root=funding_root, klines_root=klines_root,
        persist=False,
    )
    assert not funding_root.exists() or not any(funding_root.glob("**/*.parquet"))
    assert not klines_root.exists() or not any(klines_root.glob("**/*.parquet"))


def test_fetch_forward_window_data_version_format(tmp_path: Path):
    """v2 §2 fingerprint: ``YYYY-MM-DDTHH:MM+nocorr``."""
    client = _build_client_with_funding()
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    assert result.data_version.endswith("+nocorr")
    ts_part = result.data_version[: -len("+nocorr")]
    # Round-trip via fromisoformat (treats no-tz as naive; we only need shape).
    parsed = datetime.fromisoformat(ts_part)
    assert parsed.second == 0  # minute resolution


def test_fetch_forward_window_trims_to_n_settlements(tmp_path: Path):
    """Fetch returns 130 funding rows; n_settlements=50 -> window has 50."""
    client = _build_client_with_funding(n_funding=130, n_klines=200)
    result = fetch_forward_window(
        "BTC-USDT", client,
        n_settlements=50,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    assert result.window.funding.height == 50


def test_fetch_forward_window_empty_funding_does_not_raise(tmp_path: Path):
    """Cold start (no funding yet) returns a flat window, not an exception."""
    client = FakeBinanceClient({
        FAPI_TIME_URL: [{"serverTime": int(_NOW.timestamp() * 1000)}],
        FAPI_FUNDING_URL: [[]],
        SPOT_KLINES_URL: [_kline_series(10)],
        FAPI_KLINES_URL: [_kline_series(10)],
    })
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    assert result.window.funding.is_empty()
    assert result.data_version.endswith("+nocorr")


# ── End-to-end: stage [1] result feeds stage [2] adapter ──────────────────


def test_fetch_result_feeds_carry_v3_adapter(tmp_path: Path):
    """Skeleton smoke test: forward_fetch's output is adapter-compatible.

    Confirms the L1->execution boundary rename matches the
    strategy.py contract end-to-end. Positive funding for 130
    settlements + 120 trim -> adapter state 1 -> 2 delta-neutral legs.
    """
    client = _build_client_with_funding(n_funding=130, rate=0.0001)
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    adapter = CarryV3Adapter(symbol="BTC-USDT", params=CarryV3Params())
    target = adapter.compute_target_position(result.window)
    assert target.regime_state == 1
    assert len(target.legs) == 2
    assert {leg.market for leg in target.legs} == {"spot", "perp_usdt"}


def test_clock_drift_recorded_in_result(tmp_path: Path):
    client = _build_client_with_funding()
    result = fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    # Server-time stub == local _NOW; expect drift ≈ 0 within tens of ms.
    assert result.clock_drift_ms is not None
    assert abs(result.clock_drift_ms) < 1_000


# ── Defensive: order of underlying calls ──────────────────────────────────


@pytest.mark.parametrize(
    "expected_url",
    [FAPI_TIME_URL, FAPI_FUNDING_URL, SPOT_KLINES_URL, FAPI_KLINES_URL],
)
def test_fetch_forward_window_hits_each_endpoint(
    tmp_path: Path, expected_url: str,
):
    client = _build_client_with_funding()
    fetch_forward_window(
        "BTC-USDT", client,
        funding_root=tmp_path / "funding",
        klines_root=tmp_path / "klines",
    )
    urls = [u for u, _params in client.calls]
    assert expected_url in urls
