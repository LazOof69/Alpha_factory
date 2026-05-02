"""Tests for `alpha_factory.data.qc` — port + universe-aware extensions."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from alpha_factory.data.qc import (
    TIER_MAJORS,
    TIER_SMALL_CAPS,
    c1_universe_legs_present,
    compute_qc_results,
    f1_settlement_alignment,
    f2_no_missing_settlements,
    f3_funding_rate_range,
    f4_no_duplicates,
    k1_no_missing_bars,
    k2_no_duplicates,
    k3_ohlc_validity,
    k4_non_negative,
    k6_extreme_returns,
    k7_spot_perp_consistency,
    k8_no_partial_last_bar,
    n_symbols_with_error,
    summarize,
    tier_for,
    write_qc_audit,
    x1_funding_to_kline_join,
)
from alpha_factory.data.schema import (
    FUNDING_SCHEMA,
    KLINES_SCHEMA,
    QC_RUN_SCHEMA,
    UNIVERSE_SNAPSHOT_SCHEMA,
)

# ── Tier classification ───────────────────────────────────────────────────


@pytest.mark.parametrize("rank, expected", [
    (1, TIER_MAJORS),
    (10, TIER_MAJORS),
    (11, TIER_SMALL_CAPS),
    (20, TIER_SMALL_CAPS),
])
def test_tier_for_boundary(rank, expected):
    assert tier_for(rank) == expected


# ── Synthetic kline + funding builders ────────────────────────────────────


_T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _kline_row(symbol: str, market: str, hour_offset: int,
               open_p: float = 100.0, close_p: float = 100.5,
               volume: float = 1.0):
    open_t = _T0 + timedelta(hours=hour_offset)
    close_t = open_t + timedelta(hours=1) - timedelta(seconds=1)
    ingested = datetime(2026, 5, 1, tzinfo=UTC)  # well past close_t -> not partial
    return {
        "symbol": symbol, "market": market,
        "open_time": open_t, "close_time": close_t,
        "open": open_p, "high": max(open_p, close_p) * 1.01,
        "low": min(open_p, close_p) * 0.99,
        "close": close_p, "volume": volume,
        "quote_volume": volume * close_p,
        "trades": 100,
        "taker_buy_base": volume * 0.5,
        "taker_buy_quote": volume * close_p * 0.5,
        "ingested_at": ingested,
        "source": "binance_fapi_v1",
    }


def _kline_df(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=KLINES_SCHEMA)
    return pl.DataFrame(rows, schema=KLINES_SCHEMA)


def _funding_row(symbol: str, hour_offset: int, rate: float = -0.0001):
    t = _T0 + timedelta(hours=hour_offset)
    return {
        "symbol": symbol, "funding_time": t,
        "funding_rate": rate, "mark_price": 100.0,
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
        "source": "binance_fapi_funding_v1",
    }


def _funding_df(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=FUNDING_SCHEMA)
    return pl.DataFrame(rows, schema=FUNDING_SCHEMA)


def _universe_row(rank: int, symbol: str, base: str,
                  spot_pairs: list[str] | None = None):
    return {
        "as_of": datetime(2026, 5, 1, tzinfo=UTC),
        "period_start": _T0.date(),
        "period_end": (_T0 + timedelta(days=30)).date(),
        "rank": rank, "symbol": symbol, "api_symbol": symbol.replace("-", ""),
        "market": "perp_usdt", "base_asset": base, "quote_asset": "USDT",
        "quote_volume_24h": 1e9, "last_price": 100.0, "trade_count_24h": 100_000,
        "avg_trade_size": 100.0,
        "listing_date": _T0.date() - timedelta(days=365),
        "spot_pairs": spot_pairs or [],
        "primary_spot_quote_volume": 1e8,
        "total_candidates": 200,
        "min_listing_days_threshold": 180,
        "method": "live_24hr_top_n_v2",
        "ingested_at": datetime(2026, 5, 1, tzinfo=UTC),
        "source": "binance/fapi/v1/ticker/24hr",
        "fapi_exchangeinfo_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "fapi_ticker_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "spot_exchangeinfo_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
        "spot_ticker_fetched_at": datetime(2026, 5, 1, tzinfo=UTC),
    }


def _universe_df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=UNIVERSE_SNAPSHOT_SCHEMA)


# ── K1 ────────────────────────────────────────────────────────────────────


def test_k1_zero_rows_returns_error():
    df = _kline_df([])
    out = k1_no_missing_bars(df, "BTC-USDT", "perp_usdt", _T0)
    assert out.passed is False
    assert out.severity == "ERROR"


def test_k1_full_coverage_passes():
    rows = [_kline_row("BTC-USDT", "perp_usdt", h) for h in range(24)]
    df = _kline_df(rows)
    out = k1_no_missing_bars(df, "BTC-USDT", "perp_usdt", _T0)
    assert out.passed is True
    assert out.severity == "INFO"


def test_k1_partial_coverage_warns_or_errors():
    """Skip every other hour (50% coverage) -> ERROR."""
    rows = [_kline_row("BTC-USDT", "perp_usdt", h) for h in range(0, 24, 2)]
    df = _kline_df(rows)
    out = k1_no_missing_bars(df, "BTC-USDT", "perp_usdt", _T0)
    assert out.severity == "ERROR"
    assert out.details["coverage"] < 0.6


# ── K6 tier-aware ─────────────────────────────────────────────────────────


def test_k6_extreme_returns_majors_threshold_strict():
    """0.30 |log return| trips T_majors but passes T_small_caps."""
    # log(close/open) ≈ 0.30 → close/open ≈ 1.35
    rows = [
        _kline_row("BTC-USDT", "perp_usdt", 0, open_p=100.0, close_p=135.0),
        _kline_row("BTC-USDT", "perp_usdt", 1),
    ]
    df = _kline_df(rows)
    majors = k6_extreme_returns(df, "BTC-USDT", "perp_usdt", TIER_MAJORS)
    smalls = k6_extreme_returns(df, "BTC-USDT", "perp_usdt", TIER_SMALL_CAPS)
    assert majors.severity == "WARN"
    assert smalls.severity == "INFO"
    assert majors.tier == TIER_MAJORS
    assert smalls.tier == TIER_SMALL_CAPS


# ── K7 tier-aware ─────────────────────────────────────────────────────────


def test_k7_spot_perp_consistency_tier_thresholds():
    # 7% basis: trips T_majors WARN (>5%), under T_majors error (<15%);
    # passes T_small_caps WARN (>10% threshold, this is 7%).
    spot_rows = [_kline_row("BTC-USDT", "spot", h, open_p=100, close_p=100) for h in range(3)]
    perp_rows = [_kline_row("BTC-USDT", "perp_usdt", h, open_p=100, close_p=107) for h in range(3)]
    df = _kline_df(spot_rows + perp_rows)
    out_majors = k7_spot_perp_consistency(df, "BTC", TIER_MAJORS)
    out_smalls = k7_spot_perp_consistency(df, "BTC", TIER_SMALL_CAPS)
    assert out_majors.severity == "WARN"
    assert out_smalls.severity == "INFO"


def test_k7_missing_leg_returns_info():
    rows = [_kline_row("BTC-USDT", "spot", h) for h in range(3)]   # spot only
    df = _kline_df(rows)
    out = k7_spot_perp_consistency(df, "BTC", TIER_MAJORS)
    assert out.severity == "INFO"


# ── C1 universe legs-present ──────────────────────────────────────────────


def test_c1_perp_only_universe_skips_check():
    """spot_pairs=[] (perp-only like SKYAI) -> no C1 row emitted."""
    universe = _universe_df([_universe_row(5, "SKYAI-USDT", "SKYAI", spot_pairs=[])])
    perp_only = _kline_df([_kline_row("SKYAI-USDT", "perp_usdt", 0)])
    out = c1_universe_legs_present(perp_only, universe)
    assert out == []


def test_c1_both_legs_present_passes():
    universe = _universe_df([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT", "BTCUSDC"]),
    ])
    df = _kline_df([
        _kline_row("BTC-USDT", "perp_usdt", 0),
        _kline_row("BTC-USDT", "spot", 0),
    ])
    out = c1_universe_legs_present(df, universe)
    assert len(out) == 1
    assert out[0].passed is True


def test_c1_missing_spot_leg_errors():
    universe = _universe_df([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"]),
    ])
    df = _kline_df([_kline_row("BTC-USDT", "perp_usdt", 0)])  # no spot
    out = c1_universe_legs_present(df, universe)
    assert len(out) == 1
    assert out[0].passed is False
    assert out[0].severity == "ERROR"


def test_c1_missing_perp_leg_errors():
    universe = _universe_df([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"]),
    ])
    df = _kline_df([_kline_row("BTC-USDT", "spot", 0)])  # no perp
    out = c1_universe_legs_present(df, universe)
    assert out[0].passed is False


# ── Cross-cutting checks (port — quick smoke) ─────────────────────────────


def test_k2_dups_detected():
    rows = [_kline_row("BTC-USDT", "perp_usdt", 0)] * 2
    out = k2_no_duplicates(_kline_df(rows))
    assert out.severity == "ERROR"


def test_k3_invalid_ohlc_detected():
    bad = _kline_row("BTC-USDT", "perp_usdt", 0, open_p=100, close_p=200)
    bad["high"] = 50.0  # high < max(open, close)
    out = k3_ohlc_validity(_kline_df([bad]))
    assert out.severity == "ERROR"


def test_k4_negative_volume_detected():
    bad = _kline_row("BTC-USDT", "perp_usdt", 0)
    bad["volume"] = -1.0
    out = k4_non_negative(_kline_df([bad]))
    assert out.severity == "ERROR"


def test_k8_partial_bar_detected():
    bad = _kline_row("BTC-USDT", "perp_usdt", 0)
    bad["close_time"] = bad["ingested_at"] + timedelta(seconds=1)  # close_time > ingested
    out = k8_no_partial_last_bar(_kline_df([bad]))
    assert out.severity == "ERROR"


def test_f1_misalignment_detected():
    bad = _funding_row("BTC-USDT", 1)  # 01:00 UTC, not 00/08/16
    out = f1_settlement_alignment(_funding_df([bad]))
    assert out.severity == "ERROR"


def test_f2_zero_rows_errors():
    out = f2_no_missing_settlements(_funding_df([]), "BTC-USDT", _T0)
    assert out.severity == "ERROR"


def test_f3_warn_at_50bp_error_at_2pct():
    rows = [_funding_row("BTC-USDT", 0, rate=0.006)]   # 0.6% > 0.5% warn, < 2% error
    out = f3_funding_rate_range(_funding_df(rows))
    assert out.severity == "WARN"
    rows2 = [_funding_row("BTC-USDT", 0, rate=0.025)]  # 2.5% > 2% error
    out2 = f3_funding_rate_range(_funding_df(rows2))
    assert out2.severity == "ERROR"


def test_f4_dups_detected():
    rows = [_funding_row("BTC-USDT", 0)] * 2
    out = f4_no_duplicates(_funding_df(rows))
    assert out.severity == "ERROR"


# ── X1 ────────────────────────────────────────────────────────────────────


def test_x1_funding_inside_kline_passes():
    klines = _kline_df([_kline_row("BTC-USDT", "perp_usdt", 0)])
    funding = _funding_df([_funding_row("BTC-USDT", 0)])
    out = x1_funding_to_kline_join(klines, funding)
    assert out.severity == "INFO"


# ── compute_qc_results orchestrator ───────────────────────────────────────


def test_compute_qc_results_iterates_universe():
    """3-symbol universe -> per-symbol checks emitted for each + cross-cutting + C1."""
    universe = _universe_df([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"]),
        _universe_row(11, "RIVER-USDT", "RIVER", spot_pairs=[]),  # perp-only, T_small
    ])
    klines = _kline_df([
        _kline_row("BTC-USDT", "perp_usdt", h) for h in range(24)
    ] + [
        _kline_row("BTC-USDT", "spot", h) for h in range(24)
    ] + [
        _kline_row("RIVER-USDT", "perp_usdt", h) for h in range(24)
    ])
    funding = _funding_df([
        _funding_row("BTC-USDT", h * 8) for h in range(3)
    ] + [
        _funding_row("RIVER-USDT", h * 8) for h in range(3)
    ])
    listing_dates = {
        ("BTC-USDT", "perp_usdt"): _T0,
        ("BTC-USDT", "spot"): _T0,
        ("RIVER-USDT", "perp_usdt"): _T0,
    }
    results = compute_qc_results(klines, funding, universe, listing_dates)
    names = [r.name for r in results]
    # Per-symbol K1 for BTC perp + BTC spot + RIVER perp.
    assert any("k1_no_missing_bars[BTC-USDT/perp_usdt]" in n for n in names)
    assert any("k1_no_missing_bars[BTC-USDT/spot]" in n for n in names)
    assert any("k1_no_missing_bars[RIVER-USDT/perp_usdt]" in n for n in names)
    # K7 only for BTC (RIVER perp-only).
    assert any("k7_spot_perp_consistency[BTC]" in n for n in names)
    assert not any("k7_spot_perp_consistency[RIVER]" in n for n in names)
    # C1 only for BTC.
    assert any("c1_universe_legs_present[BTC-USDT]" in n for n in names)
    # Cross-cutting fires once.
    assert names.count("k2_no_duplicates") == 1
    assert names.count("x1_funding_to_kline_join") == 1


def test_compute_qc_results_pure_no_writes(tmp_path):
    """Library function must not write anything (CLAUDE.md purity)."""
    universe = _universe_df([
        _universe_row(1, "BTC-USDT", "BTC", spot_pairs=["BTCUSDT"]),
    ])
    klines = _kline_df([_kline_row("BTC-USDT", "perp_usdt", 0)])
    funding = _funding_df([])
    compute_qc_results(klines, funding, universe, {})
    assert list(tmp_path.iterdir()) == []


# ── write_qc_audit ────────────────────────────────────────────────────────


def test_write_qc_audit_atomic_and_schema(tmp_path):
    from alpha_factory.data.qc import QCResult
    results = [
        QCResult("k2_no_duplicates", True, "INFO", {"duplicates": 0}),
        QCResult("k1_no_missing_bars[BTC-USDT/perp_usdt]", False, "ERROR",
                 {"coverage": 0.5}, symbol="BTC-USDT", market="perp_usdt"),
    ]
    run_ts = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    target = write_qc_audit(results, run_ts=run_ts, audit_dir=tmp_path)
    assert target.exists()
    assert target.name.startswith("qc_run_") and target.suffix == ".parquet"
    # No .tmp.<pid> residue.
    assert not list(tmp_path.glob("*.tmp.*"))

    loaded = pl.read_parquet(target)
    assert loaded.schema == QC_RUN_SCHEMA
    assert loaded.shape[0] == 2
    assert loaded.get_column("severity").to_list() == ["INFO", "ERROR"]


# ── summarize / n_symbols_with_error ──────────────────────────────────────


def test_summarize_counts():
    from alpha_factory.data.qc import QCResult
    results = [
        QCResult("a", True, "INFO"),
        QCResult("b", True, "WARN"),
        QCResult("c", False, "ERROR"),
        QCResult("d", False, "ERROR"),
    ]
    n_e, n_w, n_i = summarize(results)
    assert (n_e, n_w, n_i) == (2, 1, 1)


def test_n_symbols_with_error_dedupes_per_key():
    from alpha_factory.data.qc import QCResult
    results = [
        QCResult("k1[BTC]", False, "ERROR", symbol="BTC-USDT", market="perp_usdt"),
        QCResult("k6[BTC]", False, "ERROR", symbol="BTC-USDT", market="perp_usdt"),
        QCResult("k1[ETH]", False, "ERROR", symbol="ETH-USDT", market="perp_usdt"),
        QCResult("k2", False, "ERROR"),  # cross-cutting, symbol=None - excluded
    ]
    assert n_symbols_with_error(results) == 2  # BTC + ETH, dedup'd
