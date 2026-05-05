"""V3 PBO sweep — Phase B Step 1 follow-up + Phase B.2 cross-symbol.

Sweeps V3 funding-compression detector over a 2-D grid of
(exit_compression_30dma, compression_lookback_settlements):

  exit thresholds: {0.05, 0.1, 0.15, 0.2, 0.3, 0.5} bp/8h
  lookback windows: {60, 90, 120} settlements (= 20d, 30d, 40d)

Total trials: 6 x 3 = 18 (>= 8 required by pbo CSCV).

Reports:
  - Per-trial metrics: full / 12m / 3m / post-ETF Sharpe + max_dd + active_frac
  - PBO across all 18 trials (probability that the IS-best combo would
    underperform OOS — Bailey et al. 2014 CSCV).
  - Robustness: is the Sharpe surface flat (good) or knife-edge (bad)
    over neighbouring threshold values?
  - Best-trial comparison vs V2 baseline.
  - Verdict: does any V3 trial supersede V2 on post-ETF Sharpe?

NOTE on V2 baseline across symbols: V2 defaults were originally
calibrated against BTC funding regimes. When this script is run on
ETH or SOL the V2 numbers are still mechanically defined (same code
path, same params), but the dominance comparison should be read as
"V3 vs symbol-agnostic V2 baseline" rather than "V3 vs symbol-tuned
V2". The PBO and best-trial robustness numbers are symbol-internal
and unaffected.

USAGE:
    uv run python scripts/v3_pbo_sweep.py                  # BTC default
    uv run python scripts/v3_pbo_sweep.py --symbol ETH-USDT
    uv run python scripts/v3_pbo_sweep.py --symbol SOL-USDT
"""
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import numpy as np
import polars as pl

from alpha_factory.alpha.carry import CarryParams, run_carry_backtest
from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
    run_carry_v3_backtest,
)
from alpha_factory.validation.contracts import SALT_PBO_PARTITION, make_rng
from alpha_factory.validation.pbo import pbo as compute_pbo

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DEFAULT_SYMBOL = "BTC-USDT"
DATA_DIR = Path("data")
ETF_LAUNCH = datetime(2024, 1, 11, tzinfo=UTC)


# ── Threshold grid ───────────────────────────────────────────────────────


# Exit thresholds in DECIMAL form (1 bp/8h = 1e-4)
EXIT_THRESHOLDS = [
    0.000005,   # 0.05 bp
    0.00001,    # 0.10 bp
    0.000015,   # 0.15 bp
    0.00002,    # 0.20 bp
    0.00003,    # 0.30 bp (V3 default)
    0.00005,    # 0.50 bp (Phase A audit suggestion)
]

# Compression lookback in settlements (3/day on Binance perp)
LOOKBACK_SETTLEMENTS = [60, 90, 120]   # 20d, 30d, 40d

# Re-entry threshold = 2x exit (matches V3's 0.3 / 0.6 bp default ratio).
REENTRY_RATIO = 2.0


# ── Archive loading ──────────────────────────────────────────────────────


def _load_klines(symbol: str) -> pl.DataFrame:
    paths = list((DATA_DIR / "klines").glob("year=*/data.parquet"))
    return pl.concat([pl.read_parquet(str(p)) for p in paths]).filter(
        pl.col("symbol") == symbol,
    )


def _load_funding(symbol: str) -> pl.DataFrame:
    paths = list((DATA_DIR / "funding").glob("year=*/data.parquet"))
    return pl.concat([pl.read_parquet(str(p)) for p in paths]).filter(
        pl.col("symbol") == symbol,
    )


# ── Metrics ──────────────────────────────────────────────────────────────


def _sharpe(returns: np.ndarray, periods_per_year: float) -> float:
    if len(returns) < 2 or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * (periods_per_year**0.5))


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) < 2:
        return 0.0
    rmax = np.maximum.accumulate(equity)
    return float(((equity - rmax) / rmax).min())


def _summarize(label: str, art) -> dict:
    """Extract metrics from a CarryArtifacts; align windows to ETF + recent."""
    curve = art.equity_curve.sort("time")
    times = curve["time"].to_numpy()
    rets = curve["period_ret_net"].to_numpy()
    eq = curve["equity"].to_numpy()
    pyear = 24 * 365.0  # hourly bars

    end_t = times[-1] if len(times) > 0 else None
    start_12m = end_t - np.timedelta64(365, "D") if end_t is not None else None
    start_3m = end_t - np.timedelta64(90, "D") if end_t is not None else None
    etf = np.datetime64(ETF_LAUNCH.replace(tzinfo=None))

    def _win(rets_, mask):
        return rets_[mask] if mask.any() else np.array([])

    full_sharpe = _sharpe(rets, pyear)
    sharpe_12m = (
        _sharpe(_win(rets, times >= start_12m), pyear)
        if start_12m is not None else 0.0
    )
    sharpe_3m = (
        _sharpe(_win(rets, times >= start_3m), pyear)
        if start_3m is not None else 0.0
    )
    sharpe_post_etf = _sharpe(_win(rets, times >= etf), pyear)

    legs = art.legs
    spot = legs.filter(pl.col("leg_id") == 0)
    active_frac = float((spot["weight"] > 0).mean())

    return {
        "label": label,
        "n_obs": int(curve.height),
        "full_sharpe": full_sharpe,
        "sharpe_12m": sharpe_12m,
        "sharpe_3m": sharpe_3m,
        "sharpe_post_etf": sharpe_post_etf,
        "max_dd": _max_dd(eq),
        "active_frac": active_frac,
        "n_transitions": int(art.n_transitions),
        "final_equity": float(eq[-1]) if len(eq) > 0 else 0.0,
        "_returns": rets,  # for PBO matrix; underscored = not for table
    }


# ── Sweep ────────────────────────────────────────────────────────────────


def main(symbol: str = DEFAULT_SYMBOL) -> None:
    log.info("=" * 60)
    log.info("V3 PBO sweep on %s", symbol)
    log.info("=" * 60)
    log.info("loading L1 archive...")
    klines = _load_klines(symbol)
    funding = _load_funding(symbol)
    log.info("klines=%d funding=%d", klines.height, funding.height)
    if klines.height == 0 or funding.height == 0:
        raise RuntimeError(
            f"empty archive for {symbol}: klines={klines.height} "
            f"funding={funding.height}",
        )

    # V2 baseline (NOTE: defaults are BTC-tuned; on ETH/SOL use as
    # symbol-agnostic floor, not as symbol-fitted competitor)
    log.info("running V2 baseline (BTC-tuned defaults)...")
    v2_art = run_carry_backtest(
        symbol, f"v2-baseline-{symbol}", CarryParams(),
        klines_df=klines, funding_df=funding,
    )
    v2 = _summarize("V2", v2_art)

    # V3 sweep: 6 thresholds x 3 lookbacks = 18 trials
    grid = list(product(EXIT_THRESHOLDS, LOOKBACK_SETTLEMENTS))
    log.info("running V3 sweep: %d trials on %s...", len(grid), symbol)

    trials = []
    for exit_thr, lookback in grid:
        params = CarryV3Params(
            exit_compression_30dma=exit_thr,
            reentry_compression_30dma=exit_thr * REENTRY_RATIO,
            compression_lookback_settlements=lookback,
        )
        art = run_carry_v3_backtest(
            symbol, f"sweep_{symbol}_{exit_thr:.6f}_{lookback}", params,
            klines_df=klines, funding_df=funding,
        )
        s = _summarize(f"V3-{exit_thr * 1e4:.2f}bp/{lookback}d", art)
        s["exit_bp"] = exit_thr * 1e4
        s["lookback"] = lookback
        trials.append(s)
        log.info(
            "  exit=%.2f bp lookback=%d  full=%.3f  12m=%.3f  postETF=%.3f  active=%.2f",
            s["exit_bp"], lookback, s["full_sharpe"], s["sharpe_12m"],
            s["sharpe_post_etf"], s["active_frac"],
        )

    # ── Build returns matrix for PBO ───────────────────────────────────
    # All trials share the same time axis (same input data). Stack into
    # T x N matrix. T = bars, N = 18 trials.
    n_obs = len(trials[0]["_returns"])
    if not all(len(t["_returns"]) == n_obs for t in trials):
        raise RuntimeError("trials returned different-length return series")
    returns_matrix = np.column_stack([t["_returns"] for t in trials])
    log.info("PBO returns matrix shape: %s", returns_matrix.shape)

    rng = make_rng(f"v3_pbo_sweep_{symbol}_2026_05_04", SALT_PBO_PARTITION)
    pbo_value = compute_pbo(returns_matrix, n_partitions="auto", rng=rng)
    log.info("PBO = %.4f (>= 0.5 means likely overfit)", pbo_value)

    # ── Print sweep table ──────────────────────────────────────────────
    print()
    print("=" * 110)
    print(f"V3 PBO SWEEP RESULTS — symbol={symbol}")
    print("=" * 110)
    print(
        f"{'exit_bp':>8} | {'lkbk':>4} | {'full_S':>7} | {'12m_S':>7} | "
        f"{'3m_S':>7} | {'pETF_S':>8} | {'maxDD':>7} | {'active':>6} | "
        f"{'#trans':>6}",
    )
    print("=" * 110)
    print(
        f"{'V2-base':>8} | {'-':>4} | {v2['full_sharpe']:>7.3f} | "
        f"{v2['sharpe_12m']:>7.3f} | {v2['sharpe_3m']:>7.3f} | "
        f"{v2['sharpe_post_etf']:>8.3f} | {v2['max_dd']:>7.4f} | "
        f"{v2['active_frac']:>6.2f} | {v2['n_transitions']:>6d}",
    )
    print("-" * 110)
    for t in trials:
        print(
            f"{t['exit_bp']:>8.2f} | {t['lookback']:>4d} | "
            f"{t['full_sharpe']:>7.3f} | {t['sharpe_12m']:>7.3f} | "
            f"{t['sharpe_3m']:>7.3f} | {t['sharpe_post_etf']:>8.3f} | "
            f"{t['max_dd']:>7.4f} | {t['active_frac']:>6.2f} | "
            f"{t['n_transitions']:>6d}",
        )
    print("=" * 110)
    print()
    print(f"PBO = {pbo_value:.4f}  (gate: < 0.5 to pass)")
    print()

    # Score each trial by # of windows where it ties-or-beats V2 within
    # noise tolerance of 0.05 Sharpe.
    noise = 0.05

    def _dominance_count(t):
        return sum([
            t["full_sharpe"] >= v2["full_sharpe"] - noise,
            t["sharpe_12m"] >= v2["sharpe_12m"] - noise,
            t["sharpe_3m"] >= v2["sharpe_3m"] - noise,
            t["sharpe_post_etf"] >= v2["sharpe_post_etf"] - noise,
        ])

    ranked = sorted(
        trials,
        key=lambda t: (_dominance_count(t), t["sharpe_post_etf"]),
        reverse=True,
    )
    best = ranked[0]
    n_dom = _dominance_count(best)
    full_d = best["full_sharpe"] - v2["full_sharpe"]
    s12_d = best["sharpe_12m"] - v2["sharpe_12m"]
    s3_d = best["sharpe_3m"] - v2["sharpe_3m"]
    petf_d = best["sharpe_post_etf"] - v2["sharpe_post_etf"]
    print(f"BEST V3 TRIAL: exit={best['exit_bp']:.2f} bp  lookback={best['lookback']}d")
    print(f"  ties-or-beats V2 in {n_dom}/4 windows (noise={noise})")
    print(f"  full = {best['full_sharpe']:.3f}  delta vs V2 = {full_d:+.3f}")
    print(f"  12m  = {best['sharpe_12m']:.3f}  delta vs V2 = {s12_d:+.3f}")
    print(f"  3m   = {best['sharpe_3m']:.3f}  delta vs V2 = {s3_d:+.3f}")
    print(f"  pETF = {best['sharpe_post_etf']:.3f}  delta vs V2 = {petf_d:+.3f}")
    print(f"  active_frac = {best['active_frac']:.2f}")
    print(f"  n_transitions = {best['n_transitions']}")

    # Show top 5 by dominance count
    print()
    print("TOP 5 BY DOMINANCE-COUNT (then post-ETF tie-break):")
    for i, t in enumerate(ranked[:5]):
        n_d = _dominance_count(t)
        print(
            f"  #{i+1}: exit={t['exit_bp']:.2f} bp lookback={t['lookback']}d  "
            f"dom={n_d}/4  full={t['full_sharpe']:.3f}  12m={t['sharpe_12m']:.3f}  "
            f"3m={t['sharpe_3m']:.3f}  pETF={t['sharpe_post_etf']:.3f}",
        )

    # ── Robustness check ───────────────────────────────────────────────
    # Group by lookback and check Sharpe variation across thresholds.
    print()
    print("ROBUSTNESS — post-ETF Sharpe variation across thresholds at each lookback:")
    for lkbk in LOOKBACK_SETTLEMENTS:
        rows_at_lkbk = sorted(
            (t for t in trials if t["lookback"] == lkbk),
            key=lambda t: t["exit_bp"],
        )
        sharpe_vals = [t["sharpe_post_etf"] for t in rows_at_lkbk]
        gap = max(sharpe_vals) - min(sharpe_vals)
        flat = "FLAT" if gap < 1.0 else "KNIFE-EDGE" if gap > 2.0 else "MODERATE"
        print(
            f"  lookback={lkbk}d  Sharpe range = "
            f"[{min(sharpe_vals):.2f}, {max(sharpe_vals):.2f}]  gap={gap:.2f}  ({flat})",
        )

    # ── Verdict ────────────────────────────────────────────────────────
    print()
    print("=" * 110)
    print("VERDICT:")
    print(f"  PBO = {pbo_value:.3f}  (gate < 0.5: {'PASS' if pbo_value < 0.5 else 'FAIL'})")
    if n_dom == 4:
        print("  V3 best trial TIES-OR-BEATS V2 in ALL 4 windows.")
        print(f"  RECOMMENDATION: update CarryV3Params defaults to "
              f"(exit={best['exit_bp']:.2f} bp, lookback={best['lookback']}d) "
              f"and SUPERSEDE V2 in validated_alphas.yaml.")
    elif n_dom == 3:
        print("  V3 best trial ties-or-beats V2 in 3/4 windows.")
        print("  RECOMMENDATION: update defaults; mark as supersede CANDIDATE "
              "(paper-trade gate 1 month before final supersede).")
    elif n_dom == 2:
        print("  V3 best trial ties-or-beats V2 in only 2/4 windows.")
        print("  RECOMMENDATION: V3 stays pass-with-caveats; do NOT supersede.")
    else:
        print("  V3 best trial loses on >= 3 windows.")
        print("  RECOMMENDATION: V3 stays pass-with-caveats; investigate further.")
    print("=" * 110)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help=f"Trading symbol (default: {DEFAULT_SYMBOL})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(symbol=args.symbol)
