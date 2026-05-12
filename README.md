# Alpha Factory

Personal quant alpha factory for crypto.

> **Current phase: C — paper-trade infrastructure.**
> Phase 0 (FS) → A (L1 archive) → B (alpha + L3 validation) complete. `carry_v3` on BTC is portfolio-eligible; the carry_v3 family extension to ETH / SOL is registered in [`validated_alphas.yaml`](validated_alphas.yaml) with current status and caveats. Next: paper-trade infra implementation per [`docs/`](docs/), then 3-month paper trade.
>
> Authoritative state: [`PROJECT.md`](PROJECT.md) (decisions, roadmap, open questions) and [`CLAUDE.md`](CLAUDE.md) (per-session operating rules, red lines, conventions).

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Feasibility study | Done — see [`feasibility/README.md`](feasibility/README.md) |
| A | L1 data archive (Binance USDT-M perp + spot, point-in-time) | Done |
| B | L2 alpha + L3 validation (registry, DSR, PBO, regime, costs) | Done |
| C | Paper-trade infrastructure (live signal, halt, event log) | In progress |
| D | 3-month paper trading + L4 portfolio (HRP) | Pending |
| E | Live capital deployment ($1k cap initially) | Pending |

## Stack

- Python 3.11 + uv
- polars + pyarrow + duckdb (data)
- scipy + numpy (stats)
- pytest, ruff

## Layout

```text
src/alpha_factory/    Production (Phase A+)
  data/               L1 — archive ingest, QC
  alpha/              L2 — carry, carry_v3, ...
  validation/         L3 — registry, DSR, PBO, regime, costs
  portfolio/          L4 — HRP, sizing (Phase D+)
  execution/          L5 — paper-trade, live (Phase C+)
  runner.py           End-to-end orchestration (L1 → L2 → L3)
scripts/              Drivers, sweeps, multi-symbol harnesses
tests/                pytest, per-module
docs/                 Design + audit + alpha docs
feasibility/          Phase 0 frozen historical record
.claude/              Claude Code config + vendored skills
```

## Skills routing

Per [`CLAUDE.md`](CLAUDE.md); the routing choice is declared in line 1 of every substantive reply.

| Layer | Skill |
|---|---|
| L1 Data | `anthropic-skills:market-data-pipeline` |
| L2 Alpha | `anthropic-skills:alpha-factor-research` |
| L3 Validation | `anthropic-skills:strategy-validation` |
| L4 Portfolio | `anthropic-skills:portfolio-construction` |
| L5 Execution | `anthropic-skills:live-trading-execution` |
| Cross-cutting | `code-reviewer`, `simplify`, `adversarial-debate`, `xlsx`, `quant-analyst` |

## Running

```bash
uv sync                 # install deps
uv run pytest           # unit tests
uv run ruff check .     # lint
```

Backtest a single carry_v3 instance via the L3 spine:

```bash
# from a CWD where the L1 archive lives at ./data/
uv run python scripts/run_carry_v3_multi_symbol.py
```

## Capital reality

$1k initial, $0 / month addition. At Sharpe 1.5 ≈ $75 / year; at Sharpe 2 ≈ $200 / year. **The 12-month deliverable is the platform + validated alphas + paper-trade record, not material income.** Math in `CLAUDE.md` "Honest Framing"; compound + leverage scale to $10k+ over 24-36 months for material monthly numbers.

---

# 中文版 (zh-TW)

個人加密貨幣量化策略工廠。

> **目前階段:Phase C — 紙上交易基礎設施。**
> Phase 0(FS)→ A(L1 歸檔)→ B(alpha + L3 驗證)已完成。`carry_v3`(BTC)可入組合;carry_v3 家族擴展到 ETH / SOL 的目前狀態與 caveats 見 [`validated_alphas.yaml`](validated_alphas.yaml)。下一步:依 [`docs/`](docs/) 實作紙上交易基礎設施,接著 3 個月紙上交易。
>
> 權威狀態:[`PROJECT.md`](PROJECT.md)(決策、路線、未決問題)+ [`CLAUDE.md`](CLAUDE.md)(操作規則、紅線、慣例)。

## 階段路線

| 階段 | 範圍 | 狀態 |
|---|---|---|
| 0 | 可行性研究 | 已完成 — 見 [`feasibility/README.md`](feasibility/README.md) |
| A | L1 資料歸檔(Binance USDT-M perp + spot,point-in-time) | 已完成 |
| B | L2 alpha + L3 驗證(registry / DSR / PBO / regime / costs) | 已完成 |
| C | 紙上交易基礎設施(live signal / halt / event log) | 進行中 |
| D | 3 個月紙上交易 + L4 組合(HRP) | 等待中 |
| E | 實盤資金部署(初始上限 $1k) | 等待中 |

## 技術棧

- Python 3.11 + uv
- polars + pyarrow + duckdb(資料)
- scipy + numpy(統計)
- pytest、ruff

## 跑起來

```bash
uv sync                 # 裝依賴
uv run pytest           # 跑測試
uv run ruff check .     # lint
```

跑單一 carry_v3 instance(經 L3 spine):

```bash
# 在有 ./data/ L1 archive 的 CWD 下執行
uv run python scripts/run_carry_v3_multi_symbol.py
```

## Skill 路由

每個實質回覆的第一行宣告本回合套用的 skill,規則寫在 [`CLAUDE.md`](CLAUDE.md)。

## 資金現實(別騙自己)

$1k 初始,$0 / 月追加。Sharpe 1.5 約對應 $75 / 年;Sharpe 2 約 $200 / 年。**12 個月的實際交付是 platform + validated alphas + paper-trade 記錄,不是有意義的收入。** 數學細節見 `CLAUDE.md` 「Honest Framing」一節;經由 compound + leverage,在 24-36 個月內累積到 $10k+,月入才開始有意義。
