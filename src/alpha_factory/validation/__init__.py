"""L3 -- Validation layer.

Implements the LdP-rigor checks every alpha must pass before entering
the L4 portfolio:

    - Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014)
    - Probability of Backtest Overfitting (CSCV method)
    - Purged + embargoed K-fold cross-validation
    - Walk-forward analysis
    - Regime stratification (open-ended labels: bull/bear, pre/post-ETF, ...)
    - Trial-log audit (DSR n_trials provenance via append-only ledger)

Public surface (callables; see contracts.py for full docs):

    ArchiveState              archive reproducibility snapshot dataclass
    get_archive_state()       returns ArchiveState (re-exported from L1)
    validate_equity_curve()   raise on EQUITY_CURVE schema / invariant violation
    make_rng(run_id, salt)    deterministic numpy.random.Generator
    infer_periodicity(curve)  modal time-delta or AmbiguousPeriodicityError
    AmbiguousPeriodicityError raised by infer_periodicity on multi-modal input

    SALT_DSR_BOOTSTRAP        salt namespace constant for DSR
    SALT_PBO_PARTITION        salt namespace constant for PBO
    SALT_REGIME_PERTURB       salt namespace constant for regime perturbation

Schemas (8) live in `schema.py`:

    RUN_REGISTRY_SCHEMA        run index, lifecycle + atomic JSON metric snapshot
    BACKTEST_RESULT_SCHEMA     run header / audit metadata
    BACKTEST_LEG_SCHEMA        per-bar per-leg long format
    PER_SYMBOL_CONTRIB_SCHEMA  per-bar per-symbol P&L attribution
    EQUITY_CURVE_SCHEMA        scalar time series, gross + net returns
    OOS_RETURNS_PANEL_SCHEMA   long format for HRP, with alpha_active mask
    REGIME_METRICS_SCHEMA      per-regime stratified summary
    TRIAL_LOG_SCHEMA           append-only ledger for DSR n_trials

Pass criteria (PROJECT.md "Validation"):

    DSR > 0 with 95% CI
    PBO <= 0.5
    Bull AND bear regime each Sharpe >= 0
    Pre-ETF AND post-ETF each Sharpe >= 0
    Max DD <= 30%
    History >= 5 years (skill canon)

Sub-modules (filled in over A.4.2 - A.4.9):

    schema.py     polars schemas + path roots + gate thresholds (A.4.1)
    contracts.py  validate_equity_curve / make_rng / infer_periodicity /
                  re-exported get_archive_state                (A.4.1)
    metrics.py    Sharpe / Sortino / Calmar / DD / ...         (A.4.2)
    cv.py         PurgedKFold / WalkForwardCV / Expanding      (A.4.3)
    dsr.py        Deflated Sharpe Ratio + bootstrap CI         (A.4.4)
    pbo.py        Probability of Backtest Overfitting (CSCV)   (A.4.5)
    regime.py     market regime classification + stratify      (A.4.6)
    costs.py      fee tiers + slippage; funding sign helper    (A.4.7)
    registry.py   register_run / get_run / list_validated_alphas
                  / log_trial / check_corrections_diff         (A.4.8)
"""
from __future__ import annotations

from alpha_factory.validation.contracts import (
    SALT_DSR_BOOTSTRAP,
    SALT_PBO_PARTITION,
    SALT_REGIME_PERTURB,
    AmbiguousPeriodicityError,
    ArchiveState,
    canonical_params_hash,
    canonical_params_json,
    get_archive_state,
    infer_periodicity,
    make_rng,
    validate_equity_curve,
)

__all__ = [
    "SALT_DSR_BOOTSTRAP",
    "SALT_PBO_PARTITION",
    "SALT_REGIME_PERTURB",
    "AmbiguousPeriodicityError",
    "ArchiveState",
    "canonical_params_hash",
    "canonical_params_json",
    "get_archive_state",
    "infer_periodicity",
    "make_rng",
    "validate_equity_curve",
]
