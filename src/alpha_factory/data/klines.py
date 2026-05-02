"""1h kline fetcher + year-partitioned archive (single-symbol primitive).

Universe-aware iteration is the orchestrator's job (see archive.py). This
module exposes pure single-(symbol, market) operations:

    fetch_klines(symbol, market, start, end, client) -> pl.DataFrame
    write_klines_partitioned(df, root=KLINES_ROOT) -> None
    probe_first_bar(symbol, market, client) -> datetime | None
    last_archived_open_time(symbol, market, root) -> datetime | None
    effective_listing_date(symbol, market, snapshot_listing, root) -> date

Production port of `feasibility/scripts/fetch_klines.py`. The pagination
cursor / partial-bar guard / `unique(keep="last")` merge logic survive
unchanged (FS-validated). New surface added in A.3.2:

* `probe_first_bar` -- single-call earliest-bar probe (replaces unbounded
  paginated probe a naive caller might construct).
* `effective_listing_date` -- resolves the canonical "first usable bar"
  per CLAUDE.md gotcha: "Listing date probe can fall back to 2017 stale
  value for perp -- always overwrite with observed first-bar from
  archive." snapshot's onboardDate is a HINT for cold start; observed
  archive `min(open_time)` is the AUTHORITY once we have any data.
  CAVEAT: relisting-then-backfill is NOT handled here; the alpha layer /
  qc layer must enforce a `tradable_from` boundary if relevant.
* Correction-diff sidecar (write path): if re-ingest produces a
  numerically-different value for an existing (symbol, market,
  open_time) row, emit a row to
  `{KLINES_ROOT}/_corrections/correction_<unix_us>.parquet` BEFORE the
  merge proceeds. CLAUDE.md red line: no silent overwrite of archive.
* Atomic write: `<file>.tmp.<pid>` + `os.replace` so a crash mid-write
  cannot leave a truncated parquet shadowing good data.
* `BinanceRateLimitError` (from binance_client) bubbles up unmodified --
  the orchestrator decides whether to abort the run.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.data.binance_client import BinanceClient
from alpha_factory.data.schema import (
    CORRECTIONS_SCHEMA,
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

# Float comparison tolerance for correction-diff. Parquet float64 round-trips
# can introduce ULP-level drift; rtol=1e-9 ignores that while still catching
# any genuine vendor correction (Binance fields are sourced as decimal strings,
# typically 8 sig figs).
CORRECTION_RTOL = 1e-9


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
    raise ValueError(f"unknown market: {market!r}")


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

    Returns a DataFrame conforming to `KLINES_SCHEMA`. Empty on no data.
    The partial-bar guard drops any final bar whose `close_time >= ingested_at`
    (defense in depth on top of `safe_end = now - 1h`).

    Raises:
        ValueError: on naive datetimes or start >= end.
        BinanceAPIError / BinanceRateLimitError: per binance_client.
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
        cursor_ms = last_open_ms + KLINE_INTERVAL_MS  # advance past the last bar

        if len(chunk) < KLINE_LIMIT_PER_CALL:
            break

        time.sleep(SLEEP_BETWEEN_CALLS_S)

    df = _rows_to_df(all_rows, symbol, market, source)
    if df.is_empty():
        return df

    # Defense in depth: drop any partial bars where close_time >= ingested_at.
    df = df.filter(pl.col("close_time") < pl.col("ingested_at"))
    df = df.unique(subset=["symbol", "market", "open_time"], keep="last")
    return df.sort("open_time")


def probe_first_bar(
    symbol: str, market: str, client: BinanceClient,
) -> datetime | None:
    """Return the earliest available `open_time` for (symbol, market), or None.

    Single API call: `startTime=0, limit=1`. Binance returns the earliest
    bar it has — used to establish a SPOT cold-start date when the
    universe snapshot only carries the perp's onboardDate.
    """
    url, _ = _endpoint_for(market)
    api_symbol = to_api_symbol(symbol)
    rows = client.get_json(url, {
        "symbol": api_symbol,
        "interval": KLINE_INTERVAL,
        "startTime": 0,
        "limit": 1,
    })
    if not rows:
        return None
    open_ms = int(rows[0][0])
    return datetime.fromtimestamp(open_ms / 1000, tz=UTC)


def last_archived_open_time(
    symbol: str, market: str, root: Path = KLINES_ROOT,
) -> datetime | None:
    """Return the latest `open_time` archived for (symbol, market), or None."""
    if not root.exists():
        return None
    files = list(root.glob("year=*/data.parquet"))
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


def _archive_first_open_time(
    symbol: str, market: str, root: Path = KLINES_ROOT,
) -> datetime | None:
    """Earliest archived open_time for (symbol, market), for listing-date authority."""
    if not root.exists():
        return None
    files = list(root.glob("year=*/data.parquet"))
    if not files:
        return None
    df = (
        pl.scan_parquet([str(f) for f in files])
        .filter((pl.col("symbol") == symbol) & (pl.col("market") == market))
        .select(pl.col("open_time").min().alias("min_open_time"))
        .collect()
    )
    if df.is_empty() or df["min_open_time"][0] is None:
        return None
    return df["min_open_time"][0]


def effective_listing_date(
    symbol: str,
    market: str,
    snapshot_listing: date | None,
    root: Path = KLINES_ROOT,
) -> date | None:
    """Authoritative listing date for QC / downstream.

    Resolution order (CLAUDE.md gotcha + R2 critique on relisting):
      1. If archive has bars: return `min(snapshot_listing, observed_min_date)`.
         Observed is authoritative because the snapshot's `onboardDate` is a
         mutable view of exchangeInfo.
      2. If no archive: return `snapshot_listing` (cold start).
      3. If neither: return None.

    NOTE: relisting-with-backfill is NOT handled here -- a delisted-then-
    relisted symbol whose pre-delisting bars are still in archive will
    return the original (older) listing date. The alpha / qc layer must
    apply a `tradable_from` boundary separately if needed.
    """
    archived_first = _archive_first_open_time(symbol, market, root)
    if archived_first is None:
        return snapshot_listing
    archived_date = archived_first.date()
    if snapshot_listing is None:
        return archived_date
    return min(snapshot_listing, archived_date)


# ── Correction-diff + atomic write ────────────────────────────────────────


_NUMERIC_KLINE_COLS = (
    "open", "high", "low", "close",
    "volume", "quote_volume",
    "taker_buy_base", "taker_buy_quote",
)
_INT_KLINE_COLS = ("trades",)


def _detect_corrections(
    existing: pl.DataFrame,
    new_df: pl.DataFrame,
    market_label_field: str = "market",
    time_field: str = "open_time",
    rtol: float = CORRECTION_RTOL,
) -> list[dict]:
    """Compare overlapping keys and return CORRECTIONS_SCHEMA-shaped rows.

    `existing` and `new_df` must both have KLINES_SCHEMA. Numeric diffs are
    detected when `abs(new - old) > rtol * abs(old)` (relative tolerance —
    scales with magnitude across BTC's 80k vs 1000PEPE's 0.004).
    """
    if existing.is_empty() or new_df.is_empty():
        return []

    keys = ["symbol", market_label_field, time_field]
    cols_to_compare_numeric = list(_NUMERIC_KLINE_COLS)
    cols_to_compare_int = list(_INT_KLINE_COLS)

    overlap = (
        new_df.select(keys + cols_to_compare_numeric + cols_to_compare_int + ["ingested_at"])
        .join(
            existing.select(keys + cols_to_compare_numeric + cols_to_compare_int + ["ingested_at"]),
            on=keys, how="inner", suffix="_old",
        )
    )
    if overlap.is_empty():
        return []

    rows: list[dict] = []
    for r in overlap.iter_rows(named=True):
        for col in cols_to_compare_numeric:
            new_v = r[col]
            old_v = r[f"{col}_old"]
            if new_v is None or old_v is None:
                continue
            if abs(new_v - old_v) > rtol * max(abs(old_v), 1.0):
                rows.append(_make_correction_row(
                    r, col, float(old_v), float(new_v),
                    market_label_field, time_field,
                ))
        for col in cols_to_compare_int:
            new_v = r[col]
            old_v = r[f"{col}_old"]
            if new_v is None or old_v is None:
                continue
            if int(new_v) != int(old_v):
                rows.append(_make_correction_row(
                    r, col, float(old_v), float(new_v),
                    market_label_field, time_field,
                ))
    return rows


def _make_correction_row(
    overlap_row: dict, field: str, old_v: float, new_v: float,
    market_label_field: str, time_field: str,
) -> dict:
    return {
        "symbol": overlap_row["symbol"],
        "market": overlap_row[market_label_field],
        "time": overlap_row[time_field],
        "field": field,
        "old_value": old_v,
        "new_value": new_v,
        "old_ingested_at": overlap_row["ingested_at_old"],
        "new_ingested_at": overlap_row["ingested_at"],
    }


def _atomic_write_parquet(df: pl.DataFrame, target: Path) -> None:
    """Write to a tmp file then `os.replace` — crash mid-write cannot truncate."""
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    df.write_parquet(tmp, compression="zstd", compression_level=3)
    os.replace(tmp, target)


def _write_corrections_sidecar(
    rows: list[dict], root: Path, ingested_at: datetime,
) -> Path:
    """Persist correction rows to `{root}/_corrections/correction_<us>.parquet`."""
    sidecar_dir = root / "_corrections"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    unix_us = int(ingested_at.timestamp() * 1_000_000)
    p = sidecar_dir / f"correction_{unix_us}.parquet"
    df = pl.DataFrame(rows, schema=CORRECTIONS_SCHEMA)
    _atomic_write_parquet(df, p)
    log.info("wrote %d corrections -> %s", len(rows), p)
    return p


def write_klines_partitioned(
    df: pl.DataFrame, root: Path = KLINES_ROOT,
) -> None:
    """Year-partitioned merge with correction-diff sidecar + atomic write.

    Pre-write: anti-join new vs existing on (symbol, market, open_time);
    rows whose numeric columns differ above `CORRECTION_RTOL` go to the
    `_corrections` sidecar (audit trail). Then the existing keep-last
    merge proceeds.

    Idempotent on re-ingest. The sort-then-unique pattern is FS-validated
    (sorting by (..., ingested_at) makes `keep="last"` pick the newest row).
    """
    if df.is_empty():
        return

    ingested_at = df["ingested_at"].max()  # representative for filename
    df = df.with_columns(year=pl.col("open_time").dt.year())

    all_corrections: list[dict] = []
    for (year_val,), year_df in df.group_by(["year"]):
        year_dir = root / f"year={int(year_val)}"
        year_dir.mkdir(parents=True, exist_ok=True)
        target = year_dir / "data.parquet"
        new_df = year_df.drop("year")

        if target.exists():
            existing = pl.read_parquet(target)
            all_corrections.extend(_detect_corrections(existing, new_df))
            merged = pl.concat([existing, new_df], how="vertical_relaxed")
        else:
            merged = new_df

        merged = (
            merged
            .sort(["symbol", "market", "open_time", "ingested_at"])
            .unique(subset=["symbol", "market", "open_time"], keep="last")
            .sort(["symbol", "market", "open_time"])
        )
        _atomic_write_parquet(merged, target)
        log.info("wrote %d rows -> %s", merged.height, target)

    if all_corrections:
        _write_corrections_sidecar(all_corrections, root, ingested_at)


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(description="Fetch + write 1h klines for one (symbol, market).")
    p.add_argument("--symbol", required=True, help="canonical symbol, e.g. BTC-USDT")
    p.add_argument("--market", required=True, choices=["spot", "perp_usdt"])
    p.add_argument("--start", help="YYYY-MM-DD or ISO. Default = resume from archive.")
    p.add_argument("--end", help="YYYY-MM-DD or ISO. Default = now - 1h.")
    p.add_argument("--dry-run", action="store_true", help="fetch but do not write parquet")
    args = p.parse_args(argv)

    end_dt = parse_iso_or_date(args.end) if args.end else datetime.now(tz=UTC)
    if args.start:
        start_dt = parse_iso_or_date(args.start)
    else:
        last = last_archived_open_time(args.symbol, args.market)
        if last is None:
            raise SystemExit(
                "no archive yet for this (symbol, market) — pass --start explicitly"
            )
        start_dt = last + timedelta(hours=1)
        log.info("resuming from %s (one bar after last archived %s)", start_dt, last)

    if start_dt >= end_dt:
        log.info("nothing to fetch: start %s >= end %s", start_dt, end_dt)
        return 0

    with BinanceClient() as client:
        df = fetch_klines(args.symbol, args.market, start_dt, end_dt, client)

    log.info(
        "fetched %d rows for %s/%s in [%s, %s)",
        df.height, args.symbol, args.market,
        start_dt.isoformat(), end_dt.isoformat(),
    )

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
