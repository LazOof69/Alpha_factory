"""Polars schemas + Binance constants for FS Study 1.

Single source of truth — all fetch / QC / verify modules import from here.
Keeping this small and pure (no I/O) makes it trivial to unit-test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

# String identifier used by polars `time_zone=` arguments. Distinct from the
# `datetime.UTC` singleton (which is an actual tzinfo object) — keeping this
# rename avoids a name clash on the latter.
TZ_UTC = "UTC"

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# We deliberately store categorical-like columns (`market`, `contract_type`,
# `status`) as Utf8 instead of pl.Categorical. Categoricals are not portable
# across parquet read/write boundaries in polars 1.x without a global string
# cache, and the storage savings are negligible at our row count.
KLINES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "market": pl.Utf8,  # "spot" | "perp_usdt"
    "open_time": pl.Datetime("us", time_zone=TZ_UTC),
    "close_time": pl.Datetime("us", time_zone=TZ_UTC),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "quote_volume": pl.Float64,
    "trades": pl.Int64,
    "taker_buy_base": pl.Float64,
    "taker_buy_quote": pl.Float64,
    "ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
    "source": pl.Utf8,
}

FUNDING_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "funding_time": pl.Datetime("us", time_zone=TZ_UTC),
    "funding_rate": pl.Float64,
    "mark_price": pl.Float64,
    "ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
    "source": pl.Utf8,
}

UNIVERSE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "market": pl.Utf8,
    "listing_date": pl.Date,
    "delisting_date": pl.Date,  # nullable
    "base_asset": pl.Utf8,
    "quote_asset": pl.Utf8,
    "contract_type": pl.Utf8,  # "spot" | "perpetual"
    "funding_interval_hours": pl.Int32,  # null for spot
    "status": pl.Utf8,  # "active" | "delisted"
    "last_verified": pl.Datetime("us", time_zone=TZ_UTC),
}

# ---------------------------------------------------------------------------
# Universe seed for FS Study 1
# ---------------------------------------------------------------------------

# Symbol normalized with hyphen ("BTC-USDT"). API uses "BTCUSDT" — see
# `to_api_symbol` for the conversion.
UNIVERSE_SEED: list[dict] = [
    {
        "symbol": "BTC-USDT", "market": "spot",
        "base_asset": "BTC", "quote_asset": "USDT",
        "contract_type": "spot", "funding_interval_hours": None,
        "status": "active",
    },
    {
        "symbol": "ETH-USDT", "market": "spot",
        "base_asset": "ETH", "quote_asset": "USDT",
        "contract_type": "spot", "funding_interval_hours": None,
        "status": "active",
    },
    {
        "symbol": "BTC-USDT", "market": "perp_usdt",
        "base_asset": "BTC", "quote_asset": "USDT",
        "contract_type": "perpetual", "funding_interval_hours": 8,
        "status": "active",
    },
    {
        "symbol": "ETH-USDT", "market": "perp_usdt",
        "base_asset": "ETH", "quote_asset": "USDT",
        "contract_type": "perpetual", "funding_interval_hours": 8,
        "status": "active",
    },
]


def to_api_symbol(symbol: str) -> str:
    """Convert canonical 'BTC-USDT' to Binance API 'BTCUSDT'."""
    return symbol.replace("-", "")


def to_canonical_symbol(api_symbol: str, base: str) -> str:
    """Convert API 'BTCUSDT' back to 'BTC-USDT' given the base asset."""
    quote = api_symbol[len(base):]
    return f"{base}-{quote}"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FAPI_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

# ---------------------------------------------------------------------------
# Source identifiers (recorded with each row for audit / API-version drift)
# ---------------------------------------------------------------------------

SOURCE_SPOT_KLINES = "binance_spot_v3"
SOURCE_FAPI_KLINES = "binance_fapi_v1"
SOURCE_FUNDING = "binance_fapi_funding_v1"

# ---------------------------------------------------------------------------
# Time / pagination
# ---------------------------------------------------------------------------

KLINE_INTERVAL = "1h"
KLINE_INTERVAL_MS = 60 * 60 * 1000
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000

# Binance per-call limits — use the smaller across spot/futures so logic is uniform.
KLINE_LIMIT_PER_CALL = 1000
FUNDING_LIMIT_PER_CALL = 1000

# Polite delay between requests (under rate limit; we are nowhere near it).
SLEEP_BETWEEN_CALLS_S = 0.2

# ---------------------------------------------------------------------------
# Filesystem layout (single source of truth — fetch & QC modules import these)
# ---------------------------------------------------------------------------

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"
KLINES_ROOT = DATA_ROOT / "klines"
FUNDING_ROOT = DATA_ROOT / "funding"
UNIVERSE_PATH = DATA_ROOT / "universe.parquet"

# ---------------------------------------------------------------------------
# Cross-module helpers — kept here so fetchers stay thin
# ---------------------------------------------------------------------------


def epoch_ms_to_utc_us(col: pl.Expr) -> pl.Expr:
    """Convert a ms-since-epoch Int column to UTC `Datetime("us", "UTC")`.

    Binance API returns timestamps as Int64 ms-since-epoch. We standardize on
    microsecond precision throughout the archive (smaller than ns, lossless
    for our 1h/8h grid).
    """
    return (
        pl.from_epoch(col, time_unit="ms")
        .dt.replace_time_zone(TZ_UTC)
        .cast(pl.Datetime("us", time_zone=TZ_UTC))
    )


def conform(df: pl.DataFrame, schema: dict) -> pl.DataFrame:
    """Reorder + cast columns to exactly match `schema`.

    Always run as the final step before `write_parquet` to guarantee schema
    stability across Python-list → DataFrame → parquet round-trips.
    """
    return df.select(list(schema.keys())).cast(schema)


def parse_iso_or_date(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' or full ISO into a UTC-aware datetime.

    Naive inputs are assumed UTC; tz-aware inputs are converted to UTC.
    Python 3.11+ `datetime.fromisoformat` already accepts bare dates.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
