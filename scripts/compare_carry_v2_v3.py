"""Compare carry V2 vs V3 on real BTC archive — Phase B V3-validation harness.

Loads the L1 archive (klines + funding for BTC-USDT spot + perp_usdt),
runs both V2 and V3 through `runner.run_carry_validation`, and prints
side-by-side metrics so the V3 thesis can be evaluated empirically.

USAGE:
    uv run python scripts/compare_carry_v2_v3.py

OUTPUT:
    - Side-by-side metrics table (full-sample / last-12m / post-ETF)
    - n_transitions + active_fraction for each
    - DSR + verdict for each
    - Indicator: does V3 dominate V2 in post-ETF window?
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.alpha.carry import STRATEGY_ID as V2_STRATEGY_ID
from alpha_factory.alpha.carry import CarryParams, run_carry_backtest
from alpha_factory.alpha.carry_v3 import STRATEGY_ID as V3_STRATEGY_ID
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
    run_carry_v3_backtest,
)
from alpha_factory.runner import run_carry_validation

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────


SYMBOL = "BTC-USDT"
DATA_DIR = Path("data")
ETF_LAUNCH = datetime(2024, 1, 11, tzinfo=UTC)


# ── Archive loading ──────────────────────────────────────────────────────


def _load_klines() -> pl.DataFrame:
    """Load klines for BTC-USDT (spot + perp_usdt) from L1 archive.

    Archive layout: data/klines/year=YYYY/data.parquet (all symbols
    + markets in a single file per year). Filter by symbol after read.
    """
    paths = list((DATA_DIR / "klines").glob("year=*/data.parquet"))
    if not paths:
        raise FileNotFoundError(f"No klines found at {DATA_DIR / 'klines'}")
    df = pl.concat([pl.read_parquet(str(p)) for p in paths])
    return df.filter(pl.col("symbol") == SYMBOL)


def _load_funding() -> pl.DataFrame:
    """Load funding for BTC-USDT (perp_usdt) from L1 archive."""
    paths = list((DATA_DIR / "funding").glob("year=*/data.parquet"))
    if not paths:
        raise FileNotFoundError(f"No funding found at {DATA_DIR / 'funding'}")
    df = pl.concat([pl.read_parquet(str(p)) for p in paths])
    return df.filter(pl.col("symbol") == SYMBOL)


def _load_btc_returns(klines: pl.DataFrame) -> pl.DataFrame:
    """Derive BTC-USDT spot hourly returns for regime classification."""
    spot = (
        klines.filter(
            (pl.col("symbol") == SYMBOL)
            & (pl.col("market") == "spot"),
        )
        .sort("open_time")
        .select(["open_time", "close"])
    )
    return spot.with_columns(
        time=pl.col("open_time"),
        returns=pl.col("close").pct_change().fill_null(0.0),
    ).select(["time", "returns"])


# ── Metrics aggregation ──────────────────────────────────────────────────


def _annualize_sharpe(returns: pl.Series, periods_per_year: float) -> float:
    """Sharpe with Lo (2002) annualization; ignore NaN."""
    arr = returns.drop_nulls().to_numpy()
    if len(arr) < 2 or arr.std() == 0:
        return 0.0
    return float(arr.mean() / arr.std() * (periods_per_year**0.5))


def _slice_window(curve: pl.DataFrame, start: datetime, end: datetime) -> pl.DataFrame:
    return curve.filter(
        (pl.col("time") >= start) & (pl.col("time") <= end),
    )


def _summarize(label: str, report) -> dict:
    """Extract comparison metrics from a RunReport."""
    curve = report.artifacts.equity_curve.sort("time")
    returns = curve["period_ret_net"]
    # Hourly bars => 24*365 = 8760 periods/year
    pyear = 24 * 365.0

    full_sharpe = _annualize_sharpe(returns, pyear)

    # Last-12m window
    end_t = curve["time"].max()
    start_12m = end_t - timedelta(days=365)
    win_12m = _slice_window(curve, start_12m, end_t)
    last_12m_sharpe = _annualize_sharpe(win_12m["period_ret_net"], pyear)

    # Last-3m window
    start_3m = end_t - timedelta(days=90)
    win_3m = _slice_window(curve, start_3m, end_t)
    last_3m_sharpe = _annualize_sharpe(win_3m["period_ret_net"], pyear)

    # Post-ETF window
    post_etf = _slice_window(curve, ETF_LAUNCH, end_t)
    post_etf_sharpe = _annualize_sharpe(post_etf["period_ret_net"], pyear)

    # Active fraction (legs spot-long weight > 0)
    legs = report.artifacts.legs
    spot = legs.filter(pl.col("leg_id") == 0)
    active_frac = float((spot["weight"] > 0).mean())

    # Final equity, max DD
    eq = curve["equity"].to_numpy()
    final_equity = float(eq[-1]) if len(eq) > 0 else 0.0
    if len(eq) > 1:
        running_max = eq.copy()
        for i in range(1, len(eq)):
            if running_max[i] < running_max[i - 1]:
                running_max[i] = running_max[i - 1]
        dd = (eq - running_max) / running_max
        max_dd = float(dd.min())
    else:
        max_dd = 0.0

    return {
        "label": label,
        "n_obs": int(curve.height),
        "full_sharpe": full_sharpe,
        "last_12m_sharpe": last_12m_sharpe,
        "last_3m_sharpe": last_3m_sharpe,
        "post_etf_sharpe": post_etf_sharpe,
        "max_dd": max_dd,
        "active_frac": active_frac,
        "n_transitions": int(report.artifacts.n_transitions),
        "final_equity": final_equity,
        "verdict": report.verdict,
        "dsr_lo": report.metrics.get("dsr_ci_lower", float("nan")),
        "dsr_point": report.metrics.get("dsr", float("nan")),
    }


def _print_table(rows: list[dict]) -> None:
    """ASCII-only side-by-side table — Windows cp950 safe."""
    keys = [
        ("label", "strategy"),
        ("n_obs", "n_obs"),
        ("full_sharpe", "Sharpe(full)"),
        ("last_12m_sharpe", "Sharpe(12m)"),
        ("last_3m_sharpe", "Sharpe(3m)"),
        ("post_etf_sharpe", "Sharpe(postETF)"),
        ("max_dd", "max_dd"),
        ("active_frac", "active_frac"),
        ("n_transitions", "n_trans"),
        ("final_equity", "final_eq"),
        ("verdict", "verdict"),
        ("dsr_point", "DSR"),
        ("dsr_lo", "DSR_lo"),
    ]
    print("=" * 80)
    for k, hdr in keys:
        v2 = rows[0][k]
        v3 = rows[1][k]
        if isinstance(v2, float):
            v2_s = f"{v2:>12.4f}"
            v3_s = f"{v3:>12.4f}"
        else:
            v2_s = f"{v2!s:>12}"
            v3_s = f"{v3!s:>12}"
        print(f"{hdr:<20} | {v2_s} | {v3_s}")
    print("=" * 80)


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    log.info("loading L1 archive...")
    klines = _load_klines()
    funding = _load_funding()
    btc_returns = _load_btc_returns(klines)
    log.info(
        "loaded klines=%d funding=%d returns=%d",
        klines.height, funding.height, btc_returns.height,
    )

    # Per-symbol daily volume (BTC: pull from spot recent 30d avg)
    spot_vol = (
        klines.filter(
            (pl.col("symbol") == SYMBOL)
            & (pl.col("market") == "spot"),
        )
        .sort("open_time", descending=True)
        .head(30 * 24)
        ["quote_volume"]
        .mean()
    )
    daily_vol = float(spot_vol or 0) * 24
    log.info("BTC-USDT estimated 30d daily quote volume = %.0f USDT", daily_vol)

    # ── V2 run ─────────────────────────────────────────────────────────
    log.info("running carry V2...")
    v2_params = CarryParams()
    v2_report = run_carry_validation(
        SYMBOL,
        "v3-compare-v2",
        v2_params,
        klines_df=klines,
        funding_df=funding,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={SYMBOL: daily_vol},
        strategy_id=V2_STRATEGY_ID,
        backtest_fn=run_carry_backtest,
    )

    # ── V3 run ─────────────────────────────────────────────────────────
    log.info("running carry V3...")
    v3_params = CarryV3Params()
    v3_report = run_carry_validation(
        SYMBOL,
        "v3-compare-v3-default-thresholds",
        v3_params,
        klines_df=klines,
        funding_df=funding,
        btc_returns_df=btc_returns,
        daily_volume_per_symbol={SYMBOL: daily_vol},
        strategy_id=V3_STRATEGY_ID,
        backtest_fn=run_carry_v3_backtest,
    )

    # ── Compare ────────────────────────────────────────────────────────
    rows = [
        _summarize("V2", v2_report),
        _summarize("V3", v3_report),
    ]
    _print_table(rows)

    # ── Verdict on V3 thesis ───────────────────────────────────────────
    print()
    print("V3 THESIS CHECK:")
    delta_post_etf = rows[1]["post_etf_sharpe"] - rows[0]["post_etf_sharpe"]
    delta_full = rows[1]["full_sharpe"] - rows[0]["full_sharpe"]
    delta_12m = rows[1]["last_12m_sharpe"] - rows[0]["last_12m_sharpe"]
    delta_3m = rows[1]["last_3m_sharpe"] - rows[0]["last_3m_sharpe"]

    print(f"  Sharpe(full):    V3 - V2 = {delta_full:+.3f}")
    print(f"  Sharpe(12m):     V3 - V2 = {delta_12m:+.3f}")
    print(f"  Sharpe(3m):      V3 - V2 = {delta_3m:+.3f}")
    print(f"  Sharpe(postETF): V3 - V2 = {delta_post_etf:+.3f}")
    print(f"  active_frac:     V3 - V2 = {rows[1]['active_frac'] - rows[0]['active_frac']:+.3f}")
    print(f"  n_transitions:   V3 = {rows[1]['n_transitions']}, V2 = {rows[0]['n_transitions']}")
    print()
    if delta_post_etf > 0.1:
        print(f"  VERDICT: V3 IMPROVES post-ETF Sharpe by {delta_post_etf:.3f}.")
    elif delta_post_etf > 0:
        print(f"  VERDICT: V3 marginally improves post-ETF (delta={delta_post_etf:.3f}).")
    else:
        print(f"  VERDICT: V3 does NOT improve post-ETF (delta={delta_post_etf:.3f}).")

    if delta_3m > 0.5:
        print(f"  V3 also fixes the last-3m bleed: delta_3m = {delta_3m:+.3f}.")
    elif delta_3m > 0:
        print(f"  V3 marginally helps recent-3m: delta_3m = {delta_3m:+.3f}.")


if __name__ == "__main__":
    main()
