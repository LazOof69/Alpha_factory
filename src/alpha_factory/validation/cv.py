"""Cross-validation splitters for L3 validation.

Three splitter classes, all yielding `Fold` objects with int index
arrays into the input time index plus tz-aware datetime bounds for
audit:

    PurgedKFold        K-fold with bidirectional embargo (LdP 2018 §7.4)
    WalkForwardCV      sliding-train walk-forward
    ExpandingWindowCV  expanding-train walk-forward

All inputs are tz-aware UTC time indices (`pl.Series` of
`Datetime("us", "UTC")`). Test fold indices never overlap; train
indices exclude the embargo zone (PurgedKFold only -- WalkForward /
Expanding rely on `step >= test_len` for non-overlap, validated at
construction time).

Why three classes:

    PurgedKFold        used by A.4.4 dsr.py / A.4.5 pbo.py for in-sample
                       cross-validation when alpha has free parameters
                       and labels may overlap consecutive bars
                       (CSCV variant via combinatorial purged K-fold)
    WalkForwardCV      used to evaluate parameter stability over a
                       moving window -- Sharpe(fold_k) trajectory
    ExpandingWindowCV  used when more recent observations should not
                       overshadow older ones (anchored regression);
                       always trains from t_0 onwards

The Fold dataclass is yielded (not persisted) -- in-memory only. PBO
audit at A.4.5 records params_hash + oos_sharpe per fold in TRIAL_LOG;
the fold geometry itself is not load-bearing for reproducibility (it
is reconstructable from CV class config + time index).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import polars as pl

__all__ = [
    "ExpandingWindowCV",
    "Fold",
    "PurgedKFold",
    "WalkForwardCV",
]


# ── Fold dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fold:
    """One split of a CV iteration.

    `train_idx` / `test_idx` are int arrays into the time index passed
    to `split()`. `test_start` / `test_end` / `embargo_end` are
    tz-aware UTC datetimes for audit and downstream PBO logging.

    For PurgedKFold, `train_idx` may be non-contiguous (test sits in
    the middle of the time series and the embargo zone is excluded
    from both sides). For WalkForward / Expanding, `train_idx` is
    contiguous and ends at `test_start`.

    `embargo_end == test_end` when no embargo applies (WalkForward /
    Expanding default).
    """
    fold_id: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    test_start: datetime
    test_end: datetime
    embargo_end: datetime


# ── Internal helpers ─────────────────────────────────────────────────────


def _validate_time_index(time_index: pl.Series) -> None:
    """Assert dtype Datetime(us, UTC), >=2 rows, no nulls, strictly increasing."""
    if time_index.dtype != pl.Datetime("us", time_zone="UTC"):
        raise ValueError(
            f"time_index dtype {time_index.dtype} != Datetime('us', 'UTC')",
        )
    if time_index.is_null().any():
        raise ValueError("time_index has nulls")
    if time_index.len() < 2:
        raise ValueError("time_index too short (< 2 rows)")
    diffs = time_index.diff().drop_nulls().dt.total_seconds().to_numpy()
    if (diffs <= 0).any():
        raise ValueError("time_index not strictly increasing")


def _modal_periodicity(time_index: pl.Series) -> timedelta:
    """Return the modal time delta in `time_index` as a timedelta.

    Used to convert int-bar embargo specs to time intervals. For
    multi-modal time indices, picks the most-common delta and returns
    that without raising -- the caller's int embargo intent is
    preserved as best-effort.
    """
    deltas = time_index.diff().drop_nulls().to_list()
    counts = Counter(deltas)
    return counts.most_common(1)[0][0]


def _check_nonneg_timedelta(name: str, val: timedelta) -> None:
    if not isinstance(val, timedelta):
        raise TypeError(
            f"{name} must be timedelta, got {type(val).__name__}",
        )
    if val.total_seconds() < 0:
        raise ValueError(f"{name} must be non-negative, got {val!r}")


def _check_positive_timedelta(name: str, val: timedelta) -> None:
    if not isinstance(val, timedelta):
        raise TypeError(
            f"{name} must be timedelta, got {type(val).__name__}",
        )
    if val.total_seconds() <= 0:
        raise ValueError(f"{name} must be positive, got {val!r}")


# ── PurgedKFold ──────────────────────────────────────────────────────────


class PurgedKFold:
    """K-fold CV with bidirectional embargo for time-series labels.

    LdP 2018 §7.4.1 distinguishes:
        purge   drop train rows whose label-evaluation overlaps test
        embargo drop train rows after test for label memory leakage

    Both reduce information leakage; for arbitrary (possibly forward-
    looking) labels, applying the same distance to both sides of the
    test fold is conservative.

    For purely-current-bar labels (e.g. period_ret_net at this row),
    the leading embargo is unnecessary and the user can pass
    `embargo=0` plus rely on WalkForwardCV downstream to enforce the
    trailing embargo.

    Args:
        n_splits: number of folds; must be >= 2
        embargo: distance to drop on BOTH sides of the test fold
            timedelta -- absolute time interval
            int       -- bars (multiplied by inferred modal periodicity
                         of `time_index` in split())
            default 0 -- no embargo (degenerates to vanilla K-fold)
    """

    def __init__(
        self,
        n_splits: int,
        embargo: timedelta | int = 0,
    ):
        if isinstance(n_splits, bool) or not isinstance(n_splits, int):
            raise TypeError(
                f"n_splits must be int, got {type(n_splits).__name__}",
            )
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        if isinstance(embargo, timedelta):
            if embargo.total_seconds() < 0:
                raise ValueError(
                    f"embargo timedelta must be non-negative, got {embargo!r}",
                )
        elif isinstance(embargo, int):
            if isinstance(embargo, bool) or embargo < 0:
                raise ValueError(
                    f"embargo int must be non-negative, got {embargo!r}",
                )
        else:
            raise TypeError(
                f"embargo must be timedelta or int, got {type(embargo).__name__}",
            )
        self.n_splits = n_splits
        self.embargo = embargo

    def split(self, time_index: pl.Series) -> Iterator[Fold]:
        """Yield `n_splits` folds. Test blocks are contiguous and non-overlapping."""
        _validate_time_index(time_index)
        n = time_index.len()
        if n < self.n_splits:
            raise ValueError(
                f"time_index length {n} < n_splits {self.n_splits}",
            )

        if isinstance(self.embargo, timedelta):
            embargo_td_py = self.embargo
        else:
            periodicity = _modal_periodicity(time_index)
            embargo_td_py = self.embargo * periodicity

        embargo_us = int(embargo_td_py.total_seconds() * 1_000_000)
        embargo_td_np = np.timedelta64(embargo_us, "us")
        t_arr = time_index.to_numpy()
        fold_size = n // self.n_splits

        for k in range(self.n_splits):
            test_start_idx = k * fold_size
            test_end_idx = (k + 1) * fold_size if k < self.n_splits - 1 else n
            test_idx = np.arange(test_start_idx, test_end_idx)

            test_start_np = t_arr[test_start_idx]
            test_end_np = t_arr[test_end_idx - 1]
            in_embargo = (
                (t_arr >= test_start_np - embargo_td_np)
                & (t_arr <= test_end_np + embargo_td_np)
            )
            train_idx = np.where(~in_embargo)[0]

            yield Fold(
                fold_id=k,
                train_idx=train_idx,
                test_idx=test_idx,
                test_start=time_index.item(int(test_start_idx)),
                test_end=time_index.item(int(test_end_idx - 1)),
                embargo_end=time_index.item(int(test_end_idx - 1))
                + embargo_td_py,
            )


# ── WalkForwardCV ────────────────────────────────────────────────────────


class WalkForwardCV:
    """Sliding-train walk-forward CV.

    Each fold has a sliding train window of length `train` ending just
    before a test window of length `test`. Folds advance by `step`.
    `step >= test` is required so consecutive test windows do not
    overlap.

    The first fold's train block may use `min_train` if data does not
    yet permit a full `train` window.

    Args:
        train: target train window length (timedelta, > 0)
        test:  test window length (timedelta, > 0)
        step:  distance between consecutive fold starts; must be >= test
        min_train: minimum acceptable train window for the first fold;
                   must be > 0 and <= train
    """

    def __init__(
        self,
        train: timedelta,
        test: timedelta,
        step: timedelta,
        min_train: timedelta,
    ):
        _check_positive_timedelta("train", train)
        _check_positive_timedelta("test", test)
        _check_positive_timedelta("step", step)
        _check_positive_timedelta("min_train", min_train)
        if step < test:
            raise ValueError(
                f"step ({step}) < test ({test}) would yield overlapping test windows",
            )
        if min_train > train:
            raise ValueError(
                f"min_train ({min_train}) must not exceed train ({train})",
            )
        self.train = train
        self.test = test
        self.step = step
        self.min_train = min_train

    def split(self, time_index: pl.Series) -> Iterator[Fold]:
        _validate_time_index(time_index)
        t_arr = time_index.to_numpy()
        t0 = t_arr[0]
        t_last = t_arr[-1]

        train_us = np.timedelta64(int(self.train.total_seconds() * 1_000_000), "us")
        test_us = np.timedelta64(int(self.test.total_seconds() * 1_000_000), "us")
        step_us = np.timedelta64(int(self.step.total_seconds() * 1_000_000), "us")
        min_train_us = np.timedelta64(
            int(self.min_train.total_seconds() * 1_000_000), "us",
        )

        # Loop while train_end is still within the index. The test window is
        # half-open [test_start, test_end) so a fold is valid as long as
        # test_idx is non-empty -- the last fold's test_end may extend
        # past t_last but still cover the trailing bars.
        fold_id = 0
        train_end_t = t0 + min_train_us
        while train_end_t <= t_last:
            test_start_t = train_end_t
            test_end_t = test_start_t + test_us
            train_start_t = max(t0, train_end_t - train_us)

            train_mask = (t_arr >= train_start_t) & (t_arr < train_end_t)
            test_mask = (t_arr >= test_start_t) & (t_arr < test_end_t)
            train_idx = np.where(train_mask)[0]
            test_idx = np.where(test_mask)[0]

            if len(train_idx) > 0 and len(test_idx) > 0:
                yield Fold(
                    fold_id=fold_id,
                    train_idx=train_idx,
                    test_idx=test_idx,
                    test_start=time_index.item(int(test_idx[0])),
                    test_end=time_index.item(int(test_idx[-1])),
                    embargo_end=time_index.item(int(test_idx[-1])),
                )
                fold_id += 1
            train_end_t = train_end_t + step_us


# ── ExpandingWindowCV ────────────────────────────────────────────────────


class ExpandingWindowCV:
    """Expanding-train walk-forward CV.

    Train always starts at the time index's first observation; train
    window extends to the start of each test fold. Test follows train.
    Folds advance by `step`. `step >= test` required for non-overlap.

    Args:
        test:      test window length (timedelta, > 0)
        step:      distance between consecutive fold starts; >= test
        min_train: train window length for the first fold; > 0
    """

    def __init__(
        self,
        test: timedelta,
        step: timedelta,
        min_train: timedelta,
    ):
        _check_positive_timedelta("test", test)
        _check_positive_timedelta("step", step)
        _check_positive_timedelta("min_train", min_train)
        if step < test:
            raise ValueError(
                f"step ({step}) < test ({test}) would yield overlapping test windows",
            )
        self.test = test
        self.step = step
        self.min_train = min_train

    def split(self, time_index: pl.Series) -> Iterator[Fold]:
        _validate_time_index(time_index)
        t_arr = time_index.to_numpy()
        t0 = t_arr[0]
        t_last = t_arr[-1]

        test_us = np.timedelta64(int(self.test.total_seconds() * 1_000_000), "us")
        step_us = np.timedelta64(int(self.step.total_seconds() * 1_000_000), "us")
        min_train_us = np.timedelta64(
            int(self.min_train.total_seconds() * 1_000_000), "us",
        )

        # See WalkForwardCV.split for the rationale on the loop bound.
        fold_id = 0
        train_end_t = t0 + min_train_us
        while train_end_t <= t_last:
            test_start_t = train_end_t
            test_end_t = test_start_t + test_us

            train_mask = (t_arr >= t0) & (t_arr < train_end_t)
            test_mask = (t_arr >= test_start_t) & (t_arr < test_end_t)
            train_idx = np.where(train_mask)[0]
            test_idx = np.where(test_mask)[0]

            if len(train_idx) > 0 and len(test_idx) > 0:
                yield Fold(
                    fold_id=fold_id,
                    train_idx=train_idx,
                    test_idx=test_idx,
                    test_start=time_index.item(int(test_idx[0])),
                    test_end=time_index.item(int(test_idx[-1])),
                    embargo_end=time_index.item(int(test_idx[-1])),
                )
                fold_id += 1
            train_end_t = train_end_t + step_us


