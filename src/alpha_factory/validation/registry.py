"""L3 registry — single source of truth for run lifecycle, trial provenance,
and the L3->L4 contract.

Public API (10 callables + 3 exception classes):

    register_run(strategy_id, variant, params, universe, *, archive_state)
        -> str (run_id)
    get_run(run_id) -> dict | None
    transition_status(run_id, from_status, to_status,
                      *, verdict=None, metrics_summary=None,
                      corrections_diff_status=None) -> None
    list_validated_alphas(*, min_dsr=None, max_pbo=None,
                          min_max_dd=None) -> pl.DataFrame

    log_trial(strategy_id, params, *, evaluated_at, data_version,
              split, realized_sharpe, n_obs,
              overwrite=False, sharpe_tol=1e-9) -> None
    count_trials(strategy_id, *, data_version=None) -> int
    get_trials(strategy_id, *, data_version=None) -> pl.DataFrame

    get_oos_panel(alpha_names) -> pl.DataFrame
    check_corrections_diff(run_id) -> str

    StaleStatusError, TrialMismatchError, FrequencyMismatchError

Storage:

    data/.state/registry/runs.parquet                          RUN_REGISTRY index
    data/.state/registry/trial_log/strategy={id}/data.parquet  TRIAL_LOG per-strategy
    data/.state/registry/.runs.lock                            cross-strategy lock
    data/.state/registry/trial_log/strategy={id}/.lock         per-strategy trial lock
    data/validation/backtests/equity/year=YYYY/strategy={id}/data.parquet
                                                               EQUITY_CURVE (read-only)

DESIGN DECISIONS captured in this module are post the A.4.8 1-round
adversarial critique (registry-canon spine debate):

* `transition_status` enforces compare-and-swap (CAS) inside the lock:
  caller's `from_status` is checked against the row's actual current
  status under the lock; on mismatch raises `StaleStatusError`. The
  lock alone serializes WRITES but not the caller's read-decide-write
  sequence, so without CAS a concurrent `check_corrections_diff` could
  silently invalidate the caller's intent.

* `log_trial` rejects sharpe-mismatch on duplicate keys: re-logging
  `(strategy_id, params_hash, data_version, split)` with a different
  `realized_sharpe` (outside `sharpe_tol`) raises `TrialMismatchError`
  unless `overwrite=True` is passed. Silent no-op on mismatch would
  hide code-drift regressions on identical (params, data) inputs.

* `canonical_params_hash` (in contracts.py) coerces all numeric types
  to float, rejects tuples / NaN / Inf / non-JSON-primitives, and
  forces canonical separators -- pinning the hash recipe so
  TRIAL_LOG uniqueness holds across reruns and machines.

* `get_oos_panel` raises `FrequencyMismatchError` when the requested
  alphas span multiple periodicities (one alpha hourly, another daily).
  Silent outer-join on time would project null values that HRP at L4
  silently mishandles.

* `list_validated_alphas` reads under the registry lock so it sees a
  consistent snapshot relative to concurrent `transition_status` /
  `check_corrections_diff` writes. Total freshness across the L4 call
  boundary still requires the caller to refresh on the order of the
  data-correction cadence -- documented at the function level.

* File locks use the L1 archive.py staleness-fallback pattern. PID
  liveness via psutil + cross-platform `filelock` library deferred
  (per A.4.8 critique #6); at retail single-host scale, a 1h
  staleness window is sufficient and the failure mode (lost-write
  on simultaneous stale-break) is documented.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from alpha_factory.data.archive import get_archive_state
from alpha_factory.validation.contracts import (
    AmbiguousPeriodicityError,
    ArchiveState,
    canonical_params_hash,
    canonical_params_json,
    infer_periodicity,
)
from alpha_factory.validation.schema import (
    BACKTESTS_EQUITY_ROOT,
    CORRECTIONS_CLEAN,
    CORRECTIONS_DIRTY,
    CORRECTIONS_UNCHECKED,
    EQUITY_CURVE_SCHEMA,
    OOS_RETURNS_PANEL_SCHEMA,
    REGISTRY_ROOT,
    RUN_REGISTRY_PATH,
    RUN_REGISTRY_SCHEMA,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_VALIDATED,
    TRIAL_LOG_ROOT,
    TRIAL_LOG_SCHEMA,
    VALID_CORRECTIONS_STATUSES,
    VALID_STATUSES,
    VALID_TRIAL_SPLITS,
    VALID_VERDICTS,
)

log = logging.getLogger(__name__)

__all__ = [
    "FrequencyMismatchError",
    "StaleStatusError",
    "TrialMismatchError",
    "check_corrections_diff",
    "count_trials",
    "get_oos_panel",
    "get_run",
    "get_trials",
    "list_validated_alphas",
    "log_trial",
    "register_run",
    "transition_status",
]


# ── Constants ────────────────────────────────────────────────────────────


_RUN_REGISTRY_LOCK_PATH = REGISTRY_ROOT / ".runs.lock"
_LOCK_STALENESS_HOURS = 1
_LOCK_ACQUIRE_TIMEOUT_S = 30.0
_LOCK_POLL_INTERVAL_S = 0.05


# Allowed state-machine transitions per RUN_REGISTRY docstring.
_ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (STATUS_RUNNING, STATUS_COMPLETE),
    (STATUS_RUNNING, STATUS_FAILED),
    (STATUS_COMPLETE, STATUS_VALIDATED),
    (STATUS_COMPLETE, STATUS_RUNNING),    # corrections-diff trigger
    (STATUS_FAILED, STATUS_RUNNING),       # manual retry
    (STATUS_VALIDATED, STATUS_RUNNING),    # corrections-diff trigger
})


# ── Exception classes ───────────────────────────────────────────────────


class StaleStatusError(RuntimeError):
    """Raised when transition_status's CAS check fails.

    The caller-supplied `from_status` does not match the row's actual
    current status under the lock -- another process moved the row
    between the caller's get_run / decide / transition_status sequence.
    Caller should re-read and decide whether to retry.
    """


class TrialMismatchError(RuntimeError):
    """Raised when log_trial sees an existing key with a different sharpe.

    The (strategy_id, params_hash, data_version, split) key already
    exists in TRIAL_LOG with a different `realized_sharpe` (outside
    sharpe_tol). Indicates code drift on identical (params, data)
    inputs -- a regression. Pass `overwrite=True` for sanctioned
    recomputes.
    """


class FrequencyMismatchError(ValueError):
    """Raised when get_oos_panel sees alphas at different periodicities.

    HRP correlation matrix on misaligned time grids is silently wrong.
    Caller must explicitly resample alphas to a common frequency
    before requesting the panel.
    """


# ── Atomic write helper ──────────────────────────────────────────────────


def _atomic_write_parquet(df: pl.DataFrame, target: Path) -> None:
    """Write parquet via tmp-then-rename for crash safety."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    df.write_parquet(str(tmp))
    os.replace(tmp, target)


# ── File-based lock ──────────────────────────────────────────────────────


def _try_acquire_lock(lock_path: Path) -> bool:
    """Attempt to grab the lock; break stale (>1h) holds with a warning.

    Lock payload: {"pid": int, "start_ts": ISO datetime}.
    Returns True on successful acquisition.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            start_ts = datetime.fromisoformat(payload["start_ts"])
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            log.warning("lock %s unparseable; breaking", lock_path)
        else:
            stale = datetime.now(UTC) - start_ts > timedelta(
                hours=_LOCK_STALENESS_HOURS,
            )
            if not stale:
                return False
            log.warning(
                "lock %s stale (held since %s); breaking",
                lock_path, payload.get("start_ts"),
            )
    new_payload = json.dumps(
        {"pid": os.getpid(), "start_ts": datetime.now(UTC).isoformat()},
    )
    tmp = lock_path.parent / f"{lock_path.name}.tmp.{os.getpid()}"
    tmp.write_text(new_payload, encoding="utf-8")
    try:
        os.replace(tmp, lock_path)
    except OSError as e:
        log.warning("failed to swap lock %s: %s", lock_path, e)
        return False
    return True


def _release_lock(lock_path: Path) -> None:
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError as e:
            log.warning("failed to release lock %s: %s", lock_path, e)


@contextlib.contextmanager
def _registry_lock() -> Iterator[None]:
    """Cross-strategy exclusive lock for RUN_REGISTRY operations.

    Used by register_run / transition_status / list_validated_alphas /
    check_corrections_diff. Times out after _LOCK_ACQUIRE_TIMEOUT_S
    with RuntimeError.
    """
    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT_S
    while not _try_acquire_lock(_RUN_REGISTRY_LOCK_PATH):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"timed out acquiring registry lock {_RUN_REGISTRY_LOCK_PATH}",
            )
        time.sleep(_LOCK_POLL_INTERVAL_S)
    try:
        yield
    finally:
        _release_lock(_RUN_REGISTRY_LOCK_PATH)


def _trial_log_dir(strategy_id: str) -> Path:
    return TRIAL_LOG_ROOT / f"strategy={strategy_id}"


def _trial_log_path(strategy_id: str) -> Path:
    return _trial_log_dir(strategy_id) / "data.parquet"


@contextlib.contextmanager
def _trial_log_lock(strategy_id: str) -> Iterator[None]:
    """Per-strategy exclusive lock for TRIAL_LOG appends."""
    lock_path = _trial_log_dir(strategy_id) / ".lock"
    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT_S
    while not _try_acquire_lock(lock_path):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"timed out acquiring trial-log lock {lock_path}",
            )
        time.sleep(_LOCK_POLL_INTERVAL_S)
    try:
        yield
    finally:
        _release_lock(lock_path)


# ── RUN_REGISTRY load / save helpers ─────────────────────────────────────


def _load_runs() -> pl.DataFrame:
    """Read RUN_REGISTRY parquet; return empty schema-conforming frame if absent."""
    if not RUN_REGISTRY_PATH.exists():
        return pl.DataFrame(schema=RUN_REGISTRY_SCHEMA)
    return pl.read_parquet(str(RUN_REGISTRY_PATH))


def _save_runs(df: pl.DataFrame) -> None:
    """Atomic write to RUN_REGISTRY_PATH after schema cast."""
    casted = df.select(list(RUN_REGISTRY_SCHEMA.keys())).cast(RUN_REGISTRY_SCHEMA)
    _atomic_write_parquet(casted, RUN_REGISTRY_PATH)


def _load_trial_log(strategy_id: str) -> pl.DataFrame:
    path = _trial_log_path(strategy_id)
    if not path.exists():
        return pl.DataFrame(schema=TRIAL_LOG_SCHEMA)
    return pl.read_parquet(str(path))


def _save_trial_log(strategy_id: str, df: pl.DataFrame) -> None:
    casted = df.select(list(TRIAL_LOG_SCHEMA.keys())).cast(TRIAL_LOG_SCHEMA)
    _atomic_write_parquet(casted, _trial_log_path(strategy_id))


# ── register_run ─────────────────────────────────────────────────────────


def register_run(
    strategy_id: str,
    variant: str,
    params: dict,
    universe: list[str],
    *,
    archive_state: ArchiveState,
) -> str:
    """Append a new RUN_REGISTRY row, status=running, return run_id (UUID v4).

    `params` is canonicalized + hashed via `contracts.canonical_params_hash`;
    the resulting hex hash lands in `params_hash`.

    `universe` is currently NOT stored in RUN_REGISTRY (the schema does
    not carry it -- BACKTEST_RESULT_SCHEMA does). Reserved for future
    use to support cross-row universe-aware queries; for now, callers
    must populate BACKTEST_RESULT separately.

    `archive_state` is the snapshot at run start; provides
    `data_version`, `archive_max_kline_time`, `qc_audit_run_ts`.
    """
    if not strategy_id:
        raise ValueError("strategy_id must be non-empty")
    if archive_state is None:
        raise ValueError("archive_state must be provided (call get_archive_state())")
    # Validate universe shape (not stored but caller hygiene check).
    if not isinstance(universe, list) or any(not isinstance(s, str) for s in universe):
        raise TypeError("universe must be list[str]")

    run_id = str(uuid.uuid4())
    params_hash = canonical_params_hash(params)
    new_row = {
        "run_id": run_id,
        "strategy_id": strategy_id,
        "variant": variant,
        "params_hash": params_hash,
        "data_version": archive_state.data_version,
        "archive_max_kline_time": archive_state.archive_max_kline_time,
        "qc_audit_run_ts": archive_state.qc_audit_run_ts,
        "registered_at": datetime.now(UTC),
        "status": STATUS_RUNNING,
        "verdict": None,
        "corrections_diff_status": CORRECTIONS_UNCHECKED,
        "metrics_summary_json": None,
    }

    with _registry_lock():
        existing = _load_runs()
        new_df = pl.DataFrame([new_row]).cast(RUN_REGISTRY_SCHEMA)
        combined = (
            pl.concat([existing, new_df]) if existing.height > 0 else new_df
        )
        _save_runs(combined)

    return run_id


# ── get_run ──────────────────────────────────────────────────────────────


def get_run(run_id: str) -> dict | None:
    """Return RUN_REGISTRY row as dict, or None if not found.

    Read-only; does NOT acquire the registry lock. Caller seeking
    a consistent snapshot should use `list_validated_alphas` (which
    locks) or wrap the read in their own short-lived lock.
    """
    df = _load_runs()
    if df.height == 0:
        return None
    match = df.filter(pl.col("run_id") == run_id)
    if match.height == 0:
        return None
    return match.row(0, named=True)


# ── transition_status (CAS) ──────────────────────────────────────────────


def _is_valid_transition(from_status: str, to_status: str) -> bool:
    return (from_status, to_status) in _ALLOWED_TRANSITIONS


def transition_status(
    run_id: str,
    from_status: str,
    to_status: str,
    *,
    verdict: str | None = None,
    metrics_summary: dict[str, Any] | None = None,
    corrections_diff_status: str | None = None,
) -> None:
    """Compare-and-swap row's status; validate transition; atomic write.

    Rules:
        - `to_status` must be in VALID_STATUSES
        - `(from_status, to_status)` must be in _ALLOWED_TRANSITIONS
        - Inside the lock, row's actual status is re-read; if it does
          NOT equal `from_status`, raise StaleStatusError (CAS failure).
        - `verdict` (when not None) must be in VALID_VERDICTS.
        - `metrics_summary` (dict, when not None) is canonical-JSON-
          encoded into RUN_REGISTRY.metrics_summary_json. ONLY allowed
          on the `complete -> validated` transition (other transitions
          either don't have metrics yet or shouldn't change them).
        - `corrections_diff_status` (when not None) must be in
          VALID_CORRECTIONS_STATUSES; updated alongside the status.
    """
    if to_status not in VALID_STATUSES:
        raise ValueError(
            f"to_status {to_status!r} not in {VALID_STATUSES}",
        )
    if from_status not in VALID_STATUSES:
        raise ValueError(
            f"from_status {from_status!r} not in {VALID_STATUSES}",
        )
    if not _is_valid_transition(from_status, to_status):
        raise ValueError(
            f"transition {from_status!r} -> {to_status!r} not allowed; "
            f"see _ALLOWED_TRANSITIONS",
        )
    if verdict is not None and verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict {verdict!r} not in {VALID_VERDICTS}")
    if metrics_summary is not None and not (
        from_status == STATUS_COMPLETE and to_status == STATUS_VALIDATED
    ):
        raise ValueError(
            "metrics_summary is only writable on the "
            "complete -> validated transition",
        )
    if (
        corrections_diff_status is not None
        and corrections_diff_status not in VALID_CORRECTIONS_STATUSES
    ):
        raise ValueError(
            f"corrections_diff_status {corrections_diff_status!r} not in "
            f"{VALID_CORRECTIONS_STATUSES}",
        )

    with _registry_lock():
        df = _load_runs()
        if df.height == 0:
            raise ValueError(f"registry empty; run_id {run_id!r} not found")
        match = df.filter(pl.col("run_id") == run_id)
        if match.height == 0:
            raise ValueError(f"run_id {run_id!r} not found in registry")

        actual_status = match["status"][0]
        if actual_status != from_status:
            raise StaleStatusError(
                f"CAS failed for run_id {run_id!r}: caller expected "
                f"status {from_status!r}, found {actual_status!r}",
            )

        rows = df.to_dicts()
        for r in rows:
            if r["run_id"] == run_id:
                r["status"] = to_status
                if verdict is not None:
                    r["verdict"] = verdict
                if metrics_summary is not None:
                    r["metrics_summary_json"] = json.dumps(
                        metrics_summary,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                if corrections_diff_status is not None:
                    r["corrections_diff_status"] = corrections_diff_status

        _save_runs(pl.DataFrame(rows))


# ── list_validated_alphas ────────────────────────────────────────────────


def list_validated_alphas(
    *,
    min_dsr: float | None = None,
    max_pbo: float | None = None,
    min_max_dd: float | None = None,
) -> pl.DataFrame:
    """Validated alpha rows passing optional thresholds; latest run per strategy.

    Filter chain:
        1. status == 'validated'
        2. corrections_diff_status == 'clean'
        3. metrics_summary_json parses; thresholds (when not None) hold:
           - dsr >= min_dsr
           - pbo <= max_pbo
           - max_dd >= min_max_dd  (DD is negative; -0.30 means
                                    "shallower than or equal to 30%")
        4. For each strategy_id, return the LATEST row by registered_at.

    Acquires the registry lock to ensure consistent snapshot relative
    to concurrent transition_status / check_corrections_diff. Caller
    note: total freshness across the L4 invocation boundary still
    requires the caller to refresh on the data-correction cadence.

    Rows whose metrics_summary_json is missing or malformed are SKIPPED
    (defensive: a partial recovery from a crash should not poison the
    L4 alpha selection).

    Returns a DataFrame conforming to RUN_REGISTRY_SCHEMA (possibly
    empty).
    """
    with _registry_lock():
        df = _load_runs()
        if df.height == 0:
            return pl.DataFrame(schema=RUN_REGISTRY_SCHEMA)

        filt = df.filter(
            (pl.col("status") == STATUS_VALIDATED)
            & (pl.col("corrections_diff_status") == CORRECTIONS_CLEAN),
        )

        if filt.height == 0:
            return pl.DataFrame(schema=RUN_REGISTRY_SCHEMA)

        kept_rows: list[dict] = []
        for r in filt.iter_rows(named=True):
            mj = r.get("metrics_summary_json")
            if mj is None:
                continue
            try:
                metrics = json.loads(mj)
            except (json.JSONDecodeError, TypeError):
                continue
            if min_dsr is not None and metrics.get("dsr", float("-inf")) < min_dsr:
                continue
            if max_pbo is not None and metrics.get("pbo", float("inf")) > max_pbo:
                continue
            if min_max_dd is not None and (
                metrics.get("max_dd", float("-inf")) < min_max_dd
            ):
                continue
            kept_rows.append(r)

        if not kept_rows:
            return pl.DataFrame(schema=RUN_REGISTRY_SCHEMA)

        kept = pl.DataFrame(kept_rows).cast(RUN_REGISTRY_SCHEMA)
        # Latest per strategy_id by registered_at
        return (
            kept.sort("registered_at", descending=True)
            .group_by("strategy_id", maintain_order=True)
            .first()
        )


# ── log_trial ────────────────────────────────────────────────────────────


def log_trial(
    strategy_id: str,
    params: dict,
    *,
    evaluated_at: datetime,
    data_version: str,
    split: str,
    realized_sharpe: float,
    n_obs: int,
    overwrite: bool = False,
    sharpe_tol: float = 1e-9,
) -> None:
    """Append a TRIAL_LOG row, idempotent on uniqueness key.

    Uniqueness key: `(strategy_id, params_hash, data_version, split)`.
    Behaviour on duplicate key:
        - same `realized_sharpe` (within `sharpe_tol`) -> silent no-op
        - different `realized_sharpe` AND `overwrite=False` (default)
          -> raise TrialMismatchError
        - different `realized_sharpe` AND `overwrite=True`
          -> replace the existing row in-place (preserves trial_id)

    `trial_id` is monotonic per `strategy_id`: max(trial_id) + 1
    on append. Per-strategy lock prevents trial_id collisions under
    concurrent appends.
    """
    if split not in VALID_TRIAL_SPLITS:
        raise ValueError(f"split {split!r} not in {VALID_TRIAL_SPLITS}")
    if not isinstance(n_obs, int) or isinstance(n_obs, bool) or n_obs < 0:
        raise ValueError(f"n_obs must be non-negative int, got {n_obs!r}")
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be tz-aware")

    params_hash = canonical_params_hash(params)
    params_json_canonical = canonical_params_json(params)

    with _trial_log_lock(strategy_id):
        existing = _load_trial_log(strategy_id)

        if existing.height > 0:
            dup_mask = (
                (pl.col("params_hash") == params_hash)
                & (pl.col("data_version") == data_version)
                & (pl.col("split") == split)
            )
            dup = existing.filter(dup_mask)
            if dup.height > 0:
                existing_sharpe = float(dup["realized_sharpe"][0])
                delta = abs(existing_sharpe - realized_sharpe)
                if delta <= sharpe_tol:
                    return                  # idempotent no-op
                if not overwrite:
                    raise TrialMismatchError(
                        f"trial key (strategy={strategy_id}, "
                        f"params_hash={params_hash[:8]}, "
                        f"data_version={data_version}, split={split}) "
                        f"exists with realized_sharpe={existing_sharpe:.6f} "
                        f"but new value is {realized_sharpe:.6f} "
                        f"(delta={delta:.2e}). Pass overwrite=True for "
                        f"sanctioned recomputes.",
                    )
                # Overwrite path: drop the duplicate row, fall through
                # to the append below (trial_id allocation continues
                # monotonic).
                existing = existing.filter(~dup_mask)

        next_trial_id = (
            int(existing["trial_id"].max() + 1) if existing.height > 0 else 1
        )
        new_row = {
            "strategy_id": strategy_id,
            "trial_id": next_trial_id,
            "params_hash": params_hash,
            "params_json": params_json_canonical,
            "evaluated_at": evaluated_at,
            "data_version": data_version,
            "split": split,
            "realized_sharpe": float(realized_sharpe),
            "n_obs": int(n_obs),
        }
        new_df = pl.DataFrame([new_row]).cast(TRIAL_LOG_SCHEMA)
        combined = (
            pl.concat([existing, new_df]) if existing.height > 0 else new_df
        )
        _save_trial_log(strategy_id, combined)


# ── count_trials / get_trials ────────────────────────────────────────────


def count_trials(
    strategy_id: str,
    *,
    data_version: str | None = None,
) -> int:
    """COUNT(distinct params_hash) for `strategy_id`. DSR n_trials reads here."""
    df = _load_trial_log(strategy_id)
    if df.height == 0:
        return 0
    if data_version is not None:
        df = df.filter(pl.col("data_version") == data_version)
    return df["params_hash"].n_unique()


def get_trials(
    strategy_id: str,
    *,
    data_version: str | None = None,
) -> pl.DataFrame:
    """Return all TRIAL_LOG rows for `strategy_id` (optionally data_version filter)."""
    df = _load_trial_log(strategy_id)
    if data_version is not None:
        df = df.filter(pl.col("data_version") == data_version)
    return df


# ── check_corrections_diff ───────────────────────────────────────────────


def check_corrections_diff(run_id: str) -> str:
    """Compare run.data_version against current archive state.

    Updates RUN_REGISTRY.corrections_diff_status to 'clean' (data
    unchanged) or 'dirty' (data changed -> re-validation needed).
    Does NOT auto-transition status to 'running' -- caller (the
    re-validation pipeline) decides. Returns the new
    corrections_diff_status.

    Atomic update under registry lock.
    """
    current = get_archive_state()

    with _registry_lock():
        df = _load_runs()
        if df.height == 0:
            raise ValueError(f"registry empty; run_id {run_id!r} not found")
        match = df.filter(pl.col("run_id") == run_id)
        if match.height == 0:
            raise ValueError(f"run_id {run_id!r} not found in registry")

        recorded_dv = match["data_version"][0]
        new_status = (
            CORRECTIONS_CLEAN if recorded_dv == current.data_version
            else CORRECTIONS_DIRTY
        )

        rows = df.to_dicts()
        for r in rows:
            if r["run_id"] == run_id:
                r["corrections_diff_status"] = new_status
        _save_runs(pl.DataFrame(rows))

    return new_status


# ── get_oos_panel ────────────────────────────────────────────────────────


def _load_equity_curve_for_run(run_id: str) -> pl.DataFrame:
    """Load EQUITY_CURVE rows scoped to one run_id, across all year partitions."""
    run = get_run(run_id)
    if run is None:
        raise ValueError(f"run_id {run_id!r} not in registry")
    strategy_id = run["strategy_id"]
    files = list(BACKTESTS_EQUITY_ROOT.glob(
        f"year=*/strategy={strategy_id}/data.parquet",
    ))
    if not files:
        return pl.DataFrame(schema=EQUITY_CURVE_SCHEMA)
    df = pl.read_parquet([str(f) for f in files])
    return df.filter(pl.col("run_id") == run_id).sort("time")


def get_oos_panel(alpha_names: list[str]) -> pl.DataFrame:
    """Build OOS_RETURNS_PANEL across alphas (long format with alpha_active mask).

    For each alpha name, find the latest validated run via
    `list_validated_alphas` and load its EQUITY_CURVE. Compute the
    union time grid; for each (alpha, time):
        if time in alpha's curve: oos_ret = period_ret_net, alpha_active=True
        else:                     oos_ret = 0.0,             alpha_active=False

    Args:
        alpha_names: list of strategy_id strings to include in the panel

    Raises:
        ValueError: if any alpha has no validated run, or empty
            EQUITY_CURVE for the latest validated run
        FrequencyMismatchError: if alphas span multiple periodicities;
            HRP correlation matrix on misaligned grids is silently
            wrong, so this is a hard error
        AmbiguousPeriodicityError: if any alpha's curve has no clear
            modal time delta (mixed-periodicity input)

    Returns:
        DataFrame conforming to OOS_RETURNS_PANEL_SCHEMA.
    """
    if not alpha_names:
        return pl.DataFrame(schema=OOS_RETURNS_PANEL_SCHEMA)
    if len(alpha_names) != len(set(alpha_names)):
        raise ValueError(f"duplicate alpha_names: {alpha_names}")

    validated = list_validated_alphas()
    selected: list[tuple[str, str]] = []
    for alpha in alpha_names:
        rows = validated.filter(pl.col("strategy_id") == alpha)
        if rows.height == 0:
            raise ValueError(
                f"no validated run for alpha {alpha!r} (status='validated' "
                f"AND corrections_diff_status='clean')",
            )
        selected.append((alpha, rows.row(0, named=True)["run_id"]))

    curves: list[tuple[str, pl.DataFrame]] = []
    freqs: set[float] = set()
    for alpha, run_id in selected:
        curve = _load_equity_curve_for_run(run_id)
        if curve.height == 0:
            raise ValueError(
                f"empty EQUITY_CURVE for alpha={alpha!r} run_id={run_id!r}",
            )
        try:
            freq = infer_periodicity(curve)
        except AmbiguousPeriodicityError as e:
            raise FrequencyMismatchError(
                f"alpha {alpha!r} has ambiguous periodicity ({e}); "
                f"caller must reshape to a single periodicity before "
                f"requesting the panel",
            ) from e
        freqs.add(freq.total_seconds())
        curves.append((alpha, curve))

    if len(freqs) > 1:
        raise FrequencyMismatchError(
            f"alpha periodicities (in seconds) differ: {sorted(freqs)}; "
            f"caller must explicitly resample alphas to a common "
            f"frequency before requesting the panel",
        )

    all_times: list[datetime] = sorted({
        t for _, c in curves for t in c["time"].to_list()
    })

    rows: list[dict] = []
    for alpha, curve in curves:
        time_to_ret: dict[datetime, float] = dict(zip(
            curve["time"].to_list(),
            curve["period_ret_net"].to_list(),
            strict=True,
        ))
        for t in all_times:
            active = t in time_to_ret
            rows.append({
                "alpha_id": alpha,
                "time": t,
                "oos_ret": time_to_ret.get(t, 0.0),
                "alpha_active": active,
            })

    return pl.DataFrame(rows).cast(OOS_RETURNS_PANEL_SCHEMA)
