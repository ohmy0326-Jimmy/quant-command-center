"""下單輔助與第四頁共用估價的單元測試。不連網、不讀金鑰。"""

import math
from datetime import date
import os
import unittest
from unittest.mock import patch

import pandas as pd
import portfolio_risk

from order_assistant import (
    Rules,
    Snapshot,
    apply_order,
    bs_price_greeks,
    build_checklist_text,
    build_sample_snapshot,
    economic_exposure,
    hedge_profit_plan,
    holding_display_values,
    loss_breaches,
    option_expiry_pnl,
    propose_rebalance,
    run_stress,
    split_betas,
    strategy_summary,
    suggest_addback_text,
    taiex_shock_state,
)
from portfolio_risk import YUANTA_FUTURES_BASE_EQUITY, equity_ratio_pct, finalize_valuation, normalize_positions


def _stock(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, sector="其他", max_loss=-100):
    return {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": 0.0 if "期" in kind else cost * qty,
        "最大虧損比例": max_loss,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": sector,
    }


class ValuationTests(unittest.TestCase):
    def test_page4_formulas_nav_leverage_beta_margin(self):
        positions = pd.DataFrame(
            [
                _stock("股票戶", "台股", "2330", 1000, 100, 110, lev=1, beta=1.2, sector="晶圓代工", max_loss=-10),
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
        valued, accounts_out, metrics = finalize_valuation(positions, accounts, usdtwd=30)

        tw = valued[valued["標的"] == "2330"].iloc[0]
        self.assertAlmostEqual(tw["淨未實現損益(原幣)"], 9670.0, places=6)
        self.assertAlmostEqual(tw["即時市值(台幣)"], 110000.0, places=6)
        # nav = 50000 + 110000 + 7500 - 10000 + 368308 = 525808
        self.assertAlmostEqual(metrics["nav"], 525808.0, places=6)
        # exposure = 2330 (110k) + SOXL (7.5k*3=22.5k) + TX notional (1 * 21000 * 200 * 1 = 4.2M) = 4,332,500
        self.assertAlmostEqual(metrics["exposure"], 4_332_500.0, places=6)
        self.assertAlmostEqual(metrics["leverage"], 4_332_500 / 525808, places=8)
        self.assertAlmostEqual(metrics["cash"], 50000.0, places=6)
        # tw_exp = 110,000 * 1.2 + 4,200,000 = 4,332,000
        # us_exp = SOXL 市值 7,500 × 槓桿 3 × SOXX 預設 Beta 1.6 = 36,000（舊規則只算槓桿 = 22,500）
        # total_beta = (4,332,000 + 36,000) / 525,808 = 4,368,000 / 525,808
        self.assertAlmostEqual(metrics["beta"], 4_368_000 / 525808, places=8)
        self.assertAlmostEqual(metrics["total_beta"], 4_368_000 / 525808, places=8)
        self.assertAlmostEqual(metrics["margin_ratio"], 368308 / 538000 * 100, places=6)
        self.assertTrue(metrics["margin_call"])
        self.assertAlmostEqual(metrics["futures_equity"], YUANTA_FUTURES_BASE_EQUITY + 200000, places=6)
        # 0.3% 稅只進損益欄，沒有從淨值再扣一次。
        self.assertAlmostEqual(metrics["nav"], 50000 + 110000 + 7500 - 10000 + 368308, places=6)

    def test_futures_account_without_positions_uses_base_equity(self):
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0}])
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame(), accounts, 32)
        self.assertAlmostEqual(accounts_out.iloc[0]["帳戶淨值"], YUANTA_FUTURES_BASE_EQUITY, places=6)
        self.assertAlmostEqual(metrics["nav"], YUANTA_FUTURES_BASE_EQUITY, places=6)

    def test_futures_account_uses_sheet_cash_ledger(self):
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 999, "融資/質押借款": 0}])
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame(), accounts, 32)
        self.assertAlmostEqual(accounts_out.iloc[0]["帳戶淨值"], 999.0, places=6)
        self.assertAlmostEqual(metrics["nav"], 999.0, places=6)

    def test_yuanta_broker_reconciliation(self):
        positions = pd.DataFrame(
            [
                _stock("元大期貨", "台股期貨", "FMF", 2, 46733, 45185, lev=1, beta=1, sector="微台指"),
            ]
        )
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "元大期貨", "現金餘額": 168308, "融資/質押借款": 0},
            ]
        )
        valued, accounts_out, metrics = finalize_valuation(positions, accounts, usdtwd=30.0)
        self.assertAlmostEqual(accounts_out.iloc[0]["帳戶淨值"], 137348.0, places=6)
        self.assertAlmostEqual(metrics["futures_initial"], 70100.0, places=6)
        self.assertAlmostEqual(metrics["futures_maint"], 53800.0, places=6)
        self.assertAlmostEqual(metrics["excess_margin"], 67248.0, places=6)
        self.assertAlmostEqual(accounts_out.iloc[0]["權益比率%/現金水位%"], round(137348 / 70100 * 100, 1), places=6)
        self.assertAlmostEqual(metrics["risk_indicator"], 137348 / 70100 * 100, places=6)
        self.assertAlmostEqual(metrics["margin_call_distance"], 137348 - 53800, places=6)
        self.assertAlmostEqual(metrics["margin_call_points"], (137348 - 53800) / 20.0, places=6)
        self.assertAlmostEqual(metrics["futures_cash_ledger"], 168308.0, places=6)
        self.assertAlmostEqual(accounts_out.iloc[0]["現金餘額"], 168308.0, places=6)
        self.assertAlmostEqual(metrics["cash"], 67248.0, places=6)

    def test_option_row_gets_no_futures_margin(self):
        positions = pd.DataFrame(
            [
                {
                    "帳戶": "元大期貨",
                    "種類": "台股期貨",
                    "標的": "FMF",
                    "名稱": "FMF",
                    "持有數量": 2,
                    "平均成本": 46733,
                    "投入總金額": 0,
                    "最大虧損比例": -100,
                    "標的槓桿": 1.0,
                    "即時報價": 45185,
                    "Beta值": 1.0,
                    "產業別": "微台指",
                    "合約乘數": math.nan,
                },
                {
                    "帳戶": "元大期貨",
                    "種類": "台指選擇權",
                    "標的": "TXO202611W1C45000",
                    "名稱": "TXO 202611W1 C 45000",
                    "持有數量": 1,
                    "平均成本": 100,
                    "投入總金額": 5000,
                    "最大虧損比例": -100,
                    "標的槓桿": 1.0,
                    "即時報價": 120,
                    "Beta值": 1.0,
                    "產業別": "其他",
                    "合約乘數": 50,
                },
            ]
        )
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "元大期貨", "現金餘額": 168308, "融資/質押借款": 0},
            ]
        )
        valued, accounts_out, metrics = finalize_valuation(positions, accounts, usdtwd=30.0)
        self.assertAlmostEqual(accounts_out.iloc[0]["原始保證金總額"], 70100.0, places=6)
        self.assertAlmostEqual(accounts_out.iloc[0]["維持保證金總額"], 53800.0, places=6)
        expected_nav = 168308 + (45185 - 46733) * 20 + 120 * 1 * 50
        self.assertAlmostEqual(accounts_out.iloc[0]["帳戶淨值"], expected_nav, places=6)

    def test_option_no_quote_label_surfaces(self):
        positions = pd.DataFrame(
            [
                {
                    "帳戶": "元大期貨",
                    "種類": "台股期貨",
                    "標的": "FMF",
                    "名稱": "FMF",
                    "持有數量": 2,
                    "平均成本": 46733,
                    "投入總金額": 0,
                    "最大虧損比例": -100,
                    "標的槓桿": 1.0,
                    "即時報價": 45185,
                    "Beta值": 1.0,
                    "產業別": "微台指",
                    "合約乘數": math.nan,
                    "報價來源": "TAIFEX 微臺指期 TMFL6-M 夜盤 19:35 成交價 45,185",
                },
                {
                    "帳戶": "元大期貨",
                    "種類": "台指選擇權",
                    "標的": "TXO202611W1C45000",
                    "名稱": "TXO 202611W1 C 45000",
                    "持有數量": 1,
                    "平均成本": 100,
                    "投入總金額": 5000,
                    "最大虧損比例": -100,
                    "標的槓桿": 1.0,
                    "即時報價": 120,
                    "Beta值": 1.0,
                    "產業別": "其他",
                    "合約乘數": 50,
                    "報價來源": "無報價，暫用成本價",
                },
            ]
        )
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "元大期貨", "現金餘額": 168308, "融資/質押借款": 0},
            ]
        )
        valued, accounts_out, metrics = finalize_valuation(positions, accounts, usdtwd=30.0)
        self.assertIn("無報價，暫用成本價", metrics["futures_price_labels"])

    def test_us_option_keeps_yahoo_quote_not_taifex(self):
        with patch("taifex_quotes.get_option_quote") as taifex_opt, patch("portfolio_risk._fetch_yahoo_price", return_value=1.23):
            _t, price, _b, _s, extras = portfolio_risk._quote_one_ex(
                "MUU261030C00045000", 23000.0, lambda t, tw: (1.0, "其他"), kind="美股選擇權"
            )
        taifex_opt.assert_not_called()
        self.assertAlmostEqual(price, 1.23, places=6)
        self.assertNotIn("無報價", extras.get("報價來源", ""))


class OrderImpactTests(unittest.TestCase):
    def _book(self):
        positions = pd.DataFrame([_stock("股票戶", "台股", "AAA", 50, 10, 10, lev=1, beta=1)])
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 500, "融資/質押借款": 0}])
        return Snapshot(positions, accounts, usdtwd=32, twii=20000)

    def test_buy_leveraged_etf_changes_leverage_and_beta(self):
        updated, warnings = apply_order(
            self._book(),
            {"ticker": "SOXL", "side": "buy", "qty": 10, "price": 10, "kind": "台股", "beta": 3, "leverage": 3, "sector": "半導體槓桿ETF"},
        )
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(updated.metrics["nav"], 1000.0, places=6)
        self.assertAlmostEqual(updated.metrics["exposure"], 800.0, places=6)
        self.assertAlmostEqual(updated.metrics["leverage"], 0.8, places=6)
        # AAA MV 500 * beta 1 + SOXL MV 100 * 槓桿 3 * SOXX 預設 Beta 1.6 = 980, NAV 1000 => 0.98
        self.assertAlmostEqual(updated.metrics["beta"], 0.98, places=6)
        self.assertAlmostEqual(updated.metrics["cash"], 400.0, places=6)

    def test_buy_shortfall_becomes_margin_debt_and_nav_unchanged(self):
        positions = pd.DataFrame([_stock("股票戶", "台股", "AAA", 10, 10, 10)])
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 50, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, 32, 20000)
        updated, warnings = apply_order(snap, {"ticker": "AAA", "side": "buy", "qty": 10, "price": 10, "kind": "台股"})
        self.assertTrue(any("融資" in note for note in warnings))
        self.assertAlmostEqual(updated.metrics["nav"], snap.metrics["nav"], places=6)
        self.assertAlmostEqual(updated.metrics["debt"], 50.0, places=6)
        self.assertAlmostEqual(updated.metrics["cash"], 0.0, places=6)

    def test_new_tx_sets_margin_and_economic_notional(self):
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0}])
        snap = Snapshot(pd.DataFrame(), accounts, 32, 20000)
        updated, _warnings = apply_order(snap, {"ticker": "TX", "side": "buy", "qty": 1, "price": 20000, "kind": "期貨"})
        self.assertTrue(updated.metrics["margin_call"])
        self.assertAlmostEqual(updated.metrics["futures_maint"], 538000.0, places=6)
        self.assertAlmostEqual(updated.metrics["futures_equity"], YUANTA_FUTURES_BASE_EQUITY, places=6)
        self.assertAlmostEqual(economic_exposure(updated), 20000 * 200, places=4)


class LossAndRebalanceTests(unittest.TestCase):
    def test_trim_fraction_brings_loss_back_to_budget(self):
        positions = pd.DataFrame([_stock("股票戶", "台股", "2454", 100, 20, 10, max_loss=-50)])
        # 成本 2000、市值 1000。浮虧約 1000 + 稅。淨值 = 現金 0 + 市值 1000。
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 9000, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, 32, 20000)
        rules = Rules(max_loss_pct_nav=2.0)
        breaches = loss_breaches(snap, rules)
        self.assertEqual(len(breaches), 1)
        item = breaches[0]
        budget = snap.metrics["nav"] * 0.02
        self.assertGreater(item["current_loss"], budget)
        self.assertAlmostEqual(item["current_loss"] * (1 - item["trim_fraction"]), budget, places=4)

    def test_rebalance_cuts_leveraged_product_first_and_hits_target(self):
        snap = build_sample_snapshot()
        rules = Rules()
        self.assertGreater(snap.metrics["leverage"], 1.8)
        self.assertLess(snap.metrics["cash_pct"], 15)
        self.assertFalse(snap.metrics["margin_call"])
        plan = propose_rebalance(snap, rules, core_tickers=["2330", "0050"], rotate_fraction=0.3, strict_sector=False)
        self.assertEqual(plan["actions"][0]["ticker"], "SOXL")
        self.assertEqual(plan["actions"][0]["action"], "賣出")
        self.assertLessEqual(plan["after"]["leverage"], 1.8 + 1e-6)
        self.assertGreaterEqual(plan["after"]["cash_pct"], 15 - 1e-6)
        sold = {row["ticker"] for row in plan["actions"] if row["action"] == "賣出"}
        self.assertNotIn("TX", sold)
        self.assertNotIn("2330", sold)
        after = plan["after_snapshot"]
        before_qty = {row["標的"]: row["持有數量"] for _, row in snap.positions.iterrows()}
        after_qty = {row["標的"]: row["持有數量"] for _, row in after.positions.iterrows()}
        self.assertGreaterEqual(after_qty["2330"], before_qty["2330"])
        self.assertLess(after_qty["2454"], before_qty["2454"])
        self.assertGreater(after_qty.get("0050", 0), before_qty["0050"])

    def test_stage2_is_tighter_than_primary_target(self):
        snap = build_sample_snapshot()
        rules = Rules()
        primary = propose_rebalance(snap, rules, ["2330", "0050"], 0.0, True, target_leverage=1.8)
        stage2 = propose_rebalance(snap, rules, ["2330", "0050"], 0.0, True, target_leverage=1.5)
        self.assertLessEqual(stage2["after"]["leverage"], 1.5 + 1e-6)
        self.assertLessEqual(stage2["after"]["leverage"], primary["after"]["leverage"] + 1e-9)


class BlackScholesTests(unittest.TestCase):
    def test_put_call_parity_and_known_call_price(self):
        spot, strike, tenor, rate, sigma = 100.0, 100.0, 1.0, 0.05, 0.2
        call, call_delta, call_theta = bs_price_greeks(spot, strike, tenor, rate, sigma, "call")
        put, put_delta, _put_theta = bs_price_greeks(spot, strike, tenor, rate, sigma, "put")
        parity = spot - strike * math.exp(-rate * tenor)
        self.assertAlmostEqual(call - put, parity, places=8)
        self.assertAlmostEqual(call, 10.45058357, places=4)
        self.assertAlmostEqual(put_delta, call_delta - 1, places=8)
        self.assertLess(call_theta, 0)
        self.assertLess(put_delta, 0)
        self.assertGreater(put_delta, -1)

    def test_protective_put_and_spread_payoff(self):
        legs = [{"kind": "put", "strike": 100, "signed_lots": 1, "premium": 5}]
        self.assertAlmostEqual(option_expiry_pnl(80, legs, 50), (20 - 5) * 50, places=6)
        self.assertAlmostEqual(option_expiry_pnl(110, legs, 50), -5 * 50, places=6)
        spread = [
            {"kind": "put", "strike": 100, "signed_lots": 1, "premium": 6},
            {"kind": "put", "strike": 90, "signed_lots": -1, "premium": 2},
        ]
        self.assertAlmostEqual(option_expiry_pnl(80, spread, 50), (10 - 4) * 50, places=6)
        collar = [
            {"kind": "put", "strike": 100, "signed_lots": 1, "premium": 4},
            {"kind": "call", "strike": 110, "signed_lots": -1, "premium": 3},
        ]
        self.assertAlmostEqual(option_expiry_pnl(120, collar, 50), (-10 - 1) * 50, places=6)

    def test_hedge_profit_rule(self):
        quiet = hedge_profit_plan(100, 250, multiple=3, realize_fraction=0.5)
        self.assertFalse(quiet["triggered"])
        self.assertEqual(quiet["realize_cash"], 0)
        hot = hedge_profit_plan(100, 350, multiple=3, realize_fraction=0.5)
        self.assertTrue(hot["triggered"])
        self.assertAlmostEqual(hot["realize_cash"], 175, places=6)
        self.assertAlmostEqual(hot["keep_value"], 175, places=6)


class StressTests(unittest.TestCase):
    def test_taiex_drop_moves_tw_stock_by_beta_and_not_us_stock(self):
        positions = pd.DataFrame(
            [
                _stock("股票戶", "台股", "2330", 100, 10, 10, beta=1.2),
                _stock("股票戶", "美股", "NVDA", 10, 10, 10, beta=1.8),
            ]
        )
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 0, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, usdtwd=30, twii=20000)
        stressed = run_stress(snap, "台指 -10%", taiex=-0.10)
        tw_price = float(stressed["snapshot"].positions.loc[stressed["snapshot"].positions["標的"] == "2330", "即時報價"].iloc[0])
        us_price = float(stressed["snapshot"].positions.loc[stressed["snapshot"].positions["標的"] == "NVDA", "即時報價"].iloc[0])
        self.assertAlmostEqual(tw_price, 10 * (1 - 0.10 * 1.2), places=6)
        self.assertAlmostEqual(us_price, 10, places=6)

    def test_sox_hits_leveraged_etf_three_times(self):
        positions = pd.DataFrame([_stock("股票戶", "美股", "SOXL", 10, 40, 40, lev=3, sector="半導體槓桿ETF")])
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 0, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, 32, 20000)
        stressed = run_stress(snap, "費半", sox=-0.30)
        price = float(stressed["snapshot"].positions.iloc[0]["即時報價"])
        self.assertAlmostEqual(price, 40 * (1 - 0.90), places=6)

    def test_futures_point_loss_and_margin_call(self):
        positions = pd.DataFrame([_stock("元大期貨", "期貨", "TX", 1, 18000, 20000)])
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, 32, 20000)
        self.assertFalse(snap.metrics["margin_call"])
        stressed = run_stress(snap, "台指 -10%", taiex=-0.10)
        self.assertAlmostEqual(stressed["futures_equity"], snap.metrics["futures_equity"] - 2000 * 200, places=4)
        self.assertTrue(stressed["margin_call"])

    def test_twd_depreciation_lifts_us_asset_nav(self):
        positions = pd.DataFrame([_stock("股票戶", "美股", "NVDA", 10, 100, 100)])
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 0, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, usdtwd=30, twii=20000)
        stressed = run_stress(snap, "貶值", fx=0.05)
        self.assertAlmostEqual(stressed["nav"], 10 * 100 * 30 * 1.05, places=4)
        self.assertGreater(stressed["drawdown_pct"], 0)


class SpecificationRefinementTests(unittest.TestCase):
    def test_tmf_futures_exposure(self):
        # 2 lots TMF at 22600 index => Notional = 2 * 22600 * 10 = 452,000 TWD
        positions = pd.DataFrame([_stock("元大期貨", "期貨", "TMF", 2, 22000, 22600)])
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0}])
        valued, _acc, metrics = finalize_valuation(positions, accounts, usdtwd=32.0)
        row = valued.iloc[0]
        self.assertEqual(row["實質曝險(台幣)"], 452000.0)
        self.assertEqual(row["Beta曝險(台幣)"], 452000.0)
        self.assertEqual(metrics["exposure"], 452000.0)

    def test_leveraged_etf_beta_exposure(self):
        # US Leveraged ETF: MV = 100k TWD, 標的槓桿 = 3, 標的 SOXX 無即時資料 => 預設 1.6，Beta曝險 = 100k * 3 * 1.6 = 480k（不用 ETF 自己的 3.2）
        positions = pd.DataFrame([_stock("美股戶", "美股", "SOXL", 100, 31.25, 31.25, lev=3.0, beta=3.2)])
        accounts = pd.DataFrame([{"帳戶名稱": "美股戶", "現金餘額": 0, "融資/質押借款": 0}])
        valued, _acc, metrics = finalize_valuation(positions, accounts, usdtwd=32.0)
        row = valued.iloc[0]
        # MV(TWD) = 100 * 31.25 * 32 = 100,000 TWD
        self.assertAlmostEqual(row["即時市值(台幣)"], 100000.0)
        self.assertAlmostEqual(row["實質曝險(台幣)"], 300000.0)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], 480000.0)
        self.assertIn("預設值", row["Beta來源"])

    def test_split_nav_and_betas(self):
        positions = pd.DataFrame(
            [
                _stock("台股戶", "台股", "2330", 1000, 200, 200, beta=1.0),
                _stock("美股戶", "美股", "NVDA", 100, 100, 100, beta=1.5),
            ]
        )
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "台股戶", "現金餘額": 100000, "融資/質押借款": 0},
                {"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0},
                {"帳戶名稱": "美股戶", "現金餘額": 50000, "融資/質押借款": 0},
            ]
        )
        # 台股戶: cash 100k + stock 200k = 300k NAV
        # 元大期貨: 168308 base equity NAV
        # 美股戶: cash 50k + stock 100*100*30=300k = 350k NAV
        # tw_nav = 300,000 + 168,308 = 468,308
        # us_nav = 350,000
        # nav = 818,308
        _valued, _acc, metrics = finalize_valuation(positions, accounts, usdtwd=30.0)
        self.assertAlmostEqual(metrics["tw_nav"], 468308.0, places=6)
        self.assertAlmostEqual(metrics["us_nav"], 350000.0, places=6)
        self.assertAlmostEqual(metrics["nav"], 818308.0, places=6)
        self.assertAlmostEqual(metrics["tw_nav"] + metrics["us_nav"], metrics["nav"], places=6)
        self.assertAlmostEqual(metrics["tw_beta"], 200000 / 468308, places=6)
        self.assertAlmostEqual(metrics["us_beta"], 450000 / 350000, places=6)
        self.assertAlmostEqual(metrics["total_beta"], 650000 / 818308, places=6)

    def test_parse_expiry_formats(self):
        from portfolio_risk import parse_expiry

        occ_row = {"種類": "美股期權", "標的": "MUU261030C00050000", "名稱": "MUU Call"}
        self.assertEqual(parse_expiry(occ_row), date(2026, 10, 30))

        us_fmt_row = {"種類": "美股期權", "標的": "SNXX", "名稱": "SNXX 10/30/2026 Call"}
        self.assertEqual(parse_expiry(us_fmt_row), date(2026, 10, 30))

        embedded_row = {"種類": "期權", "標的": "MUU", "名稱": "MUU20261030C"}
        self.assertEqual(parse_expiry(embedded_row), date(2026, 10, 30))

    def test_expired_option_filter(self):
        raw_data = pd.DataFrame(
            [
                {
                    "帳戶": "美股戶",
                    "種類": "美股期權",
                    "標的": "MUU_EXP",
                    "名稱": "MUU 2026/10/30 Call",
                    "持有數量": -5,
                    "平均成本": 1.0,
                },
                {
                    "帳戶": "美股戶",
                    "種類": "美股期權",
                    "標的": "MUU_ACTIVE",
                    "名稱": "MUU Call",
                    "到期日": "2026-11-02",
                    "持有數量": -5,
                    "平均成本": 1.0,
                },
            ]
        )
        cleaned = normalize_positions(raw_data, today=date(2026, 11, 2))
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned.iloc[0]["標的"], "MUU_ACTIVE")
        self.assertEqual(cleaned.attrs["expired_removed"], ["MUU_EXP"])

    def test_split_betas_with_put_delta(self):
        metrics = {
            "tw_exposure": 1000000.0,
            "us_exposure": 500000.0,
            "tw_nav": 500000.0,
            "us_nav": 500000.0,
            "nav": 1000000.0,
        }
        # Put delta = -100k exposure, paid 10k premium debit
        res = split_betas(metrics, tw_exposure_delta=-100000.0, tw_cash_delta=-10000.0)
        self.assertEqual(res["tw_exposure"], 900000.0)
        self.assertEqual(res["tw_nav"], 490000.0)
        self.assertAlmostEqual(res["tw_beta"], 900000 / 490000, places=6)
        self.assertAlmostEqual(res["total_beta"], 1400000 / 990000, places=6)

    def test_strategy_summary_new_keys(self):
        positions = pd.DataFrame([_stock("股票戶", "台股", "2330", 1000, 1000, 1000, beta=1.0)])
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 500000, "融資/質押借款": 0}])
        snap = Snapshot(positions, accounts, usdtwd=30.0, twii=23000.0)
        legs = [{"kind": "put", "strike": 22000, "signed_lots": 1, "premium": 200, "iv": 0.2}]
        summary = strategy_summary(snap, "保護性賣權", legs, t_years=0.1, rate=0.01)
        self.assertIn("tw_beta_before", summary)
        self.assertIn("tw_beta_after", summary)
        self.assertIn("total_beta_before", summary)
        self.assertIn("total_beta_after", summary)
        self.assertIn("option_exposure", summary)
        self.assertLess(summary["tw_beta_after"], summary["tw_beta_before"])


class FastPathTests(unittest.TestCase):
    def test_taiex_curve_matches_full_revaluation(self):
        snap = build_sample_snapshot()
        for step in (-30, -20, -10, 0, 15):
            move = step / 100.0
            fast = taiex_shock_state(snap, move)
            slow = run_stress(snap, "x", taiex=move)
            self.assertAlmostEqual(fast["nav"], slow["nav"], places=4)
            self.assertAlmostEqual(fast["futures_equity"], slow["futures_equity"], places=4)
            self.assertEqual(fast["margin_call"], slow["margin_call"])


class SecretFallbackTests(unittest.TestCase):
    def test_missing_secrets_file_returns_default(self):
        from app_secrets import read_secret

        self.assertEqual(read_secret("", "passwords", "admin"), "")
        self.assertIsNone(read_secret(None, "google_key"))
        self.assertEqual(read_secret("nope", "GEMINI_API_KEY"), "nope")


class TextTests(unittest.TestCase):
    def test_addback_and_checklist_do_not_place_orders(self):
        text = suggest_addback_text(Rules(), "2330, 0050")
        self.assertIn("1.80", text)
        self.assertIn("1.50", text)
        self.assertIn("不會自動下單", text)
        snap = build_sample_snapshot()
        checklist = build_checklist_text(snap, Rules(), [], addback_text=text)
        self.assertIn("不會送到券商", checklist)
        self.assertNotIn("private_key", checklist)


class DashboardWiringTests(unittest.TestCase):
    def test_pages_still_present_and_margin_constants_live_in_one_module(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "Dashboard.py"), encoding="utf-8") as handle:
            dashboard = handle.read()
        for label in [
            "第一頁：總經與風險鬧鐘",
            "第二頁：板塊與主題資金輪動",
            "第三頁：持倉、觀察清單與總經行事曆",
            "第四頁：部位監控與量化風控",
            "第五頁：總經週期與配置",
            "第六頁：下單輔助系統",
        ]:
            self.assertIn(label, dashboard)
        page4 = dashboard.index("第四頁：部位監控與量化風控")
        macro = dashboard.index("第五頁：總經週期與配置")
        page6 = dashboard.index("第六頁：下單輔助系統")
        self.assertLess(page4, macro)
        self.assertLess(macro, page6)
        self.assertNotIn("第五頁：Telegram 戰情推播", dashboard)
        radio = dashboard.split("切換監控面板：", 1)[1].split("key=\"nav_page\"", 1)[0]
        self.assertLess(radio.index("第二頁：板塊與主題資金輪動"), radio.index("PAGE3"))
        self.assertIn('PAGE3 = "第三頁：持倉、觀察清單與總經行事曆"', dashboard)
        self.assertNotIn("重大行事曆與會展", dashboard)
        self.assertNotIn("持倉與觀察清單行事曆", dashboard)
        self.assertNotIn("總經週期與配置", radio.split("第四頁：部位監控與量化風控", 1)[0])
        self.assertIn("send_to_telegram", dashboard)
        self.assertIn("render_macro_cycle_page", dashboard)
        self.assertIn("finalize_valuation", dashboard)
        self.assertIn("render_order_assistant", dashboard)
        self.assertNotIn("170422", dashboard)
        self.assertNotIn("701000", dashboard)
        ui_path = os.path.join(root, "order_assistant_ui.py")
        with open(ui_path, encoding="utf-8") as handle:
            order_ui = handle.read()
        self.assertIn("把這份摘要推到 Telegram", order_ui)
        self.assertIn("參數來自：", order_ui)
        self.assertIn("format_holding_tag", order_ui)
        self.assertIn("oa_apply_regime_defaults", order_ui)
        self.assertIn("@st.fragment", order_ui)
        with open(os.path.join(root, "macro_cycle_ui.py"), encoding="utf-8") as handle:
            self.assertIn('st.title("第五頁：總經週期與配置")', handle.read())


class Page6MarginAndNotionalTests(unittest.TestCase):
    def test_equity_ratio_matches_yuanta_and_page4(self):
        # 元大：權益數 105,024、原始保證金 35,050 → 299.64%
        self.assertAlmostEqual(equity_ratio_pct(105_024, 35_050), 299.64, places=2)
        self.assertIsNone(equity_ratio_pct(105_024, 0))
        positions = pd.DataFrame([_stock("元大期貨", "期貨", "FMF", 1, 45000, 45434, lev=1, beta=1)])
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 105024, "融資/質押借款": 0}])
        _valued, _accounts_out, metrics = finalize_valuation(positions, accounts, 32)
        self.assertAlmostEqual(metrics["risk_indicator"], equity_ratio_pct(metrics["futures_equity"], metrics["futures_initial"]), places=6)
        self.assertAlmostEqual(metrics["futures_initial"], 35050, places=6)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "order_assistant_ui.py"), encoding="utf-8") as handle:
            page6 = handle.read()
        with open(os.path.join(root, "Dashboard.py"), encoding="utf-8") as handle:
            page4 = handle.read()
        self.assertIn("equity_ratio_pct", page6)
        self.assertIn("equity_ratio_pct", page4)
        self.assertIn("權益比率（權益數 ÷ 原始保證金）", page6)
        # 第六頁現況健檢只留一行摘要；距追繳等完整數字在第四頁
        self.assertIn("page6_health_line", page6)
        self.assertIn("距追繳", page4)
        self.assertNotIn("期貨保證金比率（權益 ÷ 維持保證金）", page6)

    def test_fmf_industry_table_uses_notional_not_pnl(self):
        pos = {
            "ticker": "FMF",
            "name": "微台",
            "kind": "台股期貨",
            "qty": 1,
            "price": 45027,
            "fx": 1,
            "mv_twd": 4340,
            "exposure": 450270,
            "pnl_twd": 4340,
        }
        shown = holding_display_values(pos)
        self.assertEqual(shown["basis"], "名目")
        self.assertAlmostEqual(shown["amount"], 450270, places=4)
        self.assertAlmostEqual(shown["pnl"], 4340, places=4)
        self.assertNotAlmostEqual(shown["amount"], shown["pnl"], places=0)
        fallback = dict(pos, exposure=0)
        self.assertAlmostEqual(holding_display_values(fallback)["amount"], 45027 * 10, places=4)
        stock = {"ticker": "2330", "name": "台積電", "kind": "台股", "qty": 10, "price": 100, "fx": 1, "mv_twd": 1000, "exposure": 1000, "pnl_twd": 50}
        stock_shown = holding_display_values(stock)
        self.assertEqual(stock_shown["basis"], "市值")
        self.assertAlmostEqual(stock_shown["amount"], 1000, places=4)
        self.assertAlmostEqual(stock_shown["pnl"], 50, places=4)


if __name__ == "__main__":
    unittest.main()
