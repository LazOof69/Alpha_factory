"""Tests for `alpha_factory.data.schema` helpers + dtype invariants."""
from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from alpha_factory.data.schema import (
    FUNDING_SCHEMA,
    KLINES_SCHEMA,
    UNIVERSE_SNAPSHOT_SCHEMA,
    conform,
    epoch_ms_to_utc_us,
    parse_iso_or_date,
    to_api_symbol,
    to_canonical_symbol,
)

# ── Symbol mapping ────────────────────────────────────────────────────────


def test_to_api_symbol_strips_hyphen():
    assert to_api_symbol("BTC-USDT") == "BTCUSDT"
    assert to_api_symbol("1000PEPE-USDT") == "1000PEPEUSDT"


def test_to_canonical_symbol_roundtrip():
    assert to_canonical_symbol("BTCUSDT", "BTC") == "BTC-USDT"
    assert to_canonical_symbol("1000PEPEUSDT", "1000PEPE") == "1000PEPE-USDT"
    # Roundtrip property:
    for canon, base in [("BTC-USDT", "BTC"), ("ETH-USDC", "ETH")]:
        api = to_api_symbol(canon)
        assert to_canonical_symbol(api, base) == canon


# ── conform ───────────────────────────────────────────────────────────────


def test_conform_reorders_and_casts():
    df = pl.DataFrame({
        "extra_col": [1, 2],   # will be dropped
        "symbol": ["BTC-USDT", "ETH-USDT"],
    })
    schema = {"symbol": pl.Utf8}
    out = conform(df, schema)
    assert out.columns == ["symbol"]
    assert out.schema["symbol"] == pl.Utf8


def test_conform_casts_int_to_uint8():
    df = pl.DataFrame({"rank": [1, 2, 3]}).with_columns(pl.col("rank").cast(pl.Int64))
    out = conform(df, {"rank": pl.UInt8})
    assert out.schema["rank"] == pl.UInt8


def test_conform_raises_on_missing_column():
    df = pl.DataFrame({"a": [1]})
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        conform(df, {"missing": pl.Int64})


# ── epoch_ms_to_utc_us ────────────────────────────────────────────────────


def test_epoch_ms_to_utc_us_known_instant():
    # 2026-05-01 00:00:00 UTC → 1777593600000 ms.
    expected_ms = int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000)
    df = pl.DataFrame({"t_ms": [expected_ms]})
    out = df.with_columns(epoch_ms_to_utc_us(pl.col("t_ms")).alias("t"))
    assert out.schema["t"] == pl.Datetime("us", time_zone="UTC")
    assert out.item(0, "t") == datetime(2026, 5, 1, tzinfo=UTC)


def test_epoch_ms_to_utc_us_preserves_microsecond_precision():
    # ms → us is lossless. 1234567890123 ms = 2009-02-13 23:31:30.123 UTC.
    df = pl.DataFrame({"t_ms": [1_234_567_890_123]})
    out = df.with_columns(epoch_ms_to_utc_us(pl.col("t_ms")).alias("t"))
    instant = out.item(0, "t")
    assert instant.tzinfo is not None
    assert instant.microsecond == 123_000  # 0.123s = 123_000us


# ── parse_iso_or_date ─────────────────────────────────────────────────────


def test_parse_iso_or_date_bare_date():
    assert parse_iso_or_date("2026-05-01") == datetime(2026, 5, 1, tzinfo=UTC)


def test_parse_iso_or_date_full_iso_with_tz():
    out = parse_iso_or_date("2026-05-01T12:00:00+00:00")
    assert out == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_parse_iso_or_date_naive_assumed_utc():
    out = parse_iso_or_date("2026-05-01T12:00:00")
    assert out.tzinfo == UTC
    assert out.replace(tzinfo=None) == datetime(2026, 5, 1, 12, 0)


def test_parse_iso_or_date_non_utc_normalized():
    # 12:00 in TW (+08) == 04:00 UTC.
    out = parse_iso_or_date("2026-05-01T12:00:00+08:00")
    assert out == datetime(2026, 5, 1, 4, 0, tzinfo=UTC)


# ── Schema invariants ─────────────────────────────────────────────────────


def test_klines_schema_has_required_audit_fields():
    """Every row must carry ingested_at + source per CLAUDE.md red line."""
    assert KLINES_SCHEMA["ingested_at"] == pl.Datetime("us", time_zone="UTC")
    assert KLINES_SCHEMA["source"] == pl.Utf8


def test_funding_schema_has_required_audit_fields():
    assert FUNDING_SCHEMA["ingested_at"] == pl.Datetime("us", time_zone="UTC")
    assert FUNDING_SCHEMA["source"] == pl.Utf8


def test_klines_schema_time_columns_are_tz_aware_us():
    """tz-aware UTC microsecond — CLAUDE.md red line: no tz-naive datetimes."""
    assert KLINES_SCHEMA["open_time"] == pl.Datetime("us", time_zone="UTC")
    assert KLINES_SCHEMA["close_time"] == pl.Datetime("us", time_zone="UTC")


def test_funding_schema_funding_time_is_tz_aware_us():
    assert FUNDING_SCHEMA["funding_time"] == pl.Datetime("us", time_zone="UTC")


def test_universe_snapshot_schema_field_count_is_contract():
    """Schema field count is the contract — guard against accidental drift.

    Bump this number deliberately (and update consumers) when adding a
    field; never silently. Current contract: 25 fields (post Phase A.2 v2).
    """
    assert len(UNIVERSE_SNAPSHOT_SCHEMA) == 25
    assert "spot_pairs" in UNIVERSE_SNAPSHOT_SCHEMA
    assert UNIVERSE_SNAPSHOT_SCHEMA["spot_pairs"] == pl.List(pl.Utf8)
