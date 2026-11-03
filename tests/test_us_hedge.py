import pytest
from order_assistant import build_sample_snapshot, bs_price_greeks, option_expiry_pnl
from us_hedge import (
    hedge_contracts,
    us_spx_exposure,
    build_us_strategies,
    resolve_us_leg,
    iv_signal_vs_vix,
    fetch_us_vix,
    fetch_us_index_prices,
    instrument_prices,
    us_strategy_summary,
    us_hedge_stress_table,
    clear_cache,
)

def test_hedge_contracts():
    res = hedge_contracts(1000000, 500, 1.0)
    assert res["raw"] == 20
    assert res["contracts"] == 20
    
    res2 = hedge_contracts(1000000, 500, 0.5)
    assert res2["contracts"] == 10
    
    res3 = hedge_contracts(12500, 500, 1.0) # raw 0.25 -> 0
    assert res3["contracts"] == 0
    
    res4 = hedge_contracts(125000, 500, 1.0) # raw 2.5 -> 3
    assert res4["contracts"] == 3
    
    res5 = hedge_contracts(1000000, 0, 1.0)
    assert res5["contracts"] == 0

def test_us_spx_exposure():
    snap = build_sample_snapshot()
    res = us_spx_exposure(snap)
    
    soxl_row = next(r for r in res["rows"] if r["ticker"] == "SOXL")
    assert soxl_row["eff_beta"] == pytest.approx(4.8)  # lev 3 * FALLBACK SOXX 1.6
    
    nvda_row = next(r for r in res["rows"] if r["ticker"] == "NVDA")
    assert nvda_row["eff_beta"] == 1.7
    
    def mock_beta(u, _):
        if u == "SOXX": return (2.0, "x")
        raise ValueError()
        
    res2 = us_spx_exposure(snap, beta_fn=mock_beta)
    soxl_row2 = next(r for r in res2["rows"] if r["ticker"] == "SOXL")
    assert soxl_row2["eff_beta"] == 6.0 # lev 3 * 2.0
    
    res3 = us_spx_exposure(snap, overrides={"SOXL": 1.5})
    soxl_row3 = next(r for r in res3["rows"] if r["ticker"] == "SOXL")
    assert soxl_row3["eff_beta"] == 4.5 # lev 3 * 1.5

def test_strategies_and_pnl():
    premiums = {"put": 10, "short_put": 4, "call": 6}
    res = build_us_strategies(500, 2, 475, 425, 525, premiums, 0.1, 0.05, 0.2)
    strats = res["strategies"]
    
    assert "買進賣權" in strats
    assert "賣權價差" in strats
    assert "領口" in strats

    buy_put_pnl = option_expiry_pnl(400, strats["買進賣權"], 100)
    assert buy_put_pnl == 2 * (75 - 10) * 100

    put_spread_pnl = option_expiry_pnl(400, strats["賣權價差"], 100)
    assert put_spread_pnl == 2 * ((75 - 10) - (25 - 4)) * 100

    collar_pnl = option_expiry_pnl(400, strats["領口"], 100)
    assert collar_pnl == 2 * (75 - 10 + 6) * 100

    collar_pnl_550 = option_expiry_pnl(550, strats["領口"], 100)
    assert collar_pnl_550 == 2 * (-10 - (25 - 6)) * 100

    buy_put_pnl_500 = option_expiry_pnl(500, strats["買進賣權"], 100)
    assert buy_put_pnl_500 == -2 * 10 * 100

def test_invalid_strikes():
    res = build_us_strategies(500, 2, 475, 475, 450, {}, 0.1, 0.05, 0.2)
    assert "賣權價差" not in res["strategies"]
    assert "領口" not in res["strategies"]
    assert len(res["warnings"]) == 2

def test_resolve_us_leg():
    res = resolve_us_leg("put", 450, 1, None, 500, 0.1, 0.05, 0.2, vix_percent=15)
    assert res["no_quote"] is True
    assert res["source"] == "theory"
    assert res["iv"] == 0.18 # (15+3)/100

    price, _, _ = bs_price_greeks(500, 450, 0.1, 0.05, 0.22, "put")
    res2 = resolve_us_leg("put", 450, 1, price, 500, 0.1, 0.05, 0.2)
    assert res2["no_quote"] is False
    assert res2["source"] == "manual"
    assert abs(res2["iv"] - 0.22) < 1e-4

    res3 = resolve_us_leg("put", 550, 1, 10.0, 500, 0.1, 0.05, 0.2)  # 低於內含價值 50
    assert res3["source"] == "manual_bad_iv"
    assert res3["iv"] == 0.2

def test_iv_signal_vs_vix():
    history = list(range(10, 30))
    res1 = iv_signal_vs_vix(12, history)
    assert res1["color"] == "green"
    
    res2 = iv_signal_vs_vix(20, history)
    assert res2["color"] == "yellow"
    
    res3 = iv_signal_vs_vix(28, history)
    assert res3["color"] == "red"
    
    res4 = iv_signal_vs_vix(15, [])
    assert res4["color"] == "none"

def test_fetchers():
    clear_cache()
    
    def mock_vix_get():
        return [("2023-01-01", 15.0)] * 5
        
    res = fetch_us_vix(get=mock_vix_get)
    assert res["ok"] is True
    assert res["value"] == 15.0
    assert res["history_days"] == 5

    def mock_vix_fail():
        raise ValueError()
        
    res2 = fetch_us_vix(get=mock_vix_fail)
    assert res2["ok"] is False
    assert "無報價" in res2["source"]

    def mock_idx_get(sym):
        if sym == "^GSPC": return 5000.0
        if sym == "SPY": return 500.0
        
    res3 = fetch_us_index_prices(get=mock_idx_get)
    assert res3["ok"] is True
    assert res3["SPX"] == 5000.0
    assert res3["SPY"] == 500.0

def test_instrument_prices():
    res = instrument_prices(6000, 598)
    assert res["XSP"] == 600
    assert res["SPY"] == 598
    assert abs(res["spx_per_spy"] - 10.0334) < 1e-3

    res2 = instrument_prices(6000, None)
    assert res2["SPY"] == 600
    assert res2["spy_estimated"] is True

def test_summary_and_stress():
    snap = build_sample_snapshot()
    premiums = {"put": 10}
    strats = build_us_strategies(500, 2, 475, 425, 525, premiums, 0.1, 0.05, 0.2)
    legs = strats["strategies"]["買進賣權"]
    
    summary = us_strategy_summary(snap, "買進賣權", legs, 500, 0.1, 0.05, 1000000)
    assert summary["us_beta_after"] < summary["us_beta_before"]
    assert summary["total_beta_after"] < summary["total_beta_before"]
    assert summary["net_debit"] == summary["net_debit_usd"] * snap.usdtwd

    stress = us_hedge_stress_table(snap, summary, 500, 1000000)
    row_20 = next(r for r in stress if r["move"] == -0.20)
    assert row_20["hedge_pnl"] > 0
    
    nav0 = snap.metrics.get("nav", 0.0)
    assert row_20["nav"] >= nav0 + row_20["port_pnl"] - summary["net_debit"]


def test_stress_and_pct_units():
    snap = build_sample_snapshot()
    exp = us_spx_exposure(snap)["exposure_twd"]
    built = build_us_strategies(500.0, 2, 475.0, 425.0, 525.0, {"put": 10.0, "short_put": 4.0, "call": 6.0}, 0.25, 0.04, 0.2)
    summary = us_strategy_summary(snap, "買進賣權", built["strategies"]["買進賣權"], 500.0, 0.25, 0.04, exp)
    assert summary["premium_pct_nav"] == pytest.approx(summary["net_debit"] / snap.metrics["nav"] * 100.0)
    rows = us_hedge_stress_table(snap, summary, 500.0, exp)
    nav0 = snap.metrics["nav"]
    assert rows[1]["drawdown_pct"] == pytest.approx((rows[1]["nav"] - nav0) / nav0 * 100.0)
    assert rows[1]["name"] == "S&P 500 -20%"


def test_index_prices_one_symbol_fails():
    def get(symbol):
        if symbol == "SPY":
            raise RuntimeError("boom")
        return 6000.0

    res = fetch_us_index_prices(get=get)
    assert res["ok"] is True and res["SPX"] == 6000.0 and res["SPY"] is None
