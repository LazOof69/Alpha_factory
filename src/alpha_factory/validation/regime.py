"""Market-regime classification + stratified-metrics for L3 validation.

Three public callables:

    classify_market_regime(btc_returns_df, *, vol_window=30, trend_window=200)
        Long-format (time, regime_kind, regime_label) DataFrame for
        joining onto an EQUITY_CURVE. Three kinds:

            vol     low | mid | high   -- rolling-vol terciles
            trend   bull | bear         -- sign of trailing N-bar return
            etf     pre_etf | post_etf  -- static cutoff 2024-01-11

        All time-series classifications use `shift(1)` to ensure the
        regime label at time t was computable at time t-1 (no
        lookahead). The vol terciles are calibrated against the
        full-sample distribution of rolling vol; for live signal use
        callers should re-implement with expanding quantiles instead.

    stratify_metrics(curve, regimes, *, periods_per_year)
        Inner-join curve onto regimes (on `time`); group by
        (regime_kind, regime_label); compute Sharpe / annualized
        return / max-DD / hit-rate per group. Returns long DataFrame
        conforming to REGIME_METRICS_SCHEMA.

    load_btc_returns(start, end, *, freq)
        Helper: load BTC-USDT spot klines from L1, compute per-period
        returns, optionally aggregate to daily. Returns (time, returns).

Funding-regime classification is INTENTIONALLY NOT in this module --
per CLAUDE.md, the funding-regime detection layer is alpha-specific
(carry strategy in production without funding-regime detection layer
is a red line). Each alpha owns its own funding regime logic;
regime.py only handles cross-alpha stratification dimensions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

import numpy as np
import polars as pl

from alpha_factory.data.archive import load_klines_archive
from alpha_factory.validation.contracts import validate_equity_curve
from alpha_factory.validation.metrics import hit_rate, max_drawdown, sharpe
from alpha_factory.validation.schema import (
    ETF_CUTOFF_DATETIME,
    REGIME_KIND_ETF,
    REGIME_KIND_TREND,
    REGIME_KIND_VOL,
    REGIME_LABEL_BEAR,
    REGIME_LABEL_BULL,
    REGIME_LABEL_POST_ETF,
    REGIME_LABEL_PRE_ETF,
    REGIME_LABEL_VOL_HIGH,
    REGIME_LABEL_VOL_LOW,
    REGIME_LABEL_VOL_MID,
    REGIME_METRICS_SCHEMA,
    TZ_UTC,
)

__all__ = [
    "classify_market_regime",
    "load_btc_returns",
    "stratify_metrics",
]


# ── Constants ────────────────────────────────────────────────────────────


# Vol-tercile cutoff quantiles. Bottom third of rolling-vol distribution
# is "low", top third is "high", middle is "mid". Calibrated against the
# input's own full distribution -- see module docstring on lookahead.
_VOL_TERCILE_LOW = 0.33
_VOL_TERCILE_HIGH = 0.67


# ── classify_market_regime ───────────────────────────────────────────────


def classify_market_regime(
    btc_returns_df: pl.DataFrame,
    *,
    vol_window: int = 30,
    trend_window: int = 200,
) -> pl.DataFrame:
    """Classify market regimes for stratification.

    Args:
        btc_returns_df: pl.DataFrame with at least columns 'time' (tz-aware
            UTC Datetime) and 'returns' (Float64 per-period returns).
        vol_window: rolling-window length (in bars) for the vol regime
            classifier. Default 30 bars.
        trend_window: rolling-window length for trend regime (sign of
            trailing N-bar cumulative return). Default 200 bars.

    Returns:
        Long-format DataFrame with columns:
            time         tz-aware UTC datetime
            regime_kind  vol | trend | etf
            regime_label kind-specific labels

        Three rows per input bar (one per regime kind), MINUS rows
        where the regime is undefined (e.g. before vol_window or
        trend_window are filled).

    Lookahead handling:
        Vol and trend classifiers use `.shift(1)` so the regime at
        time t was determinable at time t-1. ETF cutoff is static
        (no lookahead concern).

        Vol terciles are calibrated against the FULL-SAMPLE rolling-vol
        distribution -- this is a forward-looking cutoff calibration
        but acceptable for backtest stratification (we're labeling
        periods after the fact, not generating live signals). Live
        signal callers must re-derive thresholds expanding-only.

    Raises:
        ValueError: missing 'time' or 'returns' columns
        ValueError: time column not tz-aware UTC
    """
    if "time" not in btc_returns_df.columns:
        raise ValueError("btc_returns_df must have 'time' column")
    if "returns" not in btc_returns_df.columns:
        raise ValueError("btc_returns_df must have 'returns' column")
    if btc_returns_df["time"].dtype != pl.Datetime("us", time_zone=TZ_UTC):
        raise ValueError(
            f"time dtype {btc_returns_df['time'].dtype} != "
            f"Datetime('us', '{TZ_UTC}')",
        )
    if vol_window < 2:
        raise ValueError(f"vol_window must be >= 2, got {vol_window}")
    if trend_window < 2:
        raise ValueError(f"trend_window must be >= 2, got {trend_window}")

    df = btc_returns_df.sort("time").with_columns(
        rolling_vol=pl.col("returns")
            .rolling_std(window_size=vol_window).shift(1),
        trailing_ret=pl.col("returns")
            .rolling_sum(window_size=trend_window).shift(1),
    )

    # Vol-tercile thresholds (full-sample calibration; see lookahead caveat)
    vol_q_low = df["rolling_vol"].quantile(_VOL_TERCILE_LOW)
    vol_q_high = df["rolling_vol"].quantile(_VOL_TERCILE_HIGH)

    vol_df = (
        df.select([
            "time",
            pl.lit(REGIME_KIND_VOL).alias("regime_kind"),
            (
                pl.when(pl.col("rolling_vol") < vol_q_low)
                  .then(pl.lit(REGIME_LABEL_VOL_LOW))
                  .when(pl.col("rolling_vol") > vol_q_high)
                  .then(pl.lit(REGIME_LABEL_VOL_HIGH))
                  .when(pl.col("rolling_vol").is_not_null())
                  .then(pl.lit(REGIME_LABEL_VOL_MID))
                  .otherwise(None)
            ).alias("regime_label"),
        ])
        .filter(pl.col("regime_label").is_not_null())
    )

    trend_df = (
        df.select([
            "time",
            pl.lit(REGIME_KIND_TREND).alias("regime_kind"),
            (
                pl.when(pl.col("trailing_ret") > 0)
                  .then(pl.lit(REGIME_LABEL_BULL))
                  .when(pl.col("trailing_ret") <= 0)
                  .then(pl.lit(REGIME_LABEL_BEAR))
                  .otherwise(None)
            ).alias("regime_label"),
        ])
        .filter(pl.col("regime_label").is_not_null())
    )

    etf_df = df.select([
        "time",
        pl.lit(REGIME_KIND_ETF).alias("regime_kind"),
        (
            pl.when(pl.col("time") < ETF_CUTOFF_DATETIME)
              .then(pl.lit(REGIME_LABEL_PRE_ETF))
              .otherwise(pl.lit(REGIME_LABEL_POST_ETF))
        ).alias("regime_label"),
    ])

    return pl.concat([vol_df, trend_df, etf_df]).sort(["regime_kind", "time"])


# ── stratify_metrics ─────────────────────────────────────────────────────


def stratify_metrics(
    curve: pl.DataFrame,
    regimes: pl.DataFrame,
    *,
    periods_per_year: float,
) -> pl.DataFrame:
    """Compute per-(regime_kind, regime_label) Sharpe / ann_return / max_dd / hit_rate.

    Inner-join `curve` onto `regimes` on `time`; group by
    (regime_kind, regime_label); compute the four metrics on each
    group's per-period returns + equity slice.

    Args:
        curve: EQUITY_CURVE_SCHEMA DataFrame (validated via
            `validate_equity_curve` before joining)
        regimes: DataFrame with at least (time, regime_kind, regime_label)
            in long format -- typically the output of
            `classify_market_regime`
        periods_per_year: passed to `metrics.sharpe`; mandatory because
            Sharpe annualization is configuration-dependent

    Returns:
        DataFrame conforming to REGIME_METRICS_SCHEMA: one row per
        (run_id, regime_kind, regime_label) where the join produced
        >= 2 observations. n_obs < 2 groups are skipped (Sharpe
        undefined). Groups where the equity slice is constant produce
        Sharpe=NaN.
    """
    validate_equity_curve(curve)
    if "regime_kind" not in regimes.columns or "regime_label" not in regimes.columns:
        raise ValueError(
            "regimes must have columns 'time', 'regime_kind', 'regime_label'",
        )
    if "time" not in regimes.columns:
        raise ValueError("regimes must have 'time' column")

    if curve.height == 0:
        return pl.DataFrame(schema=REGIME_METRICS_SCHEMA)

    joined = curve.join(regimes, on="time", how="inner")
    if joined.height == 0:
        return pl.DataFrame(schema=REGIME_METRICS_SCHEMA)

    rows: list[dict] = []
    for (kind, label), group in joined.group_by(
        ["regime_kind", "regime_label"], maintain_order=True,
    ):
        rets = group["period_ret_net"].to_numpy()
        rets = rets[~np.isnan(rets)]
        n_obs = len(rets)
        if n_obs < 2:
            continue
        sr = sharpe(rets, periods_per_year=periods_per_year)
        ann_ret = float(rets.mean() * periods_per_year)
        eq = group["equity"].to_numpy()
        mdd = max_drawdown(eq)
        hr = hit_rate(rets)
        rows.append({
            "run_id": group["run_id"][0],
            "regime_kind": kind,
            "regime_label": label,
            "n_obs": int(n_obs),
            "sharpe": sr,
            "ann_return": ann_ret,
            "max_dd": mdd,
            "hit_rate": hr,
        })

    if not rows:
        return pl.DataFrame(schema=REGIME_METRICS_SCHEMA)
    return pl.DataFrame(rows).cast(REGIME_METRICS_SCHEMA)


# ── load_btc_returns ─────────────────────────────────────────────────────


_FREQ_HOURLY: Literal["1h"] = "1h"
_FREQ_DAILY: Literal["1d"] = "1d"


def load_btc_returns(
    start: datetime,
    end: datetime,
    *,
    freq: Literal["1h", "1d"] = "1d",
) -> pl.DataFrame:
    """Load BTC-USDT spot returns from the L1 archive.

    Reads spot BTC-USDT klines, computes simple per-bar returns
    (close.pct_change()), drops the first row (null pct_change), and
    optionally aggregates to daily by compound (1+r).prod() - 1
    over each calendar day.

    Args:
        start: tz-aware UTC datetime; inclusive lower bound on open_time
        end:   tz-aware UTC datetime; inclusive upper bound on open_time
        freq: "1h" (passthrough) or "1d" (daily aggregation)

    Returns:
        DataFrame (time, returns) with tz-aware UTC time and Float64
        returns.

    Raises:
        ValueError: freq not in {"1h", "1d"}
        ValueError: no BTC-USDT spot klines in archive within [start, end]
        ValueError: start or end not tz-aware UTC
    """
    if freq not in (_FREQ_HOURLY, _FREQ_DAILY):
        raise ValueError(f"unsupported freq {freq!r}; use '1h' or '1d'")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be tz-aware datetimes")

    df = load_klines_archive()
    if df.height == 0:
        raise ValueError("L1 klines archive is empty")

    btc = (
        df.filter(
            (pl.col("symbol") == "BTC-USDT") & (pl.col("market") == "spot"),
        )
        .filter(
            (pl.col("open_time") >= start) & (pl.col("open_time") <= end),
        )
        .sort("open_time")
        .select(["open_time", "close"])
    )
    if btc.height == 0:
        raise ValueError(
            f"no BTC-USDT spot klines in archive within [{start}, {end}]",
        )

    btc = btc.with_columns(
        returns=pl.col("close").pct_change(),
    ).drop_nulls("returns")

    if freq == _FREQ_HOURLY:
        return btc.select([
            pl.col("open_time").alias("time"),
            pl.col("returns"),
        ])

    # Daily aggregation: compound returns within each calendar day
    return (
        btc.group_by_dynamic(
            "open_time", every="1d", closed="left", label="left",
        )
        .agg(returns=((1.0 + pl.col("returns")).product() - 1.0))
        .rename({"open_time": "time"})
    )
