"""Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) + bootstrap CI.

Three layers of API:

    expected_max_sharpe(n_trials)
        E[max{Z_n}_n=1..N | Z_n iid standard normal] in z-units; the
        deflation adjustment.

    sharpe_se_lo(sharpe, n_obs, skew, kurt_excess)
        Standard error of a per-period Sharpe ratio per Lo (2002),
        accounting for skew + (excess) kurtosis of the return
        distribution.

    deflated_sharpe(observed_sr, n_trials, n_obs, skew, kurt_excess)
        Bailey & LdP DSR point estimate; returns probability in [0, 1].

    deflated_sharpe_bootstrap_ci(returns, n_trials, rng, ...)
        IID bootstrap CI for DSR; resamples returns with replacement.

    block_bootstrap_dsr(returns, n_trials, rng, block_size, ...)
        Fixed-block bootstrap CI for DSR; preserves short-range
        autocorrelation. Use when ACF(lag 1) on returns is non-trivial
        (carry strategies, momentum on persistent funding regimes).

CONVENTIONS:

* `observed_sr` is per-period (per-bar / per-day depending on the
  source curve). Lo (2002)'s SE formula is derived for per-period
  Sharpe; passing an annualized Sharpe into this function would
  understate the SE by a factor of sqrt(periods_per_year). Callers
  with annualized SR should divide by sqrt(periods_per_year) first.

* `kurt_excess` is Fisher's excess kurtosis (normal = 0). Matches
  alpha_factory.validation.metrics.kurtosis. Lo's formula uses raw
  kurt; this module adds 3 internally so the API stays consistent
  with metrics.

* `n_trials` is mandatory and must be >= 1. n_trials < 10 emits a
  UserWarning -- the expected-max-Sharpe approximation degrades and
  the deflation adjustment is too small to defensibly detect
  overfitting at that scale.

* PROJECT.md gate: "DSR > 0 with 95% CI" -- interpret via
  `deflated_sharpe_bootstrap_ci` returning (point, lo, hi); pass
  iff lo > 0.5 (DSR is a probability so > 0.5 means deflated Sharpe
  is positive). For "DSR > 0.95" stricter gate, pass iff lo > 0.95.

* RNG should be obtained via `contracts.make_rng(run_id,
  SALT_DSR_BOOTSTRAP)` so DSR CI is reproducible across re-runs.
"""
from __future__ import annotations

import math
import warnings
from collections.abc import Sequence

import numpy as np
import polars as pl
from scipy import stats

__all__ = [
    "block_bootstrap_dsr",
    "deflated_sharpe",
    "deflated_sharpe_bootstrap_ci",
    "expected_max_sharpe",
    "sharpe_se_lo",
]


# ── Constants ────────────────────────────────────────────────────────────


# Euler-Mascheroni constant; appears in the expected-max-of-N-iid-Z formula.
_GAMMA_EULER_MASCHERONI = 0.5772156649015329

# n_trials threshold below which DSR is unreliable.
_N_TRIALS_WARN_THRESHOLD = 10


# ── _to_numpy_clean (same convention as metrics.py) ──────────────────────


def _to_numpy_clean(arr: pl.Series | np.ndarray | Sequence) -> np.ndarray:
    """Coerce to numpy float array; drop polars-nulls AND numpy-NaN."""
    if isinstance(arr, pl.Series):
        arr = arr.drop_nulls().to_numpy()
    arr = np.asarray(arr, dtype=float)
    return arr[~np.isnan(arr)]


# ── Expected max Sharpe under H0 ─────────────────────────────────────────


def expected_max_sharpe(n_trials: int) -> float:
    """E[max{Z_n}_n=1..N | Z_n iid standard normal], in standard-deviation units.

    Bailey & Lopez de Prado (2014) approximation:

        E[max] ~= (1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N*e))

    where gamma is Euler-Mascheroni. For N=1 returns 0 (no max selection).

    This is the "deflation" adjustment subtracted from the observed
    Sharpe-z-score before computing DSR. Larger N -> larger deflation,
    making it harder to claim the strategy beats null after a search.
    """
    if not isinstance(n_trials, int) or isinstance(n_trials, bool):
        raise TypeError(
            f"n_trials must be int, got {type(n_trials).__name__}",
        )
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if n_trials == 1:
        return 0.0
    q1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    q2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(
        (1.0 - _GAMMA_EULER_MASCHERONI) * q1 + _GAMMA_EULER_MASCHERONI * q2,
    )


# ── Lo (2002) Sharpe standard error ──────────────────────────────────────


def sharpe_se_lo(
    sharpe: float,
    n_obs: int,
    skew: float,
    kurt_excess: float,
) -> float:
    """Standard error of a per-period Sharpe ratio (Lo 2002).

        sigma^2(SR) = (1 - skew * SR + (kurt_raw - 1)/4 * SR^2) / n_obs
                    = (1 - skew * SR + (kurt_excess + 2)/4 * SR^2) / n_obs

    `n_obs` is "T" in the Lo / LdP notation (number of observations).
    Returns NaN if n_obs < 2 or the variance term turns non-positive
    (numerical edge for extreme inputs).
    """
    if not isinstance(n_obs, int) or isinstance(n_obs, bool):
        raise TypeError(f"n_obs must be int, got {type(n_obs).__name__}")
    if n_obs < 2:
        return float("nan")
    var_per_n = (
        1.0
        - skew * sharpe
        + (kurt_excess + 2.0) / 4.0 * sharpe * sharpe
    )
    if var_per_n <= 0 or not np.isfinite(var_per_n):
        return float("nan")
    return float(math.sqrt(var_per_n / n_obs))


# ── DSR point estimate ───────────────────────────────────────────────────


def deflated_sharpe(
    observed_sr: float,
    *,
    n_trials: int,
    n_obs: int,
    skew: float,
    kurt_excess: float,
) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

        DSR = Phi[(SR_obs - SR_0_hat) / sigma(SR_obs)]
            = Phi[Z_obs - E[max(Z_n)]]

    where Z_obs = SR_obs / sigma(SR_obs) (Lo SE), E[max] is the
    expected maximum under H0 over n_trials iid trials.

    Returns the probability the observed Sharpe exceeds what would be
    expected under null after correcting for the multiple-testing
    selection bias.

    Args:
        observed_sr: PER-PERIOD Sharpe ratio. (For annualized SR,
            divide by sqrt(periods_per_year) before calling.)
        n_trials: number of independent param combos searched.
            Mandatory; n_trials < 10 emits UserWarning.
        n_obs: number of observations (T in Lo / LdP notation)
        skew: sample skewness of returns
        kurt_excess: Fisher excess kurtosis of returns (normal = 0)

    Returns:
        DSR in [0, 1]; NaN if SE undefined or inputs are invalid.
    """
    if not isinstance(n_trials, int) or isinstance(n_trials, bool):
        raise TypeError(
            f"n_trials must be int, got {type(n_trials).__name__}",
        )
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    if n_trials < _N_TRIALS_WARN_THRESHOLD:
        warnings.warn(
            f"DSR with n_trials={n_trials} < {_N_TRIALS_WARN_THRESHOLD} is "
            "unreliable: expected-max approximation degrades and the "
            "deflation adjustment is too small to defensibly detect "
            "overfitting at that scale.",
            UserWarning,
            stacklevel=2,
        )

    se = sharpe_se_lo(observed_sr, n_obs, skew, kurt_excess)
    if not np.isfinite(se) or se <= 0:
        return float("nan")

    z_obs = observed_sr / se
    e_max = expected_max_sharpe(n_trials)
    return float(stats.norm.cdf(z_obs - e_max))


# ── Internal: DSR from a returns array ──────────────────────────────────


def _dsr_from_arr(arr: np.ndarray, n_trials: int) -> float:
    """Compute DSR point estimate from a per-period returns array."""
    n_obs = len(arr)
    if n_obs < 4 or arr.max() == arr.min():
        return float("nan")
    sd = arr.std(ddof=1)
    if sd <= 0:
        return float("nan")
    sr_pp = arr.mean() / sd                       # per-period
    sk = float(stats.skew(arr, bias=False))
    kt = float(stats.kurtosis(arr, fisher=True, bias=False))
    # Suppress the < 10 warning here: this helper is called from
    # bootstrap loops where the warning would fire on every iteration;
    # the outer call has already warned once.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return deflated_sharpe(
            sr_pp, n_trials=n_trials, n_obs=n_obs, skew=sk, kurt_excess=kt,
        )


# ── IID bootstrap CI for DSR ─────────────────────────────────────────────


def deflated_sharpe_bootstrap_ci(
    returns: pl.Series | np.ndarray,
    *,
    n_trials: int,
    rng: np.random.Generator,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """IID bootstrap confidence interval for DSR.

    Returns `(dsr_point, ci_lower, ci_upper)` -- all probabilities in
    [0, 1] (with NaN for under-determined cases). The bootstrap
    resamples returns with replacement and recomputes DSR per sample;
    the CI is the empirical quantile band over `n_bootstrap` resamples.

    For autocorrelated returns (carry / persistent regimes), use
    `block_bootstrap_dsr` instead -- the iid bootstrap will narrow the
    CI artificially.

    Args:
        returns: per-period returns (pl.Series or 1-D ndarray)
        n_trials: as in deflated_sharpe (mandatory; <10 warns)
        rng: numpy.random.Generator -- use
            `contracts.make_rng(run_id, SALT_DSR_BOOTSTRAP)` for
            reproducibility
        n_bootstrap: bootstrap sample count (default 1000)
        confidence: CI level (default 0.95)

    Returns:
        (point, lower, upper) -- NaN tuple if n_obs < 4 or returns
        are constant.
    """
    if n_bootstrap < 1:
        raise ValueError(f"n_bootstrap must be >= 1, got {n_bootstrap}")
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    arr = _to_numpy_clean(returns)
    n_obs = len(arr)
    if n_obs < 4 or arr.max() == arr.min():
        return float("nan"), float("nan"), float("nan")

    point = _dsr_from_arr(arr, n_trials)

    dsrs = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        sample = rng.choice(arr, size=n_obs, replace=True)
        dsrs[b] = _dsr_from_arr(sample, n_trials)

    valid = dsrs[~np.isnan(dsrs)]
    if len(valid) < 2:
        return point, float("nan"), float("nan")

    alpha = (1.0 - confidence) / 2.0
    lower = float(np.quantile(valid, alpha))
    upper = float(np.quantile(valid, 1.0 - alpha))
    return point, lower, upper


# ── Block bootstrap CI for DSR (autocorrelated returns) ──────────────────


def block_bootstrap_dsr(
    returns: pl.Series | np.ndarray,
    *,
    n_trials: int,
    rng: np.random.Generator,
    block_size: int,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
) -> tuple[float, float, float]:
    """Fixed-block bootstrap CI for DSR (preserves short-range autocorrelation).

    Resamples blocks of `block_size` consecutive returns with
    replacement (block-start indices uniform over [0, T - block_size]).
    Concatenates blocks and truncates to T to keep sample size constant.

    Use this when ACF(lag 1) on the return series is non-trivial:

    * carry strategies have persistent funding regimes (positive ACF)
    * momentum strategies have momentum-driven autocorrelation
    * any HFT or microstructure alpha

    Block size selection is workflow-dependent; common choice is
    block_size ~= T^(1/3) or ~= the empirical decorrelation lag.

    Args:
        returns: per-period returns
        n_trials: as in deflated_sharpe (mandatory; <10 warns)
        rng: numpy.random.Generator
        block_size: length of each resampled block (>= 1)
        n_bootstrap: bootstrap sample count
        confidence: CI level

    Returns:
        (point, lower, upper) -- NaN tuple if n_obs <= block_size
        or returns are constant.
    """
    if not isinstance(block_size, int) or isinstance(block_size, bool):
        raise TypeError(
            f"block_size must be int, got {type(block_size).__name__}",
        )
    if block_size < 1:
        raise ValueError(f"block_size must be >= 1, got {block_size}")
    if n_bootstrap < 1:
        raise ValueError(f"n_bootstrap must be >= 1, got {n_bootstrap}")
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")

    arr = _to_numpy_clean(returns)
    n_obs = len(arr)
    if n_obs <= block_size or arr.max() == arr.min():
        return float("nan"), float("nan"), float("nan")

    point = _dsr_from_arr(arr, n_trials)

    n_blocks = (n_obs + block_size - 1) // block_size
    dsrs = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        starts = rng.integers(0, n_obs - block_size + 1, size=n_blocks)
        sample = np.concatenate([arr[s:s + block_size] for s in starts])[:n_obs]
        dsrs[b] = _dsr_from_arr(sample, n_trials)

    valid = dsrs[~np.isnan(dsrs)]
    if len(valid) < 2:
        return point, float("nan"), float("nan")

    alpha = (1.0 - confidence) / 2.0
    lower = float(np.quantile(valid, alpha))
    upper = float(np.quantile(valid, 1.0 - alpha))
    return point, lower, upper
