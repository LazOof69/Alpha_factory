"""Phase C L1 forward fetch — rolling window per paper-trade cycle.

Stage [1] of the walking-skeleton pipeline (docs/phase_c_infra_design_v3.md
§"Build approach"). Per cycle on the AMD micro (v3 §8): fetch the rolling
120-settlement funding window + matching klines (spot + perp), persist as
the L1 forward-archive byproduct (v3 §3′), and hand the strategy adapter
a RollingWindow plus the audit fields the event log needs (data_version,
clock_drift_ms).

Composes existing L1 primitives — does NOT re-implement fetch / write /
correction-sidecar logic, which already exist in ``data/funding.py`` and
``data/klines.py``. The job of this module is the boundary between L1
and execution:

* Time-window selection (last n_settlements * 8h, with buffer)
* Spot + perp kline concat (carry pair is two-legged)
* ``funding_time`` -> ``open_time`` rename (RollingWindow contract from
  strategy.py stage [2] — the L1 archive's canonical column name stays
  ``funding_time`` to avoid touching schema + the 11 existing carry_v3
  tests; the rename is a one-line bridge at the boundary)
* Clock-drift sample (v2 §2 ``process_clock_drift_vs_binance_ms``;
  P0.5 halt trigger source)
* data_version fingerprint (v2 §2 ``data_version``)

WALKING-SKELETON LIMITATIONS (documented, not hidden)
-----------------------------------------------------
* Single symbol per call — multi-symbol orchestration belongs to the
  cron, not this composer.
* No retry around the cycle as a whole; ``binance_client`` already
  handles HTTP-level retries. Higher-level failure modes (partial
  fetch, missing settlements) are visible to the cron via the
  returned ``ForwardFetchResult`` shape and the event log.
* ``data_version`` is timestamp-only — corrections-state tracking is
  deferred. Day 1 has no prior archive so corrections cannot occur;
  later cycles' corrections are still recorded in the L1 sidecar
  (``data/{funding,klines}/_corrections/``), so the byte-stable
  replay path is intact; only the in-event marker is simplified.
* Clock drift is a mid-point estimate (network RTT/2 is the error
  bound). v2 §2's 500 ms halt threshold is loose enough that the
  estimate accuracy is not load-bearing.
* No spot vs perp ``open_time`` alignment check here — that's
  ``qc.py``'s job and runs out-of-cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from alpha_factory.data.binance_client import BinanceAPIError, BinanceClient
from alpha_factory.data.funding import (
    fetch_funding,
    write_funding_partitioned,
)
from alpha_factory.data.klines import (
    fetch_klines,
    write_klines_partitioned,
)
from alpha_factory.data.schema import (
    FAPI_TIME_URL,
    FUNDING_INTERVAL_MS,
    FUNDING_ROOT,
    KLINES_ROOT,
)
from alpha_factory.execution.strategy import RollingWindow

log = logging.getLogger(__name__)

__all__ = [
    "ForwardFetchResult",
    "fetch_forward_window",
    "sample_clock_drift_ms",
]


# ── Result type ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForwardFetchResult:
    """Output of one paper-trade cycle's L1 forward fetch.

    ``window`` feeds the strategy adapter; the remaining fields are
    stamped onto the event log's ``signal_compute`` event (v2 §2
    versioning block) by the cron.
    """

    window: RollingWindow
    data_version: str             # v2 §2 — L1 archive fingerprint
    clock_drift_ms: float | None  # v2 §2 — None on sample failure
    fetched_at: datetime          # cycle wall-clock UTC


# ── Clock drift (v2 §2 P0.5 source) ───────────────────────────────────────


def sample_clock_drift_ms(client: BinanceClient) -> float | None:
    """Return Binance server-time minus local-time mid-point, in milliseconds.

    Positive = Binance ahead of local; negative = local ahead. Mid-point
    of the bracketing local timestamps is the standard single-shot
    estimator; the worst-case error is ~RTT/2 (good enough vs v2 §2's
    500 ms halt threshold). Returns ``None`` on any failure — clock-drift
    sampling is monitoring-only and must not abort the cycle.
    """
    local_before_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    try:
        resp = client.get_json(FAPI_TIME_URL, params={})
        server_ms = int(resp["serverTime"])
    except (BinanceAPIError, KeyError, TypeError, ValueError) as e:
        log.warning("clock_drift sample failed: %s", e)
        return None
    local_after_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    local_mid_ms = (local_before_ms + local_after_ms) / 2
    return float(server_ms - local_mid_ms)


# ── Main composer ─────────────────────────────────────────────────────────


def _data_version(last_settlement: datetime) -> str:
    """v2 §2 fingerprint format: ``YYYY-MM-DDTHH:MM+nocorr``.

    Minute resolution (funding settlements land at exact 8h boundaries;
    sub-minute precision is noise). ``+nocorr`` is the skeleton-stage
    state marker; corrections-state tracking is deferred (see module
    docstring).
    """
    return f"{last_settlement.strftime('%Y-%m-%dT%H:%M')}+nocorr"


def fetch_forward_window(
    symbol: str,
    client: BinanceClient,
    *,
    n_settlements: int = 120,
    funding_root: Path = FUNDING_ROOT,
    klines_root: Path = KLINES_ROOT,
    persist: bool = True,
) -> ForwardFetchResult:
    """Fetch + persist the rolling window for one paper-trade cycle.

    Steps (v3 §"Build approach" stage [1] thinnest version):

    1. Sample clock drift (non-fatal; logged on failure).
    2. Compute fetch range = last (n_settlements + 3) settlements; +3
       is a boundary buffer so we always overshoot enough to trim back
       to ``n_settlements`` rows cleanly.
    3. Fetch funding (perp) over the range.
    4. Fetch klines (both spot + perp_usdt) over the same range; concat
       with KLINES_SCHEMA's ``market`` column distinguishing legs.
    5. If ``persist``: UPSERT into L1 archive (year-partitioned parquet
       + corrections sidecar per existing write paths — this IS the
       forward archive byproduct, v3 §3′).
    6. Trim funding to LAST ``n_settlements`` rows; rename
       ``funding_time`` -> ``open_time`` (RollingWindow contract).
    7. Compute ``data_version`` from the latest funding settlement (or
       ``fetched_at`` if funding came back empty).

    On empty funding (e.g., cold start with stale buffer or API gap):
    returns a ForwardFetchResult whose ``window.funding`` is empty;
    ``current_regime_state_v3`` short-circuits to state 0 (warmup),
    and the strategy adapter emits ``legs=()``.
    """
    fetched_at = datetime.now(tz=UTC)

    clock_drift_ms = sample_clock_drift_ms(client)

    # Window range: enough buffer to overshoot then trim. funding settles
    # every 8h, so n_settlements * 8h + 3 settlements headroom.
    span_ms = (n_settlements + 3) * FUNDING_INTERVAL_MS
    start_dt = fetched_at - timedelta(milliseconds=span_ms)
    end_dt = fetched_at

    log.info(
        "forward fetch %s: [%s, %s) n_settlements=%d",
        symbol, start_dt.isoformat(), end_dt.isoformat(), n_settlements,
    )

    funding = fetch_funding(symbol, start_dt, end_dt, client)
    spot_klines = fetch_klines(symbol, "spot", start_dt, end_dt, client)
    perp_klines = fetch_klines(symbol, "perp_usdt", start_dt, end_dt, client)

    if persist:
        if not funding.is_empty():
            write_funding_partitioned(funding, root=funding_root)
        if not spot_klines.is_empty():
            write_klines_partitioned(spot_klines, root=klines_root)
        if not perp_klines.is_empty():
            write_klines_partitioned(perp_klines, root=klines_root)

    # Concat klines so the RollingWindow holds one frame with the
    # ``market`` column distinguishing legs. vertical_relaxed survives
    # empty-frame edge cases without schema-mismatch errors.
    klines = pl.concat([spot_klines, perp_klines], how="vertical_relaxed")

    funding_trimmed = funding.sort("funding_time").tail(n_settlements)
    funding_window = funding_trimmed.rename({"funding_time": "open_time"})

    if not funding_trimmed.is_empty():
        last_settlement = funding_trimmed["funding_time"].max()
    else:
        last_settlement = fetched_at
    data_version = _data_version(last_settlement)

    window = RollingWindow(funding=funding_window, klines=klines)
    return ForwardFetchResult(
        window=window,
        data_version=data_version,
        clock_drift_ms=clock_drift_ms,
        fetched_at=fetched_at,
    )
