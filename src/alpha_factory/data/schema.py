"""Polars schemas + helpers + path constants for the L1 data layer.

Single source of truth — any module that reads or writes parquet imports
schema dicts from here. Independent of `feasibility/scripts/schema.py`,
which is Phase 0 frozen.

Why schemas live in a dict (not a pydantic / dataclass model):
    polars `cast(schema)` accepts a `dict[str, DataType]` directly; using a
    dict keeps the schema declaration tight and avoids a stringly-typed
    parallel definition.

Storage layout (under repo-root `data/`, gitignored — see .gitignore `/data/`):
    data/universe/{market}/snapshot_YYYY-MM.parquet
    data/universe/{market}/rejected_YYYY-MM.parquet
    data/klines/year=YYYY/data.parquet     (year-partitioned across all symbols)
    data/funding/year=YYYY/data.parquet
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

# String identifier for polars `time_zone=` kwargs. Distinct from
# `datetime.UTC` (a tzinfo object) — keeping a separate name avoids
# clobbering the latter on import.
TZ_UTC = "UTC"


# ── Universe snapshot schema ─────────────────────────────────────────────
#
# One parquet file per (market, year-month). Each row = one symbol in the
# top-N at the as-of moment, with full audit trail.
#
# `period_start`/`period_end` define the calendar window during which this
# snapshot is the authoritative universe (forward-only — see universe.py
# point-in-time docstring).
UNIVERSE_SNAPSHOT_SCHEMA: dict[str, pl.DataType] = {
    "as_of": pl.Datetime("us", time_zone=TZ_UTC),    # exact moment snapshot taken
    "period_start": pl.Date,                         # first day this universe is valid
    "period_end": pl.Date,                           # last day inclusive
    "rank": pl.UInt8,                                # 1..N
    "symbol": pl.Utf8,                               # canonical "BTC-USDT"
    "api_symbol": pl.Utf8,                           # API form "BTCUSDT"
    "market": pl.Utf8,                               # "perp_usdt"
    "base_asset": pl.Utf8,                           # "BTC"
    "quote_asset": pl.Utf8,                          # "USDT"
    "quote_volume_24h": pl.Float64,                  # 24h quote volume (USDT)
    "last_price": pl.Float64,                        # last trade price
    "trade_count_24h": pl.Int64,                     # trade count over 24h
    # Quality / structural flags (NOT used as filters in the snapshot itself —
    # downstream alphas may consume these). avg_trade_size is recorded
    # because it's free from the same payload, but it does NOT discriminate
    # quality alone (deep-MM majors and thin altcoins both produce small
    # values — interpret only via 2D analysis with quote_volume).
    "avg_trade_size": pl.Float64,
    # Frozen at fetch time from exchangeInfo.symbols[].onboardDate. Never
    # re-queried for historical reconstruction; Phase A.3+ archive-derived
    # universes will use first-observed-bar-date instead (different method id).
    "listing_date": pl.Date,
    # All TRADING spot pairs sharing this base_asset across {USDT,USDC,FDUSD}.
    # List (not bool) because basis/carry economics vary materially across
    # quotes — ETH/USDT vs ETH/USDC vs WBETH/USDT have different liquidity
    # and unwind risk.
    "spot_pairs": pl.List(pl.Utf8),
    # max 24h quoteVolume across spot_pairs; 0 if empty list.
    "primary_spot_quote_volume": pl.Float64,
    # Audit / reproducibility.
    # |candidate set before listing-age + top-N filters|.
    "total_candidates": pl.UInt16,
    "min_listing_days_threshold": pl.UInt16,         # threshold actually applied
    "method": pl.Utf8,                               # ranking method id
    "ingested_at": pl.Datetime("us", time_zone=TZ_UTC),  # fetch wall-clock
    # Primary endpoint label, e.g. "binance/fapi/v1/ticker/24hr".
    "source": pl.Utf8,
    # Per-endpoint timestamps for the 4-call atomicity check. All four must
    # land within `ATOMICITY_WINDOW_S` or the snapshot fails (see universe.py).
    "fapi_exchangeinfo_fetched_at": pl.Datetime("us", time_zone=TZ_UTC),
    "fapi_ticker_fetched_at": pl.Datetime("us", time_zone=TZ_UTC),
    "spot_exchangeinfo_fetched_at": pl.Datetime("us", time_zone=TZ_UTC),
    "spot_ticker_fetched_at": pl.Datetime("us", time_zone=TZ_UTC),
}


# ── Rejected candidates sidecar schema ───────────────────────────────────
#
# Companion file `rejected_YYYY-MM.parquet` per snapshot — every symbol
# that entered the candidate set (PERPETUAL + TRADING + USDT) but was
# excluded by quality/eligibility filters. Multi-reason rejections produce
# multiple rows; `is_primary_reason` flags exactly one row per symbol so
# `count(*)` reconciles against `total_candidates - len(universe) - tail`.
REJECTED_CANDIDATES_SCHEMA: dict[str, pl.DataType] = {
    "as_of": pl.Datetime("us", time_zone=TZ_UTC),
    "symbol": pl.Utf8,
    "api_symbol": pl.Utf8,
    "reason": pl.Utf8,                               # see REJECTION_REASON_*
    "observed_value": pl.Float64,                    # value that violated the threshold
    "threshold": pl.Float64,                         # threshold being applied
    "is_primary_reason": pl.Boolean,                 # exactly one True per symbol
}


# ── Rejection reason identifiers ─────────────────────────────────────────
#
# String constants make the audit trail searchable and prevent drift across
# the producer (universe.py) and any future consumer (qc.py / reports).
REJECTION_REASON_LISTING_TOO_YOUNG = "listing_age_too_short"
REJECTION_REASON_INVALID_PRICE = "last_price_invalid"


# ── Klines schema ─────────────────────────────────────────────────────────
#
# 1h OHLCV across spot + USDT-M perp. Year-partitioned parquet at
# `KLINES_ROOT/year=YYYY/data.parquet`. `market` distinguishes spot from
# perp; `symbol` is canonical "BTC-USDT".
#
# Categorical-like columns (`market`, `source`) stored as Utf8 — pl.Categorical
# does not survive parquet round-trip cleanly without a global string cache,
# and storage savings are negligible at our row count (gotcha from FS).
KLINES_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "market": pl.Utf8,                                    # "spot" | "perp_usdt"
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
    "source": pl.Utf8,                                    # see SOURCE_*
}


# ── Funding schema (perp only) ────────────────────────────────────────────
FUNDING_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "funding_time": pl.Datetime("us", time_zone=TZ_UTC),  # truncated to 1s — see FS gotcha row
    "funding_rate": pl.Float64,
    "mark_price": pl.Float64,
    "ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
    "source": pl.Utf8,
}


# ── Corrections sidecar schema ────────────────────────────────────────────
#
# Companion file emitted when re-ingest produces a numerically-different
# value for an existing (symbol, market, time) row. CLAUDE.md red line:
# "no silent overwrite of archive data — corrections must be logged."
#
# Layout: `{KLINES_ROOT|FUNDING_ROOT}/_corrections/correction_<unix_us>.parquet`.
# Flat (not year-partitioned) — one file per ingest batch with at least one
# correction. Schema is uniform across kline + funding so QC can read both
# with a single load path. Numeric comparison uses rtol=1e-9, atol=0; below
# that we treat ULP drift as no-op (parquet float round-trip).
CORRECTIONS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    # "spot" | "perp_usdt" | "perp_funding"
    "market": pl.Utf8,
    # open_time for kline, funding_time for funding rows
    "time": pl.Datetime("us", time_zone=TZ_UTC),
    "field": pl.Utf8,                                     # column whose value changed
    "old_value": pl.Float64,
    "new_value": pl.Float64,
    "old_ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
    "new_ingested_at": pl.Datetime("us", time_zone=TZ_UTC),
}


# ── Endpoints ─────────────────────────────────────────────────────────────
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FAPI_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


# ── Source identifiers (recorded with each row for audit / API drift) ─────
SOURCE_SPOT_KLINES = "binance_spot_v3"
SOURCE_FAPI_KLINES = "binance_fapi_v1"
SOURCE_FUNDING = "binance_fapi_funding_v1"


# ── Time / pagination constants ───────────────────────────────────────────
KLINE_INTERVAL = "1h"
KLINE_INTERVAL_MS = 60 * 60 * 1000
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000

# Binance per-call limits — use the smaller across spot/futures so logic is uniform.
KLINE_LIMIT_PER_CALL = 1000
FUNDING_LIMIT_PER_CALL = 1000

# Polite delay between requests (well under rate limit; we are nowhere near it).
SLEEP_BETWEEN_CALLS_S = 0.2


# ── Storage layout ────────────────────────────────────────────────────────
#
# Path is relative to CWD by design — the caller (CLI / scheduled job)
# controls the root, which keeps modules pure (no `Path(__file__)` magic
# tying production code to a worktree path). Run from repo root.
DATA_ROOT = Path("data")
UNIVERSE_ROOT = DATA_ROOT / "universe"
KLINES_ROOT = DATA_ROOT / "klines"
FUNDING_ROOT = DATA_ROOT / "funding"


# ── Helpers ───────────────────────────────────────────────────────────────


def to_api_symbol(symbol: str) -> str:
    """Convert canonical 'BTC-USDT' → Binance API form 'BTCUSDT'."""
    return symbol.replace("-", "")


def to_canonical_symbol(api_symbol: str, base: str) -> str:
    """Convert API 'BTCUSDT' → canonical 'BTC-USDT' given the base asset."""
    quote = api_symbol[len(base):]
    return f"{base}-{quote}"


def conform(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """Reorder + cast columns to exactly match `schema`. Run before `write_parquet`."""
    return df.select(list(schema.keys())).cast(schema)


def epoch_ms_to_utc_us(col: pl.Expr) -> pl.Expr:
    """Convert a ms-since-epoch Int column to UTC `Datetime("us", "UTC")`.

    Binance API returns timestamps as Int64 ms-since-epoch. We standardize
    on microsecond precision throughout the archive (smaller than ns,
    lossless for our 1h / 8h grid).
    """
    return (
        pl.from_epoch(col, time_unit="ms")
        .dt.replace_time_zone(TZ_UTC)
        .cast(pl.Datetime("us", time_zone=TZ_UTC))
    )


def parse_iso_or_date(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' or full ISO into a UTC-aware `datetime`.

    Naive inputs are assumed UTC; tz-aware inputs are converted to UTC.
    Python 3.11+ `datetime.fromisoformat` accepts bare dates.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
