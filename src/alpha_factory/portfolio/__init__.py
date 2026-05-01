"""L4 — Portfolio construction.

Combines validated alphas into a single portfolio with explicit risk targets.

Method (PROJECT.md):
    Hierarchical Risk Parity (HRP, López de Prado) as default.
    Mean-variance / Black-Litterman explicitly NOT used (estimation error
    dominates at our N).

Constraints:
    Per-alpha weight cap: 50%
    Combined max gross leverage: 2x in Phase D, 3x considered Phase E+
    Combined vol target: 10-15% annualized
    Daily VaR (95%) ≤ 5% of portfolio
"""
from __future__ import annotations
