import unittest
from datetime import date
import pandas as pd
from order_assistant import Snapshot, bs_price_greeks
from order_calculators import (
    years_to_expiry,
    iv_signal,
    leaps_call_metrics,
    normalize_futures_product,
    default_margin_per_lot,
    futures_trial,
    futures_order_impact
)
from portfolio_risk import FUTURES_MARGIN

class TestOrderCalculators(unittest.TestCase):
    def test_leaps_call_metrics(self):
        today = date(2026, 11, 3)
        expiry = date(2028, 1, 21)
        t = years_to_expiry(expiry, today)
        spot = 100.0
        strike = 80.0
        rate = 0.04
        sigma = 0.35
        
        premium, delta, _ = bs_price_greeks(spot, strike, t, rate, sigma, "call")
        
        res = leaps_call_metrics(
            spot=spot, strike=strike, expiry=expiry, premium=premium,
            rate=rate, today=today, contracts=2, target_exposure=50000,
            ref_vol=0.30, contract_size=100
        )
        
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["iv"], 0.35, places=3)
        self.assertTrue(0.7 < res["delta"] < 1.0)
        self.assertAlmostEqual(res["intrinsic"], 20.0)
        self.assertAlmostEqual(res["extrinsic"], premium - 20.0)
        self.assertAlmostEqual(res["equivalent_shares"], res["delta"] * 100 * 2)
        
        import math
        expected_suggested = math.ceil(50000 / (res["delta"] * 100 * 100))
        self.assertEqual(res["suggested_contracts"], expected_suggested)
        
    def test_leaps_premium_below_intrinsic(self):
        today = date(2026, 11, 3)
        expiry = date(2028, 1, 21)
        res = leaps_call_metrics(
            spot=100.0, strike=80.0, expiry=expiry, premium=15.0, today=today
        )
        self.assertFalse(res["ok"])
        self.assertIn("無法回推隱含波動率", res["error"])
        self.assertEqual(res["intrinsic"], 20.0)
        
    def test_leaps_short_dated_warning(self):
        today = date(2026, 1, 1)
        expiry = date(2026, 3, 1)
        t = years_to_expiry(expiry, today)
        premium, _, _ = bs_price_greeks(100, 100, t, 0.04, 0.3, "call")
        res = leaps_call_metrics(
            spot=100.0, strike=100.0, expiry=expiry, premium=premium, today=today
        )
        self.assertTrue(any("到期不到 9 個月" in w for w in res["warnings"]))
        
    def test_iv_signal(self):
        # Absolute
        res1 = iv_signal(0.25)
        self.assertEqual(res1["color"], "green")
        res2 = iv_signal(0.45)
        self.assertEqual(res2["color"], "yellow")
        res3 = iv_signal(0.65)
        self.assertEqual(res3["color"], "red")
        
        # Relative
        res4 = iv_signal(0.45, 0.40)
        self.assertEqual(res4["color"], "yellow")
        res5 = iv_signal(0.30, 0.40)
        self.assertEqual(res5["color"], "green")
        res6 = iv_signal(0.50, 0.40)
        self.assertEqual(res6["color"], "red")
        
        # None
        res7 = iv_signal(None)
        self.assertEqual(res7["color"], "none")
        self.assertEqual(res7["label"], "無法回推 IV")

    def test_normalize_futures_product(self):
        self.assertEqual(normalize_futures_product("TMF"), "FMF")
        self.assertEqual(normalize_futures_product("小台"), "MTX")
        self.assertEqual(normalize_futures_product("大台"), "TX")
        self.assertEqual(normalize_futures_product("微台"), "FMF")
        with self.assertRaises(ValueError):
            normalize_futures_product("BAD")

    def test_futures_trial_tx(self):
        res = futures_trial("TX", 2, 23000, stop_price=22800, side="buy", max_loss=100000)
        self.assertTrue(res["ok"])
        self.assertEqual(res["multiplier"], 200)
        self.assertEqual(res["notional"], 9200000)
        self.assertEqual(res["loss_per_lot"], 40000)
        self.assertEqual(res["stop_loss_total"], 80000)
        self.assertEqual(res["margin_needed"], 2 * FUTURES_MARGIN["TX"][0])
        self.assertEqual(res["max_lots_by_loss"], 2)
        
        res2 = futures_trial("TX", 2, 23000, stop_price=22800, side="buy", max_loss=50000)
        self.assertEqual(res2["max_lots_by_loss"], 1)
        self.assertTrue(any("口數超過停損上限" in w for w in res2["warnings"]))

    def test_futures_trial_mtx_short_wrong_stop(self):
        res = futures_trial("MTX", 1, 23000, stop_price=22800, side="sell")
        self.assertTrue(any("停損價在錯的一邊" in w for w in res["warnings"]))
        self.assertIsNone(res["loss_per_lot"])

    def test_futures_trial_fmf_custom_margin(self):
        res = futures_trial("FMF", 1, 23000, margin_per_lot=40000)
        self.assertEqual(res["multiplier"], 10)
        self.assertEqual(res["margin_per_lot"], 40000)
        self.assertEqual(res["margin_needed"], 40000)

    def test_default_margin_per_lot(self):
        m1 = default_margin_per_lot("FMF", ledger_margin=35050, ledger_product="TMF")
        self.assertEqual(m1, 35050)
        
        m2 = default_margin_per_lot("FMF", ledger_margin=35050, ledger_product="TX")
        self.assertEqual(m2, FUTURES_MARGIN["FMF"][0])

    def test_futures_order_impact(self):
        positions = pd.DataFrame([
            {"帳戶": "第一證券", "種類": "台股", "標的": "2330", "名稱": "2330", "持有數量": 1000, "平均成本": 900, "投入總金額": 900000, "最大虧損比例": -100, "標的槓桿": 1.0, "即時報價": 1000, "Beta值": 1.15, "產業別": "晶圓代工"},
            {"帳戶": "元大期貨", "種類": "期貨", "標的": "TX", "名稱": "TX", "持有數量": 1, "平均成本": 23000, "投入總金額": 0.0, "最大虧損比例": -100, "標的槓桿": 1.0, "即時報價": 23000, "Beta值": 1.0, "產業別": "臺指期貨"}
        ])
        accounts = pd.DataFrame([
            {"帳戶名稱": "第一證券", "現金餘額": 1_000_000.0, "融資/質押借款": 0.0},
            {"帳戶名稱": "元大期貨", "現金餘額": 800_000.0, "融資/質押借款": 0.0}
        ])
        snap = Snapshot(positions, accounts, 32.0, 23000.0)
        
        trial = futures_trial("MTX", 1, 23000, side="buy")
        impact = futures_order_impact(snap, trial, account="元大期貨")
        
        self.assertIn("after", impact)
        self.assertGreater(impact["leverage_after"], impact["leverage_before"])
        self.assertGreater(impact["exposure_after"], impact["exposure_before"])
        self.assertIsNotNone(impact["equity_ratio_before"])

if __name__ == "__main__":
    unittest.main()
