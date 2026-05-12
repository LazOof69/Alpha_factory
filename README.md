# Alpha Factory

Personal quant alpha factory for crypto.

> **Current phase: C — paper-trade infrastructure.**
> Phase 0 (FS) → A (L1 archive) → B (alpha + L3 validation) complete. `carry_v3` on BTC is portfolio-eligible; the carry_v3 family extension to ETH / SOL is registered in [`validated_alphas.yaml`](validated_alphas.yaml) with locked status and caveats. Next: paper-trade infra implementation per [`docs/`](docs/), then 3-month paper trade.
>
> Authoritative state lives in [`PROJECT.md`](PROJECT.md) (decisions, roadmap, open questions) and [`CLAUDE.md`](CLAUDE.md) (per-session operating rules, red lines, conventions). This README is a digest.

## Approach

A five-layer factory mirroring institutional alpha pipelines, scaled
down to retail capital ($1k start, Binance USDT-M perp + spot only):

| Layer | Role | Outputs |
|---|---|---|
| **L1 Data** | Point-in-time archive ingest + QC | Tz-aware UTC parquet at `data/{klines,funding,...}/year=YYYY/data.parquet`; UPSERT semantics; corrections logged, never silent |
| **L2 Alpha** | Signal logic + per-bar backtest | `CarryParams` / `CarryV3Params` etc.; emits legs, per-symbol contrib, equity curve |
| **L3 Validation** | Registry + LdP gates (DSR / PBO / regime / costs) | `RUN_REGISTRY` row per backtest; verdict pass/fail recorded atomically with `metrics_summary_json` |
| **L4 Portfolio** | HRP-weighted combination of validated alphas (Phase D+) | Per-strategy weights with correlation-aware sizing |
| **L5 Execution** | Paper-trade → live; halt controller; event log (Phase C+) | Order intent / fill record / drift monitor / 3-quantity daily reconcile |

Validation discipline follows **López de Prado** rigor: every alpha
must clear **DSR > 0** (deflated Sharpe accounting for trial inflation
via `n_trials = count_distinct(params_hash)` per strategy), **PBO ≤ 0.5**
(CSCV cross-validation, ≥ 8 trials required), regime stratification
(pre/post-ETF, bull/bear, vol terciles), and a **3-month paper-trade
gate** before any live capital. No backtest Sharpe is reported as a
forward expectation — the convention is **forward Sharpe ≈ backtest
× [0.3, 0.5] haircut**.

## Phase roadmap

| Phase | Scope | Status | Key deliverables |
|---|---|---|---|
| 0 | Feasibility study — does crypto carry exist? Is arb feasible at retail? | Done | [`feasibility/README.md`](feasibility/README.md); answer: carry yes, arb no (HFT-saturated) |
| A | L1 data archive (Binance USDT-M perp + spot, point-in-time correct) | Done | UPSERT ingest, QC gate at 0 ERROR, 50k+ hourly bars / symbol, corrections sidecar |
| B | L2 alpha + L3 validation spine | Done | `carry_v2` → `carry_v3` (compression-aware) with 18-trial PBO sweep; cross-symbol PBO on ETH/SOL; cross-sectional `funding_xs` / `momentum_xs` explored and PARKED |
| C | Paper-trade infrastructure — live signal compute, halt controller, event log, daily 3-quantity reconcile | In progress | Design v2 (post-architecture-level adversarial-debate); implementation pending |
| D | 3-month paper trading + L4 portfolio (HRP) | Pending | Tracking error < 30% vs backtest replay; multi-alpha HRP-weighted portfolio |
| E | Live capital deployment ($1k initial cap, ≤ 2× leverage) | Pending | Real money; live track record |

## Validated alphas (current)

[`validated_alphas.yaml`](validated_alphas.yaml) is the canonical
registry; this table is the digest of locked verdicts.

| ID | Status | Portfolio-eligible | Notes |
|---|---|---|---|
| `carry_v3` (BTC) | shipping | ✓ | Post-sweep defaults (`exit=0.05 bp/8h`, `lookback=120d`); PBO 0.3035 PASS; Sharpe surface FLAT (gap 0.27 across thresholds) |
| `carry_v3-eth` | on_deck | conditional on Phase C m2 paper-trade success | Per-symbol PBO 0.6302 **FAIL**; mitigated by locking BTC-derived threshold rather than re-tuning; see [`docs/alphas/carry_v3.md`](docs/alphas/carry_v3.md) Multi-Symbol section |
| `carry_v3-sol` | deferred indefinitely | — | Per-symbol PBO 0.6342 **FAIL** + marginal backtest (full-Sharpe 0.89, last-12m 0.34, max_dd −7.8%); revisit only on capital scaling or regime change |
| `carry_v2` (BTC) | superseded by v3 | — | Retained for audit / cross-check only |
| `funding_xs`, `momentum_xs` | PARKED | — | 3 rounds of adversarial-debate; concluded not portfolio-additive at retail capital + cost structure. Do not relitigate without paper-trade signal motivating it |

## Stack

- **Python 3.11** + uv (env + lock; `.python-version` and pyproject pin)
- **polars** + pyarrow + duckdb — deliberate; no pandas in production paths (5–10× faster on our archive sizes)
- scipy + numpy for stats / bootstrap / CSCV
- xlsxwriter for `results/*.xlsx` reports (polars' `write_excel` backend)
- pytest for per-module tests (isolated `tmp_path` per test so registry state never leaks)
- ruff for lint (`commit failing ruff lint` is a CLAUDE.md red line)

## Layout

```text
src/alpha_factory/    Production code (Phase A+)
  data/               L1 — archive ingest, QC, schemas
  alpha/              L2 — carry, carry_v3, ... (signal logic + backtest)
  validation/         L3 — registry, DSR, PBO, regime, costs, metrics
  portfolio/          L4 — HRP, sizing (Phase D+)
  execution/          L5 — paper-trade, live (Phase C+)
  runner.py           End-to-end orchestration (L1 → L2 → L3) with
                      pluggable backtest_fn + strategy_id
scripts/              Drivers, sweeps, multi-symbol harnesses
tests/                pytest, per-module
docs/                 Design + audit + alpha docs
  alphas/             Per-alpha audit docs (carry_v3.md, ...)
  phase_c_*.md        Phase C paper-trade plan + infra design v2
feasibility/          Phase 0 frozen historical record (do not modify)
results/              xlsx reports (output; gitignored)
validated_alphas.yaml Canonical alpha registry (single source of truth)
PROJECT.md            Decisions, roadmap, open questions
CLAUDE.md             Per-session operating rules, red lines, conventions
.claude/              Claude Code config + vendored skills
```

## Operating model

Claude is the primary contributor; the human reviews and gates
decisions. Workflow conventions live in [`CLAUDE.md`](CLAUDE.md):

- **Skill routing**: every substantive response declares which Alpha
  Factory skill applies in line 1 (e.g. `routing: anthropic-skills:strategy-validation`).
  The routing is enforced by a `UserPromptSubmit` hook.
- **Adversarial debate**: high-stakes decisions (strategy verdicts,
  irreversible designs, capital-deployment recommendations) run a
  3-phase **Audit → Attack → Resolve** protocol before being locked.
  Lower-stakes designs use a lightweight pseudocode gut-check.
- **Principles** (from [`CLAUDE.md`](CLAUDE.md)): suspicion over
  enthusiasm, evidence over opinion, LdP rigor, simplicity over
  complexity, verifiability over functionality, honesty over smoothness.

## Skills

| Layer | Skill |
|---|---|
| L1 Data | `anthropic-skills:market-data-pipeline` |
| L2 Alpha | `anthropic-skills:alpha-factor-research` |
| L3 Validation | `anthropic-skills:strategy-validation` |
| L4 Portfolio | `anthropic-skills:portfolio-construction` |
| L5 Execution | `anthropic-skills:live-trading-execution` |
| Cross-cutting | `code-reviewer`, `simplify`, `adversarial-debate`, `xlsx`, `quant-analyst` |

## Red lines

Selected subset; the full list is in [`CLAUDE.md`](CLAUDE.md).

- 🚫 No alpha enters L4 portfolio without **DSR > 0 (95% CI) AND PBO ≤ 0.5**.
- 🚫 No live capital without **3-month paper trading** first (tracking error < 30% vs backtest replay).
- 🚫 No backtest Sharpe reported as forward expectation — apply **× 0.3-0.5 haircut**.
- 🚫 Single alpha > 50% portfolio weight; leverage > 2× in Phase D.
- 🚫 Carry strategy in production without a **funding-regime detection layer**.
- 🚫 Timezone-naive datetimes anywhere; yfinance for serious backtest.
- 🚫 Production code without **code-reviewer audit**; failing ruff lint commits.
- 🚫 Silent overwrite of archive data — corrections must be logged.

## Running

```bash
uv sync                 # install deps (locked via uv.lock)
uv run pytest           # unit tests (isolated tmp_path per test)
uv run ruff check .     # lint
```

Run carry_v3 family backtest end-to-end via the L3 spine:

```bash
# from a CWD where the L1 archive lives at ./data/
uv run python scripts/run_carry_v3_multi_symbol.py
```

The harness registers each symbol's run in `data/.state/registry/runs.parquet`
under distinct strategy_ids (`carry_v3` / `carry_v3-eth` / `carry_v3-sol`)
and prints the 3 canonical run_ids at the end for copy into
[`validated_alphas.yaml`](validated_alphas.yaml).

## Capital reality

Forward dollar return at various capital × Sharpe combinations,
**after** the × 0.3 conservative haircut on backtest Sharpe:

| Capital | Sharpe 1.0 fwd | Sharpe 1.5 fwd | Sharpe 2.0 fwd |
|---|---|---|---|
| **$1k** (Phase D start) | ~$30 / yr | ~$45 / yr | ~$60 / yr |
| $10k | ~$300 / yr | ~$450 / yr | ~$600 / yr |
| $100k | ~$3k / yr | ~$4.5k / yr | ~$6k / yr |

Start: $1k initial, $0 / month addition committed (2026-04-30). At
realistic forward Sharpe 1.5 (post-haircut from backtest ~3.3), expected
annual is **~$45–75 in year 1**. **The 12-month deliverable is the
platform + validated alphas + paper-trade record, NOT material income.**
Math detail in `CLAUDE.md` "Honest Framing".

Compound + leverage scale to $10k+ over **24–36 months** for material
monthly numbers — but the project's success metric is *forward Sharpe
stability*, not next-12-month dollar return.

---

# 中文版 (zh-TW)

個人加密貨幣量化策略工廠。

> **目前階段:Phase C — 紙上交易基礎設施。**
> Phase 0(FS)→ A(L1 歸檔)→ B(alpha + L3 驗證)已完成。`carry_v3`(BTC)可入組合;carry_v3 家族擴展到 ETH / SOL 的鎖定 status 與 caveats 見 [`validated_alphas.yaml`](validated_alphas.yaml)。下一步:依 [`docs/`](docs/) 實作紙上交易基礎設施,接著 3 個月紙上交易。
>
> 權威狀態:[`PROJECT.md`](PROJECT.md)(決策、路線、未決問題)+ [`CLAUDE.md`](CLAUDE.md)(操作規則、紅線、慣例)。本 README 是摘要。

## 設計取向

五層工廠架構,對照機構級 alpha pipeline,縮放至 retail 資本($1k 起步,僅 Binance USDT-M perp + spot):

| 層 | 角色 | 產出 |
|---|---|---|
| **L1 Data** | Point-in-time 歸檔 + QC | tz-aware UTC parquet 於 `data/{klines,funding,...}/year=YYYY/data.parquet`;UPSERT 寫入;校正必登記 |
| **L2 Alpha** | 訊號邏輯 + per-bar backtest | `CarryParams` / `CarryV3Params`;產出 legs、per-symbol contrib、equity curve |
| **L3 Validation** | Registry + LdP 閘門(DSR / PBO / regime / costs) | `RUN_REGISTRY` 每 backtest 一列;verdict 連同 `metrics_summary_json` 原子寫入 |
| **L4 Portfolio** | HRP 加權組合(Phase D+) | 各策略權重,考慮 correlation |
| **L5 Execution** | Paper-trade → live;halt 控制器;event log(Phase C+) | order intent / fill / drift / 每日 3-quantity reconcile |

驗證紀律遵循 **López de Prado** 嚴格度:每個 alpha 必須通過 **DSR > 0**(考慮 trial inflation,`n_trials = count_distinct(params_hash) per strategy`)、**PBO ≤ 0.5**(CSCV,需 ≥ 8 trials)、regime 分層(pre/post-ETF、bull/bear、vol terciles)、以及 **3 個月紙上交易** 才能用實盤資金。**backtest Sharpe 永遠不直接當作 forward 期望**——慣例是 **forward Sharpe ≈ backtest × [0.3, 0.5] haircut**。

## 階段路線

| 階段 | 範圍 | 狀態 | 主要交付 |
|---|---|---|---|
| 0 | 可行性研究——crypto carry 是否存在?retail 能做 arb 嗎? | 完成 | [`feasibility/README.md`](feasibility/README.md);結論:carry 存在,arb 不行(HFT 飽和) |
| A | L1 資料歸檔(Binance USDT-M perp + spot,point-in-time) | 完成 | UPSERT 寫入、QC 0 ERROR、每 symbol 50k+ 小時棒、corrections sidecar |
| B | L2 alpha + L3 驗證 spine | 完成 | `carry_v2` → `carry_v3`(compression-aware)18-trial PBO sweep;ETH/SOL cross-symbol PBO;`funding_xs` / `momentum_xs` 探索後 PARKED |
| C | 紙上交易基礎設施——live signal compute、halt 控制器、event log、每日 3-quantity reconcile | 進行中 | Design v2(post-架構級 adversarial-debate);實作 pending |
| D | 3 個月紙上交易 + L4 組合(HRP) | 等待 | tracking error < 30% vs backtest replay;HRP 多 alpha 組合 |
| E | 實盤資金部署($1k 初始上限,槓桿 ≤ 2×) | 等待 | 實盤;live track record |

## 已驗證 alpha(目前)

[`validated_alphas.yaml`](validated_alphas.yaml) 為準;此表是鎖定 verdict 摘要。

| ID | 狀態 | 可入組合 | 備註 |
|---|---|---|---|
| `carry_v3` (BTC) | shipping | ✓ | Post-sweep 預設值(`exit=0.05 bp/8h`、`lookback=120d`);PBO 0.3035 PASS;Sharpe surface 平坦(threshold 變動間 gap 0.27) |
| `carry_v3-eth` | on_deck | 視 Phase C m2 紙上交易是否成功 | Per-symbol PBO 0.6302 **FAIL**;透過鎖定 BTC threshold(不另跑 ETH-only sweep)做緩解;見 [`docs/alphas/carry_v3.md`](docs/alphas/carry_v3.md) Multi-Symbol 段落 |
| `carry_v3-sol` | 無限期 deferred | — | Per-symbol PBO 0.6342 **FAIL** + 邊際 backtest(full-Sharpe 0.89、last-12m 0.34、max_dd −7.8%);資金規模或 regime 變化才重啟 |
| `carry_v2` (BTC) | 由 v3 取代 | — | 保留作 audit / cross-check |
| `funding_xs`、`momentum_xs` | PARKED | — | 經 3 輪 adversarial-debate;在 retail 資本 + cost 結構下無組合增量。**不要再爭辯** — 除非紙上交易訊號明顯指向重啟 |

## 技術棧

- **Python 3.11** + uv(環境 + lock;`.python-version` 與 pyproject 鎖版)
- **polars** + pyarrow + duckdb — production path 不放 pandas(我們的 archive size 上 polars 快 5–10×)
- scipy + numpy 做 stats / bootstrap / CSCV
- xlsxwriter 寫 `results/*.xlsx`(polars `write_excel` 後端)
- pytest 做 per-module 測試(每測試 isolated `tmp_path`,registry state 不會洩漏)
- ruff lint(CLAUDE.md 紅線:**commit 失敗的 ruff lint 是禁止**)

## 操作模型

Claude 是主要貢獻者;人類做 review 與決策閘門。慣例見 [`CLAUDE.md`](CLAUDE.md):

- **Skill routing**:每個實質回覆第一行宣告本回合用哪個 Alpha Factory skill(例:`routing: anthropic-skills:strategy-validation`)。由 `UserPromptSubmit` hook 強制執行。
- **Adversarial debate**:高風險決策(策略 verdict、不可逆設計、實盤資金部署)會跑 3 階段 **Audit → Attack → Resolve** 協議才鎖定。低風險設計用輕量級 pseudocode gut-check。
- **原則**(來自 [`CLAUDE.md`](CLAUDE.md)):suspicion over enthusiasm、evidence over opinion、LdP rigor、simplicity over complexity、verifiability over functionality、honesty over smoothness。

## 紅線(節錄)

完整清單見 [`CLAUDE.md`](CLAUDE.md)。

- 🚫 alpha 未通過 **DSR > 0(95% CI)且 PBO ≤ 0.5** 不可進 L4 組合。
- 🚫 未經 **3 個月紙上交易**(tracking error < 30% vs backtest replay)不可用實盤資金。
- 🚫 backtest Sharpe 不可直接報為 forward 期望 — 必須套 **× 0.3-0.5 haircut**。
- 🚫 單一 alpha 權重 > 50%;Phase D 槓桿 > 2×。
- 🚫 carry 策略上 production 沒接 **funding-regime detection layer**。
- 🚫 任何 timezone-naive datetime;yfinance 拿來做 serious backtest。
- 🚫 production code 未經 **code-reviewer audit**;commit 失敗的 ruff lint。
- 🚫 靜默覆蓋 archive 資料 — corrections 必須有記錄。

## 跑起來

```bash
uv sync                 # 裝依賴(uv.lock 鎖版)
uv run pytest           # 跑測試(每測試 isolated tmp_path)
uv run ruff check .     # lint
```

跑 carry_v3 家族 backtest(經 L3 spine):

```bash
# 在有 ./data/ L1 archive 的 CWD 下執行
uv run python scripts/run_carry_v3_multi_symbol.py
```

腳本會把各 symbol 的 run 寫入 `data/.state/registry/runs.parquet`(strategy_id 分別為 `carry_v3` / `carry_v3-eth` / `carry_v3-sol`),最後印出 3 個 canonical run_id 供貼回 [`validated_alphas.yaml`](validated_alphas.yaml)。

## 資金現實

不同資本 × Sharpe 組合下的 forward 年化美元收益,**已套用 × 0.3 保守 haircut**:

| 資本 | Sharpe 1.0 fwd | Sharpe 1.5 fwd | Sharpe 2.0 fwd |
|---|---|---|---|
| **$1k**(Phase D 起步) | ~$30 / 年 | ~$45 / 年 | ~$60 / 年 |
| $10k | ~$300 / 年 | ~$450 / 年 | ~$600 / 年 |
| $100k | ~$3k / 年 | ~$4.5k / 年 | ~$6k / 年 |

起點:$1k 初始,**$0 / 月追加**(2026-04-30 鎖定)。在現實的 forward Sharpe 1.5(由 backtest ~3.3 經 haircut)下,**第 1 年期望約 $45–75**。**12 個月的實際交付是 platform + validated alphas + paper-trade 記錄,不是有意義的收入。** 數學細節見 `CLAUDE.md` 「Honest Framing」一節。

經由 compound + leverage,在 **24–36 個月** 內累積到 $10k+,月入才開始有意義——但專案的成功指標是 **forward Sharpe 穩定度**,而不是下個 12 個月的美元收益。
