"""Tests for `alpha_factory.data.binance_client`.

Strategy: monkeypatch the inner `httpx.Client.get` with a programmable
sequence of responses (or exceptions). `time.sleep` is also patched so
backoff durations don't slow the test suite.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from alpha_factory.data import binance_client as bc_mod
from alpha_factory.data.binance_client import (
    NON_RETRYABLE_STATUSES,
    RETRYABLE_STATUSES,
    BinanceAPIError,
    BinanceClient,
    BinanceRateLimitError,
)

# ── Mock response + sequence ──────────────────────────────────────────────


class MockResponse:
    def __init__(
        self,
        status_code: int,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self) -> Any:
        return self._json


class GetCallSequence:
    """Replay a list of MockResponse / Exception items in call order."""

    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def __call__(self, url: str, params: dict[str, Any] | None = None,
                 **_kw: Any) -> MockResponse:
        self.calls.append((url, params))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def no_sleep(monkeypatch):
    """Don't actually sleep during retries."""
    monkeypatch.setattr(bc_mod.time, "sleep", lambda _s: None)


# ── Happy path ────────────────────────────────────────────────────────────


def test_get_json_returns_response_on_200(no_sleep, monkeypatch):
    seq = GetCallSequence([MockResponse(200, json_data={"k": "v"})])
    with BinanceClient() as c:
        monkeypatch.setattr(c._client, "get", seq)
        out = c.get_json("https://example.test/x", {"a": 1})
    assert out == {"k": "v"}
    assert len(seq.calls) == 1


# ── Retryable statuses ────────────────────────────────────────────────────


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_get_json_retries_on_retryable_status(status, no_sleep, monkeypatch):
    seq = GetCallSequence([
        MockResponse(status, headers={"Retry-After": "0.1"}),
        MockResponse(200, json_data=[1, 2, 3]),
    ])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        out = c.get_json("https://example.test/x", {})
    assert out == [1, 2, 3]
    assert len(seq.calls) == 2


def test_get_json_raises_after_exhausted_retries(no_sleep, monkeypatch):
    seq = GetCallSequence([MockResponse(503) for _ in range(3)])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceAPIError, match="max_retries exhausted"):
            c.get_json("https://example.test/x", {})
    assert len(seq.calls) == 3


# ── Non-retryable statuses (fail fast) ────────────────────────────────────


@pytest.mark.parametrize("status", sorted(NON_RETRYABLE_STATUSES))
def test_get_json_does_not_retry_on_non_retryable(status, no_sleep, monkeypatch):
    seq = GetCallSequence([
        MockResponse(status, text="bad"),
        MockResponse(200, json_data={"should": "not_reach"}),
    ])
    with BinanceClient(max_retries=5) as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceAPIError, match=f"status={status}"):
            c.get_json("https://example.test/x", {})
    assert len(seq.calls) == 1   # never retried


def test_get_json_raises_on_unknown_status(no_sleep, monkeypatch):
    seq = GetCallSequence([MockResponse(599, text="weird")])
    with BinanceClient() as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceAPIError, match="unexpected status=599"):
            c.get_json("https://example.test/x", {})


# ── Transient network errors ──────────────────────────────────────────────


def test_get_json_retries_on_connect_error(no_sleep, monkeypatch):
    seq = GetCallSequence([
        httpx.ConnectError("eof"),
        MockResponse(200, json_data={"ok": True}),
    ])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        out = c.get_json("https://example.test/x", {})
    assert out == {"ok": True}
    assert len(seq.calls) == 2


def test_get_json_retries_on_read_timeout(no_sleep, monkeypatch):
    seq = GetCallSequence([
        httpx.ReadTimeout("slow"),
        MockResponse(200, json_data="ok"),
    ])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        assert c.get_json("https://example.test/x", {}) == "ok"


def test_get_json_raises_after_persistent_transient(no_sleep, monkeypatch):
    seq = GetCallSequence([
        httpx.ConnectError("eof"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("bad"),
    ])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceAPIError, match="last error"):
            c.get_json("https://example.test/x", {})


# ── Backoff schedule ──────────────────────────────────────────────────────


def test_backoff_grows_exponentially_then_caps():
    c = BinanceClient(base_backoff_s=1.0, max_backoff_s=10.0)
    assert c._backoff(0) == 1.0
    assert c._backoff(1) == 2.0
    assert c._backoff(2) == 4.0
    assert c._backoff(3) == 8.0
    # Capped at max.
    assert c._backoff(4) == 10.0
    assert c._backoff(10) == 10.0


def test_backoff_honors_retry_after_header(no_sleep, monkeypatch):
    """When Binance sends Retry-After, we honor it (not exponential)."""
    seq = GetCallSequence([
        MockResponse(429, headers={"Retry-After": "7"}),
        MockResponse(200, json_data="ok"),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(bc_mod.time, "sleep", lambda s: sleeps.append(s))
    with BinanceClient(base_backoff_s=1.0, max_backoff_s=60.0) as c:
        monkeypatch.setattr(c._client, "get", seq)
        c.get_json("https://example.test/x", {})
    # The single retry should have slept 7s (from Retry-After), not 1s (base_backoff).
    assert sleeps == [7.0]


# ── Rate-limit typed exceptions (A.3.2) ───────────────────────────────────


def test_rate_limit_error_is_subclass_of_api_error():
    """Orchestrator code that catches BinanceAPIError must still see rate-limit failures."""
    assert issubclass(BinanceRateLimitError, BinanceAPIError)


def test_status_418_raises_rate_limit_immediately(no_sleep, monkeypatch):
    """418 = IP banned. Non-retryable, must raise BinanceRateLimitError so orchestrator aborts."""
    seq = GetCallSequence([MockResponse(418, text="banned")])
    with BinanceClient() as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceRateLimitError, match="status=418|IP banned"):
            c.get_json("https://example.test/x", {})
    assert len(seq.calls) == 1


def test_status_429_exhaust_raises_rate_limit_not_plain_api_error(no_sleep, monkeypatch):
    """429 retried then exhausted -> BinanceRateLimitError (orchestrator should abort)."""
    seq = GetCallSequence([MockResponse(429) for _ in range(3)])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceRateLimitError, match="rate-limit"):
            c.get_json("https://example.test/x", {})


def test_status_503_exhaust_raises_plain_api_error_not_rate_limit(no_sleep, monkeypatch):
    """503 != rate-limit. Exhaust -> BinanceAPIError (orchestrator continues to next symbol)."""
    seq = GetCallSequence([MockResponse(503) for _ in range(3)])
    with BinanceClient(max_retries=3) as c:
        monkeypatch.setattr(c._client, "get", seq)
        with pytest.raises(BinanceAPIError) as excinfo:
            c.get_json("https://example.test/x", {})
    # Must be the plain class, not the rate-limit subclass.
    assert not isinstance(excinfo.value, BinanceRateLimitError)


# ── Context manager closes underlying client ──────────────────────────────


def test_context_manager_closes_client():
    closed: list[bool] = []
    c = BinanceClient()
    orig_close = c._client.close
    c._client.close = lambda: (orig_close(), closed.append(True))[0]
    with c:
        pass
    assert closed == [True]
