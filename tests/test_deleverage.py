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
        self.assertEqual(payload["schema_version"], "1.13.0")
        self.assertEqual(SCHEMA_VERSION, "1.13.0")
        self.assertEqual(payload["deleverage"]["swaps"][0]["symbol"], "SOXL")
        self.assertAlmostEqual(payload["net_value"], metrics["nav"])
        PortfolioResponse(**payload)


if __name__ == "__main__":
    unittest.main()
