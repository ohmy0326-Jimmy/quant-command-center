"""避險策略模擬（hedge_sim）：回測、燈號與建議。不連網。"""
import pytest
import numpy as np
import pandas as pd
from hedge_sim import (
    HedgeParams, bs_put, prepare, run, metrics, simulate,
    score_dropped, trigger_status, recommend_hedge, format_quote,
    TOOLS, TOOL_LABELS
)

@pytest.fixture
def synth_data():
    rng = np.random.default_rng(7)
    n = 900
    rets = rng.normal(0.0005, 0.01, n)
    rets[300:320] = -0.02  # inject a ~30% crash
    px = 10000 * np.cumprod(1 + rets)
    dates = pd.bdate_range("2019-01-01", periods=n)
    return pd.Series(px, index=dates)

def test_params_valid():
    p = HedgeParams()
    assert p.alloc_pct == 0.10

def test_params_invalid_alloc():
    with pytest.raises(ValueError, match="alloc_pct"):
        HedgeParams(alloc_pct=-0.1)

def test_params_invalid_cov():
    with pytest.raises(ValueError, match="cov_alert 不能小於 cov_normal"):
        HedgeParams(cov_normal=1.0, cov_alert=0.5)

def test_params_invalid_trigger():
    with pytest.raises(ValueError, match="未知的觸發條件"):
        HedgeParams(triggers=("ma60", "magic"))

def test_bs_put_sanity():
    # T=0 returns intrinsic
    assert bs_put(100, 110, 0, 0.2) == 10.0
    assert bs_put(100, 90, 0, 0.2) == 0.0
    # T>0 returns > intrinsic
    assert bs_put(100, 100, 1, 0.2) > 0.0

def test_prepare_no_triggers(synth_data):
    p = HedgeParams(triggers=())
    df = prepare(synth_data, p)
    assert not df["trig"].any()

def test_prepare_ma60_only(synth_data):
    p = HedgeParams(triggers=("ma60",))
    df = prepare(synth_data, p)
    expected = (synth_data < synth_data.rolling(60).mean()).fillna(False)
    pd.testing.assert_series_equal(df["trig"], expected, check_names=False)

def test_prepare_too_short():
    with pytest.raises(ValueError, match="價格資料不足 80 筆"):
        prepare(pd.Series([100]*50), HedgeParams())

def test_run_none_mode(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    nav, cost = run(df, "none", p)
    assert cost == 0.0
    assert len(nav) == len(df)
    assert (nav > 0).all()

def test_run_futures_mode(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    nav_none, _ = run(df, "none", p)
    nav_fut, cost_fut = run(df, "futures", p)
    
    dd_none = nav_none / nav_none.cummax() - 1
    dd_fut = nav_fut / nav_fut.cummax() - 1
    
    # Futures should have a better (less negative) max drawdown during the crash
    assert dd_fut.min() > dd_none.min()
    assert cost_fut > 0

def test_run_put_mode(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    nav, cost = run(df, "put", p)
    assert cost > 0
    yrs = len(df) / 252
    assert cost <= p.annual_cap_pct * nav.max() * yrs + 1

def test_run_spread_mode(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    nav, cost = run(df, "spread", p)
    assert cost > 0
    yrs = len(df) / 252
    assert cost <= p.annual_cap_pct * nav.max() * yrs + 1

def test_run_invalid_mode(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    with pytest.raises(ValueError, match="未知的模式"):
        run(df, "magic", p)

def test_metrics_keys(synth_data):
    p = HedgeParams()
    df = prepare(synth_data, p)
    nav, cost = run(df, "none", p)
    m = metrics(nav, cost, p.nav0)
    expected_keys = {"期末淨值", "CAGR", "最大回撤", "最差月份", "累計避險成本", "回撤超過20%月數"}
    assert set(m.keys()) == expected_keys

def test_simulate_table(synth_data):
    p = HedgeParams()
    res = simulate(synth_data, p, tools=["none", "futures"])
    assert list(res["table"].index) == [TOOL_LABELS["none"], TOOL_LABELS["futures"]]

def test_score_dropped():
    history = [
        {"week": "2023-01-01", "total": 10},
        {"week": "2023-01-08", "total": 12},
        {"week": "2023-01-15", "total": 9}
    ]
    # Drop of 3 within window: max is 12, current is 9 -> 12 - 9 = 3 >= 3 (True)
    assert score_dropped(history, 9, as_of="2023-01-15", drop=3, window_days=28) is True
    # Drop of 2: max is 12, current is 10 -> 12 - 10 = 2 < 3 (False)
    assert score_dropped(history, 10, as_of="2023-01-15", drop=3, window_days=28) is False
    # Outside window ignored: max in window is 9, current is 9 -> 9 - 9 = 0 < 3 (False)
    assert score_dropped(history, 9, as_of="2023-03-01", drop=3, window_days=28) is None
    # No history
    assert score_dropped([], 9) is None

def test_trigger_status():
    p = HedgeParams()
    px = pd.Series([100]*10) # Short series
    status = trigger_status(px, p)
    assert status["lights"]["ma60"] is None
    assert status["lights"]["drawdown"] is None

def test_recommend_hedge_futures():
    p = HedgeParams()
    res = recommend_hedge(
        nav=3.45e6, beta_exposure=6.9e6, index_level=23000, vol20=0.2,
        alert=True, tool="futures", params=p, pool_balance=1e5, spent_ytd=0
    )
    # beta_exposure = 6.9e6, cov_normal = 0.5 -> notional = 3.45e6
    # mtx_lots = round(3.45e6 / (23000 * 50)) = round(3.45e6 / 1150000) = 3
    assert res["lots_info"]["mtx"] == 3
    
    res_no_alert = recommend_hedge(
        nav=3.45e6, beta_exposure=6.9e6, index_level=23000, vol20=0.2,
        alert=False, tool="futures", params=p, pool_balance=1e5, spent_ytd=0
    )
    assert res_no_alert["lots_info"]["mtx"] == 0

def test_recommend_hedge_put_quote():
    p = HedgeParams()
    def mock_quote(K):
        return 150.0 if K == 21850 else None
        
    res = recommend_hedge(
        nav=3.45e6, beta_exposure=6.9e6, index_level=23000, vol20=0.2,
        alert=True, tool="put", params=p, pool_balance=1e6, spent_ytd=0,
        quote_fn=mock_quote
    )
    # K1 = floor(23000 * 0.95 / 50) * 50 = 21850
    assert res["legs"][0]["strike"] == 21850
    assert res["legs"][0]["used_premium"] == 150.0
    assert res["legs"][0]["source"] == "報價"

def test_recommend_hedge_spread_budget():
    p = HedgeParams()
    # Force a tiny budget
    res = recommend_hedge(
        nav=3.45e6, beta_exposure=6.9e6, index_level=23000, vol20=0.2,
        alert=True, tool="spread", params=p, pool_balance=1000, spent_ytd=0
    )
    # K1 = 21850, K2 = floor(23000 * 0.88 / 50) * 50 = 20200
    assert res["legs"][0]["strike"] == 21850
    assert res["legs"][1]["strike"] == 20200
    assert res["legs"][1]["strike"] < res["legs"][0]["strike"]
    assert res["legs"][0]["strike"] % 50 == 0
    assert res["legs"][1]["strike"] % 50 == 0
    assert res["affordable_lots"] < res["lots_info"]["lots"]
    assert not res["budget_ok"]

def test_format_quote():
    assert format_quote(None) == "無報價"
    assert format_quote(0) == "無報價"
    assert format_quote(-5) == "無報價"
    assert format_quote(np.nan) == "無報價"
    assert format_quote(12.34) == "12.3"


def test_futures_with_zero_coverage_equals_no_hedge(synth_data):
    p = HedgeParams(cov_normal=0.0, futures_slip=0.0)
    df = prepare(synth_data, p)
    nav_none, _ = run(df, "none", p)
    nav_fut, cost = run(df, "futures", p)
    assert cost == 0.0
    assert np.allclose(nav_none.values, nav_fut.values)


def test_options_without_budget_never_spend(synth_data):
    p = HedgeParams(alloc_pct=0.0)
    df = prepare(synth_data, p)
    nav_none, _ = run(df, "none", p)
    for mode in ("put", "spread"):
        nav, cost = run(df, mode, p)
        assert cost == 0.0
        assert np.allclose(nav_none.values, nav.values)


def test_score_trigger_series_feeds_backtest(synth_data):
    p = HedgeParams(triggers=("score",))
    assert not prepare(synth_data, p)["trig"].any()
    alert = pd.Series(False, index=synth_data.index)
    alert.iloc[400:420] = True
    df = prepare(synth_data, p, score_alert=alert)
    assert df["trig"].sum() == 20


def test_recommend_quote_fn_errors_fall_back_to_bs():
    def boom(_k):
        raise RuntimeError("taifex down")

    rec = recommend_hedge(nav=3.45e6, beta_exposure=6.9e6, index_level=23000, vol20=0.2, alert=False,
                          tool="put", params=HedgeParams(), pool_balance=1e6, spent_ytd=0, quote_fn=boom)
    leg = rec["legs"][0]
    assert leg["quote"] is None and leg["source"] == "BS 估計"
    assert format_quote(leg["quote"]) == "無報價"
    assert leg["strike"] == 21850.0
