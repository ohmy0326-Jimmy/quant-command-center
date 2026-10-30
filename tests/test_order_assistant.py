"""下單輔助與第四頁共用估價的單元測試。不連網、不讀金鑰。"""

import math
import os
import unittest

import pandas as pd

from order_assistant import (
    Rules,
    Snapshot,
    apply_order,
    bs_price_greeks,
    build_checklist_text,
    build_sample_snapshot,
    economic_exposure,
    hedge_profit_plan,
    loss_breaches,
    option_expiry_pnl,
    propose_rebalance,
    run_stress,
    suggest_addback_text,
    taiex_shock_state,
)
from portfolio_risk import YUANTA_FUTURES_BASE_EQUITY, finalize_valuation


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
        self.assertAlmostEqual(metrics["nav"], 527922.0, places=6)
        self.assertAlmostEqual(metrics["exposure"], 1_033_500.0, places=6)
        self.assertAlmostEqual(metrics["leverage"], 1_033_500 / 527922, places=8)
        self.assertAlmostEqual(metrics["cash"], 50000.0, places=6)
        self.assertAlmostEqual(metrics["beta"], 1_100_500 / 1_033_500, places=8)
        self.assertAlmostEqual(metrics["margin_ratio"], 370422 / 538000 * 100, places=6)
        self.assertTrue(metrics["margin_call"])
        self.assertAlmostEqual(metrics["futures_equity"], YUANTA_FUTURES_BASE_EQUITY + 200000, places=6)
        # 0.3% 稅只進損益欄，沒有從淨值再扣一次。
        self.assertAlmostEqual(metrics["nav"], 50000 + 110000 + 7500 - 10000 + 370422, places=6)

    def test_futures_account_without_positions_uses_base_equity(self):
        accounts = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 999, "融資/質押借款": 0}])
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame(), accounts, 32)
        self.assertAlmostEqual(accounts_out.iloc[0]["帳戶淨值"], YUANTA_FUTURES_BASE_EQUITY, places=6)
        self.assertAlmostEqual(metrics["nav"], YUANTA_FUTURES_BASE_EQUITY, places=6)


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
        self.assertAlmostEqual(updated.metrics["beta"], 1.75, places=6)
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
            "第三頁：重大行事曆與會展",
            "第四頁：部位監控與量化風控",
            "第五頁：Telegram 戰情推播",
            "第六頁：下單輔助系統",
        ]:
            self.assertIn(label, dashboard)
        self.assertIn("finalize_valuation", dashboard)
        self.assertIn("render_order_assistant", dashboard)
        self.assertNotIn("170422", dashboard)
        self.assertNotIn("701000", dashboard)


if __name__ == "__main__":
    unittest.main()
