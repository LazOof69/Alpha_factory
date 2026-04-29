"""Fetch Binance USDT-M perp funding rate history and append to parquet archive.

CLI:
    uv run python feasibility/scripts/fetch_funding.py \\
        --symbol BTC-USDT --start 2019-09-08 --end 2025-04-29
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

import polars as pl

from binance_client import BinanceClient
from schema import (
    FAPI_FUNDING_URL,
    FUNDING_LIMIT_PER_CALL,
    FUNDING_ROOT,
    FUNDING_SCHEMA,
    SLEEP_BETWEEN_CALLS_S,
    SOURCE_FUNDING,
    conform,
    epoch_ms_to_utc_us,
    parse_iso_or_date,
    to_api_symbol,
)

log = logging.getLogger(__name__)

# Sample funding response row:
# {"symbol":"BTCUSDT","fundingTime":1568102400000,
#  "fundingRate":"-0.03750000","markPrice":"9210.50000000"}


def _rows_to_df(rows: list[dict], canonical_symbol: str) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=FUNDING_SCHEMA)

    ingested_at = datetime.now(tz=UTC)
    df = pl.DataFrame({
        "symbol": [canonical_symbol] * len(rows),
        "funding_time_ms": [int(r["fundingTime"]) for r in rows],
        "funding_rate": [float(r["fundingRate"]) for r in rows],
        "mark_price": [float(r["markPrice"]) for r in rows],
    })

    df = df.with_columns(
        # Binance occasionally reports funding_time with 1–13ms of clock skew
        # past the exact 8h boundary. Settlement is conceptually at the exact
        # 00/08/16:00 UTC mark — we drop sub-second noise on ingest so F1's
        # alignment check is meaningful and downstream joins are exact.
        epoch_ms_to_utc_us(pl.col("funding_time_ms"))
            .dt.truncate("1s")
            .alias("funding_time"),
        pl.lit(ingested_at).alias("ingested_at"),
        pl.lit(SOURCE_FUNDING).alias("source"),
    ).drop("funding_time_ms")

    return conform(df, FUNDING_SCHEMA)


def fetch_funding(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    client: BinanceClient,
) -> pl.DataFrame:
    """Fetch all funding settlements in [start_dt, end_dt) for one perp symbol."""
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("start_dt and end_dt must be timezone-aware")
    if start_dt >= end_dt:
        raise ValueError(f"start_dt {start_dt} must be < end_dt {end_dt}")

    api_symbol = to_api_symbol(symbol)
    cursor_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_rows: list[dict] = []
    while cursor_ms < end_ms:
        params = {
            "symbol": api_symbol,
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": FUNDING_LIMIT_PER_CALL,
        }
        chunk = client.get_json(FAPI_FUNDING_URL, params)
        if not chunk:
            break

        all_rows.extend(chunk)
        last_funding_ms = int(chunk[-1]["fundingTime"])
        # +1ms guards against re-fetching the boundary settlement
        cursor_ms = last_funding_ms + 1

        if len(chunk) < FUNDING_LIMIT_PER_CALL:
            break

        time.sleep(SLEEP_BETWEEN_CALLS_S)

    df = _rows_to_df(all_rows, symbol)
    if df.is_empty():
        return df

    return (
        df.unique(subset=["symbol", "funding_time"], keep="last")
        .sort(["symbol", "funding_time"])
    )


def write_funding_partitioned(df: pl.DataFrame) -> None:
    if df.is_empty():
        return
    df = df.with_columns(year=pl.col("funding_time").dt.year())
    for (year_val,), year_df in df.group_by(["year"]):
        year_dir = FUNDING_ROOT / f"year={int(year_val)}"
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
            .sort(["symbol", "funding_time", "ingested_at"])
            .unique(subset=["symbol", "funding_time"], keep="last")
            .sort(["symbol", "funding_time"])
        )
        merged.write_parquet(target, compression="zstd", compression_level=3)
        log.info("wrote %s rows to %s", merged.height, target)


def _last_archived_funding_time(symbol: str) -> datetime | None:
    if not FUNDING_ROOT.exists():
        return None
    files = list(FUNDING_ROOT.glob("year=*/data.parquet"))
    if not files:
        return None
    df = (
        pl.scan_parquet([str(f) for f in files])
        .filter(pl.col("symbol") == symbol)
        .select(pl.col("funding_time").max().alias("max_funding_time"))
        .collect()
    )
    if df.is_empty() or df["max_funding_time"][0] is None:
        return None
    return df["max_funding_time"][0]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True, help="canonical symbol, e.g. BTC-USDT")
    p.add_argument("--start", help="YYYY-MM-DD or ISO. Default = resume from archive.")
    p.add_argument("--end", help="YYYY-MM-DD or ISO. Default = now.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    end_dt = parse_iso_or_date(args.end) if args.end else datetime.now(tz=UTC)
    if args.start:
        start_dt = parse_iso_or_date(args.start)
    else:
        last = _last_archived_funding_time(args.symbol)
        if last is None:
            raise SystemExit("no archive yet — pass --start explicitly")
        start_dt = last + timedelta(milliseconds=1)
        log.info("resuming from %s", start_dt)

    if start_dt >= end_dt:
        log.info("nothing to fetch")
        return 0

    with BinanceClient() as client:
        df = fetch_funding(args.symbol, start_dt, end_dt, client)

    log.info("fetched %d funding rows for %s in [%s, %s)", df.height, args.symbol,
             start_dt.isoformat(), end_dt.isoformat())

    if df.is_empty():
        return 0
    if args.dry_run:
        log.info("dry-run: NOT writing parquet")
        log.info("preview:\n%s", df.head(3))
        return 0

    write_funding_partitioned(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
