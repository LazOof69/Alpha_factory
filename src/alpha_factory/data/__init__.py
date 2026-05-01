"""L1 — Data layer.

Point-in-time correct archive of crypto market data. Every row carries
`ingested_at` + `source` for reproducibility; every datetime is tz-aware UTC
microsecond precision.

Submodules (added incrementally during Phase A):
    schema    — polars schemas, time helpers, symbol mapping (single source of truth)
    universe  — top-N USDT-M perp construction with monthly snapshots (Phase A.2)
    klines    — Phase A.3 spot + perp 1h kline fetcher
    funding   — Phase A.3 perp 8h funding fetcher
    qc        — Phase A.4 quality-control gates (K1-K8, F1-F4, X1)

Phase 0 fetchers under `feasibility/scripts/` are frozen historical record;
production code does NOT import from there.
"""
from __future__ import annotations
