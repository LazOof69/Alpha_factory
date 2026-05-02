"""Quality checks for Phase A archives — port + universe-aware extensions.

PORTED VERBATIM from feasibility/scripts/qc.py:
    K1 coverage, K2 dups, K3 OHLC validity, K4 non-negative, K5 stale,
    K6 extreme returns, K7 spot-perp consistency, K8 partial-bar guard,
    F1 alignment, F2 coverage, F3 funding rate range, F4 dups, X1 cross-join.

NEW in A.3.4 (post 2-round adversarial debate):

* Tier-based thresholds for K6/K7. Two tiers, classification by universe rank:
    - majors      (rank ≤ 10): K6 |log return| WARN > 0.30; K7 (5%, 15%) basis
    - small_caps  (rank > 10): K6 |log return| WARN > 0.50; K7 (10%, 25%)
  Two-tier (vs three) per R2 simplicity / discontinuity argument: "BTC and
  SOL share dynamics more than BTC and a true small-cap." Rationale per
  threshold documented inline.

* C1 spot-perp leg completeness: ERROR if a universe row has non-empty
  spot_pairs but the canonical spot leg is absent from archive (or vice
  versa). Distinct from K1 ("data exists for THIS market") -- C1 is "both
  market legs present when expected."

* `compute_qc_results(klines, funding, universe)` is PURE -- returns
  list[QCResult]. Audit-trail write is in `write_qc_audit(...)`, called by
  the CLI. Per CLAUDE.md "Side effects only inside __main__ block;
  library modules pure."

* `write_qc_audit` uses the atomic-write pattern from klines.py: write to
  `<file>.tmp.<pid>`, then os.replace. Mid-write crash leaves no canonical
  audit file (clean signal vs partial-write ambiguity from a sentinel
  approach).

DEFERRED (per R2 OPTIONAL):
* Cross-month tier comparability (frozen at run-ts).
* JSON column flattening beyond `symbol` / `market` / `tier`.

CLAUDE.md red lines satisfied: ASCII PASS/FAIL output (no Unicode in
QCResult.__str__), tz-aware timestamps, no silent overwrite of audit log.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from alpha_factory.data.schema import (
    DATA_ROOT,
    QC_RUN_SCHEMA,
)

log = logging.getLogger(__name__)


# ── QC result ─────────────────────────────────────────────────────────────


@dataclass
class QCResult:
    name: str
    passed: bool
    severity: str   # "ERROR" | "WARN" | "INFO"
    details: dict = field(default_factory=dict)
    symbol: str | None = None
    market: str | None = None
    tier: str | None = None

    def __str__(self) -> str:
        # ASCII-only — Windows cp950 console (FS gotcha).
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}/{self.severity}] {self.name}: {self.details}"


# ── Severity helpers ──────────────────────────────────────────────────────


def _severity_coverage(value: float, info_min: float, warn_min: float) -> tuple[str, bool]:
    if value >= info_min:
        return "INFO", True
    if value >= warn_min:
        return "WARN", True
    return "ERROR", False


def _severity_count(error_count: int, warn_count: int) -> tuple[str, bool]:
    if error_count > 0:
        return "ERROR", False
    if warn_count > 0:
        return "WARN", True
    return "INFO", True


# ── Tier classification (NEW A.3.4) ───────────────────────────────────────


TIER_MAJORS = "majors"
TIER_SMALL_CAPS = "small_caps"
TIER_RANK_BOUNDARY = 10   # rank ≤ 10 = majors; > 10 = small_caps


def tier_for(rank: int) -> str:
    """Map a universe rank to its threshold tier.

    Two-tier scheme: rank 1-10 = majors, 11+ = small_caps. Three-tier
    (BTC/ETH special) was rejected in R2 -- discontinuity at rank=2 →ETH
    boundary masks the fact that SOL/BNB/etc. share dynamics with majors,
    not with meme tokens.
    """
    return TIER_MAJORS if rank <= TIER_RANK_BOUNDARY else TIER_SMALL_CAPS


# Tier-keyed thresholds. Document rationale per knob.
_K6_LOG_RETURN_WARN = {
    # 20% hourly is a 5-sigma event for BTC/ETH/SOL — surfacing it is the point.
    TIER_MAJORS: 0.20,
    # Meme/spec perps can routinely move 30-50% on a listing pump or liquidation
    # cascade; 50% threshold filters noise without blinding to true blowups.
    TIER_SMALL_CAPS: 0.50,
}
_K7_BASIS_WARN = {
    TIER_MAJORS: 0.02,
    TIER_SMALL_CAPS: 0.10,
}
_K7_BASIS_ERROR = {
    TIER_MAJORS: 0.10,
    TIER_SMALL_CAPS: 0.25,
}


# ── K1-K8 klines checks (port from FS, K6/K7 take tier) ───────────────────


def k1_no_missing_bars(
    df: pl.DataFrame, symbol: str, market: str, listing_dt: datetime,
) -> QCResult:
    """Coverage of expected hourly grid against listing_date — pass≥99.9%."""
    sub = df.filter((pl.col("symbol") == symbol) & (pl.col("market") == market))
    actual = sub.height
    if actual == 0:
        return QCResult(
            f"k1_no_missing_bars[{symbol}/{market}]", False, "ERROR",
            {"coverage": 0.0, "actual": 0, "note": "no rows for this instrument"},
            symbol=symbol, market=market,
        )
    archive_end = sub.select(pl.col("open_time").max()).item()
    expected_hours = (archive_end - listing_dt).total_seconds() / 3600.0 + 1
    coverage = actual / expected_hours if expected_hours > 0 else 0.0
    sev, passed = _severity_coverage(coverage, info_min=0.999, warn_min=0.99)
    return QCResult(
        f"k1_no_missing_bars[{symbol}/{market}]", passed, sev,
        {"coverage": round(coverage, 5), "actual": actual, "expected": int(expected_hours)},
        symbol=symbol, market=market,
    )


def k2_no_duplicates(df: pl.DataFrame) -> QCResult:
    n_dup = (
        df.group_by(["symbol", "market", "open_time"])
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    return QCResult(
        "k2_no_duplicates", n_dup == 0,
        "INFO" if n_dup == 0 else "ERROR",
        {"duplicates": n_dup},
    )


def k3_ohlc_validity(df: pl.DataFrame) -> QCResult:
    bad = df.filter(
        (pl.col("high") < pl.max_horizontal(["open", "close"]))
        | (pl.col("low") > pl.min_horizontal(["open", "close"]))
        | (pl.col("high") < pl.col("low"))
    ).height
    return QCResult(
        "k3_ohlc_validity", bad == 0,
        "INFO" if bad == 0 else "ERROR",
        {"violations": bad},
    )


def k4_non_negative(df: pl.DataFrame) -> QCResult:
    bad = df.filter(
        (pl.col("volume") < 0)
        | (pl.col("quote_volume") < 0)
        | (pl.col("trades") < 0)
    ).height
    return QCResult(
        "k4_non_negative", bad == 0,
        "INFO" if bad == 0 else "ERROR",
        {"violations": bad},
    )


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
    return QCResult(
        "k5_stale_prices", long_runs == 0,
        "INFO" if long_runs == 0 else "WARN",
        {"runs_24h_plus": long_runs},
    )


def k6_extreme_returns(
    df: pl.DataFrame, symbol: str, market: str, tier: str,
) -> QCResult:
    """Warn on |log(close/open)| > tier-specific threshold."""
    threshold = _K6_LOG_RETURN_WARN[tier]
    sub = df.filter((pl.col("symbol") == symbol) & (pl.col("market") == market))
    extreme = sub.filter(
        (pl.col("close") / pl.col("open")).log().abs() > threshold
    ).height
    return QCResult(
        f"k6_extreme_returns[{symbol}/{market}]", extreme == 0,
        "INFO" if extreme == 0 else "WARN",
        {"extreme_bars": extreme, "threshold": threshold, "tier": tier},
        symbol=symbol, market=market, tier=tier,
    )


def k7_spot_perp_consistency(
    df: pl.DataFrame, base: str, tier: str,
) -> QCResult:
    """abs(perp - spot)/spot per matched hour. Tier-based warn/error."""
    sym = f"{base}-USDT"
    spot = (
        df.filter((pl.col("market") == "spot") & (pl.col("symbol") == sym))
        .select(pl.col("open_time"), pl.col("close").alias("spot_close"))
    )
    perp = (
        df.filter((pl.col("market") == "perp_usdt") & (pl.col("symbol") == sym))
        .select(pl.col("open_time"), pl.col("close").alias("perp_close"))
    )
    if spot.is_empty() or perp.is_empty():
        return QCResult(
            f"k7_spot_perp_consistency[{base}]", True, "INFO",
            {"note": "missing spot or perp data"},
            symbol=sym, tier=tier,
        )
    warn_thr = _K7_BASIS_WARN[tier]
    error_thr = _K7_BASIS_ERROR[tier]
    joined = spot.join(perp, on="open_time", how="inner").with_columns(
        dev=((pl.col("perp_close") - pl.col("spot_close")).abs() / pl.col("spot_close"))
    )
    over_warn = joined.filter(pl.col("dev") > warn_thr).height
    over_error = joined.filter(pl.col("dev") > error_thr).height
    sev, passed = _severity_count(error_count=over_error, warn_count=over_warn)
    return QCResult(
        f"k7_spot_perp_consistency[{base}]", passed, sev,
        {
            "over_warn": over_warn, "over_error": over_error,
            "warn_thr": warn_thr, "error_thr": error_thr,
            "matched": joined.height, "tier": tier,
        },
        symbol=sym, tier=tier,
    )


def k8_no_partial_last_bar(df: pl.DataFrame) -> QCResult:
    bad = df.filter(pl.col("close_time") >= pl.col("ingested_at")).height
    return QCResult(
        "k8_no_partial_last_bar", bad == 0,
        "INFO" if bad == 0 else "ERROR",
        {"partial_bars": bad},
    )


# ── F1-F4 funding checks (port from FS) ───────────────────────────────────


def f1_settlement_alignment(df: pl.DataFrame) -> QCResult:
    if df.is_empty():
        return QCResult("f1_settlement_alignment", True, "INFO", {"note": "empty"})
    bad = df.filter(
        ~pl.col("funding_time").dt.hour().is_in([0, 8, 16])
        | (pl.col("funding_time").dt.minute() != 0)
        | (pl.col("funding_time").dt.second() != 0)
        | (pl.col("funding_time").dt.microsecond() != 0)
    ).height
    return QCResult(
        "f1_settlement_alignment", bad == 0,
        "INFO" if bad == 0 else "ERROR",
        {"misaligned": bad},
    )


def f2_no_missing_settlements(
    df: pl.DataFrame, symbol: str, listing_dt: datetime,
) -> QCResult:
    """Expected 3 settlements/day. Effective start = max(listing_dt, archive_first)."""
    sub = df.filter(pl.col("symbol") == symbol)
    actual = sub.height
    if actual == 0:
        return QCResult(
            f"f2_no_missing_settlements[{symbol}]", False, "ERROR",
            {"coverage": 0.0, "actual": 0, "note": "no funding rows for this symbol"},
            symbol=symbol, market="perp_usdt",
        )
    archive_first = sub.select(pl.col("funding_time").min()).item()
    archive_end = sub.select(pl.col("funding_time").max()).item()
    effective_start = max(listing_dt, archive_first)
    expected = (archive_end - effective_start).total_seconds() / (8 * 3600) + 1
    coverage = actual / expected if expected > 0 else 0.0
    sev, passed = _severity_coverage(coverage, info_min=0.9995, warn_min=0.99)
    return QCResult(
        f"f2_no_missing_settlements[{symbol}]", passed, sev,
        {"coverage": round(coverage, 5), "actual": actual, "expected": int(expected)},
        symbol=symbol, market="perp_usdt",
    )


def f3_funding_rate_range(df: pl.DataFrame) -> QCResult:
    """abs(rate) sanity check.

    Two thresholds preserved from FS:
      WARN at 0.5%/8h  -- catches BTC bull-market funding AND small-cap cap-peg
                          (small caps still capped at 0.75%/8h on Binance).
      ERROR at 2%/8h   -- above ANY historical Binance cap; indicates true
                          data corruption or a Binance-side incident.
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
        df.group_by(["symbol", "funding_time"]).len()
        .filter(pl.col("len") > 1).height
    )
    return QCResult(
        "f4_no_duplicates", n_dup == 0,
        "INFO" if n_dup == 0 else "ERROR",
        {"duplicates": n_dup},
    )


# ── X1 cross-table check (port) ───────────────────────────────────────────


def x1_funding_to_kline_join(
    klines: pl.DataFrame, funding: pl.DataFrame,
) -> QCResult:
    """Every funding row must fall inside a perp kline's [open_time, close_time)."""
    if funding.is_empty():
        return QCResult("x1_funding_to_kline_join", True, "INFO",
                        {"note": "no funding"})
    perp = klines.filter(pl.col("market") == "perp_usdt").select(
        ["symbol", "open_time", "close_time"]
    ).sort(["symbol", "open_time"])
    if perp.is_empty():
        return QCResult("x1_funding_to_kline_join", False, "ERROR",
                        {"note": "no perp klines"})
    joined = funding.join_asof(
        perp, left_on="funding_time", right_on="open_time",
        by="symbol", strategy="backward",
    )
    bad = joined.filter(
        pl.col("close_time").is_null()
        | (pl.col("funding_time") < pl.col("open_time"))
        | (pl.col("funding_time") >= pl.col("close_time"))
    ).height
    return QCResult(
        "x1_funding_to_kline_join", bad == 0,
        "INFO" if bad == 0 else "ERROR",
        {"unjoined": bad, "total": joined.height},
    )


# ── C1 universe leg-completeness (NEW A.3.4) ──────────────────────────────


def c1_universe_legs_present(
    klines: pl.DataFrame, universe: pl.DataFrame,
) -> list[QCResult]:
    """For each universe row with non-empty spot_pairs, BOTH perp and the
    canonical USDT spot leg must have ≥1 kline row in archive.

    Distinct from K1 ("data exists for this market"). C1 is "both legs
    present when the universe says we should have them." A row that is
    perp-only (spot_pairs=[]) silently passes -- there's nothing to check.
    """
    out: list[QCResult] = []
    for row in universe.iter_rows(named=True):
        sym = row["symbol"]
        spot_pairs: list[str] = list(row["spot_pairs"])
        canonical_spot = f"{row['base_asset']}USDT"
        if canonical_spot not in spot_pairs:
            continue   # universe says no canonical spot leg -- nothing to enforce
        perp_n = klines.filter(
            (pl.col("symbol") == sym) & (pl.col("market") == "perp_usdt")
        ).height
        spot_n = klines.filter(
            (pl.col("symbol") == sym) & (pl.col("market") == "spot")
        ).height
        passed = perp_n > 0 and spot_n > 0
        sev = "INFO" if passed else "ERROR"
        out.append(QCResult(
            f"c1_universe_legs_present[{sym}]", passed, sev,
            {"perp_rows": perp_n, "spot_rows": spot_n},
            symbol=sym,
        ))
    return out


# ── Pure orchestrator ─────────────────────────────────────────────────────


def compute_qc_results(
    klines: pl.DataFrame,
    funding: pl.DataFrame,
    universe: pl.DataFrame,
    listing_dates: dict[tuple[str, str], datetime],
) -> list[QCResult]:
    """PURE — return list[QCResult] for one full QC pass.

    `listing_dates` is a dict keyed by (symbol, market) → datetime. The
    caller (CLI / orchestrator) constructs it via `effective_listing_date`
    from klines.py so the snapshot-vs-observed authority logic is honored.

    Single-pass ordering:
      (1) per-(symbol, market) checks: K1
      (2) per-base checks: K6 perp + K6 spot (if exists), K7 (if both legs)
      (3) per-perp funding check: F2
      (4) cross-cutting kline checks: K2, K3, K4, K5, K8
      (5) cross-cutting funding checks: F1, F3, F4
      (6) universe-aware: C1
      (7) cross-table: X1
    """
    results: list[QCResult] = []

    for row in universe.iter_rows(named=True):
        sym = row["symbol"]
        rank = int(row["rank"])
        base = row["base_asset"]
        spot_pairs: list[str] = list(row["spot_pairs"])
        canonical_spot = f"{base}USDT"
        tier = tier_for(rank)

        # K1 + K6 perp.
        listing_dt = listing_dates.get((sym, "perp_usdt"))
        if listing_dt is not None:
            results.append(k1_no_missing_bars(klines, sym, "perp_usdt", listing_dt))
        results.append(k6_extreme_returns(klines, sym, "perp_usdt", tier))

        # K1 + K6 spot (if canonical spot leg in universe).
        if canonical_spot in spot_pairs:
            spot_listing = listing_dates.get((sym, "spot"))
            if spot_listing is not None:
                results.append(k1_no_missing_bars(klines, sym, "spot", spot_listing))
            results.append(k6_extreme_returns(klines, sym, "spot", tier))
            results.append(k7_spot_perp_consistency(klines, base, tier))

        # F2.
        if listing_dt is not None:
            results.append(f2_no_missing_settlements(funding, sym, listing_dt))

    # Cross-cutting kline checks.
    results.append(k2_no_duplicates(klines))
    results.append(k3_ohlc_validity(klines))
    results.append(k4_non_negative(klines))
    results.append(k5_stale_prices(klines))
    results.append(k8_no_partial_last_bar(klines))

    # Cross-cutting funding checks.
    results.append(f1_settlement_alignment(funding))
    results.append(f3_funding_rate_range(funding))
    results.append(f4_no_duplicates(funding))

    # Universe-aware leg-completeness.
    results.extend(c1_universe_legs_present(klines, universe))

    # Cross-table.
    results.append(x1_funding_to_kline_join(klines, funding))

    return results


# ── Audit-log writer (side-effecting, called from CLI) ────────────────────


QC_AUDIT_DIR = DATA_ROOT / "qc_runs"


def write_qc_audit(
    results: list[QCResult],
    run_ts: datetime | None = None,
    audit_dir: Path = QC_AUDIT_DIR,
) -> Path:
    """Write QC results to `audit_dir/qc_run_<unix_us>.parquet` (atomic).

    Mid-write crash leaves a `.tmp.<pid>` file but no canonical parquet —
    readers naturally skip incomplete runs.
    """
    if run_ts is None:
        run_ts = datetime.now(tz=UTC)
    rows = [{
        "run_ts": run_ts,
        "name": r.name,
        "passed": r.passed,
        "severity": r.severity,
        "symbol": r.symbol,
        "market": r.market,
        "tier": r.tier,
        "details_json": json.dumps(r.details, default=str),
    } for r in results]
    df = pl.DataFrame(rows, schema=QC_RUN_SCHEMA)
    audit_dir.mkdir(parents=True, exist_ok=True)
    unix_us = int(run_ts.timestamp() * 1_000_000)
    target = audit_dir / f"qc_run_{unix_us}.parquet"
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    df.write_parquet(tmp, compression="zstd", compression_level=3)
    os.replace(tmp, target)
    log.info("wrote %d QC results -> %s", len(results), target)
    return target


# ── Severity rollup helper ────────────────────────────────────────────────


def summarize(results: list[QCResult]) -> tuple[int, int, int]:
    """Return (n_error, n_warn, n_info) across results."""
    n_error = sum(1 for r in results if r.severity == "ERROR")
    n_warn = sum(1 for r in results if r.severity == "WARN")
    n_info = sum(1 for r in results if r.severity == "INFO")
    return n_error, n_warn, n_info


def n_symbols_with_error(results: list[QCResult]) -> int:
    """Distinct (symbol, market) keys that had at least one ERROR."""
    keys = {(r.symbol, r.market) for r in results
            if r.severity == "ERROR" and r.symbol is not None}
    return len(keys)


# Note: this module deliberately has NO `main()` / CLI entry. The QC
# orchestration belongs in archive.py (Phase A.3.5) which has the universe
# + listing-dates + archive load context. Kept pure here per CLAUDE.md.
