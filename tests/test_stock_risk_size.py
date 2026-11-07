"""現股風險預算股數。不連網、不改期貨口數算法。"""

import math
import unittest

from order_calculators import (
    STOCK_RISK_BUDGET_CAP_PCT,
    STOCK_SLIPPAGE_NOTE,
    futures_trial,
    reference_stop_price,
    stock_risk_quantity,
)


class StockRiskQuantityTests(unittest.TestCase):
    def test_taiwan_shares_round_down_and_are_not_lots(self):
        # 淨值 1,000,000 的 2% ＝ 20,000；每股停損 30 → 666.666… 股，捨去 666。
        result = stock_risk_quantity(
            nav_twd=1_000_000,
            entry_price=100,
            stop_price=70,
            kind="台股",
            budget_pct=2,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["shares"], 666)
        self.assertEqual(result["unit"], "股")
        self.assertAlmostEqual(result["loss_per_share_twd"], 30)
        self.assertAlmostEqual(result["risk_budget_twd"], 20_000)
        text = "\n".join(result["lines"])
        self.assertIn("不是口，也不是張", text)
        self.assertIn(STOCK_SLIPPAGE_NOTE, text)
        self.assertNotIn("買得起", text)
        self.assertNotIn("可負擔", text)
        self.assertIn("沒有拿來跟帳戶現金比較", text)

    def test_us_stock_converts_with_fx_and_shows_fallback(self):
        # 價差 2 美元 × 32 ＝ 64 台幣。預算 10,000 ÷ 64 ＝ 156.25 → 156 股。
        warning = "美元／台幣即時匯率抓不到，暫用預設值 32.00，美股市值與淨值可能失真。"
        result = stock_risk_quantity(
            nav_twd=500_000,
            entry_price=100,
            stop_price=98,
            kind="美股",
            budget_pct=2,
            fx_rate=32,
            fx_source="fallback",
            fx_warning=warning,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["shares"], 156)
        self.assertAlmostEqual(result["loss_per_share_twd"], 64)
        text = "\n".join(result["lines"])
        self.assertIn("US$ 2.0000 × 匯率 32.0000", text)
        self.assertIn("預設值", text)
        self.assertIn(warning, text)
        self.assertIn("不拆碎股", text)

    def test_live_fx_has_no_fallback_warning(self):
        result = stock_risk_quantity(
            nav_twd=320_000,
            entry_price=10,
            stop_price=9,
            kind="美股",
            fx_rate=32,
            fx_source="live",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["shares"], math.floor(6_400 / 32))
        text = "\n".join(result["lines"])
        self.assertIn("匯率來源：即時", text)
        self.assertNotIn("不是即時匯率", text)

    def test_missing_fx_does_not_invent_a_quantity(self):
        for rate in (None, 0, float("nan")):
            result = stock_risk_quantity(
                nav_twd=1_000_000,
                entry_price=50,
                stop_price=40,
                kind="美股",
                fx_rate=rate,
                fx_source="unknown",
            )
            self.assertFalse(result["ok"])
            self.assertIsNone(result["shares"])
            self.assertIn("匯率", result["reason"])

    def test_invalid_nav_price_and_stop_stay_unavailable(self):
        cases = [
            dict(nav_twd=0, entry_price=100, stop_price=90),
            dict(nav_twd=-1, entry_price=100, stop_price=90),
            dict(nav_twd=1_000_000, entry_price=0, stop_price=90),
            dict(nav_twd=1_000_000, entry_price=100, stop_price=100),
            dict(nav_twd=1_000_000, entry_price=100, stop_price=120),
            dict(nav_twd=1_000_000, entry_price=100, stop_price=0),
            dict(nav_twd=float("nan"), entry_price=100, stop_price=90),
        ]
        for case in cases:
            result = stock_risk_quantity(kind="台股", **case)
            self.assertFalse(result["ok"], case)
            self.assertIsNone(result["shares"])

    def test_budget_above_two_percent_is_capped_loudly(self):
        capped = stock_risk_quantity(
            nav_twd=1_000_000,
            entry_price=100,
            stop_price=90,
            kind="台股",
            budget_pct=5,
        )
        at_cap = stock_risk_quantity(
            nav_twd=1_000_000,
            entry_price=100,
            stop_price=90,
            kind="台股",
            budget_pct=STOCK_RISK_BUDGET_CAP_PCT,
        )
        self.assertTrue(capped["ok"])
        self.assertTrue(capped["budget_capped"])
        self.assertEqual(capped["budget_pct_used"], 2.0)
        self.assertEqual(capped["shares"], at_cap["shares"])
        self.assertIn("沒有放大預算", "\n".join(capped["lines"]))
        lower = stock_risk_quantity(
            nav_twd=1_000_000,
            entry_price=100,
            stop_price=90,
            kind="台股",
            budget_pct=1,
        )
        self.assertFalse(lower["budget_capped"])
        self.assertEqual(lower["shares"], math.floor(10_000 / 10))
        self.assertLess(lower["shares"], at_cap["shares"])

    def test_one_share_wider_than_budget_rounds_to_zero(self):
        result = stock_risk_quantity(nav_twd=1_000, entry_price=100, stop_price=50, kind="台股", budget_pct=1)
        # 預算 10 元，每股停損 50。
        self.assertTrue(result["ok"])
        self.assertEqual(result["shares"], 0)
        self.assertIn("0 股", "\n".join(result["lines"]))

    def test_futures_wording_is_separate_and_lot_math_unchanged(self):
        stock = stock_risk_quantity(nav_twd=1_000_000, entry_price=100, stop_price=90, kind="台股期貨")
        self.assertFalse(stock["ok"])
        self.assertIsNone(stock["shares"])
        self.assertIn("期貨口數", stock["reason"])
        trial = futures_trial("TX", 2, 20000, stop_price=19900, side="buy", max_loss=20000)
        self.assertEqual(trial["max_lots_by_loss"], math.floor(20000 / (100 * 200)))
        self.assertEqual(trial["lots"], 2)

    def test_reference_stop_prefers_sheet_over_equity_default(self):
        explicit = reference_stop_price(100, "台股", "2330", 1, -8)
        self.assertEqual(explicit["source"], "試算表")
        self.assertAlmostEqual(explicit["price"], 92)
        defaulted = reference_stop_price(100, "美股", "NVDA", 1, -100)
        self.assertIn("個股", defaulted["source"])
        self.assertAlmostEqual(defaulted["price"], 90)
        triple = reference_stop_price(40, "美股", "SOXL", 1, None)
        self.assertIn("3 倍", triple["source"])
        self.assertAlmostEqual(triple["price"], 32)
        future = reference_stop_price(20000, "期貨", "TX", 1, None)
        self.assertIsNone(future["price"])
        self.assertEqual(future["source"], "未設定")


if __name__ == "__main__":
    unittest.main()
