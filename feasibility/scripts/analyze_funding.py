"""Distribution + regime analysis on the funding rate archive.

Outputs a markdown summary to stdout and (when --xlsx) the same tables to
feasibility/results/funding_analysis.xlsx via the openpyxl backend.

Used as Study 1's first analytical pass before backtest. Numbers here drive
the regime split decisions (ETF cutoff, bull/bear threshold) for backtest_carry.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from schema import FUNDING_ROOT

log = logging.getLogger(__name__)

# Bitcoin spot ETF approval — clear structural break in funding regime.
ETF_CUTOFF = datetime(2024, 1, 11, tzinfo=UTC)

# Settlements per year for annualization (3 / day × 365)
SETTLEMENTS_PER_YEAR = 1095


def load_funding() -> pl.DataFrame:
    files = list(FUNDING_ROOT.glob("year=*/data.parquet"))
    if not files:
        raise SystemExit(f"no funding archive at {FUNDING_ROOT}")
    return pl.read_parquet([str(f) for f in files]).sort(["symbol", "funding_time"])


def yearly_stats(df: pl.DataFrame) -> pl.DataFrame:
    """Mean / median / std / % positive / annualized return per (year, symbol)."""
    return (
        df.with_columns(year=pl.col("funding_time").dt.year())
        .group_by(["symbol", "year"])
        .agg(
            n=pl.len(),
            mean_8h=pl.col("funding_rate").mean(),
            median_8h=pl.col("funding_rate").median(),
            std_8h=pl.col("funding_rate").std(),
            pct_positive=(pl.col("funding_rate") > 0).mean(),
            min_rate=pl.col("funding_rate").min(),
            max_rate=pl.col("funding_rate").max(),
        )
        .with_columns(annualized_pct=pl.col("mean_8h") * SETTLEMENTS_PER_YEAR * 100)
        .sort(["symbol", "year"])
    )


def regime_split(df: pl.DataFrame) -> pl.DataFrame:
    """Pre-ETF vs post-ETF mean / std / annualized for each symbol."""
    df = df.with_columns(
        regime=pl.when(pl.col("funding_time") < ETF_CUTOFF)
        .then(pl.lit("pre_ETF"))
        .otherwise(pl.lit("post_ETF"))
    )
    return (
        df.group_by(["symbol", "regime"])
        .agg(
            n=pl.len(),
            mean_8h=pl.col("funding_rate").mean(),
            std_8h=pl.col("funding_rate").std(),
            pct_positive=(pl.col("funding_rate") > 0).mean(),
            min_rate=pl.col("funding_rate").min(),
            max_rate=pl.col("funding_rate").max(),
        )
        .with_columns(annualized_pct=pl.col("mean_8h") * SETTLEMENTS_PER_YEAR * 100)
        .sort(["symbol", "regime"])
    )


def percentile_distribution(df: pl.DataFrame) -> pl.DataFrame:
    """Funding rate percentiles per symbol (full sample)."""
    rows = []
    for sym in sorted(df["symbol"].unique().to_list()):
        sub = df.filter(pl.col("symbol") == sym)["funding_rate"]
        rows.append({
            "symbol": sym,
            "p01": float(sub.quantile(0.01)),
            "p05": float(sub.quantile(0.05)),
            "p25": float(sub.quantile(0.25)),
            "p50": float(sub.quantile(0.50)),
            "p75": float(sub.quantile(0.75)),
            "p95": float(sub.quantile(0.95)),
            "p99": float(sub.quantile(0.99)),
            "n": int(sub.len()),
        })
    return pl.DataFrame(rows).sort("symbol")


def negative_runs(df: pl.DataFrame) -> pl.DataFrame:
    """For each symbol, longest run of consecutive negative funding settlements.

    Negative funding = shorts pay longs → carry harvester gets crushed.
    Long runs of negative funding map onto bear regimes — this stat tells us
    how robust the regime detector needs to be.
    """
    df = df.sort(["symbol", "funding_time"])
    df = df.with_columns(
        is_neg=(pl.col("funding_rate") < 0),
    ).with_columns(
        # run id: increment when sign changes or symbol changes
        run_id=(
            (pl.col("is_neg") != pl.col("is_neg").shift(1).over("symbol"))
            .fill_null(True)
            .cast(pl.Int64)
            .cum_sum()
            .over("symbol")
        )
    )
    runs = (
        df.filter(pl.col("is_neg"))
        .group_by(["symbol", "run_id"])
        .agg(
            run_len=pl.len(),
            run_start=pl.col("funding_time").min(),
            run_end=pl.col("funding_time").max(),
        )
    )
    longest = (
        runs.group_by("symbol")
        .agg(
            longest_neg_run=pl.col("run_len").max(),
            longest_run_start=pl.col("run_start").get(pl.col("run_len").arg_max()),
            longest_run_end=pl.col("run_end").get(pl.col("run_len").arg_max()),
            n_neg_runs=pl.len(),
            total_neg_settlements=pl.col("run_len").sum(),
        )
        .sort("symbol")
    )
    return longest


def render_md(yearly: pl.DataFrame, regime: pl.DataFrame,
              percentile: pl.DataFrame, neg_runs: pl.DataFrame) -> str:
    """Render all four tables to a markdown string for stdout."""
    lines: list[str] = ["# Funding Rate Analysis — Study 1\n"]

    lines.append("## Yearly stats")
    lines.append("")
    lines.append(
        "| symbol | year | n | mean (bp/8h) | annualized (%) | %positive | std (bp) | "
        "min (bp) | max (bp) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in yearly.iter_rows(named=True):
        lines.append(
            f"| {r['symbol']} | {r['year']} | {r['n']} | "
            f"{r['mean_8h']*1e4:+.2f} | {r['annualized_pct']:+.2f} | "
            f"{r['pct_positive']*100:.1f}% | {r['std_8h']*1e4:.2f} | "
            f"{r['min_rate']*1e4:+.2f} | {r['max_rate']*1e4:+.2f} |"
        )
    lines.append("")

    lines.append("## ETF regime split (cutoff = 2024-01-11)")
    lines.append("")
    lines.append("| symbol | regime | n | mean (bp/8h) | annualized (%) | %positive | std (bp) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in regime.iter_rows(named=True):
        lines.append(
            f"| {r['symbol']} | {r['regime']} | {r['n']} | "
            f"{r['mean_8h']*1e4:+.2f} | {r['annualized_pct']:+.2f} | "
            f"{r['pct_positive']*100:.1f}% | {r['std_8h']*1e4:.2f} |"
        )
    lines.append("")

    lines.append("## Funding rate percentiles (full sample)")
    lines.append("")
    lines.append("| symbol | p01 | p05 | p25 | p50 | p75 | p95 | p99 | n |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in percentile.iter_rows(named=True):
        lines.append(
            f"| {r['symbol']} | {r['p01']*1e4:+.2f} | {r['p05']*1e4:+.2f} | "
            f"{r['p25']*1e4:+.2f} | {r['p50']*1e4:+.2f} | "
            f"{r['p75']*1e4:+.2f} | {r['p95']*1e4:+.2f} | "
            f"{r['p99']*1e4:+.2f} | {r['n']} |"
        )
    lines.append("")

    lines.append("## Longest negative-funding runs (carry-harvester risk)")
    lines.append("")
    lines.append(
        "| symbol | longest run (settlements) | start | end | "
        "n runs | total negative settlements |"
    )
    lines.append("|---|---|---|---|---|---|")
    for r in neg_runs.iter_rows(named=True):
        lines.append(
            f"| {r['symbol']} | {r['longest_neg_run']} | "
            f"{r['longest_run_start'].date()} | {r['longest_run_end'].date()} | "
            f"{r['n_neg_runs']} | {r['total_neg_settlements']} |"
        )
    lines.append("")

    return "\n".join(lines)


def write_xlsx(yearly, regime, percentile, neg_runs, target: Path) -> None:
    """Write all 4 tables to a single .xlsx workbook with a sheet each."""
    target.parent.mkdir(parents=True, exist_ok=True)
    # Excel doesn't support tz-aware datetimes — cast neg_runs timestamps to
    # plain Date (which is what we display anyway).
    neg_runs_xlsx = neg_runs.with_columns(
        pl.col("longest_run_start").cast(pl.Date),
        pl.col("longest_run_end").cast(pl.Date),
    )

    sheets = [
        ("yearly", yearly),
        ("regime_etf", regime),
        ("percentiles", percentile),
        ("neg_runs", neg_runs_xlsx),
    ]
    for name, df in sheets:
        df.write_excel(workbook=str(target), worksheet=name, autofit=True)
    log.info("wrote %s", target)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--xlsx", help="Optional path to .xlsx output (defaults to results/)")
    args = p.parse_args()

    df = load_funding()
    log.info("loaded %d funding rows", df.height)

    yearly = yearly_stats(df)
    regime = regime_split(df)
    percentile = percentile_distribution(df)
    neg_runs = negative_runs(df)

    print(render_md(yearly, regime, percentile, neg_runs))

    xlsx_path = Path(args.xlsx) if args.xlsx else (
        Path(__file__).resolve().parents[1] / "results" / "funding_analysis.xlsx"
    )
    write_xlsx(yearly, regime, percentile, neg_runs, xlsx_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
