"""Top-N USDT-M perpetual universe — live snapshot construction (Phase A.2 v2).

Pulls Binance futures + spot endpoints, applies eligibility filters,
ranks by 24h quote volume, persists the top-N as a parquet snapshot keyed
by calendar month plus a `rejected_YYYY-MM.parquet` sidecar listing every
candidate that was excluded with a reason and threshold.

POINT-IN-TIME CONTRACT
----------------------
A snapshot taken at moment `as_of` is keyed by the calendar month
[period_start, period_end] containing `as_of`, and is intended for
**forward use** — the universe to trade from `as_of` onwards within that
month. `universe_as_of(when)` strictly enforces `as_of_date <= when` to
prevent lookahead from a mid-month re-snap.

`onboardDate` (the `listing_date` source) is fetched from `exchangeInfo`
once and frozen into every row of the snapshot. It is NEVER re-queried
when reconstructing historical universes — Binance's exchangeInfo is a
mutable view (re-listings, ticker rebrands) and trusting today's value
for a 2023 snapshot would silently break canon §4 (never re-compute
historical universes). Phase A.3+ archive-derived universes will use
first-observed-bar-date as `listing_date` instead, with a different
method id.

ELIGIBILITY FILTERS (v2)
------------------------
1. Structural: contractType=PERPETUAL, status=TRADING, quoteAsset=USDT.
2. Sanity: last_price > 0.
3. Listing age: `onboardDate` is at least `min_listing_days` (default 180)
   before `as_of.date()`. Threshold is decoupled from alpha lookback —
   chosen for universe stability (filters typical 3-6mo pump-and-dump
   cycles) — so adding new long-lookback alphas later does not retroact
   on historical universe membership.

Symbols passing 1 enter the candidate set (sized `total_candidates`).
Filters 2-3 produce per-symbol rejection rows (sidecar). Surviving
symbols are sorted by `quote_volume_24h` desc and the top-N kept.

QUALITY COLUMNS (v2, NOT filters)
---------------------------------
* `avg_trade_size = quoteVolume / count` — recorded because it's free
  from the same payload, but it does NOT cleanly discriminate quality
  (deep-MM majors and illiquid altcoins both produce small values).
  Downstream consumers must interpret jointly with `quote_volume_24h`.
* `spot_pairs: List[Utf8]` — all TRADING spot symbols sharing this
  base_asset across {USDT, USDC, FDUSD}. List (not bool) because basis /
  carry economics differ materially across quotes.
* `primary_spot_quote_volume` — max 24h spot quote volume across
  `spot_pairs`. Null/0 if no spot pair exists.

ATOMICITY
---------
The snapshot pulls 4 endpoints (`/fapi/v1/exchangeInfo`,
`/fapi/v1/ticker/24hr`, `/api/v3/exchangeInfo`, `/api/v3/ticker/24hr`).
All four must land within `ATOMICITY_WINDOW_S` wall-clock seconds; if
the spread exceeds the window, a `RuntimeError` is raised. The four
fetched_at timestamps are stored on every row of the snapshot for audit.

KNOWN ISSUE (deferred to Phase A.5 — live trading layer)
--------------------------------------------------------
Calendar-month period + sub-month rebalance cadence opens a survivorship
window: a symbol delisted mid-month will still appear in the snapshot
till month-end. Live trading must re-check the universe against fresh
exchangeInfo at each rebalance; for backtest, archived klines reveal
delisting via missing bars. Not addressed at A.2.

SPEC NOTE
---------
Project spec calls for "30-day quote volume." `/fapi/v1/ticker/24hr`
returns 24-hour rolling volume only; for liquid majors the 24h-vs-30d
ranking is empirically near-identical. Strict 30-day computation
requires archived klines for ALL candidate perps (Phase A.3 work). The
ranking method id (`live_24hr_top_n_v2`) is recorded per row.

Storage layout
--------------
    {root}/{market}/snapshot_{YYYY}-{MM}.parquet
    {root}/{market}/rejected_{YYYY}-{MM}.parquet  (sidecar)

Public API
----------
    fetch_live_universe(...)  → (universe_df, rejected_df)
    write_snapshot(...)       → (snapshot_path, rejected_path)
    read_snapshot(...)        → pl.DataFrame   (universe)
    read_rejected(...)        → pl.DataFrame
    universe_as_of(...)       → pl.DataFrame   (POI-checked load by date)

CLI
---
    python -m alpha_factory.data.universe --out data/universe
"""
from __future__ import annotations

import argparse
import calendar
import logging
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from alpha_factory.data.schema import (
    REJECTED_CANDIDATES_SCHEMA,
    REJECTION_REASON_INVALID_PRICE,
    REJECTION_REASON_LISTING_TOO_YOUNG,
    UNIVERSE_SNAPSHOT_SCHEMA,
    conform,
)

log = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────
BINANCE_FAPI_BASE = "https://fapi.binance.com"
SPOT_API_BASE = "https://api.binance.com"
FAPI_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
FAPI_TICKER_24HR_PATH = "/fapi/v1/ticker/24hr"
SPOT_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
SPOT_TICKER_24HR_PATH = "/api/v3/ticker/24hr"
SOURCE_LIVE_24HR = "binance/fapi/v1/ticker/24hr"

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_N_TOP = 20
DEFAULT_QUOTE_ASSET = "USDT"
DEFAULT_MARKET = "perp_usdt"
RANK_METHOD_LIVE = "live_24hr_top_n_v2"
DEFAULT_UNIVERSE_DIR = Path("data") / "universe"
HTTP_TIMEOUT_S = 30.0

# Window across which all 4 endpoints must complete. Beyond this, candidate
# composition risks straddling delistings/listings and `total_candidates`
# becomes non-reproducible (per skill canon §1, point-in-time correctness).
ATOMICITY_WINDOW_S = 5.0

# Decoupled from alpha lookback (R2-5): chosen for universe stability —
# typical pump-and-dump cycles resolve within 3-6 months.
DEFAULT_MIN_LISTING_DAYS = 180

# Spot quote assets considered for cross-pair eligibility / liquidity.
# BUSD deprecated 2023, dropped. FDUSD is the canonical replacement on Binance.
SPOT_QUOTE_ASSETS: tuple[str, ...] = ("USDT", "USDC", "FDUSD")


# ── Pure helpers ──────────────────────────────────────────────────────────


def _calendar_month_period(dt: datetime) -> tuple[date, date]:
    """Return (first_day, last_day) of the calendar month containing `dt`."""
    period_start = dt.date().replace(day=1)
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    period_end = period_start.replace(day=last_day)
    return period_start, period_end


def _default_fetch_json(url: str) -> Any:
    """One-shot GET against a Binance public endpoint. Raises on HTTP error.

    Public futures + spot endpoints are highly available; for a snapshot
    we don't need the bulk-fetch retry machinery (Phase A.3+ will pull a
    shared `BinanceClient` into this layer when the kline fetcher needs
    paginated requests).
    """
    with httpx.Client(
        timeout=HTTP_TIMEOUT_S,
        headers={"User-Agent": "alpha-factory/0.1 (+research)"},
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.json()


def _now_utc() -> datetime:
    """Tz-aware UTC `datetime` at microsecond precision."""
    return datetime.now(UTC)


def _fetch_all_endpoints(
    fetch_json: Callable[[str], Any],
) -> dict[str, Any]:
    """Pull all 4 endpoints sequentially and record per-endpoint timestamps.

    Raises `RuntimeError` if total wall-clock exceeds `ATOMICITY_WINDOW_S`
    — beyond that, candidate composition risks straddling listing changes
    and `total_candidates` is non-reproducible.
    """
    t_start = _now_utc()
    fapi_exchange_info = fetch_json(BINANCE_FAPI_BASE + FAPI_EXCHANGE_INFO_PATH)
    t_fapi_exchange = _now_utc()
    fapi_ticker = fetch_json(BINANCE_FAPI_BASE + FAPI_TICKER_24HR_PATH)
    t_fapi_ticker = _now_utc()
    spot_exchange_info = fetch_json(SPOT_API_BASE + SPOT_EXCHANGE_INFO_PATH)
    t_spot_exchange = _now_utc()
    spot_ticker = fetch_json(SPOT_API_BASE + SPOT_TICKER_24HR_PATH)
    t_spot_ticker = _now_utc()

    delta_s = (t_spot_ticker - t_start).total_seconds()
    if delta_s > ATOMICITY_WINDOW_S:
        raise RuntimeError(
            f"endpoint fetch spanned {delta_s:.2f}s > {ATOMICITY_WINDOW_S:.1f}s "
            f"window — candidate state may be inconsistent across endpoints"
        )

    return {
        "fapi_exchange_info": fapi_exchange_info,
        "fapi_ticker": fapi_ticker,
        "spot_exchange_info": spot_exchange_info,
        "spot_ticker": spot_ticker,
        "fapi_exchangeinfo_fetched_at": t_fapi_exchange,
        "fapi_ticker_fetched_at": t_fapi_ticker,
        "spot_exchangeinfo_fetched_at": t_spot_exchange,
        "spot_ticker_fetched_at": t_spot_ticker,
    }


def _build_perp_candidates(
    fapi_exchange_info: dict[str, Any],
    fapi_ticker: list[dict[str, Any]],
    quote_asset: str,
) -> pl.DataFrame:
    """Return one row per (PERPETUAL, TRADING, quoteAsset) symbol joined to ticker.

    Columns: api_symbol, base_asset, quote_asset, onboard_date_ms (Int64),
    quote_volume_24h, last_price, trade_count_24h.
    """
    info_rows: list[dict[str, Any]] = [
        {
            "api_symbol": s["symbol"],
            "base_asset": s["baseAsset"],
            "quote_asset": s["quoteAsset"],
            # `onboardDate` is ms-since-epoch; fall back to None so a stale
            # / missing field surfaces clearly downstream rather than silently
            # passing a bogus listing_date.
            "onboard_date_ms": int(s["onboardDate"]) if s.get("onboardDate") else None,
        }
        for s in fapi_exchange_info.get("symbols", [])
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == quote_asset
        )
    ]
    info_df = pl.DataFrame(
        info_rows,
        schema={
            "api_symbol": pl.Utf8,
            "base_asset": pl.Utf8,
            "quote_asset": pl.Utf8,
            "onboard_date_ms": pl.Int64,
        },
    )

    ticker_rows: list[dict[str, Any]] = [
        {
            "api_symbol": t["symbol"],
            "quote_volume_24h": float(t["quoteVolume"]),
            "last_price": float(t["lastPrice"]),
            "trade_count_24h": int(t["count"]),
        }
        for t in fapi_ticker
    ]
    ticker_df = pl.DataFrame(
        ticker_rows,
        schema={
            "api_symbol": pl.Utf8,
            "quote_volume_24h": pl.Float64,
            "last_price": pl.Float64,
            "trade_count_24h": pl.Int64,
        },
    )

    return info_df.join(ticker_df, on="api_symbol", how="inner")


def _build_spot_pairs_index(
    spot_exchange_info: dict[str, Any],
    spot_ticker: list[dict[str, Any]],
    allowed_quote_assets: tuple[str, ...] = SPOT_QUOTE_ASSETS,
) -> dict[str, list[tuple[str, float]]]:
    """Build base_asset → [(spot_api_symbol, quote_volume_24h), ...] index.

    Only TRADING symbols with quoteAsset in `allowed_quote_assets`. Spot
    24h volume is NOT joined onto every perp row directly; it's looked
    up per base_asset to handle wrapper assets (WBETH, etc.) and multi-
    pair eligibility correctly.
    """
    eligible_spot_symbols: set[str] = {
        s["symbol"]
        for s in spot_exchange_info.get("symbols", [])
        if (
            s.get("status") == "TRADING"
            and s.get("quoteAsset") in allowed_quote_assets
        )
    }
    base_by_symbol: dict[str, str] = {
        s["symbol"]: s["baseAsset"]
        for s in spot_exchange_info.get("symbols", [])
        if s["symbol"] in eligible_spot_symbols
    }
    vol_by_symbol: dict[str, float] = {
        t["symbol"]: float(t["quoteVolume"])
        for t in spot_ticker
        if t["symbol"] in eligible_spot_symbols
    }

    index: dict[str, list[tuple[str, float]]] = {}
    for sym, base in base_by_symbol.items():
        # If a spot pair has no ticker (rare race), treat as zero volume but
        # still report its existence — downstream code can decide whether to
        # discard or treat as illiquid.
        vol = vol_by_symbol.get(sym, 0.0)
        index.setdefault(base, []).append((sym, vol))
    return index


def _split_eligible_and_rejected(
    candidates: pl.DataFrame,
    as_of: datetime,
    min_listing_days: int,
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Apply quality / eligibility filters; return (eligible_df, rejection_rows).

    Rejection rows are list-of-dict matching `REJECTED_CANDIDATES_SCHEMA`
    (minus `as_of`, which is added at write time).

    Filter order is deliberate: invalid_price first (a None/0 price makes
    listing-age semi-meaningless), then listing-age. The first failure
    flagged for a symbol becomes its `is_primary_reason=True` row; any
    additional failures attach as `is_primary_reason=False` rows so
    `count(*)` reconciles against `total_candidates - len(eligible)`.
    """
    as_of_date = as_of.date()
    rejection_rows: list[dict[str, Any]] = []
    primary_recorded: set[str] = set()

    def _record(api_symbol: str, base: str, quote: str, reason: str,
                observed: float, threshold: float) -> None:
        is_primary = api_symbol not in primary_recorded
        primary_recorded.add(api_symbol)
        canonical = f"{base}-{quote}"
        rejection_rows.append({
            "symbol": canonical,
            "api_symbol": api_symbol,
            "reason": reason,
            "observed_value": observed,
            "threshold": threshold,
            "is_primary_reason": is_primary,
        })

    invalid_price_threshold = 0.0
    listing_age_threshold = float(min_listing_days)
    eligible_keep: list[bool] = []
    for row in candidates.iter_rows(named=True):
        api_sym = row["api_symbol"]
        base = row["base_asset"]
        quote = row["quote_asset"]
        last_price = row["last_price"]
        onboard_ms = row["onboard_date_ms"]

        failed = False
        if last_price is None or last_price <= invalid_price_threshold:
            _record(api_sym, base, quote,
                    REJECTION_REASON_INVALID_PRICE,
                    observed=float(last_price) if last_price is not None else 0.0,
                    threshold=invalid_price_threshold)
            failed = True

        listing_days = _listing_days_at(onboard_ms, as_of_date)
        if listing_days is None or listing_days < min_listing_days:
            _record(api_sym, base, quote,
                    REJECTION_REASON_LISTING_TOO_YOUNG,
                    observed=float(listing_days) if listing_days is not None else 0.0,
                    threshold=listing_age_threshold)
            failed = True

        eligible_keep.append(not failed)

    eligible = candidates.with_columns(
        pl.Series("__keep__", eligible_keep)
    ).filter(pl.col("__keep__")).drop("__keep__")

    return eligible, rejection_rows


def _listing_days_at(onboard_date_ms: int | None, as_of_date: date) -> int | None:
    """Days between `onboardDate` (ms-since-epoch) and `as_of_date`. None if unknown."""
    if onboard_date_ms is None:
        return None
    onboard = datetime.fromtimestamp(onboard_date_ms / 1000, tz=UTC).date()
    return (as_of_date - onboard).days


def _attach_quality_and_spot(
    eligible: pl.DataFrame,
    spot_index: dict[str, list[tuple[str, float]]],
) -> pl.DataFrame:
    """Add avg_trade_size, listing_date, spot_pairs, primary_spot_quote_volume."""
    spot_pairs_col: list[list[str]] = []
    primary_vol_col: list[float] = []
    listing_date_col: list[date | None] = []
    avg_trade_size_col: list[float] = []
    for row in eligible.iter_rows(named=True):
        base = row["base_asset"]
        pairs = sorted(spot_index.get(base, []), key=lambda x: -x[1])
        spot_pairs_col.append([p[0] for p in pairs])
        primary_vol_col.append(pairs[0][1] if pairs else 0.0)

        onboard_ms = row["onboard_date_ms"]
        if onboard_ms is None:
            listing_date_col.append(None)
        else:
            listing_date_col.append(
                datetime.fromtimestamp(onboard_ms / 1000, tz=UTC).date()
            )

        cnt = row["trade_count_24h"]
        avg_trade_size_col.append(
            row["quote_volume_24h"] / cnt if cnt > 0 else 0.0
        )

    return eligible.with_columns(
        pl.Series("spot_pairs", spot_pairs_col, dtype=pl.List(pl.Utf8)),
        pl.Series("primary_spot_quote_volume", primary_vol_col, dtype=pl.Float64),
        pl.Series("listing_date", listing_date_col, dtype=pl.Date),
        pl.Series("avg_trade_size", avg_trade_size_col, dtype=pl.Float64),
    )


# ── Public API ────────────────────────────────────────────────────────────


def fetch_live_universe(
    n_top: int = DEFAULT_N_TOP,
    quote_asset: str = DEFAULT_QUOTE_ASSET,
    min_listing_days: int = DEFAULT_MIN_LISTING_DAYS,
    as_of: datetime | None = None,
    fetch_json: Callable[[str], Any] = _default_fetch_json,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Pull live top-N USDT-M perp universe + rejected-candidates sidecar.

    Returns:
        (universe_df, rejected_df) — both conform to their respective schemas
        in `alpha_factory.data.schema`. `rejected_df` may be empty.

    Raises:
        ValueError: if `as_of` is naive.
        RuntimeError: if the candidate set is empty after structural filter,
            or if endpoint fetch exceeds `ATOMICITY_WINDOW_S`.
    """
    if as_of is None:
        as_of = _now_utc()
    elif as_of.tzinfo is None:
        raise ValueError("as_of must be tz-aware (UTC); got naive datetime")
    else:
        as_of = as_of.astimezone(UTC)

    period_start, period_end = _calendar_month_period(as_of)

    log.info("fetching futures + spot endpoints from Binance")
    payloads = _fetch_all_endpoints(fetch_json)

    candidates = _build_perp_candidates(
        payloads["fapi_exchange_info"], payloads["fapi_ticker"], quote_asset
    )
    if candidates.is_empty():
        raise RuntimeError(
            f"no perpetuals matched contractType=PERPETUAL + status=TRADING "
            f"+ quoteAsset={quote_asset!r}"
        )
    total_candidates = candidates.shape[0]

    eligible, rejection_rows = _split_eligible_and_rejected(
        candidates, as_of, min_listing_days
    )
    if eligible.is_empty():
        raise RuntimeError(
            f"no perpetuals survived eligibility filters "
            f"(min_listing_days={min_listing_days}); "
            f"{total_candidates} candidates → 0 eligible"
        )

    spot_index = _build_spot_pairs_index(
        payloads["spot_exchange_info"], payloads["spot_ticker"]
    )
    eligible = _attach_quality_and_spot(eligible, spot_index)

    ingested_at = _now_utc()
    universe = (
        eligible.sort("quote_volume_24h", descending=True)
        .head(n_top)
        .with_row_index("rank", offset=1)
        .with_columns(
            (pl.col("base_asset") + "-" + pl.col("quote_asset")).alias("symbol"),
            pl.lit(DEFAULT_MARKET).alias("market"),
            pl.lit(as_of).alias("as_of"),
            pl.lit(period_start).alias("period_start"),
            pl.lit(period_end).alias("period_end"),
            pl.lit(total_candidates).cast(pl.UInt16).alias("total_candidates"),
            pl.lit(min_listing_days).cast(pl.UInt16).alias("min_listing_days_threshold"),
            pl.lit(RANK_METHOD_LIVE).alias("method"),
            pl.lit(ingested_at).alias("ingested_at"),
            pl.lit(SOURCE_LIVE_24HR).alias("source"),
            pl.lit(payloads["fapi_exchangeinfo_fetched_at"]).alias("fapi_exchangeinfo_fetched_at"),
            pl.lit(payloads["fapi_ticker_fetched_at"]).alias("fapi_ticker_fetched_at"),
            pl.lit(payloads["spot_exchangeinfo_fetched_at"]).alias("spot_exchangeinfo_fetched_at"),
            pl.lit(payloads["spot_ticker_fetched_at"]).alias("spot_ticker_fetched_at"),
        )
    )
    universe = conform(universe, UNIVERSE_SNAPSHOT_SCHEMA)

    rejected = pl.DataFrame(
        [{**r, "as_of": as_of} for r in rejection_rows],
        schema=REJECTED_CANDIDATES_SCHEMA,
    ) if rejection_rows else pl.DataFrame(schema=REJECTED_CANDIDATES_SCHEMA)
    rejected = conform(rejected, REJECTED_CANDIDATES_SCHEMA)

    return universe, rejected


def snapshot_path(period_start: date, market: str, root: Path) -> Path:
    """Canonical path: `{root}/{market}/snapshot_YYYY-MM.parquet`."""
    return root / market / f"snapshot_{period_start.strftime('%Y-%m')}.parquet"


def rejected_path(period_start: date, market: str, root: Path) -> Path:
    """Sidecar path: `{root}/{market}/rejected_YYYY-MM.parquet`."""
    return root / market / f"rejected_{period_start.strftime('%Y-%m')}.parquet"


def write_snapshot(
    universe: pl.DataFrame,
    rejected: pl.DataFrame,
    root: Path = DEFAULT_UNIVERSE_DIR,
) -> tuple[Path, Path]:
    """Persist universe + rejected sidecar; refuses to overwrite with empty universe."""
    if universe.is_empty():
        raise ValueError("refusing to write empty universe snapshot")

    market = universe.item(0, "market")
    period_start = universe.item(0, "period_start")
    p_universe = snapshot_path(period_start, market, root)
    p_rejected = rejected_path(period_start, market, root)
    p_universe.parent.mkdir(parents=True, exist_ok=True)
    universe.write_parquet(p_universe, compression="zstd")
    # Sidecar is always written, even if empty — absence-of-file would
    # be ambiguous (did we forget? did nothing get rejected?).
    rejected.write_parquet(p_rejected, compression="zstd")
    log.info(
        "wrote universe=%d rows -> %s ; rejected=%d rows -> %s",
        len(universe), p_universe, len(rejected), p_rejected,
    )
    return p_universe, p_rejected


def read_snapshot(
    period_start: date,
    market: str = DEFAULT_MARKET,
    root: Path = DEFAULT_UNIVERSE_DIR,
) -> pl.DataFrame:
    """Read the universe snapshot for `(market, period_start)`. Raises if missing."""
    p = snapshot_path(period_start, market, root)
    if not p.exists():
        raise FileNotFoundError(f"no snapshot at {p}")
    return pl.read_parquet(p)


def read_rejected(
    period_start: date,
    market: str = DEFAULT_MARKET,
    root: Path = DEFAULT_UNIVERSE_DIR,
) -> pl.DataFrame:
    """Read the rejected-candidates sidecar for `(market, period_start)`."""
    p = rejected_path(period_start, market, root)
    if not p.exists():
        raise FileNotFoundError(f"no rejected sidecar at {p}")
    return pl.read_parquet(p)


def universe_as_of(
    when: date,
    market: str = DEFAULT_MARKET,
    root: Path = DEFAULT_UNIVERSE_DIR,
) -> pl.DataFrame:
    """Return the snapshot whose [period_start, period_end] window contains `when`.

    Strict POI semantics:
      * the snapshot's `as_of` must be on or before `when`
      * the queried date must fall inside [period_start, period_end]
      * no fall-back to an earlier month's snapshot
    """
    period_start_lookup = when.replace(day=1)
    df = read_snapshot(period_start_lookup, market, root)
    as_of_date = df.item(0, "as_of").date()
    period_start = df.item(0, "period_start")
    period_end = df.item(0, "period_end")
    if as_of_date > when:
        raise ValueError(
            f"snapshot as_of={as_of_date} is later than queried date {when} "
            f"-- refusing to leak future info"
        )
    if not (period_start <= when <= period_end):
        raise ValueError(
            f"date {when} outside snapshot period [{period_start}, {period_end}]"
        )
    return df


# ── CLI ───────────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch + persist live top-N USDT-M perpetual universe."
    )
    p.add_argument("--n-top", type=int, default=DEFAULT_N_TOP)
    p.add_argument("--quote-asset", default=DEFAULT_QUOTE_ASSET)
    p.add_argument("--min-listing-days", type=int, default=DEFAULT_MIN_LISTING_DAYS)
    p.add_argument("--out", type=Path, default=DEFAULT_UNIVERSE_DIR)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + print top-N, do not write parquet",
    )
    return p


def _format_summary(df: pl.DataFrame) -> str:
    """Compact stdout view: rank | symbol | last_price | vol_24h | trades | spot_pairs."""
    view = df.select(
        "rank",
        "symbol",
        pl.col("last_price")
        .map_elements(lambda v: f"{v:>14,.4f}", return_dtype=pl.Utf8)
        .alias("last_price"),
        pl.col("quote_volume_24h")
        .map_elements(lambda v: f"{v:>15,.0f}", return_dtype=pl.Utf8)
        .alias("vol_24h_usd"),
        "trade_count_24h",
        pl.col("spot_pairs").list.len().alias("n_spot_pairs"),
    )
    return str(view)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pl.Config.set_ascii_tables(True)  # cp950 console (CLAUDE.md gotcha)

    args = _build_argparser().parse_args(argv)

    universe, rejected = fetch_live_universe(
        n_top=args.n_top,
        quote_asset=args.quote_asset,
        min_listing_days=args.min_listing_days,
    )
    print(_format_summary(universe))
    period_start = universe.item(0, "period_start")
    period_end = universe.item(0, "period_end")
    n_unique_rejected = rejected.filter(pl.col("is_primary_reason")).shape[0]
    print(f"\nperiod:           {period_start} -> {period_end}")
    print(f"as_of:            {universe.item(0, 'as_of')}")
    print(f"method:           {universe.item(0, 'method')}")
    print(f"total_candidates: {universe.item(0, 'total_candidates')}")
    print(f"min_listing_days: {universe.item(0, 'min_listing_days_threshold')}")
    print(f"rejected:         {len(rejected)} rows ({n_unique_rejected} unique symbols)")

    if args.dry_run:
        log.info("dry-run -- not writing snapshot")
        return 0

    p_uni, p_rej = write_snapshot(universe, rejected, root=args.out)
    print(f"\nwrote universe -> {p_uni}")
    print(f"wrote rejected -> {p_rej}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
