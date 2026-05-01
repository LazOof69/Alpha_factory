"""Alpha Factory — personal quant alpha factory for crypto.

Layer overview (see PROJECT.md for full architecture):
    L1 data        — point-in-time correct archive (alpha_factory.data)
    L2 alpha       — strategies and factors           (alpha_factory.alpha)
    L3 validation  — DSR, PBO, purged CV, regime      (alpha_factory.validation)
    L4 portfolio   — HRP, sizing, risk targeting      (alpha_factory.portfolio)
    L5 execution   — paper, live, monitoring          (alpha_factory.execution)

This package is the production code. Phase 0 feasibility scripts under
`feasibility/scripts/` are frozen and serve as historical record.
"""
from __future__ import annotations

__version__ = "0.1.0"
