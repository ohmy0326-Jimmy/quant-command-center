"""淨值情境模擬：固定種子、權重、口徑與策略基本性質。不連網。"""

import os
import time

import numpy as np
import pandas as pd
import pytest

from nav_scenario_sim import (
    DEFAULT_SCENARIOS,
    SEED,
    Scenario,
    Strategy,
    apply_delever,
    build_book,
    calibrate_market,
    cash_basis_text,
    default_market,
    generate_shocks,
    make_book,
    make_figure,
    normalize_weights,
    put_spread_value,
    simulate,
    strategy_hold,
    strategy_replenish,
)
from portfolio_risk import YUANTA_FUTURES_BASE_EQUITY, finalize_valuation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECLINE = Scenario("續跌 Decline", -0.15, -0.30, 1.25, 1.0)


def _stock(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, sector="其他"):
    return {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": 0.0 if "期" in kind else cost * qty,
        "最大虧損比例": -10,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": sector,
    }


def _page4_frames():
    positions = pd.DataFrame(
        [
            _stock("股票戶", "台股", "2330", 1000, 100, 110, lev=1, beta=1.2, sector="晶圓代工"),
            _stock("股票戶", "美股", "SOXL", 10, 20, 25, lev=3, beta=3, sector="半導體槓桿ETF"),
            _stock("元大期貨", "期貨", "TX", 1, 20000, 21000, lev=1, beta=1, sector="臺指期貨"),
        ]
    )
    accounts = pd.DataFrame(
        [
            {"帳戶名稱": "股票戶", "現金餘額": 50000, "融資/質押借款": 10000},
            {"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0},
        ]
    )
    return finalize_valuation(positions, accounts, usdtwd=30)


def _prototype_book():
    million = 1_000_000.0
    amounts = {
        "fut_tw": 0.88 * million,
        "tw_2x": 1.86 * million,
        "tw_stock": 0.05 * million,
        "us_3x": 0.91149 * million,
        "us_2x_mem": 0.91149 * million,
    }
    return make_book(
        cash_residual=-0.17798 * million,
        amounts=amounts,
        levs={"tw_2x": 2.0, "us_3x": 3.0, "us_2x_mem": 2.0},
        reported_cash=0.0818 * million,
        debt=0.60 * million,
        futures_equity=0.22 * million,
        non_futures_cash=0.0818 * million,
    )


def test_normalize_weights_and_rejects_bad_input():
    base = normalize_weights([30, 45, 25])
    same = normalize_weights([0.30, 0.45, 0.25])
    scaled = normalize_weights([60, 90, 50])
    assert base == pytest.approx([0.30, 0.45, 0.25])
    assert same == pytest.approx(base)
    assert scaled == pytest.approx(base)
    with pytest.raises(ValueError):
        normalize_weights([-1, 2, 3])
    with pytest.raises(ValueError):
        normalize_weights([0, 0, 0])


def test_strategy_presets_match_the_original_rules():
    hold = strategy_hold()
    active = strategy_replenish(target_leverage=1.8)
    assert hold.hold is True
    assert active.hold is False
    assert active.target_leverage == pytest.approx(1.8)
    assert active.reduce_priority == ("futures", "lev3", "lev2", "stock")
    assert active.n_batches == 3
    assert active.stop_stock == pytest.approx(-0.08)
    assert active.brake_pause == pytest.approx(-0.08)
    assert active.brake_cut == pytest.approx(-0.12)
    assert active.brake_release == pytest.approx(-0.08)
    assert active.hedge_enabled is False
    with pytest.raises(ValueError):
        Strategy(name="太高", target_leverage=2.4)
    with pytest.raises(ValueError):
        Strategy(name="順序重複", reduce_priority=("futures", "futures", "lev2", "stock"))


def test_cash_basis_uses_financing_residual_not_reported_cash():
    _valued, _accounts, metrics = _page4_frames()
    book = build_book(_valued, metrics)
    # 第四頁這組假帳：淨值 525,808；報表現金是股票戶 50,000（期貨超額保證金為負，不計入）。
    assert metrics["nav"] == pytest.approx(525_808)
    assert metrics["cash"] == pytest.approx(50_000)
    assert book.reported_cash == pytest.approx(50_000)
    assert book.reported_cash_pct == pytest.approx(50_000 / 525_808 * 100)
    # 融資殘值＝淨值 − 2330 市值 − SOXL 市值。期貨名目不從淨值扣。
    assert book.amounts["tw_stock"] == pytest.approx(110_000)
    assert book.amounts["us_3x"] == pytest.approx(7_500)
    assert book.amounts["fut_tw"] == pytest.approx(1 * 21_000 * 200)
    assert book.gross_exposure == pytest.approx(metrics["exposure"])
    assert book.cash_residual == pytest.approx(525_808 - 110_000 - 7_500)
    # 50,000 − 10,000 融資 ＋ 元大權益（168,308 備援 ＋ 200,000 未平倉損益）
    assert book.futures_equity == pytest.approx(YUANTA_FUTURES_BASE_EQUITY + 200_000)
    assert book.cash_residual == pytest.approx(50_000 - 10_000 + book.futures_equity)
    assert book.cash_identity_gap == pytest.approx(0, abs=1)
    assert book.cash_residual != pytest.approx(book.reported_cash)
    assert "報表現金" in cash_basis_text(book)
    assert "融資殘值" in cash_basis_text(book)


def test_option_delta_is_the_exposure_and_premium_stays_in_nav():
    positions = pd.DataFrame(
        [
            {
                "帳戶": "元大期貨",
                "種類": "台指選擇權",
                "標的": "TXO202612P20000",
                "名稱": "TXO Put",
                "持有數量": 2,
                "平均成本": 100,
                "投入總金額": 0,
                "最大虧損比例": -100,
                "標的槓桿": 1,
                "即時報價": 80,
                "Beta值": 1,
                "Delta": -0.40,
                "標的現價": 20_000,
                "合約乘數": 50,
            }
        ]
    )
    accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 200_000, "融資/質押借款": 0}])
    valued, _accounts, metrics = finalize_valuation(positions, accounts, 32.0)
    book = build_book(valued, metrics)
    assert book.opt_tw_delta == pytest.approx(-0.40 * 2 * 50 * 20_000)
    assert book.opt_tw_mv == pytest.approx(80 * 2 * 50)
    assert abs(book.opt_tw_delta) == pytest.approx(abs(float(valued.iloc[0]["Beta曝險(台幣)"])))
    assert book.nav == pytest.approx(metrics["nav"])
    assert book.cash_residual + book.opt_tw_mv == pytest.approx(book.nav)


def test_opening_cut_sells_futures_before_leveraged_etfs():
    book = _prototype_book()
    assert book.nav == pytest.approx(3.555e6, rel=1e-4)
    assert book.leverage == pytest.approx(2.59, abs=0.01)
    traded, budgets = apply_delever(book, strategy_replenish(target_leverage=1.8))
    assert traded.amounts["fut_tw"] == pytest.approx(0, abs=1)
    assert budgets["fut_tw"] == pytest.approx(book.amounts["fut_tw"])
    assert traded.amounts["tw_2x"] == pytest.approx(book.amounts["tw_2x"])
    assert traded.amounts["us_2x_mem"] == pytest.approx(book.amounts["us_2x_mem"])
    assert traded.amounts["us_3x"] < book.amounts["us_3x"] - 1
    assert traded.gross_exposure / book.nav == pytest.approx(1.8, abs=0.02)
    held, _budgets = apply_delever(book, strategy_hold())
    assert held.amounts["fut_tw"] == pytest.approx(book.amounts["fut_tw"])
    assert held.leverage == pytest.approx(book.leverage)


def test_zero_shock_stock_nav_stays_put():
    book = make_book(cash_residual=0.0, amounts={"tw_stock": 1_000_000}, betas={"tw_stock": 1.0})
    zeros = np.zeros((12, 6))
    shocks = {"平坦": (zeros, zeros.copy(), zeros.copy())}
    result = simulate(
        book,
        [strategy_hold("A 續抱")],
        market=default_market(),
        scenarios=(Scenario("平坦", 0.0, 0.0, 1.0, 1.0),),
        shocks=shocks,
    )
    assert result.stats[("平坦", "A 續抱")].median_end == pytest.approx(1_000_000, rel=1e-9)


def test_one_day_return_lands_on_the_stock():
    book = make_book(cash_residual=0.0, amounts={"tw_stock": 1_000_000}, betas={"tw_stock": 1.2})
    rt = np.full((4, 1), 0.10)
    rs = np.zeros((4, 1))
    rm = np.zeros((4, 1))
    result = simulate(
        book,
        [strategy_hold("A 續抱")],
        market=default_market(),
        scenarios=(Scenario("一天", 0.0, 0.0, 1.0, 1.0),),
        shocks={"一天": (rt, rs, rm)},
    )
    assert result.stats[("一天", "A 續抱")].median_end == pytest.approx(1_000_000 * 1.12)


def test_same_seed_is_reproducible_and_strategies_share_paths():
    book = _prototype_book()
    market = default_market()
    kwargs = dict(market=market, scenarios=(DECLINE,), n_paths=80, n_days=24, seed=SEED)
    first = simulate(book, [strategy_hold(), strategy_replenish()], **kwargs)
    second = simulate(book, [strategy_hold(), strategy_replenish()], **kwargs)
    alone = simulate(book, [strategy_hold()], **kwargs)
    key = ("續跌 Decline", "A 續抱")
    assert first.stats[key].median_end == pytest.approx(second.stats[key].median_end)
    assert first.stats[key].p10_end == pytest.approx(second.stats[key].p10_end)
    assert first.stats[key].median_mdd == pytest.approx(second.stats[key].median_mdd)
    assert first.stats[key].median_end == pytest.approx(alone.stats[key].median_end)
    shocks_a = generate_shocks((DECLINE,), market, 80, 24, SEED)
    shocks_b = generate_shocks((DECLINE,), market, 80, 24, SEED)
    assert np.array_equal(shocks_a[DECLINE.name][0], shocks_b[DECLINE.name][0])


def test_weight_scaling_does_not_change_the_mixture():
    book = _prototype_book()
    market = default_market()
    strategies = [strategy_hold(), strategy_replenish()]
    kwargs = dict(market=market, scenarios=DEFAULT_SCENARIOS, n_paths=60, n_days=20, seed=SEED)
    low = simulate(book, strategies, weights=[0.30, 0.45, 0.25], **kwargs)
    high = simulate(book, strategies, weights=[30, 45, 25], **kwargs)
    for name in ("A 續抱", "B 停損回補"):
        assert low.weighted[name].median_end == pytest.approx(high.weighted[name].median_end)
        assert low.weighted[name].p_dd20 == pytest.approx(high.weighted[name].p_dd20)
        assert low.weights == pytest.approx([0.30, 0.45, 0.25])


def test_deleverage_has_milder_drawdown_than_holding_in_a_decline():
    book = _prototype_book()
    result = simulate(
        book,
        [strategy_hold(), strategy_replenish(target_leverage=1.8)],
        market=default_market(),
        scenarios=(DECLINE,),
        n_paths=240,
        n_days=40,
        seed=SEED,
    )
    held = result.stats[(DECLINE.name, "A 續抱")]
    cut = result.stats[(DECLINE.name, "B 停損回補")]
    assert result.opening_leverage["A 續抱"] == pytest.approx(book.leverage, rel=0.02)
    assert result.opening_leverage["B 停損回補"] <= 1.85
    assert cut.median_mdd > held.median_mdd + 0.03
    assert cut.p_dd20 <= held.p_dd20 + 1e-9


def test_put_spread_payoff_and_hedge_lifts_the_decline():
    assert put_spread_value(0.80, 0.95, 0.85, 0.0, 0.22) == pytest.approx(0.10)
    assert put_spread_value(1.0, 0.95, 0.85, 0.0, 0.22) == pytest.approx(0.0)
    assert float(put_spread_value(1.0, 0.95, 0.85, 126 / 252, 0.22)) > 0
    book = _prototype_book()
    plain = strategy_replenish("B 無避險")
    hedged = Strategy(
        name="B 有避險",
        target_leverage=1.8,
        hedge_enabled=True,
        hedge_ratio=1.0,
        hedge_long_strike=0.95,
        hedge_short_strike=0.85,
    )
    result = simulate(
        book,
        [plain, hedged],
        market=default_market(),
        scenarios=(DECLINE,),
        n_paths=160,
        n_days=30,
        seed=SEED,
    )
    bare = result.stats[(DECLINE.name, "B 無避險")]
    covered = result.stats[(DECLINE.name, "B 有避險")]
    assert covered.median_end > bare.median_end
    assert covered.p10_end > bare.p10_end


def test_drawdown_brake_cuts_leverage_toward_one():
    book = make_book(cash_residual=1_000_000, amounts={"fut_tw": 3_000_000})
    shock = np.full((8, 3), -0.18)
    zeros = np.zeros_like(shock)
    scenario = Scenario("急跌", -0.2, 0.0, 1.0, 1.0)
    braking = Strategy(name="有煞車", target_leverage=1.8, brake_enabled=True, brake_pause=-0.08, brake_cut=-0.12)
    free = Strategy(name="無煞車", target_leverage=1.8, brake_enabled=False)
    result = simulate(
        book,
        [braking, free],
        market=default_market(),
        scenarios=(scenario,),
        shocks={"急跌": (shock, zeros, zeros)},
    )
    assert result.stats[("急跌", "有煞車")].median_end_leverage < 1.15
    assert result.stats[("急跌", "無煞車")].median_end_leverage > 1.6


def test_rebalance_order_can_sell_stocks_before_futures():
    book = make_book(
        cash_residual=1_000_000,
        amounts={"fut_tw": 2_000_000, "tw_stock": 2_000_000},
    )
    # 淨值 3,000,000，曝險 4,000,000，槓桿 1.333。目標 1.0 要砍掉 1,000,000 曝險。
    stocks_first = Strategy(
        name="先個股",
        target_leverage=1.0,
        reduce_priority=("stock", "futures", "lev3", "lev2"),
    )
    traded, budgets = apply_delever(book, stocks_first)
    assert budgets["tw_stock"] == pytest.approx(1_000_000, rel=1e-6)
    assert budgets["fut_tw"] == pytest.approx(0, abs=1)
    assert traded.amounts["fut_tw"] == pytest.approx(2_000_000)


def test_market_calibration_uses_history_and_falls_back():
    index = pd.bdate_range("2025-08-01", periods=70)

    def fetch(symbol):
        step = 0.4 if symbol == "^SOX" else 0.1
        return pd.Series(np.linspace(100, 100 + step * 69, 70), index=index)

    market = calibrate_market(fetch=fetch)
    assert market.source == "yfinance"
    assert market.pre_tw[-1] == pytest.approx(1.0)
    assert market.pre_sx[-1] == pytest.approx(1.0)
    assert len(market.pre_tw) == 60
    assert 0 < market.vol_tw < market.vol_sox
    assert "yfinance" in market.note

    def broken(symbol):
        raise RuntimeError("離線")

    fallback = calibrate_market(fetch=broken)
    assert fallback.source == "default"
    assert fallback.vol_tw == pytest.approx(0.22)
    assert fallback.vol_sox == pytest.approx(0.45)
    assert fallback.rho == pytest.approx(0.75)
    assert "預設" in fallback.note


def test_figure_title_marks_the_model_and_keeps_the_legend_off_it():
    book = make_book(cash_residual=100_000, amounts={"tw_stock": 100_000})
    result = simulate(
        book,
        [strategy_hold(), strategy_replenish()],
        market=default_market(),
        scenarios=(DECLINE,),
        n_paths=12,
        n_days=5,
        seed=SEED,
    )
    fig = make_figure(result, (DECLINE,))
    fig.canvas.draw()
    title = fig._suptitle.get_text()
    assert "示意模型，非預測" in title
    assert fig.legends, "圖例要畫在圖外，不能省略"
    assert fig.legends[0].get_window_extent().y1 < fig._suptitle.get_window_extent().y0
    import matplotlib.pyplot as plt

    plt.close(fig)


def test_full_size_run_stays_within_a_few_seconds():
    book = _prototype_book()
    started = time.perf_counter()
    result = simulate(
        book,
        [strategy_hold(), strategy_replenish()],
        market=default_market(),
        n_paths=2000,
        n_days=126,
        seed=SEED,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 20
    assert len(result.scenario_names) == 3
    assert set(result.weighted) == {"A 續抱", "B 停損回補"}
    assert result.stats[(DEFAULT_SCENARIOS[0].name, "A 續抱")].median_end > 0


def test_simulator_is_on_page6_and_not_on_page4():
    with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
        dashboard = handle.read()
    page4 = dashboard.split('elif page == "第四頁：部位監控與量化風控":', 1)[1].split("第五頁", 1)[0]
    assert "nav_scenario" not in page4
    assert "策略模擬" not in page4
    with open(os.path.join(ROOT, "order_assistant_ui.py"), encoding="utf-8") as handle:
        ui = handle.read()
    assert "render_combined_sim_panel" in ui
    panel = open(os.path.join(ROOT, "sim_panel_ui.py"), encoding="utf-8").read()
    assert panel.count("render_nav_scenario_simulator") >= 2


def test_simulator_brake_defaults_match_page4_lights():
    import textwrap

    from streamlit.testing.v1 import AppTest

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        from nav_scenario_sim_ui import _one_strategy
        _one_strategy(1, 1.8)
        """
    )
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    pause = at.number_input(key="nss_s1_pause")
    cut = at.number_input(key="nss_s1_cut")
    assert pause.value == pytest.approx(8.0)
    assert cut.value == pytest.approx(12.0)
    captions = " ".join(str(c.value) for c in at.caption)
    assert "停新倉預設 -8%（黃燈）" in captions
    assert "降到 1x 預設 -12%（紅燈）" in captions
    assert "回到 -8% 以內後解除" in captions
