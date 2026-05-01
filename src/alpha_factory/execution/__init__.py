"""L5 — Execution layer.

Backtest → paper trading → live deployment. Uses nautilus-trader for
backtest-to-live consistency (same code on both).

Phases (PROJECT.md):
    Backtest      — pure simulation
    Paper trade   — Phase D, 3 months minimum, real Binance API real prices,
                    simulated fills
    Small live    — Phase D end, $1k real
    Scale up      — Phase 1+, conditional on tracking error < 30%

Hard kill-switches:
    Daily DD > 5%
    Latency > 5s
    Unhandled exception
    Data feed lag > 60s
"""
from __future__ import annotations
