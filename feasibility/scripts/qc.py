"""Quality checks for FS Study 1 archives.

Each check returns a `QCResult`. Hard failures have severity="ERROR" and
**must** block the data from being treated as authoritative — callers should
treat the run as failed and investigate.

Reference: thresholds defined in feasibility/README.md and discussed with the
market-data-pipeline skill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import polars as pl


@dataclass
class QCResult:
    name: str
    passed: bool
    severity: str  # "ERROR" | "WARN" | "INFO"
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        # ASCII-only — Windows default console (cp950 / cp1252) cannot encode
        # ✓/✗, and we want the QC report readable everywhere.
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}/{self.severity}] {self.name}: {self.details}"


# ---------------------------------------------------------------------------
# Severity helpers — used by checks that follow a 3-tier pass/warn/error pattern
# ---------------------------------------------------------------------------


def _severity_coverage(value: float, info_min: float, warn_min: float) -> tuple[str, bool]:
    """For metrics where higher is better (e.g. archive coverage ratio).

    Returns ("INFO", True) if value >= info_min,
            ("WARN", True) if value >= warn_min,
            ("ERROR", False) otherwise.
    """
    if value >= info_min:
        return "INFO", True
    if value >= warn_min:
        return "WARN", True
    return "ERROR", False


def _severity_count(error_count: int, warn_count: int) -> tuple[str, bool]:
    """For metrics where any positive count is bad (count of violations).

    The two thresholds are nested: every error_count is also counted in warn_count.
    """
    if error_count > 0:
        return "ERROR", False
    if warn_count > 0:
        return "WARN", True
    return "INFO", True


# ---------------------------------------------------------------------------
# Klines checks (K1–K8)
# ---------------------------------------------------------------------------


def k1_no_missing_bars(
    df: pl.DataFrame, symbol: str, market: str, listing_dt: datetime
) -> QCResult:
    """Coverage of expected hourly grid >= 99.9% (warn at <99.9%, error at <99%).

    Expected count is computed against the archive's actual `max(open_time)` for
    this (symbol, market), NOT `datetime.now()`. Otherwise an archive completed
    yesterday would falsely report 24 missing bars when QC runs today.
    """
    sub = df.filter((pl.col("symbol") == symbol) & (pl.col("market") == market))
    actual = sub.height
    if actual == 0:
        return QCResult(
            f"k1_no_missing_bars[{symbol}/{market}]", False, "ERROR",
            {"coverage": 0.0, "actual": 0, "note": "no rows for this instrument"},
        )
    archive_end: datetime = sub.select(pl.col("open_time").max()).item()
    expected_hours = (archive_end - listing_dt).total_seconds() / 3600.0 + 1
    coverage = actual / expected_hours if expected_hours > 0 else 0.0
    sev, passed = _severity_coverage(coverage, info_min=0.999, warn_min=0.99)
    return QCResult(
        f"k1_no_missing_bars[{symbol}/{market}]",
        passed,
        sev,
        {"coverage": round(coverage, 5), "actual": actual, "expected": int(expected_hours)},
    )


def k2_no_duplicates(df: pl.DataFrame) -> QCResult:
    n_dup = (
        df.group_by(["symbol", "market", "open_time"])
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    return QCResult("k2_no_duplicates", n_dup == 0,
                    "INFO" if n_dup == 0 else "ERROR",
                    {"duplicates": n_dup})


def k3_ohlc_validity(df: pl.DataFrame) -> QCResult:
    bad = df.filter(
        (pl.col("high") < pl.max_horizontal(["open", "close"]))
        | (pl.col("low") > pl.min_horizontal(["open", "close"]))
        | (pl.col("high") < pl.col("low"))
    ).height
    return QCResult("k3_ohlc_validity", bad == 0,
                    "INFO" if bad == 0 else "ERROR",
                    {"violations": bad})


def k4_non_negative(df: pl.DataFrame) -> QCResult:
    bad = df.filter(
        (pl.col("volume") < 0)
        | (pl.col("quote_volume") < 0)
        | (pl.col("trades") < 0)
    ).height
    return QCResult("k4_non_negative", bad == 0,
                    "INFO" if bad == 0 else "ERROR",
                    {"violations": bad})


def k5_stale_prices(df: pl.DataFrame) -> QCResult:
    """Warn if any (symbol,market) has a run of >= 24 consecutive flat-zero bars."""
    sorted_df = df.sort(["symbol", "market", "open_time"])
    flat = (
        (pl.col("open") == pl.col("high"))
        & (pl.col("high") == pl.col("low"))
        & (pl.col("low") == pl.col("close"))
        & (pl.col("volume") == 0)
    )
    grouped = sorted_df.with_columns(flat=flat).with_columns(
        run_id=(
            (pl.col("flat") != pl.col("flat").shift(1).over(["symbol", "market"]))
            .fill_null(True)
            .cast(pl.Int64)
            .cum_sum()
            .over(["symbol", "market"])
        )
    )
    long_runs = (
        grouped.filter(pl.col("flat"))
        .group_by(["symbol", "market", "run_id"])
        .len()
        .filter(pl.col("len") >= 24)
        .height
    )
    return QCResult("k5_stale_prices", long_runs == 0,
                    "INFO" if long_runs == 0 else "WARN",
                    {"runs_24h_plus": long_runs})


def k6_extreme_returns(df: pl.DataFrame) -> QCResult:
    """Warn on |log(close/open)| > 0.20 in 1h. Could be legit (e.g. May 2021 LUNA-like)."""
    extreme = df.filter(
        (pl.col("close") / pl.col("open")).log().abs() > 0.20
    ).height
    return QCResult("k6_extreme_returns", extreme == 0,
                    "INFO" if extreme == 0 else "WARN",
                    {"extreme_bars": extreme})


def k7_spot_perp_consistency(df: pl.DataFrame, base: str) -> QCResult:
    """abs(perp - spot)/spot per matched hour. Warn > 2%, error > 10%."""
    sym = f"{base}-USDT"
    spot = (
        df.filter((pl.col("market") == "spot") & (pl.col("symbol") == sym))
        .select([pl.col("open_time"), pl.col("close").alias("spot_close")])
    )
    perp = (
        df.filter((pl.col("market") == "perp_usdt") & (pl.col("symbol") == sym))
        .select([pl.col("open_time"), pl.col("close").alias("perp_close")])
    )
    if spot.is_empty() or perp.is_empty():
        return QCResult(f"k7_spot_perp_consistency[{base}]", True, "INFO",
                        {"note": "missing spot or perp data"})
    joined = spot.join(perp, on="open_time", how="inner").with_columns(
        dev=((pl.col("perp_close") - pl.col("spot_close")).abs() / pl.col("spot_close"))
    )
    over_2 = joined.filter(pl.col("dev") > 0.02).height
    over_10 = joined.filter(pl.col("dev") > 0.10).height
    sev, passed = _severity_count(error_count=over_10, warn_count=over_2)
    return QCResult(
        f"k7_spot_perp_consistency[{base}]", passed, sev,
        {"over_2pct": over_2, "over_10pct": over_10, "matched": joined.height},
    )


def k8_no_partial_last_bar(df: pl.DataFrame) -> QCResult:
    bad = df.filter(pl.col("close_time") >= pl.col("ingested_at")).height
    return QCResult("k8_no_partial_last_bar", bad == 0,
                    "INFO" if bad == 0 else "ERROR",
                    {"partial_bars": bad})


# ---------------------------------------------------------------------------
# Funding checks (F1–F4)
# ---------------------------------------------------------------------------


def f1_settlement_alignment(df: pl.DataFrame) -> QCResult:
    """funding_time must be exactly 00 / 08 / 16 UTC, on the second."""
    if df.is_empty():
        return QCResult("f1_settlement_alignment", True, "INFO", {"note": "empty"})
    bad = df.filter(
        ~pl.col("funding_time").dt.hour().is_in([0, 8, 16])
        | (pl.col("funding_time").dt.minute() != 0)
        | (pl.col("funding_time").dt.second() != 0)
        | (pl.col("funding_time").dt.microsecond() != 0)
    ).height
    return QCResult("f1_settlement_alignment", bad == 0,
                    "INFO" if bad == 0 else "ERROR",
                    {"misaligned": bad})


def f2_no_missing_settlements(
    df: pl.DataFrame, symbol: str, listing_dt: datetime
) -> QCResult:
    """Expected 3 settlements/day (every 8h) within the archive window.

    Use `max(listing_dt, archive_first_funding)` as the effective start —
    `listing_dt` from kline observation lands on a kline-grid (e.g. 09:00)
    which can be up to 8h before the first 8h-aligned funding settlement
    (e.g. 16:00). Naively using listing_dt over-estimates expected count.
    """
    sub = df.filter(pl.col("symbol") == symbol)
    actual = sub.height
    if actual == 0:
        return QCResult(
            f"f2_no_missing_settlements[{symbol}]", False, "ERROR",
            {"coverage": 0.0, "actual": 0, "note": "no funding rows for this symbol"},
        )
    archive_first: datetime = sub.select(pl.col("funding_time").min()).item()
    archive_end: datetime = sub.select(pl.col("funding_time").max()).item()
    effective_start = max(listing_dt, archive_first)
    expected = (archive_end - effective_start).total_seconds() / (8 * 3600) + 1
    coverage = actual / expected if expected > 0 else 0.0
    sev, passed = _severity_coverage(coverage, info_min=0.9995, warn_min=0.99)
    return QCResult(
        f"f2_no_missing_settlements[{symbol}]", passed, sev,
        {"coverage": round(coverage, 5), "actual": actual, "expected": int(expected)},
    )


def f3_funding_rate_range(df: pl.DataFrame) -> QCResult:
    """abs(rate) sanity check.

    Binance funding caps are per-symbol and have changed over time:
      * BTCUSDT cap was raised 0.75% → 1.5% / 8h in 2021.
      * Many small-cap perps remained at 0.75%.

    To avoid false ERRORs on legit bull-market funding (Feb-2021, Oct-2021,
    Q1-2024), we only flag ERROR at >= 2% / 8h — which is above any historical
    Binance cap and indicates true data corruption or a Binance side incident.
    50bp+ is still a useful WARN to surface for manual inspection.
    """
    if df.is_empty():
        return QCResult("f3_funding_rate_range", True, "INFO", {"note": "empty"})
    over_50bp = df.filter(pl.col("funding_rate").abs() > 0.005).height
    over_2pct = df.filter(pl.col("funding_rate").abs() > 0.02).height
    sev, passed = _severity_count(error_count=over_2pct, warn_count=over_50bp)
    return QCResult(
        "f3_funding_rate_range", passed, sev,
        {"over_50bp": over_50bp, "over_2pct": over_2pct},
    )


def f4_no_duplicates(df: pl.DataFrame) -> QCResult:
    n_dup = (
        df.group_by(["symbol", "funding_time"])
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    return QCResult("f4_no_duplicates", n_dup == 0,
                    "INFO" if n_dup == 0 else "ERROR",
                    {"duplicates": n_dup})


# ---------------------------------------------------------------------------
# Cross-table check (X1)
# ---------------------------------------------------------------------------


def x1_funding_to_kline_join(klines: pl.DataFrame, funding: pl.DataFrame) -> QCResult:
    """Every funding row must fall inside a perp kline's [open_time, close_time)."""
    if funding.is_empty():
        return QCResult("x1_funding_to_kline_join", True, "INFO", {"note": "no funding"})
    perp = klines.filter(pl.col("market") == "perp_usdt").select(
        ["symbol", "open_time", "close_time"]
    ).sort(["symbol", "open_time"])
    if perp.is_empty():
        return QCResult("x1_funding_to_kline_join", False, "ERROR",
                        {"note": "perp klines missing — cannot validate join"})
    joined = funding.sort(["symbol", "funding_time"]).join_asof(
        perp,
        left_on="funding_time",
        right_on="open_time",
        by="symbol",
        strategy="backward",
    )
    valid = joined.filter(
        pl.col("close_time").is_not_null()
        & (pl.col("funding_time") < pl.col("close_time"))
    ).height
    total = funding.height
    miss = total - valid
    return QCResult("x1_funding_to_kline_join", miss == 0,
                    "INFO" if miss == 0 else "ERROR",
                    {"matched": valid, "total": total, "miss": miss})


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def run_all(
    klines: pl.DataFrame,
    funding: pl.DataFrame,
    listing_dates: dict[tuple[str, str], datetime],
) -> list[QCResult]:
    """Run every applicable check. `listing_dates` keyed by (symbol, market).

    For funding checks, we use the perp listing date — funding can only exist
    after the perp launches.
    """
    results: list[QCResult] = []

    # Klines: per (symbol, market) coverage; whole-set checks for the rest.
    if not klines.is_empty():
        for (sym, mkt), listing_dt in listing_dates.items():
            results.append(k1_no_missing_bars(klines, sym, mkt, listing_dt))
        results.append(k2_no_duplicates(klines))
        results.append(k3_ohlc_validity(klines))
        results.append(k4_non_negative(klines))
        results.append(k5_stale_prices(klines))
        results.append(k6_extreme_returns(klines))
        for base in {sym.split("-")[0] for sym, _ in listing_dates}:
            results.append(k7_spot_perp_consistency(klines, base))
        results.append(k8_no_partial_last_bar(klines))

    # Funding
    if not funding.is_empty():
        results.append(f1_settlement_alignment(funding))
        for sym in funding["symbol"].unique().to_list():
            perp_listing = listing_dates.get((sym, "perp_usdt"))
            if perp_listing is not None:
                results.append(f2_no_missing_settlements(funding, sym, perp_listing))
        results.append(f3_funding_rate_range(funding))
        results.append(f4_no_duplicates(funding))

    # Cross
    if not klines.is_empty() and not funding.is_empty():
        results.append(x1_funding_to_kline_join(klines, funding))

    return results


def summarize(results: list[QCResult]) -> tuple[int, int, int]:
    """Return (n_error, n_warn, n_info)."""
    errors = sum(1 for r in results if r.severity == "ERROR")
    warns = sum(1 for r in results if r.severity == "WARN")
    infos = sum(1 for r in results if r.severity == "INFO")
    return errors, warns, infos
