"""Archive orchestrator — universe-aware fetch + QC for Phase A L1.

Ties together universe.py + klines.py + funding.py + qc.py into one
runnable pipeline. Designed to be invoked manually OR from a daily cron
(Oracle ARM, per PROJECT.md "L1 production").

CLI
---
    python -m alpha_factory.data.archive --snapshot YYYY-MM
        [--force-cold-start]
        [--max-tasks N]
        [--task-types perp_usdt,spot,perp_funding]
        [--end ISO]
        [--skip-fetch] [--skip-qc]

Behavior summary (post 2-round adversarial debate):

* RUN LOCK: data/.state/locks/run.lock holds (pid, start_ts, hostname).
  Refuses to start if file exists AND start_ts is < 24h old (staleness
  fallback; PID liveness verification deferred to production-hardening
  in Phase A.5 -- avoids psutil dependency for now).

* CIRCUIT BREAKER: data/.state/locks/circuit_breaker.json holds
  (tripped_at, retry_after, reason). Read at startup; if `now <
  retry_after`, exit 11 immediately. Updated atomically on
  BinanceRateLimitError. Cleared on clean run completion.

* LISTING-DATE CACHE: data/.state/cache/listing_dates.json wraps
  {schema_version, entries}. Avoids re-probing spot listing dates on
  every cold-start (~50 API calls saved). Schema-version mismatch -> drop.

* STATE LAYOUT split into locks/ (load-bearing -- breaker + run-lock)
  vs cache/ (regenerable -- listing dates). Operator can rm -rf
  data/.state/cache/ safely; rm -rf data/.state/locks/ removes the
  breaker that's protecting them from re-ban.

* TASK EXPANSION + ATOMICITY: per universe row (sorted by rank), emit
  {1× perp + 0..N spot per spot_pairs + 1× funding}. PER-SYMBOL ATOMICITY
  -- complete all of symbol_i's tasks before symbol_{i+1} -- prevents
  --max-tasks from leaving jagged states (4/5 spots fetched, c1 ERRORs
  next run).

* TASK FILTERS: --max-tasks N caps total task count; --task-types filters
  to subset (operator splits cold-start across runs to fit weight budget).

* RESUME: per task, start = last_archived + 1bar if archive exists; else
  effective_listing_date from cache; else SKIP unless --force-cold-start.
  Cold-start without flag is REFUSED to prevent accidental ~2k API call
  burn from a misconfigured cron.

* PER-TASK FAILURE ISOLATION:
    BinanceRateLimitError -> trip breaker, abort remaining tasks, exit 11.
    BinanceAPIError (non-rate-limit) -> log per-task, continue.
    ValueError / unexpected -> log, continue.
    Final exit: 0 / 10 / 11 (not 2 -- POSIX reserves 2 for usage error).

* POST-FETCH QC: load full archive, build listing_dates dict from cache +
  effective_listing_date helpers, call qc.compute_qc_results +
  qc.write_qc_audit. ERRORs grouped by category in summary.

DEFERRED (per debate, OPTIONAL):
* PID liveness via psutil (R2-1).
* "Archive ready for strategy {carry, basis_mr, momentum}" semantic
  rollup (R2-5) -- belongs in validation/ layer.
* Signal cleanup matrix doc table (R2-7) -- atexit cleanup is best-effort.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import hashlib
import json
import logging
import os
import platform
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.data import funding as funding_mod
from alpha_factory.data import klines as klines_mod
from alpha_factory.data import qc as qc_mod
from alpha_factory.data.binance_client import (
    BinanceAPIError,
    BinanceClient,
    BinanceRateLimitError,
)
from alpha_factory.data.schema import (
    DATA_ROOT,
    FUNDING_ROOT,
    KLINES_ROOT,
)
from alpha_factory.data.universe import read_snapshot

log = logging.getLogger(__name__)

# ── Exit codes ────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_FAIL = 10                # any non-rate-limit failure (per-task or QC)
EXIT_RATE_LIMITED = 11        # circuit breaker tripped or already-tripped

# ── State layout ──────────────────────────────────────────────────────────
STATE_DIR = DATA_ROOT / ".state"
LOCK_DIR = STATE_DIR / "locks"            # load-bearing: breaker + run-lock
CACHE_DIR = STATE_DIR / "cache"           # regenerable: listing-date probes
RUN_LOCK_FILE = LOCK_DIR / "run.lock"
BREAKER_FILE = LOCK_DIR / "circuit_breaker.json"
LISTING_CACHE_FILE = CACHE_DIR / "listing_dates.json"
QC_AUDIT_DIR = DATA_ROOT / "qc_runs"

LISTING_CACHE_SCHEMA_VERSION = 1
RUN_LOCK_STALENESS_HOURS = 24
BREAKER_DEFAULT_HOURS = 24

VALID_TASK_TYPES = ("perp_usdt", "spot", "perp_funding")


# ── Atomic JSON helpers ───────────────────────────────────────────────────


def _atomic_write_json(payload: dict, target: Path) -> None:
    """Write JSON via tmp + os.replace -- crash mid-write cannot truncate."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


# ── Run lock ──────────────────────────────────────────────────────────────


def acquire_run_lock(lock_file: Path = RUN_LOCK_FILE) -> bool:
    """Try to acquire the cross-process run lock.

    Returns True on success, False if another live run holds it.
    Stale locks (start_ts older than RUN_LOCK_STALENESS_HOURS) are broken
    with a warning -- assumed crash without atexit cleanup.

    NOTE: PID liveness via psutil deferred -- staleness fallback is
    sufficient for current scale (manual + daily cron). Production-hardened
    PID-create-time verification is a Phase A.5 concern.
    """
    if lock_file.exists():
        existing = _read_json(lock_file)
        if existing is None:
            log.warning("lock file unparseable, breaking it")
        else:
            try:
                start_ts = datetime.fromisoformat(existing["start_ts"])
            except (KeyError, ValueError):
                start_ts = None
            stale_age = (
                datetime.now(UTC) - start_ts
                if start_ts is not None else timedelta(days=999)
            )
            if start_ts is not None and stale_age < timedelta(hours=RUN_LOCK_STALENESS_HOURS):
                log.error(
                    "run lock held by pid=%s on %s since %s -- aborting",
                    existing.get("pid"), existing.get("hostname"),
                    existing.get("start_ts"),
                )
                return False
            log.warning(
                "stale lock from pid=%s, %s -- breaking",
                existing.get("pid"), existing.get("start_ts"),
            )
    payload = {
        "pid": os.getpid(),
        "start_ts": datetime.now(UTC).isoformat(),
        "hostname": platform.node(),
    }
    _atomic_write_json(payload, lock_file)
    return True


def release_run_lock(lock_file: Path = RUN_LOCK_FILE) -> None:
    if lock_file.exists():
        try:
            lock_file.unlink()
        except OSError as e:
            log.warning("could not release lock %s: %s", lock_file, e)


# ── Circuit breaker ───────────────────────────────────────────────────────


def is_breaker_tripped(
    breaker_file: Path = BREAKER_FILE, now: datetime | None = None,
) -> tuple[bool, dict | None]:
    """Returns (is_tripped, payload). Tripped == retry_after is in the future."""
    payload = _read_json(breaker_file)
    if payload is None:
        return False, None
    now = now or datetime.now(UTC)
    try:
        retry_after = datetime.fromisoformat(payload["retry_after"])
    except (KeyError, ValueError):
        return False, payload
    return now < retry_after, payload


def trip_breaker(
    reason: str, hours: int = BREAKER_DEFAULT_HOURS,
    breaker_file: Path = BREAKER_FILE, now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    payload = {
        "tripped_at": now.isoformat(),
        "retry_after": (now + timedelta(hours=hours)).isoformat(),
        "reason": reason,
    }
    _atomic_write_json(payload, breaker_file)
    log.error("circuit breaker tripped for %dh: %s", hours, reason)


def clear_breaker(breaker_file: Path = BREAKER_FILE) -> None:
    if breaker_file.exists():
        try:
            breaker_file.unlink()
        except OSError as e:
            log.warning("could not clear breaker %s: %s", breaker_file, e)


# ── Listing-date cache ────────────────────────────────────────────────────


def load_listing_cache(
    cache_file: Path = LISTING_CACHE_FILE,
) -> dict[tuple[str, str], date]:
    """Load (symbol, market) -> date map. Returns {} on schema mismatch / missing."""
    payload = _read_json(cache_file)
    if payload is None:
        return {}
    if payload.get("schema_version") != LISTING_CACHE_SCHEMA_VERSION:
        log.warning(
            "listing-date cache schema=%s != expected %d -- discarding",
            payload.get("schema_version"), LISTING_CACHE_SCHEMA_VERSION,
        )
        return {}
    out: dict[tuple[str, str], date] = {}
    for k, v in payload.get("entries", {}).items():
        try:
            sym, mkt = k.split(":", 1)
            out[(sym, mkt)] = date.fromisoformat(v)
        except ValueError:
            continue
    return out


def save_listing_cache(
    cache: dict[tuple[str, str], date], cache_file: Path = LISTING_CACHE_FILE,
) -> None:
    entries = {f"{sym}:{mkt}": d.isoformat() for (sym, mkt), d in cache.items()}
    payload = {"schema_version": LISTING_CACHE_SCHEMA_VERSION, "entries": entries}
    _atomic_write_json(payload, cache_file)


# ── Task model + expansion ────────────────────────────────────────────────


@dataclass(frozen=True)
class Task:
    symbol: str          # canonical "BTC-USDT"
    market: str          # "perp_usdt" | "spot" | "perp_funding"
    rank: int            # universe rank for tier classification
    snapshot_listing_date: date | None  # from universe row (perp); None for spot


def expand_universe_to_tasks(universe: pl.DataFrame) -> list[Task]:
    """Per-symbol atomicity: emit perp + spots + funding for each row in rank order."""
    tasks: list[Task] = []
    for row in universe.sort("rank").iter_rows(named=True):
        rank = int(row["rank"])
        sym = row["symbol"]
        base = row["base_asset"]
        snap_listing: date | None = row["listing_date"]

        # perp
        tasks.append(Task(sym, "perp_usdt", rank, snap_listing))
        # spots (one per pair in spot_pairs; canonicalize "BTCUSDT" -> "BTC-USDT")
        for spot_api in row["spot_pairs"]:
            quote = spot_api[len(base):]
            canonical = f"{base}-{quote}"
            tasks.append(Task(canonical, "spot", rank, None))
        # funding (perp only)
        tasks.append(Task(sym, "perp_funding", rank, snap_listing))
    return tasks


# ── Per-task fetch ────────────────────────────────────────────────────────


def run_one_task(
    task: Task,
    end_dt: datetime,
    force_cold_start: bool,
    listing_cache: dict[tuple[str, str], date],
    client: BinanceClient,
) -> tuple[str, int]:
    """Execute one fetch task; updates listing_cache as a side effect.

    Returns (outcome, n_rows). Outcomes:
        "done"               -- fetched + wrote
        "skipped_no_resume"  -- archive empty + no --force-cold-start
        "nothing"            -- start >= end (already fully archived)
        "skipped_no_data"    -- spot probe returned None (symbol has no klines)
    Raises:
        BinanceRateLimitError -- caller should trip breaker + abort run
        BinanceAPIError / ValueError / KeyError -- caller catches per-task
    """
    if task.market == "perp_funding":
        return _run_funding_task(task, end_dt, force_cold_start, listing_cache, client)
    return _run_kline_task(task, end_dt, force_cold_start, listing_cache, client)


def _run_funding_task(
    task: Task,
    end_dt: datetime,
    force_cold_start: bool,
    listing_cache: dict[tuple[str, str], date],
    client: BinanceClient,
) -> tuple[str, int]:
    last = funding_mod.last_archived_funding_time(task.symbol)
    if last is not None:
        start_dt = last + timedelta(milliseconds=1)
    elif not force_cold_start:
        return "skipped_no_resume", 0
    else:
        cached = listing_cache.get((task.symbol, "perp_usdt")) or task.snapshot_listing_date
        if cached is None:
            return "skipped_no_data", 0
        start_dt = datetime.combine(cached, datetime.min.time(), tzinfo=UTC)

    if start_dt >= end_dt:
        return "nothing", 0
    df = funding_mod.fetch_funding(task.symbol, start_dt, end_dt, client)
    funding_mod.write_funding_partitioned(df)
    return "done", df.height


def _run_kline_task(
    task: Task,
    end_dt: datetime,
    force_cold_start: bool,
    listing_cache: dict[tuple[str, str], date],
    client: BinanceClient,
) -> tuple[str, int]:
    last = klines_mod.last_archived_open_time(task.symbol, task.market)
    if last is not None:
        start_dt = last + timedelta(hours=1)
    elif not force_cold_start:
        return "skipped_no_resume", 0
    else:
        cached = listing_cache.get((task.symbol, task.market))
        if cached is None:
            if task.market == "perp_usdt":
                cached = task.snapshot_listing_date
            elif task.market == "spot":
                probe = klines_mod.probe_first_bar(task.symbol, task.market, client)
                if probe is None:
                    return "skipped_no_data", 0
                cached = probe.date()
            if cached is not None:
                listing_cache[(task.symbol, task.market)] = cached
        if cached is None:
            return "skipped_no_data", 0
        start_dt = datetime.combine(cached, datetime.min.time(), tzinfo=UTC)

    if start_dt >= end_dt:
        return "nothing", 0
    df = klines_mod.fetch_klines(task.symbol, task.market, start_dt, end_dt, client)
    klines_mod.write_klines_partitioned(df)
    return "done", df.height


# ── Archive load helpers (for QC) ─────────────────────────────────────────


def load_klines_archive(root: Path = KLINES_ROOT) -> pl.DataFrame:
    if not root.exists():
        return pl.DataFrame()
    files = list(root.glob("year=*/data.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.read_parquet([str(f) for f in files])


def load_funding_archive(root: Path = FUNDING_ROOT) -> pl.DataFrame:
    if not root.exists():
        return pl.DataFrame()
    files = list(root.glob("year=*/data.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.read_parquet([str(f) for f in files])


# ── Archive state snapshot (consumed by L3 validation) ────────────────────


@dataclass(frozen=True)
class ArchiveState:
    """Reproducibility snapshot of the L1 archive at a point in time.

    Recorded on every backtest run (see validation/contracts.py and
    BACKTEST_RESULT_SCHEMA) and every TRIAL_LOG entry so that future
    audits can reproduce what data was visible at the time of validation.

    `data_version` is a stable string id composed of:
        - max kline open_time at minute resolution ("YYYY-MM-DDTHH:MM")
        - "+" separator
        - corrections-sidecar fingerprint (first 8 hex of SHA-256 over
          sorted correction filenames; "nocorr" if none)

    Two archive states with identical kline-max-time but different
    corrections sidecars get distinct fingerprints, so re-validating a
    run after a corrections-diff produces a fresh data_version (this is
    exactly the trigger A.4.8 registry.check_corrections_diff watches
    for).

    Full archive SHA over all parquet bytes is deferred to A.5; the
    timestamp + corrections fingerprint composite is sufficient for
    reproducibility at our scale.
    """

    data_version: str
    archive_max_kline_time: datetime | None
    qc_audit_run_ts: datetime | None


def _corrections_fingerprint(
    klines_root: Path = KLINES_ROOT,
    funding_root: Path = FUNDING_ROOT,
) -> str:
    """SHA-256 hex (first 8 chars) over sorted correction filenames.

    Returns "nocorr" when no corrections sidecar files exist under
    either kline_root/_corrections or funding_root/_corrections.
    """
    files: list[str] = []
    for root in (klines_root / "_corrections", funding_root / "_corrections"):
        if root.exists():
            files.extend(sorted(p.name for p in root.glob("correction_*.parquet")))
    if not files:
        return "nocorr"
    h = hashlib.sha256()
    h.update("|".join(files).encode("utf-8"))
    return h.hexdigest()[:8]


def get_archive_state(
    klines_root: Path = KLINES_ROOT,
    funding_root: Path = FUNDING_ROOT,
    qc_audit_dir: Path = QC_AUDIT_DIR,
) -> ArchiveState:
    """Snapshot the archive's reproducibility state at call time.

    Returns:
        ArchiveState(data_version, archive_max_kline_time, qc_audit_run_ts)

    `archive_max_kline_time` is the max open_time across both spot and
    perp_usdt klines in the archive, or None if archive is empty.

    `qc_audit_run_ts` is the timestamp of the most recent qc_runs/
    file (parsed from `qc_run_<unix_us>.parquet`), or None if no qc
    audit has been run.

    Re-exported into `alpha_factory.validation.contracts` so that the
    L3 validation layer can call this without importing L1 internals
    directly.
    """
    df = load_klines_archive(klines_root)
    max_kline_time = df["open_time"].max() if df.height > 0 else None

    qc_audit_ts: datetime | None = None
    if qc_audit_dir.exists():
        qc_files = sorted(qc_audit_dir.glob("qc_run_*.parquet"))
        if qc_files:
            try:
                ts_us = int(qc_files[-1].stem.split("_")[-1])
                qc_audit_ts = datetime.fromtimestamp(ts_us / 1e6, tz=UTC)
            except (ValueError, IndexError):
                qc_audit_ts = None

    corr_fp = _corrections_fingerprint(klines_root, funding_root)
    if max_kline_time is None:
        data_version = f"empty+{corr_fp}"
    else:
        data_version = f"{max_kline_time.strftime('%Y-%m-%dT%H:%M')}+{corr_fp}"

    return ArchiveState(
        data_version=data_version,
        archive_max_kline_time=max_kline_time,
        qc_audit_run_ts=qc_audit_ts,
    )


# ── Orchestrator ──────────────────────────────────────────────────────────


@dataclass
class TaskResult:
    task: Task
    outcome: str           # "done" | "skipped_no_resume" | "nothing" | "skipped_no_data" | "error"
    n_rows: int = 0
    error: str | None = None


def run_fetch_phase(
    tasks: list[Task],
    end_dt: datetime,
    force_cold_start: bool,
    listing_cache: dict[tuple[str, str], date],
    client: BinanceClient,
) -> tuple[list[TaskResult], bool]:
    """Run all fetch tasks. Returns (results, rate_limited)."""
    results: list[TaskResult] = []
    rate_limited = False
    for task in tasks:
        try:
            outcome, n_rows = run_one_task(
                task, end_dt, force_cold_start, listing_cache, client,
            )
            results.append(TaskResult(task, outcome, n_rows))
            log.info("task %s/%s -> %s (%d rows)", task.symbol, task.market, outcome, n_rows)
        except BinanceRateLimitError as e:
            results.append(TaskResult(task, "error", error=f"rate_limit: {e}"))
            log.error("RATE LIMIT on %s/%s -- aborting run", task.symbol, task.market)
            rate_limited = True
            break
        except (BinanceAPIError, ValueError, KeyError) as e:
            results.append(TaskResult(task, "error", error=str(e)))
            log.error("task %s/%s failed: %s", task.symbol, task.market, e)
    return results, rate_limited


def build_listing_dates_for_qc(
    universe: pl.DataFrame,
    listing_cache: dict[tuple[str, str], date],
) -> dict[tuple[str, str], datetime]:
    """For QC: combine universe snapshot + cache + observed-archive authority.

    Calls effective_listing_date for each (symbol, market) so observed-min
    overrides hint when archive exists.
    """
    out: dict[tuple[str, str], datetime] = {}
    for row in universe.iter_rows(named=True):
        sym = row["symbol"]
        base = row["base_asset"]
        snap_listing = row["listing_date"]
        # Perp
        eff = klines_mod.effective_listing_date(sym, "perp_usdt", snap_listing)
        if eff is not None:
            out[(sym, "perp_usdt")] = datetime.combine(eff, datetime.min.time(), tzinfo=UTC)
        # Spot canonical USDT pair
        canonical_spot = f"{base}USDT"
        if canonical_spot in row["spot_pairs"]:
            cached = listing_cache.get((sym, "spot"))
            eff = klines_mod.effective_listing_date(sym, "spot", cached)
            if eff is not None:
                out[(sym, "spot")] = datetime.combine(eff, datetime.min.time(), tzinfo=UTC)
    return out


def summarize_results(
    fetch_results: list[TaskResult],
    qc_results: list,
    archive_size_bytes: int,
) -> str:
    """Format an ASCII summary for stdout."""
    n_total = len(fetch_results)
    n_done = sum(1 for r in fetch_results if r.outcome == "done")
    n_nothing = sum(1 for r in fetch_results if r.outcome == "nothing")
    n_skipped = sum(1 for r in fetch_results
                    if r.outcome in ("skipped_no_resume", "skipped_no_data"))
    n_failed = sum(1 for r in fetch_results if r.outcome == "error")

    n_error, n_warn, n_info = qc_mod.summarize(qc_results) if qc_results else (0, 0, 0)
    n_sym_err = qc_mod.n_symbols_with_error(qc_results) if qc_results else 0

    # Group errors by check-name prefix for operator triage.
    err_by_cat: dict[str, int] = {}
    for r in qc_results:
        if r.severity != "ERROR":
            continue
        cat = r.name.split("[")[0].split("_")[0]   # "k1", "k2", ..., "f3", "c1", "x1"
        err_by_cat[cat] = err_by_cat.get(cat, 0) + 1
    err_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(err_by_cat.items())) or "none"

    return (
        "\n=== Archive run summary ===\n"
        f"  fetch tasks:    {n_total} total ({n_done} done, "
        f"{n_nothing} nothing-to-fetch, {n_skipped} skipped, {n_failed} failed)\n"
        f"  qc results:     {n_error} ERROR, {n_warn} WARN, {n_info} INFO\n"
        f"  qc errors by category: {err_breakdown}\n"
        f"  symbols with ERROR: {n_sym_err}\n"
        f"  archive on disk:    {archive_size_bytes / 1e6:.1f} MB\n"
    )


def _archive_size_bytes() -> int:
    total = 0
    for root in (KLINES_ROOT, FUNDING_ROOT):
        if not root.exists():
            continue
        for p in root.rglob("*.parquet"):
            with contextlib.suppress(OSError):
                total += p.stat().st_size
    return total


# ── CLI ───────────────────────────────────────────────────────────────────


def _parse_iso_date_or_now(s: str | None) -> datetime:
    if s is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_period_start(s: str) -> date:
    """Accept 'YYYY-MM' for monthly snapshot key."""
    yr, mo = s.split("-")
    return date(int(yr), int(mo), 1)


def _parse_task_types(s: str | None) -> tuple[str, ...]:
    if s is None:
        return VALID_TASK_TYPES
    items = tuple(x.strip() for x in s.split(",") if x.strip())
    for it in items:
        if it not in VALID_TASK_TYPES:
            raise ValueError(f"unknown task-type {it!r}; allowed: {VALID_TASK_TYPES}")
    return items


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Universe-aware Binance archive fetch + QC orchestrator.",
    )
    p.add_argument("--snapshot", required=True,
                   help="universe snapshot key, format YYYY-MM")
    p.add_argument("--end", help="ISO datetime; default = now")
    p.add_argument("--force-cold-start", action="store_true",
                   help="enable cold-start fetch when archive empty (burns API weight)")
    p.add_argument("--max-tasks", type=int, default=None,
                   help="cap total fetch tasks (per-symbol atomicity preserved)")
    p.add_argument("--task-types", default=None,
                   help="comma list of {perp_usdt,spot,perp_funding}; default = all")
    p.add_argument("--skip-fetch", action="store_true",
                   help="run QC on existing archive only")
    p.add_argument("--skip-qc", action="store_true",
                   help="fetch only; skip post-fetch QC")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pl.Config.set_ascii_tables(True)   # cp950 console (CLAUDE.md gotcha)

    args = _build_argparser().parse_args(argv)

    period_start = _parse_period_start(args.snapshot)
    end_dt = _parse_iso_date_or_now(args.end)
    task_types = _parse_task_types(args.task_types)

    if not acquire_run_lock():
        return EXIT_FAIL
    atexit.register(release_run_lock)

    tripped, payload = is_breaker_tripped()
    if tripped:
        log.error("circuit breaker tripped: %s", payload)
        return EXIT_RATE_LIMITED

    universe = read_snapshot(period_start)
    listing_cache = load_listing_cache()

    fetch_results: list[TaskResult] = []
    rate_limited = False
    if not args.skip_fetch:
        all_tasks = expand_universe_to_tasks(universe)
        tasks = [t for t in all_tasks if t.market in task_types]
        if args.max_tasks is not None:
            tasks = tasks[: args.max_tasks]
        log.info("fetch plan: %d tasks (universe=%d rows)", len(tasks), universe.shape[0])
        with BinanceClient() as client:
            fetch_results, rate_limited = run_fetch_phase(
                tasks, end_dt, args.force_cold_start, listing_cache, client,
            )
        save_listing_cache(listing_cache)
        if rate_limited:
            trip_breaker(
                reason=f"rate-limited at task "
                       f"{fetch_results[-1].task.symbol}/{fetch_results[-1].task.market}",
            )
            print(summarize_results(fetch_results, [], _archive_size_bytes()))
            return EXIT_RATE_LIMITED

    qc_results: list = []
    if not args.skip_qc:
        klines_df = load_klines_archive()
        funding_df = load_funding_archive()
        listing_dates = build_listing_dates_for_qc(universe, listing_cache)
        qc_results = qc_mod.compute_qc_results(klines_df, funding_df, universe, listing_dates)
        qc_mod.write_qc_audit(qc_results, audit_dir=QC_AUDIT_DIR)

    print(summarize_results(fetch_results, qc_results, _archive_size_bytes()))

    n_failed_fetch = sum(1 for r in fetch_results if r.outcome == "error")
    n_qc_error, _, _ = qc_mod.summarize(qc_results) if qc_results else (0, 0, 0)
    if n_failed_fetch == 0 and n_qc_error == 0:
        clear_breaker()
        return EXIT_OK
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
