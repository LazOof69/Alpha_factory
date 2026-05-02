"""Tests for `alpha_factory.data.archive` -- orchestrator, lock, breaker, cache.

No live network. The orchestrator's fetch phase is exercised via a fake
BinanceClient + monkeypatched klines/funding fetch primitives so we
control row counts deterministically.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from alpha_factory.data import archive as archive_mod
from alpha_factory.data.archive import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_RATE_LIMITED,
    LISTING_CACHE_SCHEMA_VERSION,
    Task,
    TaskResult,
    acquire_run_lock,
    build_listing_dates_for_qc,
    clear_breaker,
    expand_universe_to_tasks,
    is_breaker_tripped,
    load_listing_cache,
    release_run_lock,
    run_fetch_phase,
    run_one_task,
    save_listing_cache,
    summarize_results,
    trip_breaker,
)
from alpha_factory.data.binance_client import BinanceAPIError, BinanceRateLimitError
from alpha_factory.data.schema import UNIVERSE_SNAPSHOT_SCHEMA

# ── Fixtures ──────────────────────────────────────────────────────────────


def _universe_row(rank: int, symbol: str, base: str,
                  spot_pairs: list[str] | None = None,
                  listing_date: date | None = None):
    return {
        "as_of": datetime(2026, 5, 1, tzinfo=UTC),
        "period_start": date(2026, 5, 1), "period_end": date(2026, 5, 31),
        "rank": rank, "symbol": symbol, "api_symbol": symbol.replace("-", ""),
        "market": "perp_usdt", "base_asset": base, "quote_asset": "USDT",
        "quote_volume_24h": 1e9, "last_price": 100.0, "trade_count_24h": 100_000,
        "avg_trade_size": 100.0,
        "listing_date": listing_date or date(2024, 1, 1),
        "spot_pairs": spot_pairs or [],
        "primary_spot_quote_volume": 1e8,
        "total_candidates": 200, "min_listing_days_threshold": 180,
        "method": "live_24hr_top_n_v2",
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
        "source": "binance/fapi/v1/ticker/24hr",
        "fapi_exchangeinfo_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "fapi_ticker_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "spot_exchangeinfo_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "spot_ticker_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
    }


def _universe(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=UNIVERSE_SNAPSHOT_SCHEMA)


# ── Run lock ──────────────────────────────────────────────────────────────


def test_acquire_run_lock_creates_file(tmp_path):
    lock = tmp_path / "run.lock"
    assert acquire_run_lock(lock_file=lock) is True
    assert lock.exists()
    release_run_lock(lock_file=lock)
    assert not lock.exists()


def test_acquire_run_lock_blocks_when_recent_lock_held(tmp_path):
    lock = tmp_path / "run.lock"
    acquire_run_lock(lock_file=lock)
    # Second call should refuse (start_ts < 24h old, treated as live).
    assert acquire_run_lock(lock_file=lock) is False
    release_run_lock(lock_file=lock)


def test_acquire_run_lock_breaks_stale_lock(tmp_path):
    """Lock older than 24h is broken on subsequent acquire."""
    import json
    lock = tmp_path / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    lock.write_text(json.dumps({"pid": 99999, "start_ts": stale_ts, "hostname": "x"}))
    assert acquire_run_lock(lock_file=lock) is True
    release_run_lock(lock_file=lock)


# ── Circuit breaker ───────────────────────────────────────────────────────


def test_breaker_not_tripped_when_file_missing(tmp_path):
    tripped, _ = is_breaker_tripped(breaker_file=tmp_path / "br.json")
    assert tripped is False


def test_breaker_tripped_when_retry_after_in_future(tmp_path):
    br = tmp_path / "br.json"
    trip_breaker(reason="test", hours=24, breaker_file=br)
    tripped, payload = is_breaker_tripped(breaker_file=br)
    assert tripped is True
    assert "retry_after" in payload


def test_breaker_not_tripped_when_retry_after_past(tmp_path):
    br = tmp_path / "br.json"
    # Trip in the past so retry_after is also in the past.
    past = datetime(2020, 1, 1, tzinfo=UTC)
    trip_breaker(reason="test", hours=1, breaker_file=br, now=past)
    tripped, _ = is_breaker_tripped(breaker_file=br)
    assert tripped is False


def test_clear_breaker_removes_file(tmp_path):
    br = tmp_path / "br.json"
    trip_breaker(reason="test", breaker_file=br)
    assert br.exists()
    clear_breaker(breaker_file=br)
    assert not br.exists()


# ── Listing-date cache ────────────────────────────────────────────────────


def test_listing_cache_roundtrip(tmp_path):
    cf = tmp_path / "cache.json"
    cache = {("BTC-USDT", "perp_usdt"): date(2019, 9, 25),
             ("ETH-USDT", "spot"): date(2017, 8, 17)}
    save_listing_cache(cache, cache_file=cf)
    assert cf.exists()
    loaded = load_listing_cache(cache_file=cf)
    assert loaded == cache


def test_listing_cache_drops_on_schema_mismatch(tmp_path):
    import json
    cf = tmp_path / "cache.json"
    cf.write_text(json.dumps({
        "schema_version": LISTING_CACHE_SCHEMA_VERSION + 99,
        "entries": {"BTC-USDT:perp_usdt": "2019-09-25"},
    }))
    assert load_listing_cache(cache_file=cf) == {}


def test_listing_cache_missing_file_returns_empty(tmp_path):
    assert load_listing_cache(cache_file=tmp_path / "nope.json") == {}


# ── Task expansion ────────────────────────────────────────────────────────


def test_expand_emits_perp_funding_per_row():
    universe = _universe([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=[]),
        _universe_row(5, "ETH-USDT", "ETH", spot_pairs=[]),
    ])
    tasks = expand_universe_to_tasks(universe)
    markets = [t.market for t in tasks]
    # Per row: perp + funding (no spot since spot_pairs empty in fixture).
    assert markets == ["perp_usdt", "perp_funding", "perp_usdt", "perp_funding"]


def test_expand_emits_per_spot_pair():
    universe = _universe([
        _universe_row(1, "ETH-USDT", "ETH",
                      spot_pairs=["ETHUSDT", "ETHFDUSD", "ETHUSDC"]),
    ])
    tasks = expand_universe_to_tasks(universe)
    spot_syms = sorted(t.symbol for t in tasks if t.market == "spot")
    assert spot_syms == ["ETH-FDUSD", "ETH-USDC", "ETH-USDT"]
    # Per-symbol atomicity: spots come between perp and funding for the same row.
    markets = [t.market for t in tasks]
    assert markets[0] == "perp_usdt"
    assert all(m == "spot" for m in markets[1:4])
    assert markets[4] == "perp_funding"


def test_expand_orders_by_rank():
    universe = _universe([
        _universe_row(20, "RIVER-USDT", "RIVER"),
        _universe_row(1, "BTC-USDT", "BTC"),
        _universe_row(5, "SOL-USDT", "SOL"),
    ])
    tasks = expand_universe_to_tasks(universe)
    # First task per symbol = perp; their order should be by rank ascending.
    perp_order = [t.symbol for t in tasks if t.market == "perp_usdt"]
    assert perp_order == ["BTC-USDT", "SOL-USDT", "RIVER-USDT"]


# ── run_one_task ──────────────────────────────────────────────────────────


class FakeClient:
    def get_json(self, url, params):
        return []


def test_run_one_task_perp_skips_when_no_archive_no_force(monkeypatch):
    """No archive + force_cold_start=False -> SKIP."""
    monkeypatch.setattr(archive_mod.klines_mod, "last_archived_open_time", lambda *_: None)
    task = Task("BTC-USDT", "perp_usdt", 1, date(2024, 1, 1))
    outcome, n = run_one_task(task, datetime(2026, 5, 1, tzinfo=UTC),
                              False, {}, FakeClient())
    assert outcome == "skipped_no_resume"
    assert n == 0


def test_run_one_task_perp_resumes_when_archive_present(monkeypatch):
    """Archive has data -> resume from last+1h, calls fetch_klines."""
    last = datetime(2026, 4, 30, 23, tzinfo=UTC)
    monkeypatch.setattr(archive_mod.klines_mod,
                        "last_archived_open_time", lambda *_: last)
    captured: dict = {}

    def fake_fetch(symbol, market, start, end, client):
        captured["start"] = start
        captured["end"] = end
        return pl.DataFrame({"x": [1, 2, 3]})  # 3 rows

    def fake_write(df):
        captured["wrote"] = df.shape[0]

    monkeypatch.setattr(archive_mod.klines_mod, "fetch_klines", fake_fetch)
    monkeypatch.setattr(archive_mod.klines_mod, "write_klines_partitioned", fake_write)
    task = Task("BTC-USDT", "perp_usdt", 1, date(2019, 9, 25))
    outcome, n = run_one_task(task, datetime(2026, 5, 2, tzinfo=UTC),
                              False, {}, FakeClient())
    assert outcome == "done"
    assert n == 3
    # Resume cursor advances exactly 1h past last archived.
    assert captured["start"] == last + timedelta(hours=1)


def test_run_one_task_perp_cold_start_uses_snapshot_listing(monkeypatch):
    monkeypatch.setattr(archive_mod.klines_mod, "last_archived_open_time", lambda *_: None)
    captured: dict = {}

    def fake_fetch(symbol, market, start, end, client):
        captured["start"] = start
        return pl.DataFrame({"x": [1]})

    monkeypatch.setattr(archive_mod.klines_mod, "fetch_klines", fake_fetch)
    monkeypatch.setattr(archive_mod.klines_mod, "write_klines_partitioned", lambda _df: None)
    snap = date(2019, 9, 25)
    task = Task("BTC-USDT", "perp_usdt", 1, snap)
    outcome, _ = run_one_task(task, datetime(2026, 5, 1, tzinfo=UTC),
                              True, {}, FakeClient())
    assert outcome == "done"
    assert captured["start"].date() == snap


def test_run_one_task_spot_cold_start_probes_first_bar(monkeypatch):
    monkeypatch.setattr(archive_mod.klines_mod, "last_archived_open_time", lambda *_: None)
    monkeypatch.setattr(
        archive_mod.klines_mod, "probe_first_bar",
        lambda *_: datetime(2017, 8, 17, tzinfo=UTC),
    )
    monkeypatch.setattr(archive_mod.klines_mod, "fetch_klines",
                        lambda *_args, **_kw: pl.DataFrame({"x": [1]}))
    monkeypatch.setattr(archive_mod.klines_mod, "write_klines_partitioned", lambda _df: None)
    cache: dict = {}
    task = Task("BTC-USDT", "spot", 1, None)   # no snapshot listing for spot
    outcome, _ = run_one_task(task, datetime(2026, 5, 1, tzinfo=UTC),
                              True, cache, FakeClient())
    assert outcome == "done"
    # Probe result must be cached.
    assert cache[("BTC-USDT", "spot")] == date(2017, 8, 17)


def test_run_one_task_spot_probe_returns_none_skips(monkeypatch):
    monkeypatch.setattr(archive_mod.klines_mod, "last_archived_open_time", lambda *_: None)
    monkeypatch.setattr(archive_mod.klines_mod, "probe_first_bar", lambda *_: None)
    task = Task("UNLISTED-USDT", "spot", 1, None)
    outcome, _ = run_one_task(task, datetime(2026, 5, 1, tzinfo=UTC),
                              True, {}, FakeClient())
    assert outcome == "skipped_no_data"


def test_run_one_task_funding_resumes(monkeypatch):
    last = datetime(2026, 4, 30, 16, tzinfo=UTC)
    monkeypatch.setattr(archive_mod.funding_mod,
                        "last_archived_funding_time", lambda *_: last)
    captured: dict = {}

    def fake_fetch(symbol, start, end, client):
        captured["start"] = start
        return pl.DataFrame({"x": [1, 2]})

    monkeypatch.setattr(archive_mod.funding_mod, "fetch_funding", fake_fetch)
    monkeypatch.setattr(archive_mod.funding_mod, "write_funding_partitioned", lambda _df: None)
    task = Task("BTC-USDT", "perp_funding", 1, date(2019, 9, 25))
    outcome, _ = run_one_task(task, datetime(2026, 5, 1, tzinfo=UTC),
                              False, {}, FakeClient())
    assert outcome == "done"
    assert captured["start"] == last + timedelta(milliseconds=1)


# ── run_fetch_phase rate-limit short-circuit ──────────────────────────────


def test_fetch_phase_aborts_on_rate_limit(monkeypatch):
    def fake_run_one(task, *args, **_kw):
        if task.symbol == "ETH-USDT":
            raise BinanceRateLimitError("banned")
        return "done", 1

    monkeypatch.setattr(archive_mod, "run_one_task", fake_run_one)
    tasks = [
        Task("BTC-USDT", "perp_usdt", 1, date(2024, 1, 1)),
        Task("ETH-USDT", "perp_usdt", 2, date(2024, 1, 1)),
        Task("SOL-USDT", "perp_usdt", 3, date(2024, 1, 1)),  # never reached
    ]
    results, rl = run_fetch_phase(tasks, datetime(2026, 5, 1, tzinfo=UTC),
                                  False, {}, FakeClient())
    assert rl is True
    assert len(results) == 2  # BTC done + ETH error
    assert results[-1].outcome == "error"
    assert "rate_limit" in results[-1].error


def test_fetch_phase_continues_past_non_rate_limit_failures(monkeypatch):
    def fake_run_one(task, *args, **_kw):
        if task.symbol == "ETH-USDT":
            raise BinanceAPIError("transient nope")
        return "done", 1

    monkeypatch.setattr(archive_mod, "run_one_task", fake_run_one)
    tasks = [
        Task("BTC-USDT", "perp_usdt", 1, date(2024, 1, 1)),
        Task("ETH-USDT", "perp_usdt", 2, date(2024, 1, 1)),
        Task("SOL-USDT", "perp_usdt", 3, date(2024, 1, 1)),
    ]
    results, rl = run_fetch_phase(tasks, datetime(2026, 5, 1, tzinfo=UTC),
                                  False, {}, FakeClient())
    assert rl is False
    assert len(results) == 3
    assert results[1].outcome == "error"
    assert results[2].outcome == "done"


# ── build_listing_dates_for_qc ────────────────────────────────────────────


def test_build_listing_dates_uses_snapshot_for_perp(monkeypatch):
    monkeypatch.setattr(archive_mod.klines_mod, "_archive_first_open_time",
                        lambda *_: None)   # no archive -> use snapshot
    universe = _universe([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"],
                      listing_date=date(2019, 9, 25)),
    ])
    out = build_listing_dates_for_qc(universe, listing_cache={})
    assert out[("BTC-USDT", "perp_usdt")] == datetime(2019, 9, 25, tzinfo=UTC)


def test_build_listing_dates_uses_cache_for_spot(monkeypatch):
    monkeypatch.setattr(archive_mod.klines_mod, "_archive_first_open_time",
                        lambda *_: None)
    universe = _universe([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"]),
    ])
    cache = {("BTC-USDT", "spot"): date(2017, 8, 17)}
    out = build_listing_dates_for_qc(universe, listing_cache=cache)
    assert out[("BTC-USDT", "spot")] == datetime(2017, 8, 17, tzinfo=UTC)


# ── Summary ───────────────────────────────────────────────────────────────


def test_summarize_counts_done_skipped_failed():
    results = [
        TaskResult(Task("BTC-USDT", "perp_usdt", 1, date(2024, 1, 1)), "done", 100),
        TaskResult(Task("ETH-USDT", "perp_usdt", 2, date(2024, 1, 1)), "skipped_no_resume", 0),
        TaskResult(Task("SOL-USDT", "perp_usdt", 3, date(2024, 1, 1)), "error", 0, error="x"),
        TaskResult(Task("ADA-USDT", "perp_usdt", 4, date(2024, 1, 1)), "nothing", 0),
    ]
    out = summarize_results(results, qc_results=[], archive_size_bytes=12_345_678)
    assert "1 done" in out
    assert "1 nothing-to-fetch" in out
    assert "1 skipped" in out
    assert "1 failed" in out
    assert "12.3 MB" in out


# ── Exit code constants ───────────────────────────────────────────────────


def test_exit_codes_distinct_from_posix_misuse():
    """Per R1-5 critique: avoid exit 2 (POSIX 'usage error'). Use 10/11."""
    assert EXIT_OK == 0
    assert EXIT_FAIL == 10
    assert EXIT_RATE_LIMITED == 11
    assert EXIT_FAIL != 2
    assert EXIT_RATE_LIMITED != 2
