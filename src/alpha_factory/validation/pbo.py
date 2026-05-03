"""Probability of Backtest Overfitting (PBO) via CSCV.

Bailey, Borwein, Lopez de Prado, Zhu (2014) "The Probability of
Backtest Overfitting" -- Combinatorially Symmetric Cross Validation
(CSCV).

The intuition: a strategy that picks the best of N parameter
combinations on a training period MUST be evaluated on the holdout
under the same N alternatives. If the in-sample winner ranks below
median on the holdout, the in-sample selection is overfitting noise.
PBO is the fraction of (in-sample, out-of-sample) splits across an
enumeration of CSCV partitions where the in-sample winner falls below
median out-of-sample.

CSCV procedure:

    1. Build T x N returns matrix M (T periods x N parameter trials).
    2. Split T rows into S non-overlapping partitions, S even.
    3. For each of C(S, S/2) ways to choose S/2 partitions as
       "in-sample" (IS) and the complement as "out-of-sample" (OOS):
        a. Compute Sharpe per trial on IS rows -> SR_IS_n
        b. Compute Sharpe per trial on OOS rows -> SR_OOS_n
        c. Let n* = argmax_n SR_IS_n.
        d. Rank n* in OOS Sharpes: r = #{k : SR_OOS_k <= SR_OOS_n*}
        e. omega = r / (N + 1); logit = log(omega / (1 - omega))
    4. PBO = fraction of splits where logit < 0
            = fraction where omega < 0.5
            = fraction where r < (N + 1) / 2

A constant returns column (std == 0) yields NaN Sharpe and is excluded
from the argmax / ranking step on each split.

Public API:

    pbo(returns_matrix, *, n_partitions, rng) -> float
        CSCV PBO in [0, 1]. Returns NaN when no split produced a
        well-defined logit (e.g. all-NaN Sharpes everywhere).

    enumerate_trials(strategy_callable, param_grid) -> (params_list, returns_matrix)
        Helper to build the T x N input by enumerating a parameter
        grid product and invoking strategy_callable(**params) for
        each combination. Wraps itertools.product with shape +
        ragged-output checks.

CONSTRAINTS / ASSUMPTIONS:

* `N >= 8` (number of trials / parameter combinations) is REQUIRED
  per Bailey et al. CSCV's discriminative power degrades sharply
  below this -- a 2-strategy "best vs other" comparison gives
  trivial PBO. Reject hard.

* `n_partitions = "auto"` picks 16 when T >= 64, else 8 when T >= 32,
  else raises. Even partitions only (CSCV symmetry).

* Sharpe is per-partition-block (mean / sample-std with ddof=1) and
  used only for RANKING. Annualization is irrelevant (rank-invariant).

* `rng` is required by signature but unused in full-enumeration mode.
  Reserved for a future subsampling variant when S is large enough
  that C(S, S/2) exceeds a memory / time budget (S=16 yields 12,870
  splits, manageable; S=24 yields 2.7M, prohibitive).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import combinations, product
from typing import Literal

import numpy as np
import polars as pl

__all__ = [
    "enumerate_trials",
    "pbo",
]


# ── Constants ────────────────────────────────────────────────────────────


# Minimum number of trials below which CSCV PBO is rejected as not
# meaningful. Per Bailey et al. -- with very few candidates, the
# argmax / rank statistic is too coarse to detect overfitting.
_MIN_N_TRIALS = 8


# Auto-partition thresholds.
_AUTO_S_HIGH = 16        # used when T >= 64
_AUTO_S_LOW = 8          # used when 32 <= T < 64
_AUTO_T_LOW = 32         # below this, "auto" raises


# ── Helpers ──────────────────────────────────────────────────────────────


def _coerce_returns_matrix(
    matrix: np.ndarray | pl.DataFrame,
) -> np.ndarray:
    """Coerce input to (T, N) ndarray of float; reject non-2D / empty."""
    arr = (
        matrix.to_numpy()
        if isinstance(matrix, pl.DataFrame)
        else np.asarray(matrix, dtype=float)
    )
    if arr.ndim != 2:
        raise ValueError(
            f"returns_matrix must be 2-D (T, N), got shape {arr.shape}",
        )
    if arr.size == 0:
        raise ValueError("returns_matrix is empty")
    return arr


def _resolve_n_partitions(n_obs: int, n_partitions: int | Literal["auto"]) -> int:
    """Resolve `n_partitions=auto` and validate even / size constraints."""
    if n_partitions == "auto":
        if n_obs >= 64:
            s = _AUTO_S_HIGH
        elif n_obs >= _AUTO_T_LOW:
            s = _AUTO_S_LOW
        else:
            raise ValueError(
                f"T={n_obs} < {_AUTO_T_LOW}; auto n_partitions cannot resolve. "
                f"Pass an explicit even n_partitions or grow the sample.",
            )
        return s
    if not isinstance(n_partitions, int) or isinstance(n_partitions, bool):
        raise TypeError(
            f"n_partitions must be int or 'auto', got "
            f"{type(n_partitions).__name__}",
        )
    if n_partitions < 4:
        raise ValueError(
            f"n_partitions must be >= 4 (CSCV needs S >= 4 for any split), "
            f"got {n_partitions}",
        )
    if n_partitions % 2 != 0:
        raise ValueError(
            f"n_partitions must be even (CSCV symmetry), got {n_partitions}",
        )
    if 2 * n_partitions > n_obs:
        raise ValueError(
            f"T={n_obs} < 2 * n_partitions={2 * n_partitions}; "
            f"each partition would have < 2 rows.",
        )
    return n_partitions


def _sharpe_per_column(block: np.ndarray) -> np.ndarray:
    """Per-column per-period Sharpe of a (rows, cols) block.

    Constant columns produce NaN. Constancy is detected via
    `max(col) == min(col)` (exact equality) rather than `std == 0` --
    numpy's `std(ddof=1)` on a constant array returns a float-rounding
    artefact on the order of 1e-19 instead of exactly zero, which would
    silently produce ~1e16 Sharpe and dominate the argmax in every
    split. ddof=1 sample std; annualization is intentionally omitted
    (PBO uses Sharpe only for ranking).
    """
    means = block.mean(axis=0)
    stds = block.std(axis=0, ddof=1)
    valid = block.max(axis=0) > block.min(axis=0)
    out = np.full_like(means, np.nan)
    safe_stds = np.where(valid, stds, 1.0)
    out[valid] = means[valid] / safe_stds[valid]
    return out


# ── PBO ──────────────────────────────────────────────────────────────────


def pbo(
    returns_matrix: np.ndarray | pl.DataFrame,
    *,
    n_partitions: int | Literal["auto"] = "auto",
    rng: np.random.Generator,
) -> float:
    """Probability of Backtest Overfitting via CSCV (Bailey et al. 2014).

    Args:
        returns_matrix: (T, N) per-period returns; T rows = time,
            N columns = parameter trials. polars DataFrame or ndarray.
        n_partitions: number of CSCV partitions S; must be even.
            "auto" picks 16 when T >= 64, else 8 when T >= 32, else
            raises (the LdP recommendation is S = 16).
        rng: numpy.random.Generator. Required by signature for forward
            compatibility with subsampling variants; full enumeration
            mode (the default) does not consult rng. Use
            `contracts.make_rng(run_id, SALT_PBO_PARTITION)`.

    Returns:
        PBO in [0, 1]. NaN if no split produced a well-defined logit
        (e.g. all-constant trials).

    Raises:
        ValueError: if N < 8 (insufficient trials for meaningful CSCV)
        ValueError: if T or n_partitions inconsistent (see helpers above)
    """
    arr = _coerce_returns_matrix(returns_matrix)
    n_obs, n_trials = arr.shape

    if n_trials < _MIN_N_TRIALS:
        raise ValueError(
            f"N (trials) must be >= {_MIN_N_TRIALS} for meaningful "
            f"CSCV PBO, got {n_trials}",
        )

    s = _resolve_n_partitions(n_obs, n_partitions)
    # rng is reserved for future subsampling -- silence the unused-arg
    # warning by referencing it; full enumeration ignores it.
    _ = rng

    # Build chronological partitions; the last absorbs any remainder.
    partition_size = n_obs // s
    partitions: list[np.ndarray] = []
    for k in range(s):
        start = k * partition_size
        end = (k + 1) * partition_size if k < s - 1 else n_obs
        partitions.append(arr[start:end])

    # Enumerate all C(S, S/2) splits.
    half = s // 2
    n_overfit = 0
    n_well_defined = 0
    for is_indices in combinations(range(s), half):
        is_set = set(is_indices)
        oos_indices = [k for k in range(s) if k not in is_set]
        is_block = np.concatenate([partitions[k] for k in is_indices])
        oos_block = np.concatenate([partitions[k] for k in oos_indices])

        is_sharpes = _sharpe_per_column(is_block)
        oos_sharpes = _sharpe_per_column(oos_block)

        if np.all(np.isnan(is_sharpes)):
            continue
        n_star = int(np.nanargmax(is_sharpes))
        if np.isnan(oos_sharpes[n_star]):
            continue

        # Rank: 1 = lowest, N = highest. Tie-break by `<=` (n_star's own
        # value is counted, giving rank 1 for a NaN-only OOS minimum).
        rank = int(np.sum(oos_sharpes <= oos_sharpes[n_star]))
        omega = rank / (n_trials + 1)
        if not (0.0 < omega < 1.0):
            continue
        logit = float(np.log(omega / (1.0 - omega)))
        n_well_defined += 1
        if logit < 0:
            n_overfit += 1

    if n_well_defined == 0:
        return float("nan")
    return float(n_overfit / n_well_defined)


# ── enumerate_trials helper ──────────────────────────────────────────────


def enumerate_trials(
    strategy_callable: Callable[..., Sequence[float]],
    param_grid: dict[str, Sequence],
) -> tuple[list[dict], np.ndarray]:
    """Enumerate strategy_callable over param_grid -> (params_list, T x N matrix).

    For each combination of `param_grid` values (Cartesian product),
    invokes `strategy_callable(**params)` and expects a 1-D sequence of
    per-period returns. All trials must return the SAME length series;
    ragged returns raise ValueError.

    The resulting T x N matrix is suitable for `pbo(...)` directly.

    Args:
        strategy_callable: function that returns a 1-D sequence of returns
            given the params for one combination
        param_grid: dict mapping parameter name to a sequence of candidate
            values; the cartesian product yields the trial set

    Returns:
        params_list: list of dicts, one per combination, in enumeration order
        returns_matrix: shape (T, N) ndarray of float

    Raises:
        ValueError: if param_grid is empty, or any trial's returns are
            not 1-D, or the length differs from the first trial's
    """
    if not param_grid:
        raise ValueError("param_grid is empty")
    keys = list(param_grid.keys())
    value_lists = [list(param_grid[k]) for k in keys]
    if any(len(v) == 0 for v in value_lists):
        raise ValueError(
            f"param_grid contains an empty value list: "
            f"{[k for k, v in zip(keys, value_lists, strict=True) if len(v) == 0]}",
        )

    params_list: list[dict] = []
    returns_columns: list[np.ndarray] = []
    expected_n_obs: int | None = None

    for combo in product(*value_lists):
        params = dict(zip(keys, combo, strict=True))
        rets = np.asarray(strategy_callable(**params), dtype=float)
        if rets.ndim != 1:
            raise ValueError(
                f"strategy_callable must return 1-D returns, got "
                f"{rets.ndim}-D for params={params}",
            )
        if expected_n_obs is None:
            expected_n_obs = len(rets)
        elif len(rets) != expected_n_obs:
            raise ValueError(
                f"strategy_callable returned len={len(rets)} for "
                f"params={params}, expected {expected_n_obs} "
                f"(all trials must produce the same-length series)",
            )
        params_list.append(params)
        returns_columns.append(rets)

    returns_matrix = np.column_stack(returns_columns)
    return params_list, returns_matrix
