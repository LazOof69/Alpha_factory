"""Unit tests for `alpha_factory.data.universe` (Phase A.2 v2).

Strategy: synthetic 4-endpoint Binance responses + dependency-injected
fetch_json. No network calls. Verifies POI semantics (period math + lookahead
guard), eligibility filters, rejection sidecar, atomicity guard, spot-pair
enumeration, and parquet round-trip schema stability.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from alpha_factory.data import universe as universe_mod
from alpha_factory.data.schema import (
    REJECTED_CANDIDATES_SCHEMA,
    REJECTION_REASON_INVALID_PRICE,
    REJECTION_REASON_LISTING_TOO_YOUNG,
    UNIVERSE_SNAPSHOT_SCHEMA,
)
from alpha_factory.data.universe import (
    DEFAULT_MARKET,
    RANK_METHOD_LIVE,
    _build_perp_candidates,
    _build_spot_pairs_index,
    _calendar_month_period,
    _split_eligible_and_rejected,
    fetch_live_universe,
    read_rejected,
    read_snapshot,
    snapshot_path,
    universe_as_of,
    write_snapshot,
)

# ── Fixture builders ──────────────────────────────────────────────────────


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


# A reference past date so listing-age math is deterministic across test runs.
AS_OF = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
ANCIENT = AS_OF.date() - timedelta(days=2000)   # safely > 180d
RECENT = AS_OF.date() - timedelta(days=30)      # < 180d, will fail listing-age


def _fapi_exchange_info() -> dict:
    return {
        "symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(ANCIENT)},
            {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(ANCIENT)},
            {"symbol": "SOLUSDT", "baseAsset": "SOL", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(ANCIENT)},
            # Should be EXCLUDED by quoteAsset filter:
            {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(ANCIENT)},
            # Should be EXCLUDED by contractType filter:
            {"symbol": "BTCUSDT_240329", "baseAsset": "BTC", "quoteAsset": "USDT",
             "contractType": "CURRENT_QUARTER", "status": "TRADING",
             "onboardDate": _ms(ANCIENT)},
            # Should be EXCLUDED by status filter:
            {"symbol": "DOGEUSDT", "baseAsset": "DOGE", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "BREAK",
             "onboardDate": _ms(ANCIENT)},
            # Candidate but REJECTED by listing-age (30d < 180d):
            {"symbol": "RIVERUSDT", "baseAsset": "RIVER", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(RECENT)},
            # Candidate but REJECTED by both invalid price AND young listing:
            {"symbol": "BADUSDT", "baseAsset": "BAD", "quoteAsset": "USDT",
             "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": _ms(RECENT)},
        ]
    }


def _fapi_ticker() -> list[dict]:
    return [
        {"symbol": "BTCUSDT", "quoteVolume": "10000000000",
         "lastPrice": "78000", "count": 2_800_000},
        {"symbol": "ETHUSDT", "quoteVolume": "6000000000",
         "lastPrice": "2300", "count": 3_200_000},
        {"symbol": "SOLUSDT", "quoteVolume": "1000000000",
         "lastPrice": "84",   "count": 940_000},
        {"symbol": "BTCUSDC", "quoteVolume": "20000000",
         "lastPrice": "78000", "count": 50_000},
        {"symbol": "RIVERUSDT", "quoteVolume": "120000000",
         "lastPrice": "6.5",  "count": 1_200_000},
        {"symbol": "BADUSDT",  "quoteVolume": "50000000",
         "lastPrice": "0",    "count": 100_000},
    ]


def _spot_exchange_info() -> dict:
    return {
        "symbols": [
            # BTC has TWO spot pairs across allowed quotes
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "status": "TRADING"},
            {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC",
             "status": "TRADING"},
            # ETH has USDT + FDUSD pairs
            {"symbol": "ETHUSDT",  "baseAsset": "ETH",  "quoteAsset": "USDT",
             "status": "TRADING"},
            {"symbol": "ETHFDUSD", "baseAsset": "ETH",  "quoteAsset": "FDUSD",
             "status": "TRADING"},
            # SOL only USDT
            {"symbol": "SOLUSDT", "baseAsset": "SOL", "quoteAsset": "USDT",
             "status": "TRADING"},
            # WBETH wrapper — DIFFERENT baseAsset, must NOT be attributed to ETH
            {"symbol": "WBETHUSDT", "baseAsset": "WBETH", "quoteAsset": "USDT",
             "status": "TRADING"},
            # Halted spot: must be filtered out
            {"symbol": "ETHEUR", "baseAsset": "ETH", "quoteAsset": "EUR",
             "status": "TRADING"},  # excluded by quote-asset filter
            {"symbol": "BTCUSDT_HALT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "status": "HALT"},  # excluded by status
        ]
    }


def _spot_ticker() -> list[dict]:
    return [
        {"symbol": "BTCUSDT",   "quoteVolume": "5000000000"},
        {"symbol": "BTCUSDC",   "quoteVolume": "2000000000"},
        {"symbol": "ETHUSDT",   "quoteVolume": "3000000000"},
        {"symbol": "ETHFDUSD",  "quoteVolume": "1500000000"},
        {"symbol": "SOLUSDT",   "quoteVolume": "800000000"},
        {"symbol": "WBETHUSDT", "quoteVolume": "10000000"},
    ]


def _stub_fetch_json(url: str):
    if url.endswith("/fapi/v1/exchangeInfo"):
        return _fapi_exchange_info()
    if url.endswith("/fapi/v1/ticker/24hr"):
        return _fapi_ticker()
    if url.endswith("/api/v3/exchangeInfo"):
        return _spot_exchange_info()
    if url.endswith("/api/v3/ticker/24hr"):
        return _spot_ticker()
    raise AssertionError(f"unexpected url in test stub: {url}")


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_calendar_month_period_mid_month():
    start, end = _calendar_month_period(datetime(2026, 5, 15, 12, 0, tzinfo=UTC))
    assert (start, end) == (date(2026, 5, 1), date(2026, 5, 31))


def test_calendar_month_period_february_leap_year():
    leap = _calendar_month_period(datetime(2024, 2, 1, tzinfo=UTC))
    nonleap = _calendar_month_period(datetime(2025, 2, 1, tzinfo=UTC))
    assert leap == (date(2024, 2, 1), date(2024, 2, 29))
    assert nonleap == (date(2025, 2, 1), date(2025, 2, 28))


def test_calendar_month_period_december_year_boundary():
    start, end = _calendar_month_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert (start, end) == (date(2026, 12, 1), date(2026, 12, 31))


# ── Candidate construction ────────────────────────────────────────────────


def test_build_perp_candidates_filters_to_perpetual_trading_usdt():
    df = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    api_syms = sorted(df.get_column("api_symbol").to_list())
    # BTCUSDC (wrong quote), quarterly contract, DOGE BREAK status all dropped.
    # RIVER and BAD survive structural filter — they fail eligibility later.
    assert api_syms == ["BADUSDT", "BTCUSDT", "ETHUSDT", "RIVERUSDT", "SOLUSDT"]


def test_build_perp_candidates_carries_onboard_date():
    df = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    river = df.filter(pl.col("api_symbol") == "RIVERUSDT")
    assert river.shape[0] == 1
    assert river.item(0, "onboard_date_ms") == _ms(RECENT)


# ── Spot index construction ───────────────────────────────────────────────


def test_spot_pairs_index_groups_by_base_asset():
    idx = _build_spot_pairs_index(_spot_exchange_info(), _spot_ticker())
    btc_pairs = sorted(p[0] for p in idx["BTC"])
    eth_pairs = sorted(p[0] for p in idx["ETH"])
    sol_pairs = sorted(p[0] for p in idx["SOL"])
    assert btc_pairs == ["BTCUSDC", "BTCUSDT"]
    assert eth_pairs == ["ETHFDUSD", "ETHUSDT"]
    assert sol_pairs == ["SOLUSDT"]
    # WBETH must NOT be attributed to ETH despite being an ETH wrapper.
    assert "WBETH" in idx and "ETH" not in {p[0] for p in idx["WBETH"]}
    # ETHEUR (wrong quote) and BTCUSDT_HALT (not trading) must be absent.
    assert "EUR" not in {pair for pairs in idx.values() for pair, _ in pairs}


# ── Eligibility filtering & rejection sidecar ─────────────────────────────


def test_split_eligible_rejects_young_listing():
    candidates = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    eligible, rejection_rows = _split_eligible_and_rejected(
        candidates, AS_OF, min_listing_days=180
    )
    eligible_syms = sorted(eligible.get_column("api_symbol").to_list())
    assert eligible_syms == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    rejected_syms = sorted({r["api_symbol"] for r in rejection_rows})
    assert rejected_syms == ["BADUSDT", "RIVERUSDT"]


def test_split_eligible_records_listing_reason():
    candidates = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    _, rejection_rows = _split_eligible_and_rejected(candidates, AS_OF, 180)
    river_rejections = [r for r in rejection_rows if r["api_symbol"] == "RIVERUSDT"]
    assert len(river_rejections) == 1
    assert river_rejections[0]["reason"] == REJECTION_REASON_LISTING_TOO_YOUNG
    assert river_rejections[0]["threshold"] == 180.0
    assert river_rejections[0]["observed_value"] == 30.0
    assert river_rejections[0]["is_primary_reason"] is True


def test_split_eligible_multi_reason_marks_one_primary():
    """BADUSDT fails BOTH price (lastPrice=0) AND listing-age — two rows, one primary."""
    candidates = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    _, rejection_rows = _split_eligible_and_rejected(candidates, AS_OF, 180)
    bad_rows = [r for r in rejection_rows if r["api_symbol"] == "BADUSDT"]
    assert len(bad_rows) == 2
    primary = [r for r in bad_rows if r["is_primary_reason"]]
    secondary = [r for r in bad_rows if not r["is_primary_reason"]]
    assert len(primary) == 1
    assert len(secondary) == 1
    # First failure (price) is primary because filter order is price → listing.
    assert primary[0]["reason"] == REJECTION_REASON_INVALID_PRICE
    assert secondary[0]["reason"] == REJECTION_REASON_LISTING_TOO_YOUNG


def test_split_eligible_no_filter_means_no_rejections():
    candidates = _build_perp_candidates(_fapi_exchange_info(), _fapi_ticker(), "USDT")
    # min_listing_days=0 + non-zero prices → only BADUSDT (lastPrice=0) rejected.
    eligible, rejection_rows = _split_eligible_and_rejected(candidates, AS_OF, 0)
    rejected_syms = {r["api_symbol"] for r in rejection_rows}
    assert rejected_syms == {"BADUSDT"}
    assert "RIVERUSDT" in eligible.get_column("api_symbol").to_list()


# ── End-to-end fetch ──────────────────────────────────────────────────────


def test_fetch_returns_tuple_universe_and_rejected():
    universe, rejected = fetch_live_universe(
        n_top=5, as_of=AS_OF, fetch_json=_stub_fetch_json,
    )
    assert isinstance(universe, pl.DataFrame)
    assert isinstance(rejected, pl.DataFrame)
    assert universe.shape[0] == 3   # BTC, ETH, SOL after listing-age filter
    # Rejected must include both BAD and RIVER, with primary-reason rows.
    primary_rejected = rejected.filter(pl.col("is_primary_reason"))
    assert sorted(primary_rejected.get_column("api_symbol").to_list()) == [
        "BADUSDT", "RIVERUSDT",
    ]


def test_fetch_universe_ranks_descending_and_includes_audit_fields():
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    assert universe.get_column("rank").to_list() == [1, 2, 3]
    assert universe.get_column("symbol").to_list() == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    assert universe.get_column("method").unique().to_list() == [RANK_METHOD_LIVE]
    assert universe.get_column("market").unique().to_list() == [DEFAULT_MARKET]
    # total_candidates = 5 (BTC, ETH, SOL, RIVER, BAD pass structural filter).
    assert universe.get_column("total_candidates").unique().to_list() == [5]
    assert universe.get_column("min_listing_days_threshold").unique().to_list() == [180]


def test_fetch_universe_attaches_spot_pairs():
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    btc = universe.filter(pl.col("symbol") == "BTC-USDT")
    eth = universe.filter(pl.col("symbol") == "ETH-USDT")
    assert sorted(btc.item(0, "spot_pairs")) == ["BTCUSDC", "BTCUSDT"]
    assert sorted(eth.item(0, "spot_pairs")) == ["ETHFDUSD", "ETHUSDT"]
    # primary_spot_quote_volume = max across pairs (from fixture).
    assert btc.item(0, "primary_spot_quote_volume") == 5_000_000_000
    assert eth.item(0, "primary_spot_quote_volume") == 3_000_000_000


def test_fetch_universe_avg_trade_size_computed():
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    btc = universe.filter(pl.col("symbol") == "BTC-USDT")
    # 10_000_000_000 / 2_800_000 ≈ 3571.43
    assert btc.item(0, "avg_trade_size") == pytest.approx(10_000_000_000 / 2_800_000)


def test_fetch_universe_listing_date_frozen_in_row():
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    btc = universe.filter(pl.col("symbol") == "BTC-USDT")
    assert btc.item(0, "listing_date") == ANCIENT


def test_fetch_universe_all_4_endpoint_timestamps_populated():
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    for col in [
        "fapi_exchangeinfo_fetched_at",
        "fapi_ticker_fetched_at",
        "spot_exchangeinfo_fetched_at",
        "spot_ticker_fetched_at",
    ]:
        assert universe.get_column(col).null_count() == 0


def test_fetch_universe_schema_conforms():
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    assert universe.columns == list(UNIVERSE_SNAPSHOT_SCHEMA.keys())
    for col, dtype in UNIVERSE_SNAPSHOT_SCHEMA.items():
        assert universe.schema[col] == dtype, f"{col}: got {universe.schema[col]}, want {dtype}"
    assert rejected.columns == list(REJECTED_CANDIDATES_SCHEMA.keys())
    for col, dtype in REJECTED_CANDIDATES_SCHEMA.items():
        assert rejected.schema[col] == dtype


def test_fetch_universe_rejects_naive_as_of():
    with pytest.raises(ValueError, match="tz-aware"):
        fetch_live_universe(
            as_of=datetime(2026, 5, 1, 12, 0),  # naive
            fetch_json=_stub_fetch_json,
        )


def test_fetch_universe_normalizes_non_utc_tz():
    from datetime import timedelta as td
    from datetime import timezone

    tw_tz = timezone(td(hours=8))
    as_of_tw = datetime(2026, 5, 1, 20, 0, tzinfo=tw_tz)  # 12:00 UTC
    universe, _ = fetch_live_universe(as_of=as_of_tw, fetch_json=_stub_fetch_json)
    assert universe.item(0, "as_of") == as_of_tw.astimezone(UTC)


def test_fetch_universe_raises_when_no_perpetuals():
    def empty_fetch(url: str):
        if url.endswith("/exchangeInfo"):
            return {"symbols": []}
        return []

    with pytest.raises(RuntimeError, match="no perpetuals"):
        fetch_live_universe(as_of=AS_OF, fetch_json=empty_fetch)


def test_fetch_universe_raises_when_no_eligible():
    """All structural-filter survivors fail listing-age → no eligible."""
    with pytest.raises(RuntimeError, match="no perpetuals survived"):
        # min_listing_days enormous → everyone fails.
        fetch_live_universe(
            as_of=AS_OF, fetch_json=_stub_fetch_json, min_listing_days=10_000,
        )


# ── Atomicity guard ────────────────────────────────────────────────────────


def test_atomicity_guard_raises_when_endpoint_window_exceeded(monkeypatch):
    """Mock `_now_utc` so the wall-clock between first and last fetch >5s.

    With `as_of` supplied explicitly, fetch_live_universe makes exactly 5
    `_now_utc` calls inside `_fetch_all_endpoints` (t_start + 4 t_*),
    followed by 1 for `ingested_at` after the guard has passed.
    """
    fake_clock = iter([
        AS_OF,                                  # t_start
        AS_OF + timedelta(seconds=1),           # after fapi_exchange_info
        AS_OF + timedelta(seconds=2),           # after fapi_ticker
        AS_OF + timedelta(seconds=4),           # after spot_exchange_info
        AS_OF + timedelta(seconds=10),          # after spot_ticker — exceeds 5s
    ])
    monkeypatch.setattr(universe_mod, "_now_utc", lambda: next(fake_clock))

    with pytest.raises(RuntimeError, match="window"):
        fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)


def test_atomicity_guard_passes_within_window(monkeypatch):
    """Same wiring but spread well under 5s → no error."""
    fake_clock = iter([
        AS_OF + timedelta(seconds=t) for t in (0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
    ])
    monkeypatch.setattr(universe_mod, "_now_utc", lambda: next(fake_clock))
    universe, _ = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    assert universe.shape[0] == 3


# ── Disk roundtrip ─────────────────────────────────────────────────────────


def test_snapshot_path_format(tmp_path: Path):
    p = snapshot_path(date(2026, 5, 1), "perp_usdt", tmp_path)
    assert p == tmp_path / "perp_usdt" / "snapshot_2026-05.parquet"


def test_write_snapshot_writes_both_files(tmp_path: Path):
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    p_uni, p_rej = write_snapshot(universe, rejected, root=tmp_path)
    assert p_uni.exists()
    assert p_rej.exists()
    assert p_rej.name == "rejected_2026-05.parquet"


def test_roundtrip_preserves_tz_aware_datetime_columns(tmp_path: Path):
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    loaded = read_snapshot(date(2026, 5, 1), root=tmp_path)
    for col in [
        "as_of", "ingested_at",
        "fapi_exchangeinfo_fetched_at", "fapi_ticker_fetched_at",
        "spot_exchangeinfo_fetched_at", "spot_ticker_fetched_at",
    ]:
        assert loaded.schema[col] == UNIVERSE_SNAPSHOT_SCHEMA[col]


def test_roundtrip_preserves_spot_pairs_list(tmp_path: Path):
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    loaded = read_snapshot(date(2026, 5, 1), root=tmp_path)
    btc = loaded.filter(pl.col("symbol") == "BTC-USDT")
    assert sorted(btc.item(0, "spot_pairs")) == ["BTCUSDC", "BTCUSDT"]


def test_read_rejected_returns_sidecar(tmp_path: Path):
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    loaded_rej = read_rejected(date(2026, 5, 1), root=tmp_path)
    assert sorted(
        loaded_rej.filter(pl.col("is_primary_reason")).get_column("api_symbol").to_list()
    ) == ["BADUSDT", "RIVERUSDT"]


def test_audit_count_reconciles(tmp_path: Path):
    """total_candidates == top_n_kept + unique_rejected_symbols + tail (eligible-but-not-top-n)."""
    universe, rejected = fetch_live_universe(
        n_top=2, as_of=AS_OF, fetch_json=_stub_fetch_json,
    )
    total = universe.item(0, "total_candidates")
    unique_rejected = rejected.filter(pl.col("is_primary_reason")).shape[0]
    kept = universe.shape[0]
    eligible = total - unique_rejected
    tail = eligible - kept
    # 5 total candidates - 2 rejected (BAD, RIVER) = 3 eligible; top 2 kept; tail = 1.
    assert total == 5 and unique_rejected == 2 and kept == 2 and tail == 1


def test_write_refuses_empty_universe(tmp_path: Path):
    empty_universe = pl.DataFrame(schema=UNIVERSE_SNAPSHOT_SCHEMA)
    empty_rejected = pl.DataFrame(schema=REJECTED_CANDIDATES_SCHEMA)
    with pytest.raises(ValueError, match="empty universe"):
        write_snapshot(empty_universe, empty_rejected, root=tmp_path)


# ── universe_as_of (POI semantics) ────────────────────────────────────────


def test_universe_as_of_loads_at_or_after_snapshot_as_of(tmp_path: Path):
    as_of = datetime(2026, 5, 15, tzinfo=UTC)
    universe, rejected = fetch_live_universe(as_of=as_of, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    assert universe_as_of(date(2026, 5, 15), root=tmp_path).shape[0] == universe.shape[0]
    assert universe_as_of(date(2026, 5, 31), root=tmp_path).shape[0] == universe.shape[0]


def test_universe_as_of_rejects_lookahead(tmp_path: Path):
    as_of = datetime(2026, 5, 15, tzinfo=UTC)
    universe, rejected = fetch_live_universe(as_of=as_of, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    with pytest.raises(ValueError, match="later than|lookahead"):
        universe_as_of(date(2026, 5, 1), root=tmp_path)


def test_universe_as_of_rejects_other_month(tmp_path: Path):
    universe, rejected = fetch_live_universe(as_of=AS_OF, fetch_json=_stub_fetch_json)
    write_snapshot(universe, rejected, root=tmp_path)
    with pytest.raises(FileNotFoundError):
        universe_as_of(date(2026, 6, 1), root=tmp_path)
