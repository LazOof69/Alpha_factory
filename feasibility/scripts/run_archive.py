"""Orchestrator: archive klines + funding for the FS Study 1 universe, then QC.

Default behaviour: full historical fetch from each instrument's listing date
to one hour before now, then run all quality checks. With `--sample 30d` it
fetches only the last 30 days for a fast smoke-test.

CLI:
    uv run python feasibility/scripts/run_archive.py            # full
    uv run python feasibility/scripts/run_archive.py --sample 30d  # last 30 days
    uv run python feasibility/scripts/run_archive.py --skip-fetch  # QC only
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import UTC, datetime, timedelta

import polars as pl

from binance_client import BinanceClient
from fetch_funding import fetch_funding, write_funding_partitioned
from fetch_klines import fetch_klines, write_klines_partitioned
from qc import run_all, summarize
from schema import (
    FAPI_KLINES_URL,
    FUNDING_ROOT,
    KLINE_INTERVAL,
    KLINES_ROOT,
    SPOT_KLINES_URL,
    UNIVERSE_PATH,
    UNIVERSE_SCHEMA,
    UNIVERSE_SEED,
    to_api_symbol,
)

log = logging.getLogger(__name__)

# Far-back time we use as a probe to discover the real listing time.
PROBE_START_MS = 1_500_000_000_000  # 2017-07-14 — earlier than any USDT-M perp.
ABSOLUTE_FALLBACK_START = datetime(2017, 8, 17, tzinfo=UTC)


def discover_listing_date(client: BinanceClient, symbol: str, market: str) -> datetime:
    """Probe the kline endpoint with a far-back start and read the first bar's open_time."""
    url = SPOT_KLINES_URL if market == "spot" else FAPI_KLINES_URL
    api_symbol = to_api_symbol(symbol)
    rows = client.get_json(url, {
        "symbol": api_symbol,
        "interval": KLINE_INTERVAL,
        "startTime": PROBE_START_MS,
        "limit": 1,
    })
    if not rows:
        log.warning("no probe data for %s/%s — using absolute fallback", symbol, market)
        return ABSOLUTE_FALLBACK_START
    open_ms = int(rows[0][0])
    return datetime.fromtimestamp(open_ms / 1000, tz=UTC)


def parse_sample_window(s: str) -> timedelta:
    """Parse '30d' / '7d' / '24h' / '90d' style strings into a timedelta."""
    m = re.fullmatch(r"(\d+)([dh])", s)
    if not m:
        raise ValueError(f"bad --sample window: {s!r} (use e.g. '30d' or '24h')")
    n = int(m.group(1))
    return timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)


def write_universe(listing_dates: dict[tuple[str, str], datetime]) -> None:
    """Compose universe.parquet from UNIVERSE_SEED + discovered listing dates."""
    now = datetime.now(tz=UTC)
    rows = []
    for seed in UNIVERSE_SEED:
        listing_dt = listing_dates.get((seed["symbol"], seed["market"]))
        rows.append({
            "symbol": seed["symbol"],
            "market": seed["market"],
            "listing_date": listing_dt.date() if listing_dt else None,
            "delisting_date": None,
            "base_asset": seed["base_asset"],
            "quote_asset": seed["quote_asset"],
            "contract_type": seed["contract_type"],
            "funding_interval_hours": seed["funding_interval_hours"],
            "status": seed["status"],
            "last_verified": now,
        })
    df = pl.DataFrame(rows).cast(UNIVERSE_SCHEMA)  # type: ignore[arg-type]
    UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(UNIVERSE_PATH, compression="zstd", compression_level=3)
    log.info("wrote universe.parquet (%d rows)", df.height)


def load_klines() -> pl.DataFrame:
    files = list(KLINES_ROOT.glob("year=*/data.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.read_parquet([str(f) for f in files])


def load_funding() -> pl.DataFrame:
    files = list(FUNDING_ROOT.glob("year=*/data.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.read_parquet([str(f) for f in files])


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--sample", help="e.g. 30d / 7d / 24h — fetch only the last N (smoke test)")
    p.add_argument("--skip-fetch", action="store_true", help="run QC only on existing archive")
    p.add_argument("--end", help="ISO datetime; default now-1h")
    args = p.parse_args()

    end_dt = datetime.fromisoformat(args.end).astimezone(UTC) if args.end else (
        datetime.now(tz=UTC) - timedelta(hours=1)
    )

    # `probed_listing_dates` are first-pass hints from API probe; we OVERWRITE
    # them after fetch with the actual `min(open_time)` observed in the
    # archived data. This keeps QC's listing_date consistent regardless of
    # whether we ran via fetch or via --skip-fetch.
    probed_listing_dates: dict[tuple[str, str], datetime] = {}
    listing_dates: dict[tuple[str, str], datetime] = {}

    if not args.skip_fetch:
        with BinanceClient() as client:
            # Step 1: probe listing dates (first-pass hint only)
            for seed in UNIVERSE_SEED:
                key = (seed["symbol"], seed["market"])
                listing = discover_listing_date(client, *key)
                probed_listing_dates[key] = listing
                log.info("probed listing %s/%s = %s", *key, listing.date())

            # Step 2: fetch klines
            for seed in UNIVERSE_SEED:
                sym, mkt = seed["symbol"], seed["market"]
                listing = probed_listing_dates[(sym, mkt)]
                start = max(listing, end_dt - parse_sample_window(args.sample)) \
                    if args.sample else listing
                if start >= end_dt:
                    log.info("skip %s/%s: nothing to fetch", sym, mkt)
                    continue
                log.info("fetching klines %s/%s [%s, %s)", sym, mkt,
                         start.isoformat(), end_dt.isoformat())
                df = fetch_klines(sym, mkt, start, end_dt, client)
                log.info("  → %d rows", df.height)
                write_klines_partitioned(df)

            # Step 3: fetch funding (perp only)
            for seed in UNIVERSE_SEED:
                if seed["market"] != "perp_usdt":
                    continue
                sym = seed["symbol"]
                listing = probed_listing_dates[(sym, "perp_usdt")]
                start = max(listing, end_dt - parse_sample_window(args.sample)) \
                    if args.sample else listing
                if start >= end_dt:
                    continue
                log.info("fetching funding %s [%s, %s)", sym,
                         start.isoformat(), end_dt.isoformat())
                df = fetch_funding(sym, start, end_dt, client)
                log.info("  → %d rows", df.height)
                write_funding_partitioned(df)

    # Step 4: derive authoritative listing_dates from observed archive data.
    # This unifies the fetch and --skip-fetch paths and corrects any probe
    # fallback (e.g., absolute fallback → 2017 for a perp that launched 2019).
    klines_for_listing = load_klines()
    if not klines_for_listing.is_empty():
        grouped = (
            klines_for_listing.group_by(["symbol", "market"])
            .agg(pl.col("open_time").min().alias("first"))
            .to_dicts()
        )
        for r in grouped:
            listing_dates[(r["symbol"], r["market"])] = r["first"]
    # Log any divergence between probe and observation (good signal).
    for key, observed in listing_dates.items():
        probed = probed_listing_dates.get(key)
        if probed and abs((observed - probed).total_seconds()) > 3600:
            log.warning("listing date diverged for %s/%s: probe=%s observed=%s",
                        *key, probed.date(), observed.date())

    if not args.skip_fetch:
        # Step 5: write universe metadata using authoritative listing_dates.
        write_universe(listing_dates)

    # Step 5: load and QC
    klines = load_klines()
    funding = load_funding()
    log.info("loaded %d klines rows, %d funding rows", klines.height, funding.height)

    results = run_all(klines, funding, listing_dates)
    n_error, n_warn, n_info = summarize(results)

    print("\n=== QC Report ===")
    for r in results:
        print(r)
    print(f"\nSummary: {n_error} ERROR, {n_warn} WARN, {n_info} INFO")

    return 1 if n_error > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
