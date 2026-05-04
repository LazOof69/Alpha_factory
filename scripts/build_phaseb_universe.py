"""Build curated Phase B universe (15 perps with >= 2 yr listing).

Per Phase B sequence decision (user pick 'option B' on 2026-05-03):

  "BTC, ETH, SOL (already in archive)
   + BNB, XRP, DOGE, ADA, AVAX, LINK, DOT, MATIC, UNI, LTC, ATOM, TRX"
   = 15 perps total"

Rationale (vs current ADV-only top-20 which includes meme/AI tokens):
  - cross-sectional alpha (funding_xs) needs N >= 10 SIMULTANEOUSLY active
    in the backtest window
  - 2024+ meme/AI tokens (LAB, SKYAI, B, BIO, ORDI, HYPE, etc.) only have
    < 1 yr history, making 2019-2023 backtest sample N=3-4 (degenerate)
  - "classical alts" all listed 2020-2022 → 2023+ has N=15 active
  - size diversity is also more even (mega/large/mid/small caps balanced)

This script overwrites data/universe/perp_usdt/snapshot_2026-05.parquet
with the curated list. Once written, archive.py will fetch the missing
12 alts on the next run.

USAGE:
    uv run python scripts/build_phaseb_universe.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

# Use feasibility's binance_client for API access (already proven).
sys.path.insert(0, str(Path(__file__).parent.parent / "feasibility" / "scripts"))

from binance_client import BinanceClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Curated Phase B universe (per option B) ────────────────────────────


# Narrow mode (default) — option B from 2026-05-03 decision.
# 14 classical alts + BTC/ETH/SOL = 15 (MATIC delisted post-Polygon-rebrand
# → 14 actually). All listing >= 2 yr.
PHASEB_BASE_ASSETS = [
    "BTC", "ETH", "SOL",                        # already in archive
    "BNB", "XRP", "DOGE", "ADA", "AVAX",        # large-cap alts
    "LINK", "DOT", "MATIC", "UNI", "LTC",       # mid-cap alts
    "ATOM", "TRX",                              # additional classical
]

# Wide mode (--wide) — post adversarial-debate corrective universe (2026-05-03).
# Adds 6 high-funding-dispersion tokens with listing >= 1 yr by May 2026.
# These are the SOURCE OF THE FUNDING_XS ALPHA HYPOTHESIS that the narrow
# universe was specifically excluded — the debate verdict said narrow was
# the wrong call. Wider universe lets us re-test the thesis honestly.
PHASEB_WIDE_EXTRA_ASSETS = [
    "ORDI",       # ~2 yr (2024-Q1 listing); BRC-20 token
    "1000LUNC",   # ~3 yr (2022-Q3 listing post-Luna collapse); meme-grade volatility
    "1000PEPE",   # ~3 yr (2023-Q2 listing); meme token
    "TAO",        # ~2 yr (2024 listing); AI/Bittensor
    "HYPE",       # ~1 yr (2024-Q4 listing); Hyperliquid token (borderline)
    "BIO",        # ~1.4 yr (2025-01 listing); already in archive (was excluded
                  # from narrow); biotech meme
]

OUT_PATH = Path("data/universe/perp_usdt/snapshot_2026-05.parquet")
PROBE_START_MS = 1_500_000_000_000  # 2017-07-14


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_canonical(api_symbol: str, base: str) -> str:
    quote = api_symbol[len(base):]
    return f"{base}-{quote}"


def _probe_listing_date(cli: BinanceClient, api_symbol: str) -> date | None:
    """Probe perp futures kline endpoint for the first-bar open_time."""
    rows = cli.get_json(
        "https://fapi.binance.com/fapi/v1/klines",
        {
            "symbol": api_symbol,
            "interval": "1h",
            "startTime": PROBE_START_MS,
            "limit": 1,
        },
    )
    if not rows:
        return None
    open_ms = int(rows[0][0])
    return datetime.fromtimestamp(open_ms / 1000, tz=UTC).date()


def _spot_pairs_for(cli: BinanceClient, base: str) -> tuple[list[str], float | None]:
    """Find spot pairs for the base asset and primary spot quote_volume."""
    rows = cli.get_json("https://api.binance.com/api/v3/ticker/24hr", {})
    quotes_priority = ["USDT", "USDC", "FDUSD"]
    pairs = []
    primary_qv = None
    for q in quotes_priority:
        sym = f"{base}{q}"
        for r in rows:
            if r["symbol"] == sym:
                pairs.append(sym)
                if primary_qv is None:
                    primary_qv = float(r["quoteVolume"])
                break
    return pairs, primary_qv


# ── Build snapshot ───────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wide", action="store_true",
        help="Wide universe: 14 narrow + 6 high-dispersion meme/AI tokens "
             "(post adversarial-debate corrective universe; for funding_xs re-test)",
    )
    args = parser.parse_args()

    if args.wide:
        base_assets = PHASEB_BASE_ASSETS + PHASEB_WIDE_EXTRA_ASSETS
        method = "phaseb_wide_post_debate_v1"
        threshold_days = 365   # 1 year minimum
        log.info("Phase B WIDE universe builder (20 perps; post-debate)")
    else:
        base_assets = PHASEB_BASE_ASSETS
        method = "phaseb_curated_classical_alts_v1"
        threshold_days = 730   # 2 years minimum
        log.info("Phase B NARROW universe builder (14 perps; option B)")

    now = datetime.now(tz=UTC)
    period_start = date(2026, 5, 1)
    period_end = date(2026, 5, 31)

    rows: list[dict] = []
    with BinanceClient() as cli:
        # Get current ticker (USDT-M perp) for ADV ranking
        ticker_rows = cli.get_json(
            "https://fapi.binance.com/fapi/v1/ticker/24hr", {},
        )
        ticker_by_symbol = {r["symbol"]: r for r in ticker_rows}
        ticker_fetched_at = datetime.now(tz=UTC)

        for rank, base in enumerate(base_assets, start=1):
            api_symbol = f"{base}USDT"
            t = ticker_by_symbol.get(api_symbol)
            if t is None:
                log.warning(
                    "perp %s not on Binance USDT-M -- skipping", api_symbol,
                )
                continue

            log.info("probing listing date for %s ...", api_symbol)
            listing_dt = _probe_listing_date(cli, api_symbol)
            if listing_dt is None:
                log.warning("no probe data for %s -- skipping", api_symbol)
                continue

            spot_pairs, primary_qv = _spot_pairs_for(cli, base)
            spot_fetched_at = datetime.now(tz=UTC)
            spot_exinfo_fetched_at = spot_fetched_at  # reuse for sidecar field

            row = {
                "as_of": now,
                "period_start": period_start,
                "period_end": period_end,
                "rank": rank,
                "symbol": _to_canonical(api_symbol, base),
                "api_symbol": api_symbol,
                "market": "perp_usdt",
                "base_asset": base,
                "quote_asset": "USDT",
                "quote_volume_24h": float(t["quoteVolume"]),
                "last_price": float(t["lastPrice"]),
                "trade_count_24h": int(t["count"]),
                "avg_trade_size": (
                    float(t["quoteVolume"]) / int(t["count"])
                    if int(t["count"]) > 0 else 0.0
                ),
                "listing_date": listing_dt,
                "spot_pairs": spot_pairs,
                "primary_spot_quote_volume": primary_qv,
                "total_candidates": len(base_assets),
                "min_listing_days_threshold": threshold_days,
                "method": method,
                "ingested_at": now,
                "source": "binance/fapi/v1/ticker/24hr",
                "fapi_exchangeinfo_fetched_at": ticker_fetched_at,
                "fapi_ticker_fetched_at": ticker_fetched_at,
                "spot_exchangeinfo_fetched_at": spot_exinfo_fetched_at,
                "spot_ticker_fetched_at": spot_fetched_at,
            }
            rows.append(row)
            log.info(
                "  rank=%d %s listing=%s ADV=%s spot_pairs=%s",
                rank, row["symbol"], listing_dt,
                f"{row['quote_volume_24h']:,.0f}", spot_pairs,
            )

    if not rows:
        log.error("no rows built -- aborting")
        return 1

    df = pl.DataFrame(rows)

    # Match the existing snapshot's dtypes (read it for reference)
    if OUT_PATH.exists():
        existing = pl.read_parquet(OUT_PATH)
        target_schema = existing.schema
        # Cast to the existing schema where possible
        for col, dtype in target_schema.items():
            if col in df.columns and df.schema[col] != dtype:
                try:
                    df = df.with_columns(pl.col(col).cast(dtype))
                except Exception as e:
                    log.warning("cast %s -> %s failed: %s", col, dtype, e)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(OUT_PATH, compression="zstd")
    log.info(
        "wrote curated snapshot: %d perps -> %s", df.height, OUT_PATH,
    )
    log.info("you can now run: uv run python -m alpha_factory.data.archive --snapshot 2026-05")
    return 0


if __name__ == "__main__":
    sys.exit(main())
