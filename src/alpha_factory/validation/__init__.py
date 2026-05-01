"""L3 — Validation layer.

Implements the LdP-rigor checks that any alpha must pass before entering
the L4 portfolio:

    - Deflated Sharpe Ratio (Bailey & López de Prado 2014)
    - Probability of Backtest Overfitting (CSCV method)
    - Purged + embargoed K-fold cross-validation
    - Walk-forward analysis
    - Regime stratification (bull/bear, pre/post-ETF for crypto)
    - Friction haircut (× 0.3-0.5 of backtest Sharpe)

Public interface:
    validate(alpha_results) -> ValidationReport

Pass criteria (PROJECT.md):
    DSR > 0 with 95% CI
    PBO ≤ 0.5
    Bull AND bear regime each Sharpe ≥ 0 (post-friction)
    Pre-ETF AND post-ETF each Sharpe ≥ 0
    Max DD ≤ 30%
"""
from __future__ import annotations
