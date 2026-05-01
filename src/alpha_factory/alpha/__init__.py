"""L2 — Alpha layer.

Strategies and factors. Each alpha is its own module with a consistent
interface so the L3 validation framework can run them uniformly.

Planned alphas (PROJECT.md):
    carry          — Phase A: long spot + short perp delta-neutral
    funding_xs     — Phase B: cross-sectional funding factor
    xs_momentum    — Phase B: cross-sectional momentum
    basis_mr       — Phase B: perp-spot basis mean-reversion
    vrp            — Phase E (optional): variance risk premium
    microstructure — Phase E (optional): orderbook signals

Each alpha module exposes:
    name        — str, registry id
    parameters  — dataclass with default values
    generate(panel, params) -> DataFrame  — per-bar return / position series
    economic_story  — str, why this alpha exists (mandatory by strategy-validation)
"""
from __future__ import annotations
