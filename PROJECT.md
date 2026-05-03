# Alpha Factory — Project Specification

> **Status**: Phase 0 (FS) complete · Phase A starting
> **Last revised**: 2026-04-30
> **Owner**: butter.chen666@gmail.com
> **Scope**: Personal quant alpha factory for crypto

---

## TL;DR

12-month roadmap to build a multi-alpha crypto portfolio at retail scale.

- **L1-L3 production infrastructure** (data, alpha, validation) by month 6
- **Portfolio of 3-7 low-correlation alphas** (Phase B-E) targeting **combined forward Sharpe 1.5–2.0**
- **3-month paper trading → $1k live** by month 12
- **Strategy rotation > single-alpha optimization** — Grinold's Law (BR matters)
- Carry alone delivers forward Sharpe ~0.75; **portfolio combination is the source of edge**, not any one strategy

**Goal is NOT max next-12-month dollar return.** At $1k starting, even Sharpe 2 yields ~$200/yr. Real outputs:
1. A working alpha-factory platform that scales linearly with capital
2. Verified forward Sharpe estimates anchoring sizing decisions
3. A registry of validated, regime-stratified, low-correlation alphas

Material monthly cash requires capital ≥ $10k (achievable via monthly add + compound + 2-3x crypto-perp leverage by month 24-36).

---

## Goals

1. Build a production-grade alpha factory infra (Layer 1-3 of 5-layer stack)
2. Develop 4-7 validated crypto alphas spanning carry, cross-sectional funding, momentum, basis, and (Phase E) VRP/microstructure
3. Achieve forward portfolio Sharpe ≥ 1.5 sustained over 3+ months of paper trading
4. Validate forward → live consistency: 3-month paper trade with tracking error < 30%
5. Documentation as self-correction: PROJECT.md + CLAUDE.md anchor decisions to avoid relitigating

## Non-Goals (explicit "we are NOT doing")

- ❌ US / Taiwan stocks — different microstructure, broker, regulation
- ❌ Sub-minute / HFT — no latency edge from Oracle Cloud as personal trader
- ❌ Cross-exchange arbitrage — saturated by HFT firms, retail Sharpe < 0
- ❌ Options trading until Phase E (Deribit account + pricing infra deferred)
- ❌ L2 orderbook infrastructure until Phase E (1-month build cost; marginal Sharpe)
- ❌ Live trading before Phase D (3-month paper trading is non-negotiable)
- ❌ Black-Litterman / subjective views in Phase A-C (HRP / risk parity only)
- ❌ Multi-venue execution before single-venue (Binance) is proven
- ❌ Hedge-fund operational complexity (FIX, colocation, prime broker)
- ❌ Live tax accounting in code (handled separately under Taiwan tax basis)
- ❌ Maximum dollar return as success metric (Sharpe-anchored; capital scales separately)

---

## Decisions Log (committed; do NOT re-discuss without explicit revisit)

### Strategic
| # | Decision | Rationale |
|---|---|---|
| 1 | Crypto only (Binance USDT-M perp + spot) | unified microstructure, 24/7, lowest fees |
| 2 | BTC + ETH for FS; expand to top 20 USDT-M perp by 30-day ADV in Phase A | prove on liquid majors first |
| 3 | 1h klines + 8h funding cadence | reasonable data volume; sub-hour not needed for these alphas |
| 4 | $1k initial capital; monthly addition TBD by user | bootstrap |
| 5 | Forward Sharpe target: portfolio combined 1.5 (realistic) / 2.0 (stretch); carry alone 0.75 | post-haircut realistic |
| 6 | 12-month timeline Phase A → E | sustainable at 10-20 hr/wk |
| 7 | Skip Study 2 (triangular arb) — prioritize Phase B multi-alpha portfolio | better Sharpe contribution per dev hour |
| 8 | Pass criteria 0.5 Sharpe gate (relaxed from 0.8 after post-ETF compression observed) | account for structural funding shift |

### Technical
| # | Decision | Rationale |
|---|---|---|
| 1 | Python 3.11 + uv | uv beats pip/poetry on resolution; mlfinlab not on 3.13 yet |
| 2 | polars > pandas | 5-10x at our scale, native parquet |
| 3 | Parquet partitioned by year | DuckDB-friendly, scales |
| 4 | httpx + custom retry > ccxt for historical fetch | direct pagination control |
| 5 | mlfinlab for DSR/PBO (Phase B) | LdP canonical |
| 6 | vectorbt for parameter sweeps (Phase A+) | speed |
| 7 | nautilus-trader for paper-to-live (Phase D) | same code on both |
| 8 | Local dev → Oracle ARM 24/7 ingest after FS | local fast, Oracle persistent |
| 9 | Skill-routing hook in `.claude/settings.json` | enforce per-task skill discipline |

### Validation (LdP-rigor)
| # | Decision | Rationale |
|---|---|---|
| 1 | Any alpha enters L4 portfolio: PBO ≤ 0.5 + DSR > 0 (95% CI) | LdP standard |
| 2 | Bull AND bear regime each: post-friction Sharpe ≥ 0 | regime-robust |
| 3 | Pre-ETF AND post-ETF (crypto): each Sharpe ≥ 0 | structural shift |
| 4 | Paper trading minimum 3 months before live | live-trading-execution skill rule |
| 5 | Forward Sharpe haircut: × 0.3–0.5 of backtest | empirical cross-asset |
| 6 | Listing date from observed first-bar, not API probe alone | resolved in Phase 0 audit |

---

## 5-Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│  L5  Execution        Binance API · Paper · Live        │
│                       nautilus-trader · Phase D         │
├─────────────────────────────────────────────────────────┤
│  L4  Portfolio        HRP / risk parity / vol target    │
│                       riskfolio-lib · Phase C           │
├─────────────────────────────────────────────────────────┤
│  L3  Validation       Purged CV · DSR · PBO · regime    │
│                       mlfinlab · Phase A onwards        │
├─────────────────────────────────────────────────────────┤
│  L2  Alpha            Carry · Funding XS · Momentum     │
│                       Basis · (Phase E: VRP, micro)     │
├─────────────────────────────────────────────────────────┤
│  L1  Data             Binance archive · DuckDB query    │
│                       httpx + polars + parquet          │
└─────────────────────────────────────────────────────────┘
```

### L1 — Data Layer

**Scope**: point-in-time correct archive of crypto market data.

**Decisions**:
- Source: Binance public REST (no API key required)
- Storage: Parquet partitioned by year, schema in `schema.py`
- Universe: Phase 0 = BTC, ETH; Phase A onward = top 20 USDT-M perp by 30d ADV (refreshed monthly, snapshot per day)
- Frequency: 1h klines (spot + perp) + 8h funding (perp only)
- Audit fields: every row has `ingested_at` + `source` for reproducibility

**Quality gates** (`qc.py`):
- K1–K8 klines (coverage, dups, OHLC validity, stale, extreme returns, spot-perp consistency, partial bars, non-negative)
- F1–F4 funding (alignment, coverage, range sanity, dups)
- X1 cross-table funding-to-kline join validation
- 0 ERROR required before data is treated as authoritative

### L2 — Alpha Layer

| Phase | Strategy | Description | Data req | Forward Sharpe (haircut) |
|---|---|---|---|---|
| 0 | Carry harvester | long spot + short perp delta-neutral, V2 with regime detection | klines + funding | **0.75–1.0** |
| B | Funding XS factor | Top 20 perp ranked by funding; long low / short high; delta-neutral | klines + funding | 1.0–1.4 |
| B | XS momentum | Top 20 perp ranked by N-day return; long-short basket | klines | 0.5–0.8 |
| B | Basis mean-reversion | Perp-spot basis as signal; long convergence | klines | 0.7–1.0 |
| E (opt) | VRP harvest | Sell IV via Deribit options | options (Deribit) | 1.0–1.5 |
| E (opt) | OFI / microstructure | Order-flow imbalance signals | L2 tick / depth | 1.5–2.0 (capacity-bound) |

### L3 — Validation Layer

**Required for any alpha entering L4**:
1. Lookahead-bias review by `code-reviewer` skill before backtest run
2. Survivorship-clean universe (handled at L1)
3. Walk-forward / purged-embargoed CV when alpha has free parameters
4. Deflated Sharpe Ratio > 0 with 95% CI; N_trials = effective independent parameter combinations
5. Probability of Backtest Overfitting ≤ 0.5
6. Bull and bear regime Sharpe each ≥ 0 (post-friction)
7. Pre-ETF and post-ETF (BTC ETF cutoff 2024-01-11) each Sharpe ≥ 0
8. Max DD ≤ 30%

**Non-negotiable**: failing ANY gate → alpha does NOT enter portfolio.

### L4 — Portfolio Layer

**Method**: Hierarchical Risk Parity (HRP, López de Prado) as default. Mean-variance / Black-Litterman explicitly NOT used (estimation error overwhelms signal at our N).

**Risk constraints**:
- Per-alpha weight cap: 50%
- Combined max gross leverage: 2x in Phase D, 3x considered Phase E+
- Combined vol target: 10–15% annualized
- Daily VaR (95%) ≤ 5% of portfolio
- Daily P&L kill-switch: -5% triggers manual review

### L5 — Execution Layer

**Deployment phases**:
1. Backtest (Phase A-C): pure simulation
2. Paper trading (Phase D, **3 months minimum**): real Binance API, real prices, simulated fills
3. Small live (Phase D end): $1k → real money on Binance perp + spot
4. Scale up (Phase 1+): conditional on live tracking error < 30% vs backtest

**Order management**:
- Limit orders preferred (maker rebate)
- TWAP for large rebalances
- Hard kill-switch on: daily DD > 5%, latency > 5s, unhandled exception, data feed lag > 60s

---

## Tech Stack

| Component | Choice | Rationale |
|---|---|---|
| Python | 3.11 | mlfinlab not yet on 3.13 |
| Package mgr | uv | fast resolution, project-level Python |
| DataFrame | polars 1.40+ | 5-10x pandas, native parquet |
| Storage | Parquet, partitioned by year | DuckDB compat |
| Query | duckdb | ad-hoc SQL on parquet |
| HTTP | httpx | async-ready, retry control |
| Stats | scipy | DSR / Sharpe SE / Lo (2002) |
| Backtest | vectorbt (Phase A+) | parameter sweep speed |
| LdP | mlfinlab (Phase B+) | canonical PBO/DSR/Triple Barrier |
| Portfolio | riskfolio-lib (Phase C+) | HRP + Ledoit-Wolf |
| Live | nautilus-trader (Phase D) | unified backtest + paper + live |
| Lint | ruff | fast |
| Test | pytest | standard |
| Plot | matplotlib + plotly | sufficient |
| .xlsx | xlsxwriter (via polars write_excel) | xlsx skill compat |

---

## Directory Structure

```
Alpha_factory/
├── .claude/                       Claude Code config
│   ├── settings.json              allowlist + skill-routing hook
│   ├── hooks/
│   │   └── skill_routing.py
│   └── skills/                    code-reviewer + 5 plugin .skill files
├── feasibility/                   Phase 0 archive (preserved)
│   ├── data/                      gitignored archives
│   ├── notebooks/
│   ├── results/                   funding_analysis.xlsx, backtest_carry.xlsx
│   └── scripts/                   schema.py, fetch_*.py, qc.py, run_archive.py,
│                                  analyze_funding.py, backtest_carry.py
├── src/                           Phase A onwards (production)
│   └── alpha_factory/
│       ├── data/                  L1: fetchers, qc, schema
│       ├── alpha/                 L2: strategies, factors
│       ├── validation/            L3: DSR, PBO, regime, CV
│       ├── portfolio/             L4: HRP, sizing
│       └── execution/             L5: paper, live, monitoring
├── tests/                         per-module test files
├── notebooks/                     research, exploration
├── results/                       final reports + xlsx
├── pyproject.toml
├── uv.lock
├── .python-version
├── .gitignore
├── PROJECT.md                     this file
├── CLAUDE.md                      per-session operating rules
└── README.md
```

---

## Phase Roadmap

### Phase 0 — Feasibility Study  (DONE — Apr 2026)
- [x] Project skeleton (uv + pyproject + .claude)
- [x] Binance archive pipeline + 13 QC checks
- [x] Full historical archive: 231k klines + 12k funding
- [x] Funding distribution analysis: BTC post-ETF -30.8% / ETH -41.6% confirmed
- [x] Carry V1 + V2 backtest, both pass relaxed gates
- [x] Two strategy-validation audit passes
- [x] **Verdict**: STUDY_1_PASS

### Phase A — Production Data + Validation Infrastructure  (mo 1-3, DONE — May 2026)
**Outcomes**:
- [x] L1 production: top 20 USDT-M perp universe, monthly rebalance (A.2)
- [x] `src/alpha_factory/data/` archive pipeline (A.3.1-A.3.5): klines + funding + QC + universe-aware orchestrator with rate-limit breaker
- [x] `src/alpha_factory/validation/` L3 layer (A.4): schemas (8) + contracts + metrics + cv + DSR + PBO + regime + costs + registry
- [x] Carry V2 ported to `src/alpha_factory/alpha/carry.py` (A.5.1)
- [x] `src/alpha_factory/runner.py` end-to-end orchestration (A.5.2; 1-round pseudocode critique + code-reviewer audit integrated)
- [x] Live archive cold-start fetch: top 5 perps + spots + funding (A.5.3; ~30s wall, 27.9 MB on disk, BTC 6.6yr / ETH 6.4yr / SOL 4.5yr)
- [x] 3-phase adversarial audit on carry V2 verdict (A.5.3)
- [x] Carry V2 documentation + validated_alphas.yaml registry (A.5.4)
- [x] **Verdict**: PASS-WITH-CAVEATS (see `docs/alphas/carry_v2.md` + `validated_alphas.yaml`)

**Pass gate met**: full-sample DSR > 0 (1.0); recent-12m DSR > 0.5 (0.625) at honest n_trials=100; regime gates all positive; max DD 1.89% << 30%. PBO N/A for single-params (deferred to Phase B sweep). Soft gate "funding-regime detection" partially met — V2 catches absolute negative regimes but is blind to compression-toward-zero (the 2025-2026 threat); V3 detector deferred to Phase B.

### Phase B — Multi-Alpha Generation  (mo 3-6)
**Outcomes**:
- 3 new alphas in `src/alpha_factory/alpha/`: `funding_xs.py`, `xs_momentum.py`, `basis_mr.py`
- Each backtested over full archive, validated via L3
- Inter-alpha correlation matrix; effective N estimated for Grinold's Law

**Pass gate**: ≥3 alphas pass L3 independently (DSR + PBO); top 3 by orthogonality selected

### Phase C — Portfolio Construction  (mo 6-9)
**Outcomes**:
- HRP combination of validated alphas
- Vol-targeted to 10–15% annualized
- Combined OOS performance backtested via walk-forward

**Pass gate**: combined OOS Sharpe ≥ 1.5; max DD ≤ 30%

### Phase D — Paper Trading + Live  (mo 9-12)
**Outcomes**:
- 3-month paper trading via nautilus-trader on Binance API
- Live tracking error vs backtest measured weekly
- $1k live deployment in final 4 weeks (conditional on paper passing)

**Pass gate**: paper Sharpe ≥ 0.75; tracking error < 30%; live $1k Sharpe ≥ 0 over first month

### Phase E — Optional Expansion  (mo 12+)
**Outcomes** (each independent, may not all happen):
- Deribit options account; VRP harvest alpha
- L2 microstructure infrastructure on Oracle ARM (ClickHouse + WebSocket ingest)
- Re-validate combined portfolio with new alphas

**Pass gate**: combined Sharpe ≥ 2.0 with new alpha additions

---

## Forward Expectation Anchors (Honest)

| Metric | Backtest | Forward expectation | Source |
|---|---|---|---|
| Carry alone Sharpe (BTC, full 6.6y) | +3.37 | **0.86–1.44** | last-12m × 0.3-0.5 haircut |
| Carry alone Sharpe (BTC, recent 12m) | +2.88 | **0.86–1.44** | direct anchor (audit recommendation) |
| Carry alone Sharpe (BTC, last 3m) | **-0.95** | <span style="color:red">**at-risk**</span> | live monitor; revoke if Q3-2026 stays negative |
| Carry alone ann return | +5.18% (full 6.6y avg) | **0.5–1.5% / yr** | last-12m ann_ret 1.38% × haircut |
| Phase B combined (4 alphas) | TBD | **1.5–2.0** | Grinold on weakly-correlated alphas |
| Phase E combined (with VRP/micro) | TBD | **2.0–2.5** | adding orthogonal sources |
| Live tracking error vs backtest | n/a | < 30% | live-trading-execution rule |

**Note (post-A.5.3 audit)**: the Phase 0 V2 backtest reported Sharpe 3.17;
the production runner on the full-archive smoke test reports 3.37 (slightly
higher due to richer data window through 2026-05). The honest anchor is the
**last-12m window of 2.88**, which already incorporates the post-ETF
compression observed in PROJECT.md decision #5. Headline 3.37 includes the
2021 leverage-froth peak and 2024 post-ETF launch frenzy — both
non-recurring outliers. Full audit and caveats: `docs/alphas/carry_v2.md`.

**Capital trajectory** (forward Sharpe 1.5, **$0/mo addition** — pure compound, decided 2026-04-30):

| Month | 1x lev (~7.5%/yr) | 3x lev (~22.5%/yr) |
|---|---|---|
| 0 | $1,000 | $1,000 |
| 12 | $1,075 | $1,225 |
| 24 | $1,156 | $1,500 |
| 36 | $1,242 | $1,838 |
| 60 | $1,436 | $2,760 |
| 120 | $2,061 | $7,621 |

**Brutal reality at $0/mo addition**: even with 3x leverage and Sharpe 1.5, $1k → $7.6k in 10 years. Material monthly cash ($100+) is **not realistic on this trajectory**.

**Project framing therefore**: alpha factory is a **platform-building exercise + skill investment**, NOT a 12-month income source.

- Phase 0–D outputs: working system + validated alphas + paper-trading record + $1k live track record
- Real return (next 12 months): primarily learning + GitHub repo / portfolio for future opportunities (job, scaling capital from elsewhere)
- The "strategy works" verdict from Phase D unlocks the option to scale up capital from external sources (savings, side income) when ready

If user later changes mind on monthly additions → update Open Questions #1 + this section.

---

## Red Lines (NEVER violate without explicit user override)

### Validation
- 🚫 No alpha enters L4 portfolio without DSR > 0 (95% CI) AND PBO ≤ 0.5
- 🚫 No live capital without 3-month paper trading
- 🚫 No backtest Sharpe used as forward expectation without × 0.3–0.5 haircut
- 🚫 No alpha with > 3 free parameters skips walk-forward CV
- 🚫 No regime-conditional strategy without bull/bear stratification check

### Risk
- 🚫 Leverage > 2x in Phase D
- 🚫 Single alpha > 50% portfolio weight
- 🚫 Manual override of risk limits without 24h cooldown documented
- 🚫 Live position without explicit kill-switch

### Data
- 🚫 yfinance for serious backtesting
- 🚫 Timezone-naive datetimes anywhere
- 🚫 "Predicted funding" treated as known at signal time
- 🚫 Silent overwrite of archive data — corrections logged

### Code
- 🚫 Production code without code-reviewer audit
- 🚫 Sharpe number reported without strategy-validation audit
- 🚫 Commit failing ruff lint

### Strategy-specific
- 🚫 Carry strategy in production without funding-regime detection
- 🚫 Cross-sectional alpha without sector-neutral / size-neutral construction

---

## Open Questions / Deferred Decisions

| # | Question | Decision phase |
|---|---|---|
| 1 | ~~Monthly capital addition amount~~ → **$0/mo (committed 2026-04-30)**, revisit if income/savings change | resolved |
| 2 | Purged CV vs walk-forward — which for parameter validation | Phase A early |
| 3 | ~~Universe top-20 cutoff method~~ → **Resolved Phase A.2** (commit `9f01cee`): top-20 by Binance 24h quote volume + 180d listing-age filter + spot-pair eligibility flag (`spot_pairs` list, not bool) + `total_candidates` audit. 4-endpoint atomicity guard within 5s window. Live snapshot `live_24hr_top_n_v2`; historical from archived klines = future method id. | resolved |
| 4 | ~~mlfinlab vs from-scratch DSR/PBO implementation~~ → **Resolved Phase A.4**: from-scratch in `src/alpha_factory/validation/{dsr,pbo}.py` (mlfinlab is Python 3.11-incompatible; from-scratch is 400 LOC, fully tested) | resolved |
| 5 | Oracle ARM SSH access details + cron scheduling | Phase A end / Phase B early |
| 6 | Deribit account opening for VRP | Phase D end |
| 7 | Multi-exchange (Bybit / OKX) addition | Phase E |
| 8 | Tax accounting integration | Phase D end |
| 9 | **Carry V3 funding-compression detector** — V2 catches absolute negative-funding regimes but is blind to compression-toward-zero (2025-2026 threat). Add 30d-MA < 0.5bp/8h → exit, or similar. Required before Phase D live capital. | Phase B early |
| 10 | **Carry V2 last-3m Sharpe = -0.95 watch**: if Q3-2026 rolling 3m stays negative, revoke V2 verdict and require V3 before any further use. Track monthly. | Phase B continuous |
| 11 | **Backfill remaining 15 perps** — A.5.3 fetched only top-5 (BTC/ETH/SOL/BIO/LAB). For Phase B XS factors (funding XS, momentum, basis MR), need full top-20 archive. ~12k weight, ~8 min wall. | Phase B early |

---

## Skill Mapping

`.claude/settings.json` UserPromptSubmit hook enforces routing. First line of any substantive response states which skill applies.

| Task | Skill |
|---|---|
| Designing data archive / quality checks | `anthropic-skills:market-data-pipeline` |
| Designing alpha logic | `anthropic-skills:alpha-factor-research` |
| Computing Sharpe / DSR / PBO / running backtest | `anthropic-skills:strategy-validation` |
| Combining strategies / sizing | `anthropic-skills:portfolio-construction` |
| Paper / live execution | `anthropic-skills:live-trading-execution` |
| Reviewing newly written script | `code-reviewer` |
| Refactor cleanup | `simplify` |
| Results table | `anthropic-skills:xlsx` |
| General quant analysis | `anthropic-skills:quant-analyst` |

---

## References

- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- López de Prado, M. (2020). *Machine Learning for Asset Managers*. Cambridge.
- Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio". JPM.
- Grinold & Kahn (2000). *Active Portfolio Management*. McGraw-Hill.
- Lo, A. (2002). "The Statistics of Sharpe Ratios". Financial Analysts Journal.
- Bitwise / Coinshares quarterly crypto factor reports.

---

*This document is the canonical project spec. CLAUDE.md is the per-session operating ruleset. Any change to this file requires explicit conversation decision.*
