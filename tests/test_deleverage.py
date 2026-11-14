"""降槓進度用已扣質押的淨值。缺報價不是 0。目標不是 REGIME_RISK。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from api.schemas import PortfolioResponse
from deleverage import SWAP_TO_1X, USER_DELEVERAGE_TARGETS, build_deleverage
from macro_cycle import REGIME_RISK
from portfolio_briefing import SCHEMA_VERSION, portfolio_payload
from portfolio_risk import finalize_valuation

AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _etf(symbol, qty, price, kind="美股"):
    return {
        "帳戶": "股票戶",
        "種類": kind,
        "標的": symbol,
        "名稱": symbol,
        "持有數量": qty,
        "平均成本": price,
        "投入總金額": abs(qty) * price,
        "標的槓桿": SWAP_TO_1X[symbol.split(".")[0]]["multiple"],
        "即時報價": price,
        "Beta值": 1.0,
        "產業別": "其他",
    }


class DeleverageTests(unittest.TestCase):
    def test_targets_are_not_regime_risk(self):
        self.assertEqual(REGIME_RISK["偏多"]["target_leverage"], 1.8)
        self.assertEqual([item["leverage"] for item in USER_DELEVERAGE_TARGETS], [2.0, 1.5])
        self.assertEqual(SWAP_TO_1X["00663L"]["underlying"], "^TWII")
        self.assertNotEqual(SWAP_TO_1X["00663L"]["underlying"], "0050")
        self.assertEqual(SWAP_TO_1X["SOXL"]["multiple"], 3.0)
        self.assertEqual(SWAP_TO_1X["MUU"]["multiple"], 2.0)

    def test_reduction_uses_nav_after_pledge(self):
        # 淨值 100 萬已含扣掉質押。名目 208 萬 → 槓桿 2.08。
        metrics = {"nav": 1_000_000.0, "exposure": 2_080_000.0, "exposure_complete": True}
        positions = pd.DataFrame([
            {**_etf("SOXL", 100, 1000), "即時市值(台幣)": 100_000, "換算匯率": 1.0},
            {**_etf("MUU", 50, 1000), "即時市值(台幣)": 50_000, "換算匯率": 1.0},
        ])
        view = build_deleverage(positions, metrics)
        self.assertAlmostEqual(view["leverage"], 2.08)
        by_id = {item["id"]: item for item in view["targets"]}
        self.assertAlmostEqual(by_id["bullish_cap"]["reduction_twd"], 80_000)
        self.assertAlmostEqual(by_id["cut_period"]["reduction_twd"], 580_000)
        self.assertAlmostEqual(by_id["bullish_cap"]["progress"], 2.0 / 2.08)
        self.assertEqual([item["symbol"] for item in view["swaps"]], ["SOXL", "MUU"])
        self.assertAlmostEqual(view["swaps"][0]["reduction_twd"], 200_000)
        self.assertAlmostEqual(view["swaps"][0]["cumulative_leverage"], 1.88)
        self.assertAlmostEqual(view["swaps"][1]["cumulative_leverage"], 1.83)

    def test_missing_price_is_unknown(self):
        positions = pd.DataFrame([_etf("SOXL", 10, float("nan"))])
        positions.loc[0, "即時市值(台幣)"] = float("nan")
        view = build_deleverage(positions, {"nav": 1_000_000, "exposure": 100, "exposure_complete": True})
        self.assertEqual(len(view["swaps"]), 1)
        self.assertIsNone(view["swaps"][0]["reduction_twd"])
        self.assertIsNone(view["swaps"][0]["cumulative_leverage"])

    def test_api_includes_deleverage(self):
        accounts = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 1_000_000, "融資/質押借款": 305_430}])
        valued, _accounts, metrics = finalize_valuation(pd.DataFrame([_etf("SOXL", 10, 20)]), accounts, 32.0)
        payload = portfolio_payload(valued, metrics, usdtwd=32.0, as_of=AS_OF)
        self.assertEqual(payload["schema_version"], "1.17.1")
        self.assertEqual(SCHEMA_VERSION, "1.17.1")
        self.assertEqual(payload["deleverage"]["swaps"][0]["symbol"], "SOXL")
        self.assertAlmostEqual(payload["net_value"], metrics["nav"])
        PortfolioResponse(**payload)

    def test_same_symbol_across_accounts_is_one_row(self):
        metrics = {"nav": 1_000_000.0, "exposure": 2_000_000.0, "exposure_complete": True}
        positions = pd.DataFrame([
            {**_etf("SOXL", 10, 100), "帳戶": "甲", "即時市值(台幣)": 100_000, "換算匯率": 1.0},
            {**_etf("SOXL", 5, 100), "帳戶": "乙", "即時市值(台幣)": 50_000, "換算匯率": 1.0},
        ])
        view = build_deleverage(positions, metrics)
        self.assertEqual([item["symbol"] for item in view["swaps"]], ["SOXL"])
        self.assertAlmostEqual(view["swaps"][0]["market_value_twd"], 150_000)
        self.assertAlmostEqual(view["swaps"][0]["reduction_twd"], 300_000)
        self.assertIn("2 個帳戶", view["swaps"][0]["note"])

    def test_one_account_missing_price_keeps_the_symbol_unknown(self):
        positions = pd.DataFrame([
            {**_etf("SOXL", 10, 100), "即時市值(台幣)": 100_000},
            {**_etf("SOXL", 5, float("nan")), "即時市值(台幣)": float("nan")},
        ])
        view = build_deleverage(positions, {"nav": 1_000_000, "exposure": 100, "exposure_complete": True})
        self.assertEqual(len(view["swaps"]), 1)
        self.assertIsNone(view["swaps"][0]["reduction_twd"])

    def test_allocation_bands_and_tactical_cap(self):
        metrics = {
            "nav": 1_000_000.0,
            "exposure": 1_100_000.0,
            "exposure_complete": True,
            "cash": 120_000.0,
            "brake_light": "🔴",
            "bear_overall": "yellow",
        }
        positions = pd.DataFrame([
            {"帳戶": "股票戶", "種類": "台股", "標的": "2330", "名稱": "台積電", "持有數量": 10,
             "即時報價": 100, "即時市值(台幣)": 550_000, "標的槓桿": 1},
            {"帳戶": "股票戶", "種類": "美股", "標的": "XLV", "名稱": "XLV", "持有數量": 10,
             "即時報價": 100, "即時市值(台幣)": 250_000, "標的槓桿": 1, "換算匯率": 32},
            {**_etf("SOXL", 10, 100), "即時市值(台幣)": 100_000, "標的槓桿": 3},
            {"帳戶": "股票戶", "種類": "美股選擇權", "標的": "QQQ241220C00480000", "名稱": "QQQ Call",
             "持有數量": 1, "即時市值(台幣)": 200_000, "標的槓桿": 1, "即時報價": 2},
            {"帳戶": "股票戶", "種類": "台股", "標的": "9999", "名稱": "未對照",
             "持有數量": 1, "即時市值(台幣)": 10_000, "標的槓桿": 1, "即時報價": 10},
        ])
        view = build_deleverage(positions, metrics)
        self.assertEqual(view["exposure_band"]["status"], "在區間內")
        self.assertEqual(view["exposure_band"]["gap_twd"], 0.0)
        by_id = {item["id"]: item for item in view["buckets"]}
        self.assertEqual(by_id["core"]["status"], "在區間內")
        self.assertNotIn("SOXL", by_id["core"]["symbols"])
        self.assertEqual(by_id["defense"]["status"], "在區間內")
        self.assertEqual(by_id["event"]["status"], "超過上限")
        self.assertAlmostEqual(by_id["event"]["gap_twd"], 50_000)
        self.assertIn("單筆風險", by_id["event"]["note"])
        self.assertIsNone(view["event_risk"]["per_trade_pct"])
        self.assertTrue(view["tactical"]["exceeded"])
        self.assertAlmostEqual(view["tactical"]["notional_twd"], 300_000)
        self.assertEqual(view["tactical"]["positions"][0]["tag"], "戰術曝險")
        self.assertEqual([item["symbol"] for item in view["unclassified"]], ["9999"])
        self.assertEqual([item["symbol"] for item in view["priority_reductions"]], ["SOXL"])
        quiet = build_deleverage(positions, {**metrics, "brake_light": "🟢", "bear_overall": "unknown"})
        self.assertEqual(quiet["priority_reductions"], [])


if __name__ == "__main__":
    unittest.main()
