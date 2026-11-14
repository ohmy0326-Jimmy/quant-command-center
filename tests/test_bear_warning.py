"""熊市預警的每一條規則。缺資料是未知，不算通過，也不改 REGIME_RISK。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from api.schemas import PortfolioResponse
from bear_warning import (
    CAPTION,
    LEADERS,
    THRESHOLDS,
    assess,
    breadth_percent,
    curve_indicator,
    defensive_indicator,
    high_beta_indicator,
    hy_widening_indicator,
    index_trend_indicator,
    leaders_indicator,
    vix_indicator,
)
from macro_cycle import REGIME_RISK
from portfolio_briefing import portfolio_payload

AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _rising(n=250, start=100.0, step=0.2):
    return [start + step * index for index in range(n)]


class RuleTests(unittest.TestCase):
    def test_regime_table_is_unchanged(self):
        self.assertEqual(
            {name: item["target_leverage"] for name, item in REGIME_RISK.items()},
            {"牛": 2.0, "偏多": 1.8, "中性": 1.5, "偏空": 1.2, "熊": 1.0},
        )

    def test_missing_inputs_are_unknown_and_not_a_pass(self):
        report = assess({"regime": "偏多"})
        self.assertEqual(report["caption"], CAPTION)
        self.assertTrue(all(layer["state"] == "unknown" for layer in report["layers"]))
        self.assertEqual(report["suggested_cap"], 1.8)
        self.assertEqual(report["suggested_action"], "預警未亮，沿用牛熊分數的槓桿上限。")
        self.assertIsNone(assess({})["suggested_cap"])
        self.assertEqual(curve_indicator("利差", "T10Y2Y", None)["state"], "unknown")
        self.assertEqual(defensive_indicator({})["state"], "unknown")
        self.assertEqual(leaders_indicator([True, None])["state"], "unknown")
        self.assertEqual(high_beta_indicator(None, -0.01)["state"], "unknown")
        self.assertEqual(index_trend_indicator("指數", [1, 2, 3], "測試")["state"], "unknown")
        self.assertEqual(hy_widening_indicator(None)["state"], "unknown")
        self.assertEqual(vix_indicator([20.0] * 3)["state"], "unknown")

    def test_each_trigger(self):
        self.assertEqual(curve_indicator("利差", "T10Y2Y", -0.2)["state"], "red")
        self.assertEqual(curve_indicator("利差", "T10Y2Y", 0.1)["state"], "yellow")
        self.assertEqual(curve_indicator("利差", "T10Y2Y", 1.0)["state"], "green")
        self.assertEqual(hy_widening_indicator(100)["state"], "red")
        self.assertEqual(hy_widening_indicator(50)["state"], "yellow")
        self.assertEqual(hy_widening_indicator(0)["state"], "green")
        self.assertEqual(vix_indicator([30.0] * 10)["state"], "red")
        self.assertEqual(vix_indicator([10.0] * 9 + [26.0])["state"], "yellow")
        self.assertEqual(vix_indicator([12.0] * 10)["state"], "green")
        self.assertEqual(leaders_indicator([True, True, True, False])["state"], "red")
        self.assertEqual(leaders_indicator([True, True, False, False, False])["state"], "yellow")
        self.assertEqual(leaders_indicator([False, False, False, False])["state"], "green")
        self.assertEqual(high_beta_indicator(-0.05, 0.0)["state"], "red")
        self.assertEqual(high_beta_indicator(-0.01, 0.0)["state"], "yellow")
        self.assertEqual(high_beta_indicator(0.02, 0.0)["state"], "green")
        falling = [200.0] * 220 + [100.0]
        self.assertEqual(index_trend_indicator("指數", falling, "測試")["state"], "red")
        rising = _rising(250)
        self.assertEqual(index_trend_indicator("指數", rising, "測試")["state"], "green")

    def test_breadth_divergence_and_proxy_label(self):
        closes = {"A": _rising(200, 10, 0.1), "B": [5.0] * 200, "C": [1.0] * 10}
        percent, counted = breadth_percent(closes)
        self.assertEqual(counted, 2)
        self.assertAlmostEqual(percent, 50.0)
        report = assess({
            "regime": "牛",
            "breadth_us": 40.0,
            "breadth_us_prior": 60.0,
            "spx_last": 99.0,
            "spx_high": 100.0,
            "breadth_tw": 70.0,
            "breadth_tw_prior": 70.0,
            "twii_last": 100.0,
            "twii_high": 100.0,
        })
        us = report["breadth"]["us"]
        tw = report["breadth"]["tw"]
        self.assertEqual(us["state"], "red")
        self.assertEqual(tw["state"], "green")
        self.assertIn("0050", report["breadth"]["tw_universe"])
        self.assertIn("標普 500", report["breadth"]["us_universe"])
        self.assertEqual(THRESHOLDS["near_high_pct"], 2.0)

    def test_defensive_rotation(self):
        def series(base, weekly):
            values = [100.0]
            for change in weekly:
                for _ in range(5):
                    values.append(values[-1] * (1 + change / 5))
            return values
        prices = {
            "SPY": series(100, [0.0, 0.0]),
            "XLV": series(100, [0.02, 0.02]),
            "XLP": series(100, [0.02, 0.02]),
            "XLU": series(100, [0.02, 0.02]),
            "XLK": series(100, [-0.02, -0.02]),
        }
        self.assertEqual(defensive_indicator(prices)["state"], "red")

    def test_caps_take_the_more_conservative(self):
        quiet = assess({"regime": "偏多", "t10y2y": 1.0, "t10y3m": 1.0})
        self.assertEqual(quiet["layers"][0]["state"] in {"yellow", "red", "unknown", "green"}, True)
        leading = assess({
            "regime": "偏多",
            "credit": {"drtscilm_light": "red", "hy_light": "green", "hy_change_bp": 0},
            "t10y2y": 1.0,
            "t10y3m": 1.0,
            "prices": {"SPY": _rising(20), "XLV": _rising(20), "XLP": _rising(20), "XLU": _rising(20), "XLK": _rising(20)},
        })
        self.assertEqual(leading["layers"][0]["state"], "red")
        self.assertEqual(leading["warning_cap"], 1.5)
        self.assertEqual(leading["suggested_cap"], 1.5)
        both = assess({
            "regime": "偏多",
            "credit": {"drtscilm_light": "red", "hy_light": "green", "hy_change_bp": 0},
            "t10y2y": 1.0,
            "t10y3m": 1.0,
            "prices": {"SPY": _rising(40), "XLV": _rising(40), "XLP": _rising(40), "XLU": _rising(40), "XLK": _rising(40)},
            "breadth_us": 30,
            "breadth_us_prior": 50,
            "spx_last": 80,
            "spx_high": 100,
            "breadth_tw": 40,
            "breadth_tw_prior": 40,
            "twii_last": 80,
            "twii_high": 100,
            "leader_below": [False, False, False, False],
            "sphb_return": 0.01,
            "splv_return": 0.0,
        })
        self.assertEqual(both["suggested_cap"], 1.2)
        self.assertIn("加避險", both["suggested_action"])
        confirmed = assess({"regime": "牛", "credit": {"hy_change_bp": 120}})
        self.assertEqual(confirmed["layers"][2]["state"], "red")
        self.assertEqual(confirmed["suggested_cap"], 1.0)
        self.assertIn("只留核心與避險", confirmed["suggested_action"])
        self.assertEqual(LEADERS[0], "AAPL")

    def test_api_object(self):
        payload = portfolio_payload(
            pd.DataFrame(),
            {"nav": 3_140_000, "exposure": 6_500_000, "leverage": 2.07, "cash_pct": 10, "beta": 1, "exposure_complete": True},
            usdtwd=32.0,
            as_of=AS_OF,
            regime="偏多",
        )
        self.assertEqual(payload["schema_version"], "1.12.0")
        self.assertEqual(payload["bear_warning"]["caption"], CAPTION)
        self.assertEqual(payload["bear_warning"]["suggested_cap"], 1.8)
        PortfolioResponse(**payload)


if __name__ == "__main__":
    unittest.main()
