"""Fetch Binance 1h klines (spot or USDT-M perp) and append to parquet archive.

Append-only: each run extends the archive forward from the last `open_time`
already on disk. To force a full re-fetch, delete the relevant
`feasibility/data/klines/year=YYYY/` partitions first.

CLI:
    uv run python feasibility/scripts/fetch_klines.py \\
        --symbol BTC-USDT --market spot \\
        --start 2019-09-08 --end 2025-04-29

Module use:
    from fetch_klines import fetch_klines
    df = fetch_klines("BTC-USDT", "spot", start_dt, end_dt, client)
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

import polars as pl

from binance_client import BinanceClient
from schema import (
    FAPI_KLINES_URL,
    KLINE_INTERVAL,
    KLINE_INTERVAL_MS,
    KLINE_LIMIT_PER_CALL,
    KLINES_ROOT,
    KLINES_SCHEMA,
    SLEEP_BETWEEN_CALLS_S,
    SOURCE_FAPI_KLINES,
    SOURCE_SPOT_KLINES,
    SPOT_KLINES_URL,
    conform,
    epoch_ms_to_utc_us,
    parse_iso_or_date,
    to_api_symbol,
)

log = logging.getLogger(__name__)

# Binance kline row layout (spot v3 and futures v1 are identical):
# 0  open_time (ms)        | 6  close_time (ms)
# 1  open      (str)       | 7  quote_asset_volume (str)
# 2  high      (str)       | 8  number_of_trades (int)
# 3  low       (str)       | 9  taker_buy_base_asset_volume (str)
# 4  close     (str)       | 10 taker_buy_quote_asset_volume (str)
# 5  volume    (str)       | 11 ignore


def _endpoint_for(market: str) -> tuple[str, str]:
    """Return (url, source-tag) for the given market."""
    if market == "spot":
        return SPOT_KLINES_URL, SOURCE_SPOT_KLINES
    if market == "perp_usdt":
        return FAPI_KLINES_URL, SOURCE_FAPI_KLINES
    raise ValueError(f"unknown market: {market}")


def _rows_to_df(rows: list[list], symbol: str, market: str, source: str) -> pl.DataFrame:
    """Convert raw Binance kline rows into a typed polars DataFrame."""
    if not rows:
        return pl.DataFrame(schema=KLINES_SCHEMA)

    ingested_at = datetime.now(tz=UTC)

    df = pl.DataFrame({
        "symbol": [symbol] * len(rows),
        "market": [market] * len(rows),
        "open_time_ms": [r[0] for r in rows],
        "close_time_ms": [r[6] for r in rows],
        "open": [float(r[1]) for r in rows],
        "high": [float(r[2]) for r in rows],
        "low": [float(r[3]) for r in rows],
        "close": [float(r[4]) for r in rows],
        "volume": [float(r[5]) for r in rows],
        "quote_volume": [float(r[7]) for r in rows],
        "trades": [int(r[8]) for r in rows],
        "taker_buy_base": [float(r[9]) for r in rows],
        "taker_buy_quote": [float(r[10]) for r in rows],
    })

    df = df.with_columns(
        epoch_ms_to_utc_us(pl.col("open_time_ms")).alias("open_time"),
        epoch_ms_to_utc_us(pl.col("close_time_ms")).alias("close_time"),
        pl.lit(ingested_at).alias("ingested_at"),
        pl.lit(source).alias("source"),
    ).drop(["open_time_ms", "close_time_ms"])

    return conform(df, KLINES_SCHEMA)


def fetch_klines(
    symbol: str,
    market: str,
    start_dt: datetime,
    end_dt: datetime,
    client: BinanceClient,
) -> pl.DataFrame:
    """Fetch all 1h klines in [start_dt, end_dt) for one (symbol, market).

    Returns a DataFrame with KLINES_SCHEMA. Filters out any partial last bar
    (close_time >= now). Empty DataFrame if no data exists in the range.
    """
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("start_dt and end_dt must be timezone-aware (UTC)")
    if start_dt >= end_dt:
        raise ValueError(f"start_dt {start_dt} must be < end_dt {end_dt}")

    url, source = _endpoint_for(market)
    api_symbol = to_api_symbol(symbol)

    # Round end down to the previous completed hour boundary.
    now = datetime.now(tz=UTC)
    safe_end = min(end_dt, now - timedelta(hours=1))
    safe_end_ms = int(safe_end.timestamp() * 1000)

    cursor_ms = int(start_dt.timestamp() * 1000)
    all_rows: list[list] = []

    while cursor_ms < safe_end_ms:
        params = {
            "symbol": api_symbol,
            "interval": KLINE_INTERVAL,
            "startTime": cursor_ms,
            "endTime": safe_end_ms,
            "limit": KLINE_LIMIT_PER_CALL,
        }
        chunk = client.get_json(url, params)
        if not chunk:
            log.info("no more rows for %s/%s at cursor %d", symbol, market, cursor_ms)
            break

        all_rows.extend(chunk)
        last_open_ms = chunk[-1][0]
        # advance one bar past the last open so the next call doesn't re-fetch it
        cursor_ms = last_open_ms + KLINE_INTERVAL_MS

        if len(chunk) < KLINE_LIMIT_PER_CALL:
            break

        time.sleep(SLEEP_BETWEEN_CALLS_S)

    df = _rows_to_df(all_rows, symbol, market, source)
    if df.is_empty():
        return df

    # Defense in depth: filter any partial bars where close_time >= ingested_at.
    df = df.filter(pl.col("close_time") < pl.col("ingested_at"))
    df = df.unique(subset=["symbol", "market", "open_time"], keep="last")
    return df.sort("open_time")


def _last_archived_open_time(symbol: str, market: str) -> datetime | None:
    """Return the latest open_time on disk for (symbol, market), or None."""
    if not KLINES_ROOT.exists():
        return None
    files = list(KLINES_ROOT.glob("year=*/data.parquet"))
    if not files:
        return None
    df = (
        pl.scan_parquet([str(f) for f in files])
        .filter((pl.col("symbol") == symbol) & (pl.col("market") == market))
        .select(pl.col("open_time").max().alias("max_open_time"))
        .collect()
    )
    if df.is_empty() or df["max_open_time"][0] is None:
        return None
    return df["max_open_time"][0]


def write_klines_partitioned(df: pl.DataFrame) -> None:
    """Write klines DataFrame to data/klines/year=YYYY/data.parquet (append-merge)."""
    if df.is_empty():
        return

    df = df.with_columns(year=pl.col("open_time").dt.year())
    for (year_val,), year_df in df.group_by(["year"]):
        year_dir = KLINES_ROOT / f"year={int(year_val)}"
        year_dir.mkdir(parents=True, exist_ok=True)
        target = year_dir / "data.parquet"

        new_df = year_df.drop("year")
        if target.exists():
            existing = pl.read_parquet(target)
            merged = pl.concat([existing, new_df], how="vertical_relaxed")
        else:
            merged = new_df

        merged = (
            merged
            .sort(["symbol", "market", "open_time", "ingested_at"])
            .unique(subset=["symbol", "market", "open_time"], keep="last")
            .sort(["symbol", "market", "open_time"])
        )
        merged.write_parquet(target, compression="zstd", compression_level=3)
        log.info("wrote %s rows to %s", merged.height, target)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True, help="canonical symbol, e.g. BTC-USDT")
    p.add_argument("--market", required=True, choices=["spot", "perp_usdt"])
    p.add_argument("--start", help="YYYY-MM-DD or ISO. Default = resume from archive.")
    p.add_argument("--end", help="YYYY-MM-DD or ISO. Default = now (clamped to now-1h).")
    p.add_argument("--dry-run", action="store_true", help="fetch but do not write parquet")
    args = p.parse_args()

    end_dt = parse_iso_or_date(args.end) if args.end else datetime.now(tz=UTC)
    if args.start:
        start_dt = parse_iso_or_date(args.start)
    else:
        last = _last_archived_open_time(args.symbol, args.market)
        if last is None:
            raise SystemExit("no archive yet for this (symbol, market) — pass --start explicitly")
        start_dt = last + timedelta(hours=1)
        log.info("resuming from %s (one bar after last archived %s)", start_dt, last)

    if start_dt >= end_dt:
        log.info("nothing to fetch: start %s >= end %s", start_dt, end_dt)
        return 0

    with BinanceClient() as client:
        df = fetch_klines(args.symbol, args.market, start_dt, end_dt, client)

    log.info("fetched %d rows for %s/%s in [%s, %s)", df.height, args.symbol, args.market,
             start_dt.isoformat(), end_dt.isoformat())

    if df.is_empty():
        return 0
    if args.dry_run:
        log.info("dry-run: NOT writing parquet")
        log.info("preview:\n%s", df.head(3))
        return 0

    write_klines_partitioned(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
