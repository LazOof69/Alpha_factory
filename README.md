# Alpha Factory

Personal quant alpha factory for crypto.

> **Current status: Feasibility Study (FS) phase.**
> `PROJECT.md` and `CLAUDE.md` will be written **after** FS conclusions, not before.
>
> See **[`feasibility/README.md`](feasibility/README.md)** for current FS scope, pass criteria, and progress.

## Why FS first

Two empirical assumptions need to be validated before the full system spec is locked:

1. Funding harvester carry exists across regimes (Study 1)
2. Triangular arb is feasible at retail capital (Study 2, conditional on Study 1)

If either fails, Sharpe / ROI targets in `PROJECT.md` will be revised down.

## Stack

- Python 3.11 + uv
- polars + pyarrow + duckdb (data)
- scipy + numpy (stats)
- vectorbt + mlfinlab (added after FS, in Phase A)

## Skills used

This project leverages these Claude Code skills (all `anthropic-skills:` namespace plus `code-reviewer`):

| Layer | Skill |
|---|---|
| L1 Data | `market-data-pipeline` |
| L2 Alpha | `alpha-factor-research` |
| L3 Validation | `strategy-validation` |
| L4 Portfolio | `portfolio-construction` |
| L5 Execution | `live-trading-execution` |
| Cross-cutting | `code-reviewer`, `simplify`, `xlsx`, `quant-analyst` |
