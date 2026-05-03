# Phase C — Paper Trade carry_v3 Family

> **Status:** PLAN (not yet implemented)
> **Created:** 2026-05-04
> **Decision context:** 3rd-round adversarial-debate concluded "ship the
> shippable asset"; Phase B Step 1 produced carry_v3 (BTC) PBO-validated
> + Phase B Step 2 added ETH/SOL via option (c). Phase C is the
> live-money-bridge.

---

## Goal

Run the carry_v3 family (BTC + ETH + SOL) as a paper trade on Binance
for ≥ 3 months (CLAUDE.md hard requirement), producing a continuous
record of:

1. Per-instance signal output vs backtest expectation
2. Realized fill prices, fees, funding settlement vs modeled
3. Tracking error: (realized P&L) - (backtest-replay P&L) on the same
   data window
4. Position state (active vs exited) regime transitions in real time

Pass/fail decision after 3-month window:

- **PASS → Phase D**: tracking error < 30%, no red-line breaches, regime
  transitions match backtest within reasonable noise
- **FAIL**: re-investigate before risking live capital

---

## Required infrastructure (NOT YET BUILT)

### 1. Live signal compute layer
- **Source**: same `run_carry_v3_backtest` core but in streaming mode.
  At each new funding settlement (every 8h), recompute V3 state using
  the latest 120-settle window and emit position target.
- **Output**: target position per (symbol, market, side) → JSON or row
  in a `paper_trades` table.
- **Cadence**: triggered by new funding row appended to L1 archive
  (cron every 30 min checking new settlements).

### 2. Binance read-only API access
- **Purpose**: pull mark price + funding rate (already done by L1) +
  account balance + open orders (NEW, needed to verify fills).
- **Auth**: read-only API key; no withdraw permission.
- **Rate limits**: same breaker pattern as L1 archive runner.

### 3. Paper position book
- **Storage**: SQLite or parquet with rows = (timestamp, symbol, market,
  side, qty, entry_price, exit_price, fees_paid, funding_received).
- **Lifecycle**: when signal flips active→exited, simulate market-order
  unwind at next-bar-open price + slippage model. When exited→active,
  same in reverse.

### 4. Daily reconciliation script
- **Purpose**: every 24h, compute:
  - realized P&L for last 24h (per instance + total)
  - backtest-replay P&L for same 24h
  - tracking error = (realized - replay) / replay
- **Output**: append row to `data/paper_trade/daily_reconcile.parquet`
- **Alert threshold**: |tracking_error| > 0.30 → log warning, surface
  to user

### 5. Halt mechanism
- **Triggers** (any one halts paper trade automatically):
  - 3 consecutive days of |tracking_error| > 0.50
  - Realized max_dd > 5% (PROJECT.md "1% max DD per day" red line)
  - Binance API outage > 4h
  - Any signal computation crash
- **Action**: flat the book, record reason in `paper_trade_halts.parquet`,
  email/log alert.

### 6. Cost model
- **Fees**: Binance VIP-0 (default retail tier): 8 bp spot maker,
  10 bp spot taker, 5 bp perp maker, 5 bp perp taker. Round-trip = 16 bp.
- **Slippage**: zero at FS notional (~$1k); confirm empirically post-paper.
- **Funding**: actual settlement amounts captured by L1 funding fetch.

---

## User decisions needed BEFORE implementation

| Decision | Options | My recommendation |
|---|---|---|
| API access tier | (a) Spot-only testnet (b) Real Binance read-only (c) Real Binance with trading-disabled key | **(b)** — testnet has different funding regime; real API gives true tracking-error signal |
| Capital target for paper | (a) $1k notional (true FS) (b) $100 notional (cheap testing) (c) $0 (pure simulation) | **(a)** — fees + slippage scale with notional; only $1k matches forward deployment |
| Hosting infra | (a) Local cron on user's machine (b) Oracle ARM (PROJECT.md "L1 production") (c) skip cron, manual run | **(b)** — Oracle ARM already provisioned for L1 daily; reuse |
| Paper trade scope | (a) BTC only (lowest risk) (b) BTC + ETH (red-line satisfaction) (c) BTC + ETH + SOL (Path Y) | **(b) for first month, expand to (c) for month 2-3** |
| Monitoring cadence | (a) Daily reconcile only (b) Daily + per-transition alerting (c) Per-bar telemetry | **(b)** |

---

## Effort estimate (HONEST)

| Component | Estimated dev time |
|---|---|
| Live signal compute (L5) | 4-6 hr |
| Binance read-only API client (extending L1 client) | 2 hr |
| Paper position book storage + lifecycle | 3-4 hr |
| Daily reconcile script | 2-3 hr |
| Halt mechanism + alerting | 2-3 hr |
| Tests (per layer) | 4-6 hr |
| Oracle ARM cron deployment | 2 hr |
| **Total** | **~20-26 hr** |

This is approximately **1 working week** of focused dev time. Plus ≥ 3
months of wall-clock waiting for the paper-trade signal.

---

## Pre-Phase-C tasks (residual Phase B cleanup, can do in parallel)

These don't block paper-trade infra but improve registry cleanliness:

1. **trial_log key fix**: add `symbol` (or `symbols_tuple_hash`) to the
   trial_log key so multi-symbol same-params runs don't collide. ~1 hr
   + tests.
2. **PBO sweep on ETH + SOL**: extend v3_pbo_sweep.py harness to
   per-symbol sweep. Verify thresholds (0.05bp, 120d) are robust on
   ETH and SOL funding regimes. Currently SWEPT ON BTC ONLY. ~1 hr.
3. **carry_v3 doc update**: add multi-symbol section + reference
   carry_v3-eth and carry_v3-sol entries. ~30 min.

---

## Decision tree

```
NOW (after this commit + push)
  │
  ├─ continue Phase B cleanup (trial_log fix + ETH/SOL PBO sweep)?
  │    YES → 2-3 hr work, then proceed to Phase C infra
  │    NO  → skip; accept exploratory metrics on ETH/SOL; proceed to
  │          Phase C infra immediately
  │
  ├─ Phase C infra (build paper-trade plumbing)
  │    20-26 hr work, single user-week sprint
  │
  └─ Wait 3 months for paper-trade signal + decide on Phase D (live)
```

---

## What NOT to do (debate-loop avoidance)

Per 3rd-round adversarial-debate verdict on 2026-05-04:
- Don't build more L2 alphas before paper trade signals back
- Don't run another funding_xs/momentum_xs design iteration
- Don't expand universe further without specific hypothesis
- Don't add new orthogonality work until 3 carry instances have been
  paper-traded

The next big alpha-research cycle should be **driven by paper-trade
findings**, not by another design loop. If carry_v3 family paper trade
shows tracking error > 30% or unexpected regime behavior, that's a
real signal worth investigating. Without that signal, more alpha
design is unjustified at retail capital.
