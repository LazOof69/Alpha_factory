"""Tests for `alpha_factory.data.funding`.

Mirror of test_klines.py structure. Synthetic Binance funding rows +
fake client; verifies pagination, boundary skew truncation, null-rate
filtering, correction-diff tolerance, atomic write, resume.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from alpha_factory.data.funding import (
    CORRECTION_RTOL,
    _detect_corrections,
    _rows_to_df,
    fetch_funding,
    last_archived_funding_time,
    write_funding_partitioned,
)
from alpha_factory.data.schema import (
    CORRECTIONS_SCHEMA,
    FUNDING_INTERVAL_MS,
    FUNDING_LIMIT_PER_CALL,
    FUNDING_SCHEMA,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


def _funding_row(funding_ms: int, rate: float = -0.0001, mark: float = 65000.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": funding_ms,
        "fundingRate": f"{rate:.8f}",
        "markPrice": f"{mark:.8f}",
    }


def _series_from(start_ms: int, n: int, rate: float = -0.0001) -> list[dict]:
    return [
        _funding_row(start_ms + i * FUNDING_INTERVAL_MS, rate + i * 1e-5)
        for i in range(n)
    ]


START_DT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
END_DT = datetime(2026, 1, 8, 0, 0, tzinfo=UTC)
START_MS = int(START_DT.timestamp() * 1000)


class FakeClient:
    def __init__(self, queue: list) -> None:
        self.queue = list(queue)
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict):
        self.calls.append((url, dict(params)))
        return self.queue.pop(0)


# ── _rows_to_df ───────────────────────────────────────────────────────────


def test_rows_to_df_empty_returns_empty_with_schema():
    df = _rows_to_df([], "BTC-USDT")
    assert df.is_empty()
    assert df.schema == FUNDING_SCHEMA


def test_rows_to_df_typed_correctly():
    rows = _series_from(START_MS, 3)
    df = _rows_to_df(rows, "BTC-USDT")
    assert df.shape[0] == 3
    assert df.schema == FUNDING_SCHEMA
    assert df.get_column("symbol").unique().to_list() == ["BTC-USDT"]


def test_rows_to_df_truncates_funding_time_boundary_skew():
    """Binance occasionally returns funding_time with 1-13ms past the 8h boundary."""
    skewed_ms = START_MS + 7   # 7ms past the boundary
    rows = [_funding_row(skewed_ms)]
    df = _rows_to_df(rows, "BTC-USDT")
    # truncate("1s") should reset to 00:00:00, not 00:00:00.007.
    assert df.item(0, "funding_time") == START_DT
    assert df.item(0, "funding_time").microsecond == 0


def test_rows_to_df_drops_null_funding_rate(caplog):
    rows = [
        {"symbol": "BTCUSDT", "fundingTime": START_MS,
         "fundingRate": "", "markPrice": "65000"},   # empty -> null after cast
        {"symbol": "BTCUSDT", "fundingTime": START_MS + FUNDING_INTERVAL_MS,
         "fundingRate": "-0.0001", "markPrice": "65000"},
    ]
    df = _rows_to_df(rows, "BTC-USDT")
    assert df.shape[0] == 1   # null-rate row dropped
    assert df.item(0, "funding_rate") == -0.0001


def test_rows_to_df_handles_missing_mark_price_as_null():
    rows = [{"symbol": "BTCUSDT", "fundingTime": START_MS,
             "fundingRate": "-0.0001", "markPrice": ""}]
    df = _rows_to_df(rows, "BTC-USDT")
    assert df.shape[0] == 1
    assert df.item(0, "mark_price") is None


# ── fetch_funding ─────────────────────────────────────────────────────────


def test_fetch_funding_rejects_naive_start():
    client = FakeClient([])
    with pytest.raises(ValueError, match="timezone-aware"):
        fetch_funding("BTC-USDT", datetime(2026, 1, 1), END_DT, client)


def test_fetch_funding_rejects_inverted_range():
    client = FakeClient([])
    with pytest.raises(ValueError, match="must be <"):
        fetch_funding("BTC-USDT", END_DT, START_DT, client)


def test_fetch_funding_single_chunk():
    rows = _series_from(START_MS, 5)
    client = FakeClient([rows])
    df = fetch_funding("BTC-USDT", START_DT, END_DT, client)
    assert df.shape[0] == 5
    assert len(client.calls) == 1


def test_fetch_funding_paginates_when_full_chunk():
    full = _series_from(START_MS, FUNDING_LIMIT_PER_CALL)
    next_start = START_MS + FUNDING_LIMIT_PER_CALL * FUNDING_INTERVAL_MS
    tail = _series_from(next_start, 3)
    client = FakeClient([full, tail])
    df = fetch_funding("BTC-USDT", START_DT,
                       START_DT + timedelta(days=400), client)
    assert df.shape[0] == FUNDING_LIMIT_PER_CALL + 3
    assert len(client.calls) == 2
    # Cursor advanced by last_funding_ms + 1.
    expected_cursor = full[-1]["fundingTime"] + 1
    assert client.calls[1][1]["startTime"] == expected_cursor


def test_fetch_funding_empty_response_breaks_loop():
    client = FakeClient([[]])
    df = fetch_funding("BTC-USDT", START_DT, END_DT, client)
    assert df.is_empty()
    assert df.schema == FUNDING_SCHEMA


# ── last_archived_funding_time ─────────────────────────────────────────────


def test_last_archived_funding_time_missing_root(tmp_path):
    assert last_archived_funding_time("BTC-USDT", tmp_path / "missing") is None


def test_last_archived_funding_time_returns_max(tmp_path):
    rows = _series_from(START_MS, 3)
    df = _rows_to_df(rows, "BTC-USDT")
    write_funding_partitioned(df, root=tmp_path)
    out = last_archived_funding_time("BTC-USDT", tmp_path)
    assert out == START_DT + timedelta(hours=16)   # 3 events, last at +16h


def test_last_archived_funding_time_filters_by_symbol(tmp_path):
    btc = _rows_to_df(_series_from(START_MS, 3), "BTC-USDT")
    eth_start = START_MS + 5 * FUNDING_INTERVAL_MS
    eth = _rows_to_df(_series_from(eth_start, 2), "ETH-USDT")
    write_funding_partitioned(pl.concat([btc, eth]), root=tmp_path)
    btc_max = last_archived_funding_time("BTC-USDT", tmp_path)
    eth_max = last_archived_funding_time("ETH-USDT", tmp_path)
    assert btc_max == START_DT + timedelta(hours=16)
    assert eth_max == START_DT + timedelta(hours=48)


# ── _detect_corrections ────────────────────────────────────────────────────


def _existing_funding_df():
    df = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    return df.with_columns(
        pl.lit(datetime(2026, 1, 1, tzinfo=UTC)).alias("ingested_at")
    )


def test_detect_corrections_no_overlap_returns_empty():
    existing = _existing_funding_df()
    far_rows = _series_from(START_MS + 100 * FUNDING_INTERVAL_MS, 2)
    new_df = _rows_to_df(far_rows, "BTC-USDT")
    assert _detect_corrections(existing, new_df) == []


def test_detect_corrections_identical_returns_empty():
    existing = _existing_funding_df()
    same = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    assert _detect_corrections(existing, same) == []


def test_detect_corrections_ulp_drift_below_tolerance_no_phantom():
    existing = _existing_funding_df()
    drifted = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    drifted = drifted.with_columns(
        pl.col("mark_price") + pl.col("mark_price") * (CORRECTION_RTOL / 10)
    )
    assert _detect_corrections(existing, drifted) == []


def test_detect_corrections_genuine_funding_rate_change():
    existing = _existing_funding_df()
    new_df = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    new_df = new_df.with_columns(pl.col("funding_rate") * 1.001)
    corrections = _detect_corrections(existing, new_df)
    assert len(corrections) == 2
    assert {c["field"] for c in corrections} == {"funding_rate"}
    assert {c["market"] for c in corrections} == {"perp_funding"}


def test_detect_corrections_genuine_mark_price_change():
    existing = _existing_funding_df()
    new_df = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    new_df = new_df.with_columns(pl.col("mark_price") * 1.001)
    corrections = _detect_corrections(existing, new_df)
    assert len(corrections) == 2
    assert {c["field"] for c in corrections} == {"mark_price"}


# ── write_funding_partitioned ──────────────────────────────────────────────


def test_write_partitioned_empty_is_noop(tmp_path):
    write_funding_partitioned(pl.DataFrame(schema=FUNDING_SCHEMA), root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_write_partitioned_creates_year_dir(tmp_path: Path):
    df = _rows_to_df(_series_from(START_MS, 3), "BTC-USDT")
    write_funding_partitioned(df, root=tmp_path)
    assert (tmp_path / "year=2026" / "data.parquet").exists()


def test_write_partitioned_resume_merges_dedup(tmp_path: Path):
    df1 = _rows_to_df(_series_from(START_MS, 3), "BTC-USDT")
    write_funding_partitioned(df1, root=tmp_path)
    # Overlapping batch: events 2,3 reappear + 4,5 new.
    df2 = _rows_to_df(_series_from(START_MS + 1 * FUNDING_INTERVAL_MS, 4),
                      "BTC-USDT")
    write_funding_partitioned(df2, root=tmp_path)
    full = pl.read_parquet(tmp_path / "year=2026" / "data.parquet")
    assert full.shape[0] == 5  # 3 + 4 - 2 overlap


def test_write_partitioned_emits_corrections_sidecar(tmp_path: Path):
    """Re-ingest with bumped funding_rate above tolerance -> sidecar emitted."""
    df1 = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    write_funding_partitioned(df1, root=tmp_path)

    df2 = _rows_to_df(_series_from(START_MS, 2), "BTC-USDT")
    df2 = df2.with_columns(pl.col("funding_rate") * 1.001)
    write_funding_partitioned(df2, root=tmp_path)

    sidecar_files = list((tmp_path / "_corrections").glob("correction_*.parquet"))
    assert len(sidecar_files) == 1
    sidecar = pl.read_parquet(sidecar_files[0])
    assert sidecar.schema == CORRECTIONS_SCHEMA
    assert sidecar.shape[0] == 2
    assert sidecar.get_column("market").unique().to_list() == ["perp_funding"]


def test_write_partitioned_no_sidecar_when_no_corrections(tmp_path: Path):
    df = _rows_to_df(_series_from(START_MS, 3), "BTC-USDT")
    write_funding_partitioned(df, root=tmp_path)
    write_funding_partitioned(df, root=tmp_path)   # identical re-ingest
    assert not (tmp_path / "_corrections").exists()


def test_write_partitioned_no_tmp_files_after_success(tmp_path: Path):
    df = _rows_to_df(_series_from(START_MS, 3), "BTC-USDT")
    write_funding_partitioned(df, root=tmp_path)
    assert not list(tmp_path.glob("**/*.tmp.*"))


def test_write_partitioned_splits_across_years(tmp_path: Path):
    # 2025-12-31 16:00, 2025-12-31 24:00 (= 2026-01-01 00:00).
    end_2025_dt = datetime(2025, 12, 31, 16, 0, tzinfo=UTC)
    end_2025_ms = int(end_2025_dt.timestamp() * 1000)
    rows = _series_from(end_2025_ms, 3)   # spans 16:00, 24:00 (2026-01-01 00:00), 08:00
    df = _rows_to_df(rows, "BTC-USDT")
    write_funding_partitioned(df, root=tmp_path)
    assert (tmp_path / "year=2025" / "data.parquet").exists()
    assert (tmp_path / "year=2026" / "data.parquet").exists()
