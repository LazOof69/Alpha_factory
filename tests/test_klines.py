"""Tests for `alpha_factory.data.klines`.

Strategy: synthetic Binance kline rows + dependency-injected
`BinanceClient` (pure mock with `get_json`). No network calls.
Verifies pagination, partial-bar guard, correction-diff numerical
tolerance, atomic write, listing-date authority.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from alpha_factory.data.klines import (
    CORRECTION_RTOL,
    _detect_corrections,
    _endpoint_for,
    _rows_to_df,
    effective_listing_date,
    fetch_klines,
    last_archived_open_time,
    probe_first_bar,
    write_klines_partitioned,
)
from alpha_factory.data.schema import (
    CORRECTIONS_SCHEMA,
    KLINE_INTERVAL_MS,
    KLINES_SCHEMA,
    SOURCE_FAPI_KLINES,
    SOURCE_SPOT_KLINES,
)

# ── Synthetic kline rows ──────────────────────────────────────────────────


def _kline_row(open_ms: int, close_ms: int, base: float = 100.0) -> list:
    """A Binance-shaped 12-tuple at the given open_ms timestamp."""
    return [
        open_ms,
        f"{base}",       # open
        f"{base * 1.01}",   # high
        f"{base * 0.99}",   # low
        f"{base * 1.005}",  # close
        "1.5",            # volume
        close_ms,
        f"{base * 1.5}",  # quote_volume
        42,               # trades
        "0.7",            # taker_buy_base
        f"{base * 0.7}",  # taker_buy_quote
        "0",              # ignore
    ]


def _series_from(start_ms: int, n: int, base: float = 100.0) -> list[list]:
    """`n` consecutive 1h klines starting at `start_ms`."""
    out = []
    for i in range(n):
        o = start_ms + i * KLINE_INTERVAL_MS
        c = o + KLINE_INTERVAL_MS - 1
        out.append(_kline_row(o, c, base + i))
    return out


# Reference instants.
START_DT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
END_DT = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
START_MS = int(START_DT.timestamp() * 1000)


# ── Mock BinanceClient ────────────────────────────────────────────────────


class FakeClient:
    """Minimal fake BinanceClient stub: queue prescribed `get_json` returns."""

    def __init__(self, queue: list) -> None:
        self.queue = list(queue)
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        return self.queue.pop(0)


# ── _endpoint_for ─────────────────────────────────────────────────────────


def test_endpoint_for_spot():
    url, src = _endpoint_for("spot")
    assert "api.binance.com" in url
    assert src == SOURCE_SPOT_KLINES


def test_endpoint_for_perp():
    url, src = _endpoint_for("perp_usdt")
    assert "fapi.binance.com" in url
    assert src == SOURCE_FAPI_KLINES


def test_endpoint_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown market"):
        _endpoint_for("futures")


# ── _rows_to_df ───────────────────────────────────────────────────────────


def test_rows_to_df_empty_returns_empty_with_schema():
    df = _rows_to_df([], "BTC-USDT", "spot", "binance_spot_v3")
    assert df.is_empty()
    assert df.schema == KLINES_SCHEMA


def test_rows_to_df_typed_correctly():
    rows = _series_from(START_MS, 3)
    df = _rows_to_df(rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    assert df.shape[0] == 3
    assert df.schema == KLINES_SCHEMA
    assert df.get_column("symbol").unique().to_list() == ["BTC-USDT"]
    assert df.get_column("market").unique().to_list() == ["perp_usdt"]
    assert df.get_column("source").unique().to_list() == [SOURCE_FAPI_KLINES]
    # Tz-aware UTC microsecond preserved.
    assert df.schema["open_time"] == pl.Datetime("us", time_zone="UTC")


# ── fetch_klines ──────────────────────────────────────────────────────────


def test_fetch_klines_rejects_naive_start():
    client = FakeClient([])
    with pytest.raises(ValueError, match="timezone-aware"):
        fetch_klines("BTC-USDT", "perp_usdt",
                     datetime(2026, 1, 1), END_DT, client)


def test_fetch_klines_rejects_inverted_range():
    client = FakeClient([])
    with pytest.raises(ValueError, match="must be <"):
        fetch_klines("BTC-USDT", "perp_usdt", END_DT, START_DT, client)


def test_fetch_klines_single_chunk_under_limit():
    """Server returns < KLINE_LIMIT_PER_CALL rows -> we stop after one call."""
    rows = _series_from(START_MS, 5)
    client = FakeClient([rows])
    df = fetch_klines("BTC-USDT", "perp_usdt", START_DT, END_DT, client)
    assert df.shape[0] == 5
    assert len(client.calls) == 1


def test_fetch_klines_paginates_when_full_chunk():
    """Server returns exactly KLINE_LIMIT_PER_CALL -> we fetch again."""
    full = _series_from(START_MS, 1000)
    next_start = START_MS + 1000 * KLINE_INTERVAL_MS
    tail = _series_from(next_start, 5)
    client = FakeClient([full, tail])
    df = fetch_klines("BTC-USDT", "perp_usdt", START_DT,
                      START_DT + timedelta(hours=2000), client)
    assert df.shape[0] == 1005
    assert len(client.calls) == 2
    # Second call advances cursor by one bar past the last open of chunk 1.
    expected_cursor = full[-1][0] + KLINE_INTERVAL_MS
    assert client.calls[1][1]["startTime"] == expected_cursor


def test_fetch_klines_empty_response_breaks_loop():
    client = FakeClient([[]])
    df = fetch_klines("BTC-USDT", "perp_usdt", START_DT, END_DT, client)
    assert df.is_empty()
    assert df.schema == KLINES_SCHEMA


def test_fetch_klines_drops_partial_bar_via_close_time_guard():
    """Construct a row whose close_time is in the FUTURE — must be dropped."""
    future_close_ms = int((datetime.now(tz=UTC) + timedelta(hours=10)).timestamp() * 1000)
    open_ms = future_close_ms - KLINE_INTERVAL_MS + 1
    partial = [_kline_row(open_ms, future_close_ms)]
    client = FakeClient([partial])
    # End is far enough that fetch_klines won't safe-end-clamp this away.
    df = fetch_klines("BTC-USDT", "perp_usdt", START_DT,
                      datetime.now(tz=UTC) + timedelta(days=1), client)
    assert df.is_empty()


# ── probe_first_bar ────────────────────────────────────────────────────────


def test_probe_first_bar_returns_first_bar_open_time():
    rows = [_kline_row(START_MS, START_MS + KLINE_INTERVAL_MS - 1)]
    client = FakeClient([rows])
    out = probe_first_bar("BTC-USDT", "perp_usdt", client)
    assert out == START_DT
    # Single API call with startTime=0, limit=1.
    assert client.calls[0][1] == {
        "symbol": "BTCUSDT", "interval": "1h", "startTime": 0, "limit": 1,
    }


def test_probe_first_bar_returns_none_on_empty():
    client = FakeClient([[]])
    assert probe_first_bar("NEWCOIN-USDT", "perp_usdt", client) is None


# ── last_archived_open_time ────────────────────────────────────────────────


def test_last_archived_open_time_missing_root(tmp_path):
    assert last_archived_open_time("BTC-USDT", "perp_usdt", tmp_path / "missing") is None


def test_last_archived_open_time_empty_archive(tmp_path):
    (tmp_path / "year=2026").mkdir(parents=True)
    assert last_archived_open_time("BTC-USDT", "perp_usdt", tmp_path) is None


def test_last_archived_open_time_returns_max_for_symbol(tmp_path):
    rows_btc = _series_from(START_MS, 5)
    rows_eth = _series_from(START_MS + 5 * KLINE_INTERVAL_MS, 3)
    btc_df = _rows_to_df(rows_btc, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    eth_df = _rows_to_df(rows_eth, "ETH-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(pl.concat([btc_df, eth_df]), root=tmp_path)
    btc_max = last_archived_open_time("BTC-USDT", "perp_usdt", tmp_path)
    eth_max = last_archived_open_time("ETH-USDT", "perp_usdt", tmp_path)
    assert btc_max == START_DT + timedelta(hours=4)
    assert eth_max == START_DT + timedelta(hours=7)


# ── effective_listing_date ─────────────────────────────────────────────────


def test_effective_listing_date_cold_start_returns_snapshot(tmp_path):
    snap = date(2024, 5, 1)
    assert effective_listing_date("BTC-USDT", "perp_usdt", snap, tmp_path) == snap


def test_effective_listing_date_observed_older_than_snapshot(tmp_path):
    rows = _series_from(START_MS, 3)
    df = _rows_to_df(rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    snap = date(2027, 1, 1)  # later than archived data
    out = effective_listing_date("BTC-USDT", "perp_usdt", snap, tmp_path)
    assert out == START_DT.date()


def test_effective_listing_date_snapshot_older_than_observed(tmp_path):
    rows = _series_from(START_MS, 3)
    df = _rows_to_df(rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    snap = date(2020, 1, 1)
    out = effective_listing_date("BTC-USDT", "perp_usdt", snap, tmp_path)
    assert out == snap


def test_effective_listing_date_no_archive_no_snapshot_returns_none(tmp_path):
    assert effective_listing_date("BTC-USDT", "perp_usdt", None, tmp_path) is None


# ── _detect_corrections ────────────────────────────────────────────────────


def _fake_existing_df():
    rows = _series_from(START_MS, 2, base=100.0)
    df = _rows_to_df(rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    return df.with_columns(
        pl.lit(datetime(2026, 1, 1, tzinfo=UTC)).alias("ingested_at")
    )


def test_detect_corrections_no_overlap_returns_empty():
    existing = _fake_existing_df()
    far_rows = _series_from(START_MS + 100 * KLINE_INTERVAL_MS, 2, base=200.0)
    new_df = _rows_to_df(far_rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    assert _detect_corrections(existing, new_df) == []


def test_detect_corrections_identical_returns_empty():
    existing = _fake_existing_df()
    same = _rows_to_df(_series_from(START_MS, 2, base=100.0),
                       "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    assert _detect_corrections(existing, same) == []


def test_detect_corrections_ulp_drift_below_tolerance_no_phantom():
    existing = _fake_existing_df()
    new_rows = _series_from(START_MS, 2, base=100.0)
    # Inject a sub-rtol drift on the close field.
    drifted = _rows_to_df(new_rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    drifted = drifted.with_columns(
        pl.col("close") + pl.col("close") * (CORRECTION_RTOL / 10)
    )
    corrections = _detect_corrections(existing, drifted)
    assert corrections == []


def test_detect_corrections_genuine_diff_emits_row():
    existing = _fake_existing_df()
    new_rows = _series_from(START_MS, 2, base=100.0)
    new_df = _rows_to_df(new_rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    new_df = new_df.with_columns(pl.col("close") * 1.001)  # 0.1% — well above tolerance
    corrections = _detect_corrections(existing, new_df)
    assert len(corrections) == 2  # both rows have changed close
    fields = sorted({c["field"] for c in corrections})
    assert fields == ["close"]


def test_detect_corrections_integer_field_diff():
    existing = _fake_existing_df()
    new_rows = _series_from(START_MS, 2, base=100.0)
    new_df = _rows_to_df(new_rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    new_df = new_df.with_columns(pl.col("trades") + 1)
    corrections = _detect_corrections(existing, new_df)
    assert len(corrections) == 2
    assert all(c["field"] == "trades" for c in corrections)


# ── write_klines_partitioned ──────────────────────────────────────────────


def test_write_partitioned_empty_is_noop(tmp_path):
    write_klines_partitioned(pl.DataFrame(schema=KLINES_SCHEMA), root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_write_partitioned_creates_year_dir(tmp_path):
    df = _rows_to_df(_series_from(START_MS, 3),
                     "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    assert (tmp_path / "year=2026" / "data.parquet").exists()


def test_write_partitioned_resume_merges_dedup(tmp_path):
    df1 = _rows_to_df(_series_from(START_MS, 3),
                      "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df1, root=tmp_path)
    # Second batch: 1 overlap + 2 new bars.
    df2 = _rows_to_df(_series_from(START_MS + 2 * KLINE_INTERVAL_MS, 3, base=200.0),
                      "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df2, root=tmp_path)

    full = pl.read_parquet(tmp_path / "year=2026" / "data.parquet")
    assert full.shape[0] == 5  # 3 + 3 - 1 overlap = 5
    # The overlapping row should reflect the SECOND batch's value (newer ingested_at).
    overlap_row = full.filter(pl.col("open_time") == START_DT + timedelta(hours=2))
    assert overlap_row.item(0, "open") == 200.0   # base from second batch


def test_write_partitioned_emits_corrections_sidecar(tmp_path):
    """Re-ingest with numerically different close should emit correction rows."""
    df1 = _rows_to_df(_series_from(START_MS, 2, base=100.0),
                      "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df1, root=tmp_path)

    # Same 2 rows but close is bumped 0.1% (above tolerance).
    df2 = _rows_to_df(_series_from(START_MS, 2, base=100.0),
                      "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    df2 = df2.with_columns(pl.col("close") * 1.001)
    write_klines_partitioned(df2, root=tmp_path)

    sidecar_files = list((tmp_path / "_corrections").glob("correction_*.parquet"))
    assert len(sidecar_files) == 1
    sidecar = pl.read_parquet(sidecar_files[0])
    assert sidecar.schema == CORRECTIONS_SCHEMA
    assert sidecar.shape[0] == 2  # 2 rows, both close changed
    assert sidecar.get_column("field").unique().to_list() == ["close"]


def test_write_partitioned_no_sidecar_when_no_corrections(tmp_path):
    df = _rows_to_df(_series_from(START_MS, 3),
                     "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    write_klines_partitioned(df, root=tmp_path)  # identical re-ingest
    assert not (tmp_path / "_corrections").exists()


def test_write_partitioned_no_tmp_files_after_success(tmp_path):
    df = _rows_to_df(_series_from(START_MS, 3),
                     "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    # No `.tmp.<pid>` files should remain.
    assert not list(tmp_path.glob("**/*.tmp.*"))


def test_write_partitioned_splits_across_years(tmp_path):
    """Bars spanning a year boundary write to two partitions."""
    end_2025_dt = datetime(2025, 12, 31, 22, 0, tzinfo=UTC)
    end_2025_ms = int(end_2025_dt.timestamp() * 1000)
    rows = _series_from(end_2025_ms, 4)  # 22:00, 23:00 of 2025; 00:00, 01:00 of 2026
    df = _rows_to_df(rows, "BTC-USDT", "perp_usdt", SOURCE_FAPI_KLINES)
    write_klines_partitioned(df, root=tmp_path)
    assert (tmp_path / "year=2025" / "data.parquet").exists()
    assert (tmp_path / "year=2026" / "data.parquet").exists()
    p2025 = pl.read_parquet(tmp_path / "year=2025" / "data.parquet")
    p2026 = pl.read_parquet(tmp_path / "year=2026" / "data.parquet")
    assert p2025.shape[0] == 2
    assert p2026.shape[0] == 2
