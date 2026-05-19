# Next Steps — 2026-05-13 (post provenance-loss pivot)

> Handoff snapshot。權威狀態仍是 [`PROJECT.md`](PROJECT.md) + [`CLAUDE.md`](CLAUDE.md) + [`validated_alphas.yaml`](validated_alphas.yaml)。

---

## ⚠ 頭等大事:Provenance Loss(2026-05-01)

production L1 archive(`data/klines`、`data/funding`)、backtest artifacts(`data/validation/`)、RUN_REGISTRY + TRIAL_LOG(`data/.state/registry/`)在 2026-05-01 OneDrive→ASCII relocation 時**全毀**(gitignored 沒被帶走;Phase A-B 在 Windows 跑、不在 quant-1)。2026-05-13 窮盡驗證**不可復原**。只剩 Phase 0 `feasibility/data/`(BTC/ETH、無 SOL)。

**經 adversarial-debate + strategy-validation 裁決(B1/B3)**:
- carry_v3(BTC)**降級**:`status: suspended`、`portfolio_eligible: false`。理由:CLAUDE.md 紅線「無可驗證 DSR/PBO 不得入 L4」「Sharpe/DSR 無 audit 不得上報」現皆被違反——audit trail 已毀。
- 所有 yaml 數字 = **frozen historical record,非 current claim**;保留不刪(刪=竄改)。
- 經濟論述/機制/鎖定參數**程式裡都還在**,失去的是 *empirical verification* 不是設計。
- **唯一 requalification path = Phase C 3 個月 paper-trade**(它不消費歷史 archive,自產 fresh forward 資料)。paper-trade 從「下一步」升級為**唯一能讓 carry_v3 重獲資格的路徑,無 backtest 後援**。

已標註:`validated_alphas.yaml` 頂部 `registry_provenance` block + carry_v3 entry;`docs/alphas/carry_v3.md` 頂部 admonition。

---

## Q1 收尾狀態

| 子項 | 狀態 |
|---|---|
| Q1.a — quant-1 NTP fallback | **仍有效**(獨立 ops,跟 archive 無關)。要做就 SSH quant-1 跑那段 timesyncd 指令 |
| Q1.b — multi-symbol script 重跑取 run_ids | **❌ CANCELLED**。沒 archive 可跑;且本來就是化妝品級 registry 整理,非關鍵路徑 |
| Q1.c — carry_v3.md multi-symbol section | 已 commit(`13ebd71`),內容保留為歷史 context;頂部已加 provenance admonition |
| yaml/carry_v3.md provenance 降級 | ✅ 本次完成(待 commit) |

---

## 下一步 = Phase C(現在是關鍵路徑,不是 optional)

carry_v3 suspended → **Phase C paper-trade 是唯一能讓它復活的路**。優先級從「Q2 之一」升為**專案主線**。

依賴:**v2 設計文件 `docs/phase_c_infra_design_v2.md` 在 PR #3(`claude/reverent-swirles-bc2bff`)上**,不在這個 worktree。Phase C 動工前需先處理 PR #3(merge 或把 doc 取出)。

Phase C component(依 v2 設計):
1. Live signal compute(從 fresh rolling 資料算 carry_v3 訊號;**不需歷史 archive**)
2. Halt controller(paper alert-only / live flat 雙模)
3. Event log + versioning(git_hash / data_version / clock_drift / signal_inputs_hash)— **event-log-as-source-of-truth,正是 archive 遺失的結構性解法**
4. Daily 3-quantity reconcile
5. Cost model config(Binance BNB rebate ON,round-trip taker 25 bp)

執行環境決策(已定):重活在本機 / 輕量 live signal 在新建的 AMD 1c/1GB micro。但**現在沒有重活了**(不重建 archive)——micro 直接當 paper-trade signal host。

---

## 結構性修正(別再發生)

- gitignored archive 無備份 → 單點遺失。
- Phase C **event-log-as-source-of-truth**(v2 設計)是結構解。
- 加 host-side snapshot/backup 政策:archive 與 registry 落在持久 host(micro / quant-1)後,定期 snapshot,**不再放在會被 relocation/clean 掉的 gitignored 本機路徑**。

---

## Open PRs

| PR | branch | 狀態 | 內容 |
|---|---|---|---|
| #4 | claude/peaceful-napier-802ee7 | MERGED | Q1 cleanup(script via runner + carry_v3.md + README v2) |
| #3 | claude/reverent-swirles-bc2bff | OPEN | **含 v2 design doc**(Phase C 依賴)+ cross-symbol PBO sweep + ETH/SOL downgrade。Phase C 動工前要處理 |

本次 provenance 降級的 commit 還沒推——決定走 PR 還是直接 main 待定。

---

## 不要再爭辯(已 PARKED / 已裁決)

- `funding_xs` / `momentum_xs`:3 輪 debate 後 PARKED,不重啟除非 paper-trade 訊號驅動。
- 重建 archive:debate 已裁不為化妝品恐慌重建。真要重 backtest(新 alpha / 正式 re-baseline)才做有意識 scoped 重建,且須重建 DSR n_trials(TRIAL_LOG 已毀)。
- carry_v3 經濟論述:已驗證合理,不重議;爭點只在「empirical 重新背書」= paper-trade。

---

## 參考

- [`validated_alphas.yaml`](validated_alphas.yaml) — `registry_provenance` block 為準
- [`docs/alphas/carry_v3.md`](docs/alphas/carry_v3.md) — 頂部 provenance admonition
- [`CLAUDE.md`](CLAUDE.md) — 紅線、操作規則
- [`docs/phase_c_paper_trade_plan.md`](docs/phase_c_paper_trade_plan.md) — Phase C 計畫(v2 在 PR #3)
