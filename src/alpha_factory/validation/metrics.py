"""Risk-adjusted return statistics for one backtest run.

Operates on `EQUITY_CURVE` rows (period_ret_net, equity, time) and
`BACKTEST_LEG` rows (notional) from a single run_id.

`periods_per_year` is MANDATORY on every entry point (keyword-only,
no default). Callers compute it from an EQUITY_CURVE via
`periods_per_year_from(curve)` -- which delegates to
`contracts.infer_periodicity` and converts to a float -- or pass a
literal (e.g. 365 for crypto-daily, 8760 for hourly, 252 for trading
days) when the curve's periodicity is known a priori.

Function families:

    Pure scalar metrics on a returns Series:
        sharpe, sortino, hit_rate, profit_factor, skew, kurtosis,
        var_quantile, cvar_quantile

    Drawdown metrics on equity:
        max_drawdown, max_drawdown_duration_days, calmar

    Compound metrics (multiple inputs):
        turnover_per_year, capacity_estimate_usd

    EQUITY_CURVE convenience wrappers:
        full_sample_sharpe, active_only_sharpe (diagnostic only)
        to_periodicity (resample to coarser period)

Sharpe-CI / DSR refinement (Lo 2002, Bailey & LdP 2014) lands in
A.4.4 dsr.py -- this module only computes the point estimates.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
from scipy import stats

from alpha_factory.validation.contracts import infer_periodicity
from alpha_factory.validation.schema import EQUITY_CURVE_SCHEMA

__all__ = [
    "active_only_sharpe",
    "calmar",
    "capacity_estimate_usd",
    "cvar_quantile",
    "full_sample_sharpe",
    "hit_rate",
    "kurtosis",
    "max_drawdown",
    "max_drawdown_duration_days",
    "periods_per_year_from",
    "profit_factor",
    "sharpe",
    "skew",
    "sortino",
    "to_periodicity",
    "turnover_per_year",
    "var_quantile",
]


# ── Constants ────────────────────────────────────────────────────────────

SECONDS_PER_YEAR = 365 * 24 * 3600


# ── Type-coercion helper ─────────────────────────────────────────────────


def _to_numpy_clean(arr: pl.Series | np.ndarray | list) -> np.ndarray:
    """Coerce to numpy float array; drop polars-nulls AND numpy-NaN.

    Polars distinguishes null from NaN; both are invalid here, so drop
    both. List input is accepted for ergonomic test setup (caller
    typically passes pl.Series).
    """
    if isinstance(arr, pl.Series):
        arr = arr.drop_nulls().to_numpy()
    arr = np.asarray(arr, dtype=float)
    return arr[~np.isnan(arr)]


# ── periods_per_year helper ──────────────────────────────────────────────


def periods_per_year_from(curve: pl.DataFrame) -> float:
    """Compute periods_per_year from an EQUITY_CURVE via inferred periodicity.

    Convenience for the common pattern:

        ppy = periods_per_year_from(curve)
        sharpe(curve["period_ret_net"], periods_per_year=ppy)

    Raises:
        AmbiguousPeriodicityError if the curve has no clear modal time
        delta (e.g. mixed 8h-funding + 1h-kline rows).
    """
    periodicity = infer_periodicity(curve)
    return SECONDS_PER_YEAR / periodicity.total_seconds()


# ── Pure scalar metrics on a returns Series ──────────────────────────────


def sharpe(returns: pl.Series | np.ndarray, *, periods_per_year: float) -> float:
    """Annualized Sharpe ratio.

        Sharpe = mean(r) / std(r, ddof=1) * sqrt(periods_per_year)

    Returns NaN when fewer than 2 valid observations or returns are constant.

    Note: an explicit constant check (`arr.max() == arr.min()`) precedes
    the std computation -- numpy's `arr.std(ddof=1)` on a constant array
    can return tiny float-rounding artefacts on the order of 1e-19
    instead of exactly zero, which would silently produce an absurd
    Sharpe (1e16-ish) rather than NaN.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) < 2 or arr.max() == arr.min():
        return float("nan")
    std = arr.std(ddof=1)
    if std <= 0 or np.isnan(std):
        return float("nan")
    return float(arr.mean() / std * np.sqrt(periods_per_year))


def sortino(
    returns: pl.Series | np.ndarray,
    *,
    periods_per_year: float,
    target: float = 0.0,
) -> float:
    """Annualized Sortino ratio (downside-deviation-adjusted Sharpe).

        Sortino = (mean(r) - target) / downside_dev(r, target) * sqrt(ppy)
        downside_dev = sqrt(mean(min(r - target, 0)^2))

    The downside denominator divides by N (population, not sample) per
    convention -- Sortino's invariance to upside variance is what
    distinguishes it from Sharpe.

    Returns NaN when fewer than 2 valid observations or downside_dev == 0.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) < 2:
        return float("nan")
    excess = arr - target
    downside = np.minimum(excess, 0.0)
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0:
        return float("nan")
    return float(excess.mean() / dd * np.sqrt(periods_per_year))


def hit_rate(returns: pl.Series | np.ndarray) -> float:
    """Fraction of strictly-positive returns. NaN if no observations."""
    arr = _to_numpy_clean(returns)
    if len(arr) == 0:
        return float("nan")
    return float((arr > 0).mean())


def profit_factor(returns: pl.Series | np.ndarray) -> float:
    """sum(positive returns) / |sum(negative returns)|.

    Returns inf when no losses (and at least one win); NaN when no
    observations or all-zero returns.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) == 0:
        return float("nan")
    wins = float(arr[arr > 0].sum())
    loss_sum = float(arr[arr < 0].sum())
    if loss_sum == 0:
        return float("inf") if wins > 0 else float("nan")
    return wins / abs(loss_sum)


def skew(returns: pl.Series | np.ndarray) -> float:
    """Sample skewness (bias-corrected). NaN if fewer than 3 observations."""
    arr = _to_numpy_clean(returns)
    if len(arr) < 3:
        return float("nan")
    return float(stats.skew(arr, bias=False))


def kurtosis(returns: pl.Series | np.ndarray) -> float:
    """Excess kurtosis (Fisher's definition; normal distribution = 0).

    Bias-corrected. NaN if fewer than 4 observations.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) < 4:
        return float("nan")
    return float(stats.kurtosis(arr, fisher=True, bias=False))


def var_quantile(returns: pl.Series | np.ndarray, *, q: float = 0.05) -> float:
    """q-th quantile of returns. Default q=0.05 (95% VaR).

    Returns a number with the sign convention of the underlying returns
    (typically negative for the tail). NaN if no observations.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) == 0:
        return float("nan")
    return float(np.quantile(arr, q))


def cvar_quantile(returns: pl.Series | np.ndarray, *, q: float = 0.05) -> float:
    """Mean of returns at or below the q-th quantile (Conditional VaR).

    Returns NaN if no observations or no rows below the threshold.
    """
    arr = _to_numpy_clean(returns)
    if len(arr) == 0:
        return float("nan")
    threshold = np.quantile(arr, q)
    tail = arr[arr <= threshold]
    if len(tail) == 0:
        return float("nan")
    return float(tail.mean())


# ── Drawdown / Calmar ────────────────────────────────────────────────────


def max_drawdown(equity: pl.Series | np.ndarray) -> float:
    """Most-negative running drawdown from an equity series.

    Returns the minimum of `(equity[t] - running_max[:t]) / running_max[:t]`
    over the series -- a non-positive float (0 when equity is monotone
    non-decreasing, negative otherwise). NaN for empty input.
    """
    arr = _to_numpy_clean(equity)
    if len(arr) == 0:
        return float("nan")
    running_max = np.maximum.accumulate(arr)
    drawdown = (arr - running_max) / running_max
    return float(drawdown.min())


def max_drawdown_duration_days(curve: pl.DataFrame) -> int:
    """Longest drawdown duration in calendar days from an EQUITY_CURVE.

    Convention: a drawdown is the interval from a peak until equity
    next reaches or exceeds that peak. Duration is `recovery_time -
    peak_time`. If a drawdown is still open at the end of the curve,
    its duration is measured up to the last observation.

    Returns 0 if equity is monotone non-decreasing (no drawdown).
    """
    if curve.height == 0:
        return 0
    eq = curve["equity"].to_numpy()
    times = curve["time"].to_numpy()  # numpy datetime64
    n = len(eq)

    running_max = np.maximum.accumulate(eq)
    if not (eq < running_max).any():
        return 0

    # For each bar, the index at which the current running_max was set.
    # peak_idx[i] = argmax over eq[:i+1] -- the "peak from which the
    # current drawdown (if any) is being measured".
    peak_idx = np.zeros(n, dtype=int)
    cur_peak = 0
    for i in range(n):
        if eq[i] >= eq[cur_peak]:
            cur_peak = i
        peak_idx[i] = cur_peak

    longest_us = 0
    i = 0
    while i < n:
        if eq[i] < running_max[i]:
            # Find recovery: first j >= i where eq[j] >= peak value.
            peak_value = eq[peak_idx[i]]
            peak_t = times[peak_idx[i]]
            j = i
            while j < n and eq[j] < peak_value:
                j += 1
            # j is recovery index (or n if never recovered).
            recovery_t = times[j] if j < n else times[n - 1]
            duration_us = (recovery_t - peak_t) / np.timedelta64(1, "us")
            if duration_us > longest_us:
                longest_us = duration_us
            i = j  # skip past this drawdown
        else:
            i += 1

    return int(longest_us / (24 * 3600 * 1_000_000))


def calmar(curve: pl.DataFrame, *, periods_per_year: float) -> float:
    """Calmar ratio: annualized return / |max_drawdown|.

    Uses geometric annualization on the equity ratio:

        ann_ret = (equity[-1] / equity[0]) ** (periods_per_year / n_periods) - 1

    where n_periods = curve.height - 1 (number of return observations).

    Returns NaN if fewer than 2 rows, equity[0] <= 0, or max_drawdown is
    non-negative (no drawdown means Calmar undefined).
    """
    if curve.height < 2:
        return float("nan")
    eq = curve["equity"].to_numpy()
    if eq[0] <= 0:
        return float("nan")

    n = len(eq) - 1
    ann_ret = (eq[-1] / eq[0]) ** (periods_per_year / n) - 1.0

    mdd = max_drawdown(eq)
    if mdd >= 0 or np.isnan(mdd):
        return float("nan")
    return float(ann_ret / abs(mdd))


# ── Compound metrics ─────────────────────────────────────────────────────


def turnover_per_year(
    legs: pl.DataFrame,
    equity_curve: pl.DataFrame,
    *,
    periods_per_year: float,
) -> float:
    """Annualized turnover from BACKTEST_LEG, normalized by mean equity.

        turnover_per_year =
            sum_t(sum_legs(|notional[t] - notional[t-1]|))
            / mean(equity)
            * periods_per_year
            / n_periods

    `n_periods` is `equity_curve.height` (one row per bar), not the
    number of trade events. Per-leg deltas are computed within
    (symbol, leg_id) groups; the FIRST bar of each group is dropped
    (no prior notional to diff against).

    Returns NaN if `legs.height == 0`, `equity_curve.height < 2`, or
    mean(equity) <= 0.
    """
    if legs.height == 0 or equity_curve.height < 2:
        return float("nan")

    legs_sorted = legs.sort(["symbol", "leg_id", "time"])
    deltas = (
        legs_sorted.with_columns(
            delta=pl.col("notional").diff().over(["symbol", "leg_id"]).abs(),
        )
        .drop_nulls("delta")
    )
    if deltas.height == 0:
        return 0.0

    per_bar_volume = deltas.group_by("time").agg(pl.col("delta").sum())
    total_traded = float(per_bar_volume["delta"].sum())

    mean_equity = float(equity_curve["equity"].mean())
    if mean_equity <= 0:
        return float("nan")

    n_periods = equity_curve.height
    return total_traded / mean_equity * periods_per_year / n_periods


def capacity_estimate_usd(
    legs: pl.DataFrame,
    daily_volume_per_symbol: dict[str, float],
    *,
    target_pct_of_adv: float = 0.01,
) -> float:
    """USDT capacity at which the bottleneck symbol's max notional equals
    `target_pct_of_adv * ADV`.

    Per-symbol bottleneck:

        capacity_for_sym(s) = (target_pct * ADV(s)) / weight_in_strategy(s)

    where weight_in_strategy(s) = max_notional(s) / mean_total_abs_notional.
    Strategy capacity = min(capacity_for_sym) over traded symbols.

    Caveats:

    * `target_pct_of_adv = 0.01` is conservative but arbitrary -- LdP
      offers no canonical answer; capacity estimation is workflow-
      dependent. Override per backtest.
    * Daily volume must be supplied externally (typically from L1
      universe snapshot's `quote_volume_24h`). Missing symbols raise
      ValueError -- silent NaN would mask a sizing bug.
    * The estimate ignores price impact non-linearity, market regime,
      and order-book depth distribution. Treat as upper bound.

    Returns NaN if `legs.height == 0` or all max_notionals are zero.
    """
    if legs.height == 0:
        return float("nan")

    per_sym = legs.group_by("symbol").agg(
        pl.col("notional").abs().max().alias("max_notional"),
    )
    avg_total = (
        legs.group_by("time")
        .agg(pl.col("notional").abs().sum().alias("total"))
        ["total"].mean()
    )
    if avg_total is None or avg_total <= 0:
        return float("nan")

    capacities: list[float] = []
    for row in per_sym.iter_rows(named=True):
        sym = row["symbol"]
        max_n = row["max_notional"]
        if sym not in daily_volume_per_symbol:
            raise ValueError(
                f"daily volume not provided for symbol {sym!r} "
                f"(populate daily_volume_per_symbol from L1 universe snapshot)",
            )
        adv = daily_volume_per_symbol[sym]
        if max_n <= 0 or adv <= 0:
            continue
        weight = max_n / avg_total
        capacities.append((target_pct_of_adv * adv) / weight)

    if not capacities:
        return float("nan")
    return float(min(capacities))


# ── EQUITY_CURVE convenience wrappers ────────────────────────────────────


def full_sample_sharpe(
    curve: pl.DataFrame, *, periods_per_year: float,
) -> float:
    """Sharpe over the entire EQUITY_CURVE on `period_ret_net`.

    This is the canonical Sharpe -- the headline number reported in
    RUN_REGISTRY metrics_summary_json and consumed by DSR / PBO gates.
    """
    return sharpe(curve["period_ret_net"], periods_per_year=periods_per_year)


def active_only_sharpe(
    curve: pl.DataFrame,
    active_mask: pl.Series,
    *,
    periods_per_year: float,
) -> float:
    """Sharpe filtered to bars where `active_mask` is True. DIAGNOSTIC ONLY.

    The L3 gates (DSR / PBO / regime / max_dd) consume `full_sample_sharpe`,
    not this. A high `active_only_sharpe` paired with low
    `full_sample_sharpe` indicates the strategy spends too much time
    flat (regime-detection thrashing, conservative signal threshold,
    etc.) -- diagnose the gap, never report `active_only_sharpe` as
    the strategy's headline number.

    The `active_mask` length must match `curve.height` exactly.
    Typical construction:

        active_times = legs.filter(pl.col("notional") > 0)["time"].unique()
        active_mask = curve["time"].is_in(active_times)
    """
    if len(active_mask) != curve.height:
        raise ValueError(
            f"active_mask length {len(active_mask)} != curve height {curve.height}",
        )
    filtered = curve["period_ret_net"].filter(active_mask)
    return sharpe(filtered, periods_per_year=periods_per_year)


# ── Periodicity resampling ───────────────────────────────────────────────


def _timedelta_to_polars_every(td: timedelta) -> str:
    """Convert a timedelta to a polars `every=` string ('1d', '8h', etc.).

    Polars `group_by_dynamic` accepts only whole units; raises ValueError
    if `td` is not a clean multiple of seconds at minute / hour / day grain.
    """
    total_us = int(round(td.total_seconds() * 1_000_000))
    if total_us <= 0:
        raise ValueError(f"timedelta must be positive: got {td!r}")
    us_per_day = 24 * 3600 * 1_000_000
    us_per_hour = 3600 * 1_000_000
    us_per_minute = 60 * 1_000_000
    us_per_second = 1_000_000
    if total_us % us_per_day == 0:
        return f"{total_us // us_per_day}d"
    if total_us % us_per_hour == 0:
        return f"{total_us // us_per_hour}h"
    if total_us % us_per_minute == 0:
        return f"{total_us // us_per_minute}m"
    if total_us % us_per_second == 0:
        return f"{total_us // us_per_second}s"
    raise ValueError(
        f"timedelta {td!r} is not a whole-second multiple at d/h/m/s grain",
    )


def to_periodicity(
    curve: pl.DataFrame, target_periodicity: timedelta,
) -> pl.DataFrame:
    """Resample an EQUITY_CURVE to a coarser periodicity.

    Returns a DataFrame conforming to EQUITY_CURVE_SCHEMA with:

        time              floored to `target_periodicity` boundaries
        equity            last value within each bucket
        period_ret_gross  prod(1 + r_gross) - 1 within bucket
        period_ret_net    prod(1 + r_net)   - 1 within bucket
        cumulative_pnl    derived from new equity (= equity - equity[0])

    `target_periodicity` must be coarser than the curve's existing
    periodicity (e.g. daily from hourly). Same-or-finer targets pass
    through but are pointless; this function does not interpolate.

    The first resampled bucket retains the input's first equity value,
    so equity[0] in output equals curve[0]['equity'] (start of bucket).
    """
    if curve.height == 0:
        return curve.clone()

    every = _timedelta_to_polars_every(target_periodicity)

    resampled = (
        curve.sort("time")
        .group_by_dynamic("time", every=every, closed="left", label="left")
        .agg(
            pl.col("run_id").first().alias("run_id"),
            pl.col("equity").last().alias("equity"),
            ((1.0 + pl.col("period_ret_gross")).product() - 1.0)
                .alias("period_ret_gross"),
            ((1.0 + pl.col("period_ret_net")).product() - 1.0)
                .alias("period_ret_net"),
        )
    )
    eq0 = resampled["equity"][0]
    resampled = resampled.with_columns(
        cumulative_pnl=pl.col("equity") - eq0,
    )
    return resampled.select(list(EQUITY_CURVE_SCHEMA.keys())).cast(
        EQUITY_CURVE_SCHEMA,
    )
