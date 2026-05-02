"""Perp funding-rate fetcher + year-partitioned archive (single-symbol primitive).

Production port of `feasibility/scripts/fetch_funding.py`. Universe-aware
iteration is the orchestrator's job (A.3.5). Same architecture as
`alpha_factory.data.klines`:

* Pagination cursor advances by `last_funding_ms + 1` (vs +KLINE_INTERVAL_MS
  for klines) — Binance returns funding events at the exact 8h boundary, +1ms
  guards against re-fetching the boundary settlement.
* Correction-diff sidecar pre-write per CLAUDE.md red line ("no silent
  overwrite of archive data — corrections must be logged"). rtol=1e-9 to
  ignore parquet float ULP drift.
* Atomic write via `<file>.tmp.<pid>` + `os.replace`.
* `BinanceRateLimitError` propagates from BinanceClient unchanged; the
  orchestrator decides whether to abort.

FS GOTCHA preserved: Binance occasionally reports `fundingTime` with
1-13ms of clock skew past the exact 8h boundary; we `dt.truncate("1s")`
on ingest so F1 alignment QC is meaningful and downstream joins are exact.
Also: empty-string `markPrice` / `fundingRate` on edge-case early rows are
cast `strict=False` to null; rows with null `funding_rate` are dropped
(primary field; null would silently zero out PnL in carry backtest) and
the count is logged.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.data.binance_client import BinanceClient
from alpha_factory.data.schema import (
    CORRECTIONS_SCHEMA,
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

# Match klines.py rtol so QC reports a uniform sensitivity across both archives.
CORRECTION_RTOL = 1e-9

# Funding correction-diff target: only mark_price + funding_rate are numeric.
_NUMERIC_FUNDING_COLS = ("funding_rate", "mark_price")


# ── Row → DataFrame ──────────────────────────────────────────────────────


def _rows_to_df(rows: list[dict], canonical_symbol: str) -> pl.DataFrame:
    """Convert raw Binance funding rows into a typed polars DataFrame."""
    if not rows:
        return pl.DataFrame(schema=FUNDING_SCHEMA)

    ingested_at = datetime.now(tz=UTC)
    # Strings preserved through cast(strict=False) — empty strings on edge-case
    # early rows turn into null cleanly. "Missing -> null" is the right semantics
    # (vs silent zero, which would distort PnL).
    df = pl.DataFrame({
        "symbol": [canonical_symbol] * len(rows),
        "funding_time_ms": [int(r["fundingTime"]) for r in rows],
        "funding_rate_raw": [r.get("fundingRate") for r in rows],
        "mark_price_raw": [r.get("markPrice") for r in rows],
    })

    df = df.with_columns(
        # 1-13ms boundary skew gotcha -- truncate to second.
        epoch_ms_to_utc_us(pl.col("funding_time_ms"))
            .dt.truncate("1s")
            .alias("funding_time"),
        pl.col("funding_rate_raw").cast(pl.Float64, strict=False).alias("funding_rate"),
        pl.col("mark_price_raw").cast(pl.Float64, strict=False).alias("mark_price"),
        pl.lit(ingested_at).alias("ingested_at"),
        pl.lit(SOURCE_FUNDING).alias("source"),
    ).drop(["funding_time_ms", "funding_rate_raw", "mark_price_raw"])

    n_total = df.height
    df = df.filter(pl.col("funding_rate").is_not_null())
    n_dropped = n_total - df.height
    if n_dropped > 0:
        log.warning(
            "dropped %d funding rows with null funding_rate (symbol=%s)",
            n_dropped, canonical_symbol,
        )
    return conform(df, FUNDING_SCHEMA)


# ── Public API ────────────────────────────────────────────────────────────


def fetch_funding(
    symbol: str,
    start_dt: datetime,
    end_dt: datetime,
    client: BinanceClient,
) -> pl.DataFrame:
    """Fetch all funding settlements in [start_dt, end_dt) for one perp symbol.

    Returns a DataFrame conforming to `FUNDING_SCHEMA`. Empty on no data.

    Raises:
        ValueError: on naive datetimes or start >= end.
        BinanceAPIError / BinanceRateLimitError: per binance_client.
    """
    if start_dt.tzinfo is None or end_dt.tzinfo is None:
        raise ValueError("start_dt and end_dt must be timezone-aware (UTC)")
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
        cursor_ms = last_funding_ms + 1   # +1ms guards against boundary re-fetch

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


def last_archived_funding_time(
    symbol: str, root: Path = FUNDING_ROOT,
) -> datetime | None:
    """Return the latest archived `funding_time` for `symbol`, or None."""
    if not root.exists():
        return None
    files = list(root.glob("year=*/data.parquet"))
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


# ── Correction-diff + atomic write ────────────────────────────────────────


def _detect_corrections(
    existing: pl.DataFrame,
    new_df: pl.DataFrame,
    rtol: float = CORRECTION_RTOL,
) -> list[dict]:
    """Compare overlapping (symbol, funding_time) keys; return CORRECTIONS_SCHEMA rows.

    Numeric diffs flagged when `abs(new - old) > rtol * max(abs(old), 1.0)`
    (relative tolerance — ignores parquet float ULP drift while still
    catching genuine vendor corrections). For null-vs-null and
    null-vs-value, we conservatively skip (downstream null filter in
    `_rows_to_df` already keeps null `funding_rate` out of archive).
    """
    if existing.is_empty() or new_df.is_empty():
        return []

    keys = ["symbol", "funding_time"]
    cols = list(_NUMERIC_FUNDING_COLS)

    overlap = (
        new_df.select(keys + cols + ["ingested_at"])
        .join(
            existing.select(keys + cols + ["ingested_at"]),
            on=keys, how="inner", suffix="_old",
        )
    )
    if overlap.is_empty():
        return []

    rows: list[dict] = []
    for r in overlap.iter_rows(named=True):
        for col in cols:
            new_v = r[col]
            old_v = r[f"{col}_old"]
            if new_v is None or old_v is None:
                continue
            if abs(new_v - old_v) > rtol * max(abs(old_v), 1.0):
                rows.append({
                    "symbol": r["symbol"],
                    "market": "perp_funding",
                    "time": r["funding_time"],
                    "field": col,
                    "old_value": float(old_v),
                    "new_value": float(new_v),
                    "old_ingested_at": r["ingested_at_old"],
                    "new_ingested_at": r["ingested_at"],
                })
    return rows


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


def write_funding_partitioned(
    df: pl.DataFrame, root: Path = FUNDING_ROOT,
) -> None:
    """Year-partitioned merge with correction-diff sidecar + atomic write."""
    if df.is_empty():
        return

    ingested_at = df["ingested_at"].max()
    df = df.with_columns(year=pl.col("funding_time").dt.year())

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
            .sort(["symbol", "funding_time", "ingested_at"])
            .unique(subset=["symbol", "funding_time"], keep="last")
            .sort(["symbol", "funding_time"])
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
    p = argparse.ArgumentParser(description="Fetch + write funding rate for one perp symbol.")
    p.add_argument("--symbol", required=True, help="canonical symbol, e.g. BTC-USDT")
    p.add_argument("--start", help="YYYY-MM-DD or ISO. Default = resume from archive.")
    p.add_argument("--end", help="YYYY-MM-DD or ISO. Default = now.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    end_dt = parse_iso_or_date(args.end) if args.end else datetime.now(tz=UTC)
    if args.start:
        start_dt = parse_iso_or_date(args.start)
    else:
        last = last_archived_funding_time(args.symbol)
        if last is None:
            raise SystemExit("no archive yet — pass --start explicitly")
        start_dt = last + timedelta(milliseconds=1)
        log.info("resuming from %s", start_dt)

    if start_dt >= end_dt:
        log.info("nothing to fetch")
        return 0

    with BinanceClient() as client:
        df = fetch_funding(args.symbol, start_dt, end_dt, client)

    log.info(
        "fetched %d funding rows for %s in [%s, %s)",
        df.height, args.symbol, start_dt.isoformat(), end_dt.isoformat(),
    )

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
