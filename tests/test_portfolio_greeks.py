"""全組合希臘值與目標 Beta。台美分開，缺值不是 0。"""

import unittest

from portfolio_greeks import aggregate_greeks, beta_targets, hedge_contracts, market_of, position_greeks


class GreekTests(unittest.TestCase):
    def test_markets_stay_separate_and_points_use_the_index(self):
        rows = [
            {"種類": "期貨", "標的": "TX", "持有數量": 1, "即時報價": 20000},
            {"種類": "美股", "標的": "SPY", "持有數量": 10, "即時報價": 500, "標的槓桿": 1, "Theta": 0},
        ]
        result = aggregate_greeks(rows, tw_index=20000, us_index=5000)
        self.assertNotIn("total", result)
        self.assertAlmostEqual(result["tw"]["delta_notional"], 4_000_000)
        self.assertAlmostEqual(result["tw"]["delta_points"], 200)
        self.assertEqual(result["tw"]["currency"], "TWD")
        self.assertAlmostEqual(result["us"]["delta_notional"], 5000)
        self.assertAlmostEqual(result["us"]["delta_points"], 1)
        self.assertEqual(result["us"]["currency"], "USD")
        self.assertTrue(result["tw"]["delta_complete"])
        self.assertEqual(result["tw"]["theta_per_day"], 0)

    def test_missing_option_delta_is_null_not_zero(self):
        rows = [
            {"種類": "期貨", "標的": "TX", "持有數量": 1, "即時報價": 20000},
            {"種類": "選擇權", "標的": "TXO202611C20000", "持有數量": 2, "即時報價": 100},
        ]
        result = aggregate_greeks(rows, tw_index=20000)
        self.assertFalse(result["tw"]["delta_complete"])
        self.assertIsNone(result["tw"]["delta_points"])
        self.assertIsNone(result["tw"]["theta_per_day"])
        self.assertAlmostEqual(result["tw"]["delta_notional"], 4_000_000)
        self.assertIn("TXO202611C20000", result["tw"]["missing"])
        self.assertNotEqual(result["tw"]["delta_notional"], 0)

    def test_zero_delta_is_kept(self):
        row = position_greeks({
            "種類": "選擇權",
            "標的": "TXO",
            "持有數量": 3,
            "Delta": 0,
            "標的現價": 20000,
            "Theta": -1,
        })
        self.assertEqual(row["notional"], 0)
        self.assertEqual(row["market"], "TW")
        result = aggregate_greeks([row], tw_index=20000)
        self.assertTrue(result["tw"]["delta_complete"])
        self.assertEqual(result["tw"]["delta_notional"], 0)
        self.assertAlmostEqual(result["tw"]["theta_per_day"], -150)

    def test_us_option_does_not_enter_taiwan(self):
        self.assertEqual(market_of("美股選擇權", "SPY241220P00500000"), "US")
        self.assertEqual(market_of("期貨", "2330期"), "TW")
        result = aggregate_greeks([
            {"種類": "美股選擇權", "標的": "SPY241220P00500000", "持有數量": 1, "Delta": -0.4, "標的現價": 500, "Theta": -0.2},
        ], us_index=5000)
        self.assertEqual(result["tw"]["delta_notional"], 0)
        self.assertAlmostEqual(result["us"]["delta_notional"], -0.4 * 1 * 100 * 500)
        self.assertTrue(result["us"]["missing"] == [])


class HedgeContractTests(unittest.TestCase):
    def test_full_and_half_lots_round_half_up(self):
        plan = hedge_contracts(250, True)
        self.assertTrue(plan["available"])
        by_name = {row["name"]: row for row in plan["rows"]}
        self.assertAlmostEqual(by_name["大台"]["full"], 250 / 200)
        self.assertEqual(by_name["大台"]["full_lots"], 1)
        self.assertAlmostEqual(by_name["大台"]["half"], 0.625)
        self.assertEqual(by_name["大台"]["half_lots"], 1)
        self.assertEqual(by_name["小台"]["point_value"], 50)
        self.assertEqual(by_name["小台"]["full_lots"], 5)
        self.assertEqual(by_name["微台"]["point_value"], 10)
        self.assertEqual(by_name["微台"]["full_lots"], 25)
        self.assertEqual(by_name["微台"]["half_lots"], 13)
        self.assertIn("選擇權", plan["note"])

    def test_incomplete_or_non_positive_hides_lots(self):
        missing = hedge_contracts(None, False)
        self.assertFalse(missing["available"])
        self.assertEqual(missing["rows"], [])
        self.assertIn("不完整", missing["reason"])
        flat = hedge_contracts(0, True)
        self.assertFalse(flat["available"])
        self.assertIn("不大於 0", flat["reason"])
        short = hedge_contracts(-40, True)
        self.assertFalse(short["available"])
        self.assertEqual(short["rows"], [])

    def test_existing_short_is_already_inside_delta_points(self):
        rows = [
            {"種類": "台股", "標的": "2330", "持有數量": 40000, "即時報價": 200, "標的槓桿": 1},
            {"種類": "期貨", "標的": "MTX", "持有數量": -1, "即時報價": 20000},
        ]
        result = aggregate_greeks(rows, tw_index=20000)
        # 現股 8,000,000／20000＝400 點，小台空 1 口＝-50 點，淨 350。不再把空單扣第二次。
        self.assertAlmostEqual(result["tw"]["delta_points"], 350)
        hedge = result["tw"]["hedge_contracts"]
        self.assertTrue(hedge["available"])
        tx = hedge["rows"][0]
        self.assertAlmostEqual(tx["full"], 350 / 200)
        self.assertNotAlmostEqual(tx["full"], 400 / 200)


class BetaTargetTests(unittest.TestCase):
    def test_regime_band_and_same_market_hedge(self):
        targets = beta_targets("牛", 1.5, 0.5, 1_000_000, 2_000_000, tw_index=20000, us_index=5000, usdtwd=32)
        self.assertTrue(targets["default"])
        self.assertEqual(targets["tw"]["low"], 1.10)
        self.assertEqual(targets["tw"]["high"], 1.40)
        self.assertAlmostEqual(targets["tw"]["gap"], 0.10)
        self.assertAlmostEqual(targets["tw"]["hedge_notional"], 100_000)
        self.assertAlmostEqual(targets["tw"]["tx_lots"], 100_000 / (20000 * 200))
        self.assertIn("TXO", targets["tw"]["instruments"])
        self.assertIsNone(targets["us"]["tx_lots"])
        self.assertAlmostEqual(targets["us"]["gap"], 0.5 - 1.10)
        self.assertAlmostEqual(targets["us"]["hedge_notional"], (0.5 - 1.10) * 2_000_000 / 32)

    def test_unknown_regime_has_no_target(self):
        targets = beta_targets(None, 1.2, 1.2, 100, 100)
        self.assertIsNone(targets["regime"])
        self.assertIsNone(targets["tw"])
        self.assertIsNone(targets["us"])
        targets = beta_targets("不確定", 1.2, 1.2, 100, 100)
        self.assertIsNone(targets["tw"])

    def test_incomplete_beta_does_not_invent_a_hedge(self):
        targets = beta_targets("中性", 1.2, 1.2, 1_000_000, 1_000_000, beta_complete=False)
        self.assertIsNone(targets["tw"]["gap"])
        self.assertIsNone(targets["tw"]["hedge_notional"])
        self.assertFalse(targets["tw"]["complete"])


if __name__ == "__main__":
    unittest.main()
