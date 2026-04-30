"""Carry-trade backtest (Study 1 V1) — long spot + short perp, always-on.

Strategy:
    Position: $500 long spot + $500 short perp (delta-neutral, 1x lev each leg).
    Always-on from each instrument's listing — no regime detection in V1.
    Funding income credited at every 8h settlement bar.
    Entry cost: 16 bp round-trip ($1.60 on $1000 notional). No exit cost
        (always-on; effectively the position is held forever).

Outputs (stdout markdown + xlsx workbook):
    - Sharpe: full-sample / per-year / ETF pre vs post
    - Annualized return / max DD / hit rate per regime
    - Per-symbol daily equity curve (xlsx sheet)

CLI:
    uv run python feasibility/scripts/backtest_carry.py
    uv run python feasibility/scripts/backtest_carry.py --symbols BTC-USDT
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from schema import FUNDING_ROOT, KLINES_ROOT

log = logging.getLogger(__name__)

# Bitcoin spot ETF approval — same cutoff as analyze_funding.py.
ETF_CUTOFF = datetime(2024, 1, 11, tzinfo=UTC)

DEFAULT_CAPITAL = 1000.0  # = $500 spot + $500 perp short
DEFAULT_FEE_BPS = 16.0    # entry round-trip: perp 4 + spot 10 + slippage 2

DAYS_PER_YEAR = 365  # crypto trades 24/7

# V2 regime detection thresholds. Calibrated to the economic break-even of
# transaction cost: a round-trip exit+reentry costs 16 bp; avoiding a single
# week of negative funding only pays off if cumulative 7d funding ≤ -16 bp,
# i.e. 7d MA ≤ -16/21 ≈ -0.76 bp/8h. Threshold rounded to -1 bp/8h, with
# hysteresis at re-entry to avoid flip-flopping (re-enter at +0.5 bp/8h).
# Funding distribution (from analyze_funding): p01 = -2.0 bp, median ≈ +1 bp.
EXIT_FUNDING_7DMA = -0.0001      # 7d MA < -1 bp/8h → exit
REENTRY_FUNDING_7DMA = 0.00005   # 7d MA > +0.5 bp/8h → re-enter
LOOKBACK_SETTLEMENTS = 21        # 7 days × 3 settlements/day


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_aligned_klines(symbol: str) -> pl.DataFrame:
    """Load 1h klines and inner-join spot vs perp on `open_time` for one symbol.

    Inner join naturally drops bars where one side is missing — so the
    backtest only runs on hours where both spot and perp have a bar (the
    normal case after 2019-09).
    """
    files = list(KLINES_ROOT.glob("year=*/data.parquet"))
    df = pl.read_parquet([str(f) for f in files]).filter(pl.col("symbol") == symbol)
    spot = df.filter(pl.col("market") == "spot").select(
        ["open_time", pl.col("close").alias("spot_close")]
    )
    perp = df.filter(pl.col("market") == "perp_usdt").select(
        ["open_time", pl.col("close").alias("perp_close")]
    )
    return spot.join(perp, on="open_time", how="inner").sort("open_time")


def load_funding(symbol: str) -> pl.DataFrame:
    files = list(FUNDING_ROOT.glob("year=*/data.parquet"))
    return (
        pl.read_parquet([str(f) for f in files])
        .filter(pl.col("symbol") == symbol)
        .select(["funding_time", "funding_rate"])
        .sort("funding_time")
    )


# ---------------------------------------------------------------------------
# Backtest core
# ---------------------------------------------------------------------------


def run_carry_backtest(
    symbol: str,
    capital: float = DEFAULT_CAPITAL,
    fee_bps: float = DEFAULT_FEE_BPS,
) -> pl.DataFrame:
    """Compute 1h-bar strategy returns and equity curve for one symbol.

    Strategy return per bar:
        r_t = (spot_close_t / spot_close_{t-1} - 1)             # long spot
            - (perp_close_t / perp_close_{t-1} - 1)             # short perp
            + funding_rate_t  (only when bar is a funding settlement)

    Equity:
        equity_t = capital * (1 - fee_bps / 10000) * Π_{s≤t} (1 + r_s)
    """
    aligned = load_aligned_klines(symbol)
    funding = load_funding(symbol)

    bars = aligned.with_columns(
        spot_ret=pl.col("spot_close").pct_change().fill_null(0.0),
        perp_ret=pl.col("perp_close").pct_change().fill_null(0.0),
    )

    # Funding income hits the bar whose open_time exactly matches funding_time.
    # truncate("1s") was applied at ingest, so funding_time is on exact 8h
    # boundaries and every funding settlement coincides with an hourly bar.
    bars = (
        bars.join(
            funding.rename({"funding_time": "open_time"}),
            on="open_time",
            how="left",
        )
        .with_columns(funding_income=pl.col("funding_rate").fill_null(0.0))
        .drop("funding_rate")
    )

    # Per-bar strategy return: basis change + funding (when applicable).
    # Note: we apply funding_rate to the FULL strategy notional even though
    # the perp leg is half. This is correct — Binance perp funding is paid
    # on the perp NOTIONAL ($500), and on $1000 capital this is 0.5% of
    # capital per 1bp of funding. To express as a return on $1000, we'd
    # use 0.5 × funding_rate. We use funding_rate directly here on the
    # assumption capital_per_leg = capital / 2; both legs sized equally.
    # The reported Sharpe is therefore consistent with a $1000 portfolio.
    bars = bars.with_columns(
        strategy_ret=(pl.col("spot_ret") - pl.col("perp_ret"))
        + 0.5 * pl.col("funding_income"),
    )

    # Initial equity after entry fee.
    initial = capital * (1.0 - fee_bps / 10000.0)
    bars = bars.with_columns(
        equity=initial * (1.0 + pl.col("strategy_ret")).cum_prod(),
        symbol=pl.lit(symbol),
    )
    return bars


def aggregate_to_daily(bars: pl.DataFrame) -> pl.DataFrame:
    """Aggregate 1h bars to daily — Sharpe is more standard on daily."""
    return (
        bars.with_columns(date=pl.col("open_time").dt.date())
        .group_by("date", maintain_order=True)
        .agg(
            # Compound 1h returns into the day's return: (1+r1)(1+r2)... - 1
            daily_ret=((1.0 + pl.col("strategy_ret")).log().sum().exp() - 1.0),
            equity=pl.col("equity").last(),
        )
        .sort("date")
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def _sharpe(returns: pl.Series, periods_per_year: float) -> float:
    r = returns.drop_nulls()
    if r.len() < 2 or r.std() in (None, 0):
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(periods_per_year))


def _max_dd(equity: pl.Series) -> float:
    """Most-negative running drawdown from equity series. Returns negative number."""
    if equity.len() == 0:
        return float("nan")
    peaks = equity.cum_max()
    dd = (equity / peaks) - 1.0
    return float(dd.min())


def _hit_rate(returns: pl.Series) -> float:
    r = returns.drop_nulls()
    if r.len() == 0:
        return float("nan")
    return float((r > 0).mean())


def regime_stats(bars: pl.DataFrame, symbol: str, variant: str = "V1") -> list[dict]:
    """Per-regime stats: full / per-year / ETF pre vs post. `variant` is V1 / V2."""
    daily = aggregate_to_daily(bars)
    rows: list[dict] = []

    def pack(label: str, sub: pl.DataFrame) -> dict:
        return {
            "symbol": symbol,
            "variant": variant,
            "regime": label,
            "n_days": sub.height,
            "sharpe": _sharpe(sub["daily_ret"], DAYS_PER_YEAR),
            "ann_return_pct": (
                float(sub["daily_ret"].mean() * DAYS_PER_YEAR * 100)
                if sub.height > 0 else float("nan")
            ),
            "max_dd_pct": _max_dd(sub["equity"]) * 100 if sub.height > 0 else float("nan"),
            "hit_rate_pct": _hit_rate(sub["daily_ret"]) * 100 if sub.height > 0 else float("nan"),
        }

    rows.append(pack("full", daily))
    for (year,), year_df in daily.group_by(pl.col("date").dt.year()):
        rows.append(pack(f"year_{year}", year_df))

    pre = daily.filter(pl.col("date") < ETF_CUTOFF.date())
    post = daily.filter(pl.col("date") >= ETF_CUTOFF.date())
    if not pre.is_empty():
        rows.append(pack("pre_ETF", pre))
    if not post.is_empty():
        rows.append(pack("post_ETF", post))

    return rows


# ---------------------------------------------------------------------------
# V2: regime detection on funding 7d MA
# ---------------------------------------------------------------------------


def apply_v2_regime(
    bars: pl.DataFrame,
    capital: float,
    fee_bps: float,
) -> tuple[pl.DataFrame, int]:
    """Layer regime detection on top of V1 bars. Returns (bars_v2, n_transitions).

    Logic (point-in-time correct — uses only past funding):
      - At every settlement bar, compute rolling 7d MA of funding_income.
      - If state == active and 7d MA < EXIT_FUNDING_7DMA  → exit
        If state == exited and 7d MA > REENTRY_FUNDING_7DMA → re-enter
      - Between settlements, regime state is forward-filled.
      - Each transition (active↔exited) pays a one-way cost of `fee_bps / 2`
        bp on `capital`, charged as a return hit on the transition bar.
      - When `regime == 0` (exited), strategy_ret is forced to 0.
    """
    settle = (
        bars.with_row_index("idx")
        .filter(pl.col("funding_income") != 0.0)
        .with_columns(
            funding_7dma=pl.col("funding_income").rolling_mean(
                window_size=LOOKBACK_SETTLEMENTS, min_samples=LOOKBACK_SETTLEMENTS
            )
        )
        .select(["open_time", "funding_7dma"])
    )

    # Walk settlements deciding regime state — pure Python state machine.
    regime_at_settle: dict = {}
    current = 1  # start active at first bar
    n_transitions = 0
    for row in settle.iter_rows(named=True):
        ma = row["funding_7dma"]
        new = current
        if ma is not None:
            if current == 1 and ma < EXIT_FUNDING_7DMA:
                new = 0
            elif current == 0 and ma > REENTRY_FUNDING_7DMA:
                new = 1
        if new != current:
            n_transitions += 1
        regime_at_settle[row["open_time"]] = new
        current = new

    # Forward-fill regime to every bar; mark transition bars for fee deduction.
    times = bars["open_time"].to_list()
    states: list[int] = []
    transition_cost_per_bar: list[float] = []
    one_way_cost_return = (fee_bps / 2.0) / 10000.0  # 8 bp as a fractional return
    current = 1
    for t in times:
        if t in regime_at_settle:
            new_state = regime_at_settle[t]
            transition_cost_per_bar.append(-one_way_cost_return if new_state != current else 0.0)
            current = new_state
        else:
            transition_cost_per_bar.append(0.0)
        states.append(current)

    bars = bars.with_columns(
        regime_v2=pl.Series(states),
        transition_cost=pl.Series(transition_cost_per_bar),
    )
    bars = bars.with_columns(
        # Replace strategy_ret in-place: zero out PnL when exited, deduct
        # transition costs at switch bars.
        strategy_ret=(pl.col("strategy_ret") * pl.col("regime_v2"))
            + pl.col("transition_cost"),
    )
    # Re-build equity from updated returns; same one-time entry haircut as V1.
    initial = capital * (1.0 - fee_bps / 10000.0)
    bars = bars.with_columns(
        equity=initial * (1.0 + pl.col("strategy_ret")).cum_prod(),
    )
    return bars, n_transitions


def run_v2_carry_backtest(
    symbol: str,
    capital: float = DEFAULT_CAPITAL,
    fee_bps: float = DEFAULT_FEE_BPS,
) -> tuple[pl.DataFrame, int]:
    """V2 carry — V1 bars + regime detection."""
    bars = run_carry_backtest(symbol, capital, fee_bps)
    return apply_v2_regime(bars, capital, fee_bps)


def render_md(
    stats: list[dict],
    capital: float,
    fee_bps: float,
    transitions_summary: dict[str, int] | None = None,
) -> str:
    lines = [
        "# Carry Backtest — Study 1 (V1 always-on vs V2 regime-detection)\n",
        f"Capital: ${capital:.0f} ($500 long spot + $500 short perp, delta-neutral).",
        f"Entry cost: {fee_bps:.0f} bp round-trip.",
        f"V2 exit threshold: funding 7d MA < {EXIT_FUNDING_7DMA*1e4:+.1f} bp/8h",
        f"V2 re-entry threshold: funding 7d MA > {REENTRY_FUNDING_7DMA*1e4:+.1f} bp/8h",
        f"V2 per-transition cost: {fee_bps/2:.1f} bp one-way",
        "",
    ]
    if transitions_summary:
        lines.append("## V2 regime transitions")
        for k, v in transitions_summary.items():
            lines.append(f"- {k}: **{v}** transitions")
        lines.append("")

    lines.extend([
        "## Metrics",
        "",
        "| symbol | variant | regime | n_days | Sharpe | ann.return | max DD | hit rate |",
        "|---|---|---|---|---|---|---|---|",
    ])

    def sort_key(r: dict) -> tuple:
        sym = r["symbol"]
        var = r.get("variant", "V1")
        reg = r["regime"]
        var_order = 0 if var == "V1" else 1
        if reg == "full":
            return (sym, var_order, 0, 0)
        if reg.startswith("year_"):
            return (sym, var_order, 1, int(reg[5:]))
        if reg == "pre_ETF":
            return (sym, var_order, 2, 0)
        if reg == "post_ETF":
            return (sym, var_order, 2, 1)
        return (sym, var_order, 9, 0)

    for r in sorted(stats, key=sort_key):
        lines.append(
            f"| {r['symbol']} | {r.get('variant', 'V1')} | {r['regime']} | "
            f"{r['n_days']} | {r['sharpe']:+.3f} | {r['ann_return_pct']:+.2f}% | "
            f"{r['max_dd_pct']:.2f}% | {r['hit_rate_pct']:.1f}% |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="BTC-USDT,ETH-USDT")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--xlsx", help="Path to xlsx output")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    all_stats: list[dict] = []
    daily_curves: dict[tuple[str, str], pl.DataFrame] = {}
    transitions: dict[str, int] = {}

    for sym in symbols:
        log.info("backtesting %s V1", sym)
        bars_v1 = run_carry_backtest(sym, args.capital, args.fee_bps)
        log.info("  V1: %d bars, equity $%.2f → $%.2f",
                 bars_v1.height, args.capital, bars_v1["equity"].last())
        all_stats.extend(regime_stats(bars_v1, sym, variant="V1"))
        daily_curves[(sym, "V1")] = aggregate_to_daily(bars_v1)

        log.info("backtesting %s V2", sym)
        bars_v2, n_trans = run_v2_carry_backtest(sym, args.capital, args.fee_bps)
        transitions[sym] = n_trans
        log.info("  V2: %d transitions, equity $%.2f → $%.2f",
                 n_trans, args.capital, bars_v2["equity"].last())
        all_stats.extend(regime_stats(bars_v2, sym, variant="V2"))
        daily_curves[(sym, "V2")] = aggregate_to_daily(bars_v2)

    print(render_md(all_stats, args.capital, args.fee_bps, transitions))

    xlsx_path = (
        Path(args.xlsx) if args.xlsx
        else Path(__file__).resolve().parents[1] / "results" / "backtest_carry.xlsx"
    )
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)

    stats_df = pl.DataFrame(all_stats)
    stats_df.write_excel(workbook=str(xlsx_path), worksheet="stats", autofit=True)
    for (sym, variant), daily in daily_curves.items():
        daily.write_excel(
            workbook=str(xlsx_path),
            worksheet=f"daily_{sym}_{variant}",
            autofit=True,
        )
    log.info("wrote %s", xlsx_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
