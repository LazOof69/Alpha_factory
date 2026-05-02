"""L3 validation interface contracts -- pure callables.

Re-exported from alpha_factory.data.archive (single source of truth for
archive state lives in L1):

    ArchiveState         frozen dataclass; archive reproducibility snapshot
    get_archive_state()  returns an ArchiveState

Native to this module (validation-specific helpers):

    validate_equity_curve(curve)              raises on schema/invariant violations
    make_rng(run_id, salt) -> Generator       deterministic RNG factory
    infer_periodicity(curve) -> timedelta     modal time delta or
                                              AmbiguousPeriodicityError

Salt namespace constants (use one of these; ad-hoc strings are forbidden
by convention -- see make_rng docstring):

    SALT_DSR_BOOTSTRAP    salt for DSR bootstrap CI (A.4.4)
    SALT_PBO_PARTITION    salt for PBO CSCV partitioning (A.4.5)
    SALT_REGIME_PERTURB   salt for regime-perturbation tests (A.4.6)

Why callables here, not dataclasses:
    The L3 validation layer is polars-DataFrame-centric -- there is no
    "ValidationReport" Python object that lives in memory and needs to
    be marshaled. Reports are rows in RUN_REGISTRY + sidecar parquets
    (see schema.py). The four functions here are the small set of
    primitives every downstream A.4.x sub-phase needs:

        A.4.2 metrics.py     calls infer_periodicity to set
                             periods_per_year; validate_equity_curve
                             before computing Sharpe
        A.4.3 cv.py          uses make_rng for deterministic shuffling
        A.4.4 dsr.py         uses make_rng(SALT_DSR_BOOTSTRAP) for
                             bootstrap CI; reads TRIAL_LOG for n_trials
        A.4.5 pbo.py         uses make_rng(SALT_PBO_PARTITION) for CSCV
        A.4.6 regime.py      uses make_rng(SALT_REGIME_PERTURB)
        A.4.8 registry.py    calls get_archive_state on every register_run
                             to compute data_version
        A.4.9 integration    validate_equity_curve in round-trip tests
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import timedelta

import numpy as np
import polars as pl

from alpha_factory.data.archive import ArchiveState, get_archive_state
from alpha_factory.validation.schema import EQUITY_CURVE_SCHEMA

__all__ = [
    "SALT_DSR_BOOTSTRAP",
    "SALT_PBO_PARTITION",
    "SALT_REGIME_PERTURB",
    "AmbiguousPeriodicityError",
    "ArchiveState",
    "get_archive_state",
    "infer_periodicity",
    "make_rng",
    "validate_equity_curve",
]


# ── Salt namespace for make_rng ──────────────────────────────────────────
#
# Ad-hoc salt strings are forbidden by convention: any reuse across two
# different call-sites breaks DSR / PBO independence assumptions because
# the RNGs would be identical-by-accident. Adding a new use-case requires
# adding a new SALT_* constant here -- not a string literal at the call
# site. The validation engine code-reviewer audit is expected to flag
# any `make_rng(..., salt="...")` call where salt is not one of these
# constants.
SALT_DSR_BOOTSTRAP = "dsr_bootstrap"
SALT_PBO_PARTITION = "pbo_partition"
SALT_REGIME_PERTURB = "regime_perturb"


# ── Periodicity inference ────────────────────────────────────────────────


class AmbiguousPeriodicityError(ValueError):
    """Raised by infer_periodicity when the curve has no clear modal time delta.

    Two failure modes:
        1) curve.height < 2 -- not enough rows to compute a delta
        2) the modal delta has the same frequency as another delta
           (mixed-periodicity input, e.g. 8h funding settlement rows
           interleaved with 1h klines)

    Callers must reshape the input to a single periodicity (e.g.
    resample to daily) before retrying. metrics.py uses this exception
    to refuse computing periods_per_year on ambiguous curves rather
    than silently picking one of the modes.
    """


def infer_periodicity(curve: pl.DataFrame) -> timedelta:
    """Return the modal time delta in `curve['time']`.

    Used by metrics.py to compute periods_per_year:

        periods_per_year = 365 * 24 * 3600 / periodicity.total_seconds()

    Raises:
        AmbiguousPeriodicityError if no clear single mode exists.
    """
    if curve.height < 2:
        raise AmbiguousPeriodicityError(
            f"curve too short to infer periodicity (height={curve.height} < 2)"
        )
    # diff() on a Datetime series returns Duration; to_list() gives Python
    # timedelta. Counter is keyed on timedelta (hashable).
    deltas = curve["time"].diff().drop_nulls().to_list()
    counts = Counter(deltas)
    most_common = counts.most_common()
    if not most_common:
        raise AmbiguousPeriodicityError(
            "no time deltas computed (drop_nulls removed all rows)"
        )
    if len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
        raise AmbiguousPeriodicityError(
            f"multiple time deltas tied for modal frequency: "
            f"{most_common[:3]} (consider resampling input to single periodicity)"
        )
    return most_common[0][0]


# ── Equity curve validation ──────────────────────────────────────────────


# Tight relative tolerance: equity continuity should hold to float
# round-trip precision. Polars float ops introduce <1e-12 drift at our
# magnitudes; 1e-9 absorbs that without masking real bugs.
_EQUITY_CONSISTENCY_RTOL = 1e-9


def validate_equity_curve(curve: pl.DataFrame) -> None:
    """Validate `curve` conforms to EQUITY_CURVE_SCHEMA + invariants.

    Checks (in order; raises ValueError on first failure):

        1) Column names + dtypes match EQUITY_CURVE_SCHEMA exactly
        2) `time` is strictly increasing with no duplicates
        3) `equity` > 0 (no negative wealth) and not NaN/null
        4) `equity[t]` == `equity[t-1] * (1 + period_ret_net[t])`
           within rtol=1e-9 for t >= 1 (first row is exempt;
           period_ret_net[0] is unconstrained -- bootstrap run-in)
        5) `cumulative_pnl[t]` == `equity[t] - equity[0]` within rtol=1e-9

    Empty curves (height=0) are accepted -- consumers handle the
    insufficient-data case separately.
    """
    # 1. Schema match -- exact columns + exact dtypes
    expected = list(EQUITY_CURVE_SCHEMA.keys())
    if list(curve.columns) != expected:
        raise ValueError(
            f"EQUITY_CURVE columns mismatch: got {list(curve.columns)} "
            f"expected {expected}"
        )
    for col, dtype in EQUITY_CURVE_SCHEMA.items():
        if curve.schema[col] != dtype:
            raise ValueError(
                f"EQUITY_CURVE column {col!r} dtype {curve.schema[col]} "
                f"!= expected {dtype}"
            )

    if curve.height == 0:
        return

    # 2. time strictly increasing, no duplicates
    times = curve["time"]
    if times.is_null().any():
        raise ValueError("EQUITY_CURVE time has nulls")
    if curve.height >= 2:
        diffs_seconds = times.diff().drop_nulls().dt.total_seconds().to_numpy()
        if (diffs_seconds <= 0).any():
            raise ValueError(
                "EQUITY_CURVE time not strictly increasing (or has duplicates)"
            )

    # 3. equity > 0, not NaN / null
    eq_series = curve["equity"]
    if eq_series.is_null().any():
        raise ValueError("EQUITY_CURVE equity has nulls")
    eq = eq_series.to_numpy()
    if np.isnan(eq).any():
        raise ValueError("EQUITY_CURVE equity has NaN values")
    if (eq <= 0).any():
        raise ValueError(
            "EQUITY_CURVE equity has non-positive values (no negative wealth allowed)"
        )

    # 4. period_ret_net consistency: equity[t] == equity[t-1] * (1 + r_net[t])
    if curve.height >= 2:
        pr_net = curve["period_ret_net"].to_numpy()
        expected_eq = eq[:-1] * (1.0 + pr_net[1:])
        if not np.allclose(
            eq[1:], expected_eq, rtol=_EQUITY_CONSISTENCY_RTOL, atol=0,
        ):
            mismatches = np.where(
                ~np.isclose(
                    eq[1:], expected_eq, rtol=_EQUITY_CONSISTENCY_RTOL, atol=0,
                )
            )[0]
            raise ValueError(
                f"EQUITY_CURVE period_ret_net inconsistent with equity at row "
                f"indices {mismatches[:5].tolist()} "
                f"(showing first 5 of {len(mismatches)})"
            )

    # 5. cumulative_pnl consistency
    cum = curve["cumulative_pnl"].to_numpy()
    expected_cum = eq - eq[0]
    if not np.allclose(cum, expected_cum, rtol=_EQUITY_CONSISTENCY_RTOL, atol=0):
        raise ValueError(
            "EQUITY_CURVE cumulative_pnl inconsistent with equity "
            "(must equal equity[t] - equity[0])"
        )


# ── Deterministic RNG factory ────────────────────────────────────────────


def make_rng(run_id: str, salt: str = "") -> np.random.Generator:
    """Deterministic numpy.random.Generator seeded by SHA-256 over (run_id, salt).

    Same `(run_id, salt)` inputs always produce the same Generator
    state, so DSR bootstrap / PBO partitioning / regime perturbation
    are reproducible across runs and machines:

        A.4.4 dsr.py     make_rng(run_id, SALT_DSR_BOOTSTRAP)
        A.4.5 pbo.py     make_rng(run_id, SALT_PBO_PARTITION)
        A.4.6 regime.py  make_rng(run_id, SALT_REGIME_PERTURB)

    Pass one of the SALT_* constants in this module. Empty salt is
    permitted by the signature but discouraged: any two call-sites
    using salt="" with the same run_id would receive identical RNG
    state, which breaks the independence assumptions in DSR / PBO
    statistics. The code-reviewer audit is expected to flag empty
    salt usage.
    """
    blob = f"{run_id}:{salt}".encode()
    seed_int = int(hashlib.sha256(blob).hexdigest()[:16], 16)
    return np.random.default_rng(seed_int)
