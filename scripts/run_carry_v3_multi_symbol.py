"""Run carry V3 on multiple symbols (BTC, ETH, SOL) — option (c) from
3rd-debate verdict on 2026-05-04.

The 1-alpha portfolio red-line concern is resolved by running carry_v3
as a FAMILY of symbol-specific instances rather than a single asset.
Each instance shares mechanism (long spot + short perp delta-neutral
with funding-compression detector) but differs in:
  - underlying funding-rate dynamics (BTC ~0.5 bp/8h base, ETH ~1 bp,
    SOL ~3 bp historically)
  - vol regime (BTC stable, SOL volatile)
  - correlation to BTC directional (BTC=1.0, ETH~0.85, SOL~0.75)
  - size-factor exposure (mega / large / large-mid)

Each instance is registered through `runner.run_carry_validation`
(L3 spine) under a distinct strategy_id matching validated_alphas.yaml:

  BTC-USDT -> "carry_v3"       (head-of-family)
  ETH-USDT -> "carry_v3-eth"
  SOL-USDT -> "carry_v3-sol"

Distinct strategy_ids naturally separate the per-strategy TRIAL_LOG
parquet files, so multi-symbol same-params runs do not collide on the
(strategy_id, params_hash, data_version, split) idempotency key. A
prior version of this script bypassed the runner; the family-naming
convention above makes that bypass unnecessary.

Each invocation appends a fresh RUN_REGISTRY row per strategy_id;
`list_validated_alphas` returns the latest row per strategy_id, so a
re-run supersedes prior canonical entries for the same family member.

USAGE:
    # Run from a CWD where the L1 archive lives at ./data/
    # (typically the main Alpha_factory directory).
    uv run python scripts/run_carry_v3_multi_symbol.py
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.alpha.carry_v3 import (
    CarryV3Params,
    run_carry_v3_backtest,
)
from alpha_factory.runner import run_carry_validation

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
ETF_LAUNCH = datetime(2024, 1, 11, tzinfo=UTC)
SYMBOLS = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]

# strategy_id naming follows validated_alphas.yaml family convention:
# BTC is the head-of-family with no symbol suffix.
SYMBOL_TO_STRATEGY_ID = {
    "BTC-USDT": "carry_v3",
    "ETH-USDT": "carry_v3-eth",
    "SOL-USDT": "carry_v3-sol",
}


def _load_archive() -> tuple[pl.DataFrame, pl.DataFrame]:
    klines = pl.concat([
        pl.read_parquet(str(p))
        for p in (DATA_DIR / "klines").glob("year=*/data.parquet")
    ])
    funding = pl.concat([
        pl.read_parquet(str(p))
        for p in (DATA_DIR / "funding").glob("year=*/data.parquet")
    ])
    return klines, funding


def _btc_returns_for_regime(klines: pl.DataFrame) -> pl.DataFrame:
    spot = (
        klines.filter(
            (pl.col("symbol") == "BTC-USDT")
            & (pl.col("market") == "spot"),
        )
        .sort("open_time")
        .select(["open_time", "close"])
    )
    return spot.with_columns(
        time=pl.col("open_time"),
        returns=pl.col("close").pct_change().fill_null(0.0),
    ).select(["time", "returns"])


def _annualize_sharpe(rets, periods_per_year: float = 24 * 365.0) -> float:
    arr = rets.drop_nulls().to_numpy()
    if len(arr) < 2 or arr.std() == 0:
        return 0.0
    return float(arr.mean() / arr.std() * (periods_per_year**0.5))


def _summarize(label: str, art) -> dict:
    curve = art.equity_curve.sort("time")
    rets = curve["period_ret_net"]
    eq = curve["equity"].to_numpy()

    full_sharpe = _annualize_sharpe(rets)

    if curve.height > 0:
        end_t_dt = curve["time"].max()
        cutoff_12m = end_t_dt - timedelta(days=365)
        win_12m = curve.filter(pl.col("time") >= cutoff_12m)
        sharpe_12m = _annualize_sharpe(win_12m["period_ret_net"])
    else:
        sharpe_12m = 0.0

    win_post_etf = curve.filter(pl.col("time") >= ETF_LAUNCH)
    sharpe_post_etf = _annualize_sharpe(win_post_etf["period_ret_net"])

    legs = art.legs
    spot = legs.filter(pl.col("leg_id") == 0)
    active_frac = float((spot["weight"] > 0).mean())

    if len(eq) > 1:
        rmax = eq.copy()
        for i in range(1, len(eq)):
            if rmax[i] < rmax[i - 1]:
                rmax[i] = rmax[i - 1]
        max_dd = float(((eq - rmax) / rmax).min())
    else:
        max_dd = 0.0

    return {
        "label": label,
        "n_obs": int(curve.height),
        "full_sharpe": full_sharpe,
        "sharpe_12m": sharpe_12m,
        "sharpe_post_etf": sharpe_post_etf,
        "max_dd": max_dd,
        "active_frac": active_frac,
        "n_transitions": int(art.n_transitions),
        "final_equity": float(eq[-1]) if len(eq) > 0 else 0.0,
    }


def main() -> None:
    log.info("loading archive...")
    klines, funding = _load_archive()
    log.info(
        "klines=%d funding=%d", klines.height, funding.height,
    )

    # BTC returns power the regime classifier across ALL 3 instances
    # (regime is BTC-led, not strategy-specific). Compute once, share.
    btc_returns = _btc_returns_for_regime(klines)

    rows: list[dict] = []
    reports: list[tuple[str, str, str, str]] = []  # symbol, strategy_id, run_id, verdict
    for symbol in SYMBOLS:
        strategy_id = SYMBOL_TO_STRATEGY_ID[symbol]
        log.info(
            "running carry V3 on %s (strategy_id=%s) through runner ...",
            symbol, strategy_id,
        )
        params = CarryV3Params()
        kf = klines.filter(pl.col("symbol") == symbol)
        ff = funding.filter(pl.col("symbol") == symbol)

        # `daily_volume_per_symbol` here is a placeholder; the runner
        # uses it only for the advisory `capacity_estimate_usd` metric
        # (see runner.py CAPACITY ESTIMATE LIMITATION). For accurate
        # capacity numbers, re-pipe from L1 universe snapshot.
        report = run_carry_validation(
            symbol,
            f"multisymbol-{symbol}",
            params,
            klines_df=kf,
            funding_df=ff,
            btc_returns_df=btc_returns,
            daily_volume_per_symbol={symbol: 1e9},
            strategy_id=strategy_id,
            backtest_fn=run_carry_v3_backtest,
        )

        s = _summarize(symbol, report.artifacts)
        s["run_id"] = report.run_id
        s["verdict"] = report.verdict
        rows.append(s)
        reports.append((symbol, strategy_id, report.run_id, report.verdict))
        log.info(
            "  %s: run_id=%s verdict=%s full=%.3f 12m=%.3f postETF=%.3f "
            "active=%.2f trans=%d",
            symbol, report.run_id, report.verdict,
            s["full_sharpe"], s["sharpe_12m"], s["sharpe_post_etf"],
            s["active_frac"], s["n_transitions"],
        )

    # Print comparison
    print()
    print("=" * 92)
    print(
        f"{'symbol':>10} | {'full_S':>7} | {'12m_S':>7} | {'pETF_S':>8} | "
        f"{'maxDD':>7} | {'active':>6} | {'#trans':>6} | {'final_eq':>9}",
    )
    print("=" * 92)
    for r in rows:
        print(
            f"{r['label']:>10} | {r['full_sharpe']:>7.3f} | "
            f"{r['sharpe_12m']:>7.3f} | {r['sharpe_post_etf']:>8.3f} | "
            f"{r['max_dd']:>7.4f} | {r['active_frac']:>6.2f} | "
            f"{r['n_transitions']:>6d} | {r['final_equity']:>9.2f}",
        )
    print("=" * 92)
    print()

    # Approximate L3 gate check: full Sharpe >= 0 AND 12m Sharpe >= 0
    # AND post-ETF Sharpe >= 0 AND |maxDD| <= 0.30 (CLAUDE.md red line)
    n_pass = sum(
        1 for r in rows
        if r["full_sharpe"] >= 0 and r["sharpe_12m"] >= 0
        and r["sharpe_post_etf"] >= 0 and r["max_dd"] >= -0.30
    )
    print(f"L3-gate-approx: {n_pass}/{len(rows)} symbols clear all 4 gates.")
    if n_pass == 3:
        print(
            "  >>> All 3 carry V3 instances pass approx gates. "
            "Option (c) viable for portfolio.",
        )
    elif n_pass >= 2:
        print(
            f"  >>> {n_pass} symbols pass. Top-N selection for portfolio; "
            "investigate the failure(s).",
        )
    else:
        print(
            "  >>> Insufficient passes; option (c) not viable on current archive.",
        )

    # Canonical RUN_REGISTRY rows produced by this run.
    # The runner-derived verdict here uses single-run gates; sweep-level
    # PBO / cross-symbol PBO must be checked separately (see
    # scripts/v3_pbo_sweep.py for BTC sweep + cross-symbol harness).
    print()
    print("CANONICAL RUN_IDS (paste into validated_alphas.yaml):")
    print("-" * 78)
    for sym, sid, rid, verdict in reports:
        print(f"  {sid:18s}  ({sym:10s})  run_id = {rid}  verdict = {verdict}")
    print("-" * 78)
    print()


if __name__ == "__main__":
    main()
