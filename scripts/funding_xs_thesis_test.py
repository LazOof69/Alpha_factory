"""Thesis test — does funding_xs have alpha BEYOND cross-sectional momentum?

Per adversarial-debate skill Phase 3 attack #2: the funding signal might
be a noisy mirror of short-term cross-sectional momentum mean-reversion.
If funding_xs returns are subsumed by momentum_xs in regression, the
strategy's "funding inefficiency" thesis is the wrong angle and the
implementation is mis-targeted (we'd be better off building a direct
momentum-XS alpha).

DECISION RULE:
  PROCEED:  residual alpha >= 0.5 Sharpe AND R-squared (vs BTC + momentum) < 0.5
  KILL:     residual alpha < 0.3 Sharpe OR R-squared > 0.7
  AMBIGUOUS: otherwise — surface to user

DATA:
  L1 archive at data/{klines,funding}/year=YYYY/data.parquet for the
  14 curated perps (BTC ETH SOL BNB XRP DOGE ADA AVAX LINK DOT UNI LTC
  ATOM TRX). Weekly bars (Monday 00:00 UTC anchor).

CONSTRUCTION:
  Both strategies share infrastructure:
    - Universe: same 14 perps, same N>=10 min, same eligibility filter
    - Cadence: weekly rebal (every 21 settlements; Monday 00:00 UTC)
    - Quantile: 30/30 (long bottom 30%, short top 30%, middle 40% no pos)
    - Equal-weight within basket
    - Perp-only legs (no spot)
    - GROSS returns (no fees in this test — both strategies pay equal
      transaction cost so it cancels in the regression test)

  Signal differs:
    funding_xs: smoothed_signal[s, t] = mean(funding_rate[s, t-14:t-1])
                                       - median(funding_rate[s, t-270:t-1])
                LONG = bottom 30% by signal (lowest = most negative funding)
                SHORT = top 30% by signal (highest = most positive funding)
    momentum_xs: signal[s, t] = -mean(weekly_return[s, t-2:t-1])
                LONG = bottom 30% by signal (i.e. negated mean = top losers)
                SHORT = top 30% by signal (top winners)
                (i.e. classic short-term cross-sectional reversal)

USAGE:
    uv run python scripts/funding_xs_thesis_test.py
"""
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
SETTLEMENTS_PER_DAY = 3   # 8h cadence on Binance perp
SETTLEMENTS_PER_WEEK = 21
DEMEAN_LOOKBACK = 270     # 90d
SIGNAL_LOOKBACK_FUNDING = 14
MOMENTUM_LOOKBACK_WEEKS = 2
MIN_UNIVERSE_SIZE = 10
QUANTILE = 0.30
WEEKS_PER_YEAR = 52.0

# Narrow universe (option B from 2026-05-03; classical alts only)
NARROW_UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "DOT-USDT",
    "UNI-USDT", "LTC-USDT", "ATOM-USDT", "TRX-USDT",
]

# Wide universe (post adversarial-debate corrective; adds 6 high-dispersion
# meme/AI tokens listed >= 1 yr; the source of the funding_xs alpha thesis
# that the narrow universe specifically excluded).
WIDE_UNIVERSE = NARROW_UNIVERSE + [
    "ORDI-USDT", "1000LUNC-USDT", "1000PEPE-USDT",
    "TAO-USDT", "HYPE-USDT", "BIO-USDT",
]


# ── Data loading ─────────────────────────────────────────────────────────


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


# ── Build per-symbol weekly bars ─────────────────────────────────────────


def _build_per_symbol_panel(
    klines: pl.DataFrame, funding: pl.DataFrame, universe: list[str],
) -> pl.DataFrame:
    """Return long-format weekly panel (week_start, symbol, weekly_ret,
    weekly_funding_avg, n_funding_in_week).

    weekly_ret = perp close-to-close return over Monday-to-Monday week.
    weekly_funding_avg = mean of all funding settlements in the week.
    """
    perp = (
        klines.filter(
            (pl.col("symbol").is_in(universe))
            & (pl.col("market") == "perp_usdt"),
        )
        .select(["symbol", "open_time", "close"])
        .sort(["symbol", "open_time"])
    )

    # Anchor weeks: Monday 00:00 UTC. Truncate open_time to week start.
    perp = perp.with_columns(
        week_start=pl.col("open_time").dt.truncate("1w"),  # ISO week (Mon)
    )

    # Per-symbol per-week: take last close in week / first close in week - 1
    weekly = (
        perp.group_by(["symbol", "week_start"])
        .agg(
            close_first=pl.col("close").first(),
            close_last=pl.col("close").last(),
        )
        .with_columns(
            weekly_ret=(pl.col("close_last") / pl.col("close_first")) - 1.0,
        )
        .sort(["symbol", "week_start"])
    )

    # Funding panel
    fund = (
        funding.filter(pl.col("symbol").is_in(universe))
        .select(["symbol", "funding_time", "funding_rate"])
        .with_columns(
            week_start=pl.col("funding_time").dt.truncate("1w"),
        )
        .group_by(["symbol", "week_start"])
        .agg(
            weekly_funding_sum=pl.col("funding_rate").sum(),
            weekly_funding_avg=pl.col("funding_rate").mean(),
            n_funding_in_week=pl.col("funding_rate").len(),
        )
    )

    panel = weekly.join(
        fund, on=["symbol", "week_start"], how="left",
    ).fill_null(strategy="zero")
    return panel.sort(["symbol", "week_start"])


# ── Signal construction ─────────────────────────────────────────────────


def _add_funding_signal(panel: pl.DataFrame) -> pl.DataFrame:
    """Add funding_signal column.

    Per spec: smoothed_signal[s, t] = mean(funding_rate[s, t-14d : t-1])
                                      - median(funding_rate[s, t-90d : t-1])

    We approximate at the WEEKLY bar level using:
      mean component: 2-week rolling mean of weekly_funding_avg, shift(1)
      demean component: 13-week rolling median of weekly_funding_avg, shift(1)

    (~14 settlements ≈ 2 weeks; 270 settlements = 90d ≈ 13 weeks.)

    LONG = bottom 30% (most negative signal = most over-shorted candidate)
    SHORT = top 30% (most positive signal = most over-longed candidate)
    """
    # Use shift(1) explicitly so signal at t uses week-(t-1) and earlier
    panel = panel.with_columns(
        funding_signal=(
            pl.col("weekly_funding_avg").shift(1).rolling_mean(window_size=2)
            - pl.col("weekly_funding_avg").shift(1).rolling_median(window_size=13)
        ).over("symbol"),
    )
    return panel


def _add_momentum_signal(panel: pl.DataFrame) -> pl.DataFrame:
    """Add momentum_signal column.

    momentum_signal[s, t] = -mean(weekly_ret[s, t-2 : t-1])
    LONG = bottom 30% (most negative = recent losers, mean-revert candidates)
    SHORT = top 30% (most positive = recent winners, mean-revert short)
    """
    panel = panel.with_columns(
        momentum_signal=(
            -pl.col("weekly_ret").shift(1).rolling_mean(window_size=MOMENTUM_LOOKBACK_WEEKS)
        ).over("symbol"),
    )
    return panel


# ── Strategy returns ────────────────────────────────────────────────────


def _compute_strategy_returns(
    panel: pl.DataFrame, signal_col: str, label: str,
) -> pl.DataFrame:
    """Per-week strategy returns for a given signal column.

    Returns DataFrame with columns: week_start, strategy_ret, n_universe.

    Strategy:
      At each week_start (rebal bar):
        eligible = symbols with non-null signal AND weekly_ret available
                   AND >= 13 weeks of prior funding history (for warmup).
        if |eligible| < min_universe: strategy_ret = 0 for that week.
        else:
          rank by signal ascending (most negative first)
          long_basket = bottom 30%  → +1/n_long weight on next-week perp ret
          short_basket = top 30%    → -1/n_short weight on next-week perp ret
          strategy_ret[w] = mean(long_perp_ret[w]) - mean(short_perp_ret[w])
                          (gross of fees; funding income captured in
                           weekly_ret since position holds 1 week)
    """
    out_rows = []
    weeks = panel["week_start"].unique().sort()

    for w in weeks:
        slice_w = panel.filter(pl.col("week_start") == w)
        eligible = slice_w.filter(
            pl.col(signal_col).is_not_null() & pl.col("weekly_ret").is_not_null(),
        )
        if eligible.height < MIN_UNIVERSE_SIZE:
            out_rows.append({
                "week_start": w,
                "strategy_ret": 0.0,
                "n_universe": eligible.height,
            })
            continue

        ranked = eligible.sort(signal_col)
        n = ranked.height
        n_long = max(1, int(n * QUANTILE))
        n_short = max(1, int(n * QUANTILE))

        long_basket = ranked.head(n_long)
        short_basket = ranked.tail(n_short)

        long_ret = long_basket["weekly_ret"].mean()
        short_ret = short_basket["weekly_ret"].mean()
        # Funding cash flow per leg per week:
        #   long position: -weekly_funding_sum (long pays funding when rate>0)
        #   short position: +weekly_funding_sum (short receives funding when rate>0)
        # Strategy goes long long_basket, short short_basket.
        long_funding = long_basket["weekly_funding_sum"].mean()
        short_funding = short_basket["weekly_funding_sum"].mean()

        # Per-leg PnL = price_change + funding_cash_flow
        # Long-leg (LONG basket) per-coin PnL = +1 * weekly_ret - weekly_funding_sum
        # Short-leg (SHORT basket) per-coin PnL = -1 * weekly_ret + weekly_funding_sum
        # Strategy gross = 0.5*(long_PnL_avg + short_PnL_avg)
        price_term = 0.5 * (long_ret - short_ret)
        funding_term = 0.5 * (short_funding - long_funding)
        strategy_ret = price_term + funding_term

        out_rows.append({
            "week_start": w,
            "strategy_ret": strategy_ret,
            "price_term": price_term,
            "funding_term": funding_term,
            "n_universe": eligible.height,
        })

    df = pl.DataFrame(out_rows)
    log.info(
        "  %s: %d weeks, %d active (n>=%d), mean_ret=%.4f%%",
        label, df.height,
        df.filter(pl.col("n_universe") >= MIN_UNIVERSE_SIZE).height,
        MIN_UNIVERSE_SIZE,
        df["strategy_ret"].mean() * 100,
    )
    return df


# ── BTC return for regression control ───────────────────────────────────


def _build_btc_weekly(panel: pl.DataFrame) -> pl.DataFrame:
    btc = (
        panel.filter(pl.col("symbol") == "BTC-USDT")
        .select(["week_start", pl.col("weekly_ret").alias("btc_ret")])
        .sort("week_start")
    )
    return btc


# ── Sharpe + regression ─────────────────────────────────────────────────


def _annualized_sharpe(returns: np.ndarray) -> float:
    arr = returns[~np.isnan(returns)]
    if len(arr) < 4 or arr.std(ddof=1) == 0:
        return 0.0
    return float(arr.mean() / arr.std(ddof=1) * np.sqrt(WEEKS_PER_YEAR))


def _regression(y: np.ndarray, design: np.ndarray) -> dict:
    """OLS y = design @ beta + e. `design` must include intercept column."""
    mask = ~np.isnan(y) & ~np.any(np.isnan(design), axis=1)
    y_, d_ = y[mask], design[mask]
    n, k = d_.shape
    beta, *_ = np.linalg.lstsq(d_, y_, rcond=None)
    y_hat = d_ @ beta
    resid = y_ - y_hat
    ss_res = (resid**2).sum()
    ss_tot = ((y_ - y_.mean())**2).sum()
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    sigma2 = ss_res / max(n - k, 1)
    cov = sigma2 * np.linalg.inv(d_.T @ d_)
    se = np.sqrt(np.diag(cov))
    t_stats = beta / se
    return {
        "beta": beta,
        "se": se,
        "t_stats": t_stats,
        "r2": float(r2),
        "n_obs": int(n),
        "residuals": resid,
    }


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wide", action="store_true",
        help="Use 20-perp wide universe (post-debate corrective; "
             "default = 14-perp narrow option B)",
    )
    args = parser.parse_args()
    universe = WIDE_UNIVERSE if args.wide else NARROW_UNIVERSE
    universe_label = "WIDE (post-debate)" if args.wide else "NARROW (option B)"

    log.info("loading archive...")
    klines, funding = _load_archive()
    log.info(
        "klines=%d funding=%d (universe = %d perps, %s)",
        klines.height, funding.height, len(universe), universe_label,
    )

    log.info("building per-symbol weekly panel...")
    panel = _build_per_symbol_panel(klines, funding, universe)
    panel = _add_funding_signal(panel)
    panel = _add_momentum_signal(panel)
    log.info(
        "panel rows=%d, week range=[%s, %s]",
        panel.height,
        panel["week_start"].min(),
        panel["week_start"].max(),
    )

    # Restrict to effective backtest start = 2021-Q1
    cutoff = datetime(2021, 1, 1, tzinfo=UTC)
    panel = panel.filter(pl.col("week_start") >= cutoff)
    log.info("after 2021-Q1 cutoff: %d rows", panel.height)

    log.info("computing strategy returns...")
    funding_xs = _compute_strategy_returns(panel, "funding_signal", "funding_xs")
    momentum_xs = _compute_strategy_returns(panel, "momentum_signal", "momentum_xs")
    btc = _build_btc_weekly(panel)

    # Align time series
    joined = (
        funding_xs.rename({"strategy_ret": "fxs_ret"})
        .join(
            momentum_xs.rename({"strategy_ret": "mxs_ret"}).select(
                ["week_start", "mxs_ret"],
            ),
            on="week_start", how="inner",
        )
        .join(btc, on="week_start", how="inner")
        .filter(
            pl.col("fxs_ret").is_not_null()
            & pl.col("mxs_ret").is_not_null()
            & pl.col("btc_ret").is_not_null(),
        )
        .sort("week_start")
    )
    log.info("aligned series: %d weekly bars", joined.height)

    fxs = joined["fxs_ret"].to_numpy()
    mxs = joined["mxs_ret"].to_numpy()
    btc_arr = joined["btc_ret"].to_numpy()

    fxs_sharpe = _annualized_sharpe(fxs)
    mxs_sharpe = _annualized_sharpe(mxs)
    btc_sharpe = _annualized_sharpe(btc_arr)

    # Cross-strategy Pearson correlations
    corr_fxs_mxs = float(np.corrcoef(fxs, mxs)[0, 1])
    corr_fxs_btc = float(np.corrcoef(fxs, btc_arr)[0, 1])
    corr_mxs_btc = float(np.corrcoef(mxs, btc_arr)[0, 1])

    # Run regressions
    intercept = np.ones(len(fxs))
    # Model 1: fxs ~ a + b * BTC
    design1 = np.column_stack([intercept, btc_arr])
    res1 = _regression(fxs, design1)
    alpha1_weekly = res1["beta"][0]
    alpha1_sharpe = (alpha1_weekly / res1["residuals"].std(ddof=1)) * np.sqrt(WEEKS_PER_YEAR)

    # Model 2: fxs ~ a + b1 * BTC + b2 * mxs   (CRITICAL test)
    design2 = np.column_stack([intercept, btc_arr, mxs])
    res2 = _regression(fxs, design2)
    alpha2_weekly = res2["beta"][0]
    alpha2_sharpe = (alpha2_weekly / res2["residuals"].std(ddof=1)) * np.sqrt(WEEKS_PER_YEAR)

    # Print report
    print()
    print("=" * 88)
    print("FUNDING_XS THESIS TEST — Attack #2 from Phase B Step 2 critique")
    print("=" * 88)
    print()
    print(f"Sample: 2021-Q1 onwards, {joined.height} weekly bars (~{joined.height/52:.1f}yr)")
    print(f"Universe: {len(universe)} USDT-M perps ({universe_label})")
    print()
    print("Strategy raw Sharpe (gross of fees):")
    print(f"  funding_xs:  {fxs_sharpe:+.3f}")
    print(f"  momentum_xs: {mxs_sharpe:+.3f}")
    print(f"  BTC perp:    {btc_sharpe:+.3f}  (reference)")
    print()
    print("Pearson correlations:")
    print(f"  corr(funding_xs, momentum_xs) = {corr_fxs_mxs:+.3f}")
    print(f"  corr(funding_xs, BTC)         = {corr_fxs_btc:+.3f}")
    print(f"  corr(momentum_xs, BTC)        = {corr_mxs_btc:+.3f}")
    print()
    print("Regression Model 1: funding_xs ~ alpha + beta_btc * btc_ret")
    print(f"  alpha (weekly):       {res1['beta'][0]:+.6f}  (t={res1['t_stats'][0]:+.2f})")
    print(f"  alpha (annualized Sharpe): {alpha1_sharpe:+.3f}")
    print(f"  beta_btc:             {res1['beta'][1]:+.4f}  (t={res1['t_stats'][1]:+.2f})")
    print(f"  R-squared:                   {res1['r2']:.4f}")
    print()
    print("Regression Model 2: funding_xs ~ alpha + beta_btc * btc + beta_mxs * mxs  [CRITICAL]")
    print(f"  alpha (weekly):       {res2['beta'][0]:+.6f}  (t={res2['t_stats'][0]:+.2f})")
    print(f"  alpha (annualized Sharpe): {alpha2_sharpe:+.3f}")
    print(f"  beta_btc:             {res2['beta'][1]:+.4f}  (t={res2['t_stats'][1]:+.2f})")
    print(f"  beta_momentum_xs:     {res2['beta'][2]:+.4f}  (t={res2['t_stats'][2]:+.2f})")
    print(f"  R-squared (Model 2):         {res2['r2']:.4f}")
    print()
    print("=" * 88)
    print("VERDICT (per pre-registered decision rule):")
    print("=" * 88)

    residual_alpha = alpha2_sharpe
    r2 = res2["r2"]

    if residual_alpha >= 0.5 and r2 < 0.5:
        verdict = "PROCEED — thesis survives momentum control"
    elif residual_alpha < 0.3 or r2 > 0.7:
        verdict = "KILL — thesis subsumed; build momentum_xs directly instead"
    else:
        verdict = "AMBIGUOUS — surface to user, possibly run extended diagnostics"

    print(f"  Residual alpha after BTC + momentum_xs: {residual_alpha:+.3f} Sharpe")
    print(f"  R-squared of (BTC + momentum) on funding_xs:   {r2:.3f}")
    print()
    print(f"  >>> {verdict} <<<")
    print()
    print("Decision rule recap:")
    print("  PROCEED:    residual >= 0.5 Sharpe AND R-squared < 0.5")
    print("  KILL:       residual < 0.3 Sharpe OR R-squared > 0.7")
    print("  AMBIGUOUS:  otherwise")
    print("=" * 88)


if __name__ == "__main__":
    main()
