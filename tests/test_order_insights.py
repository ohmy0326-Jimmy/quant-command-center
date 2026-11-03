import unittest
import numpy as np
import pandas as pd

from order_assistant import Rules, Snapshot
from order_insights import (
    ACCOUNT_CHOICES,
    account_kind_warnings,
    account_options,
    account_view,
    beta_split_change,
    default_account,
    futures_margin_call_level,
    hedge_suggestion,
    leveraged_beta_note,
    margin_account_call_levels,
    priority_list,
    rs_raw_score,
    rs_rating_from_raws,
    rs_symbol,
    triggered_stop_losses,
)


def _stock(
    account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, sector="其他", max_loss=-100
):
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


class TestOrderInsights(unittest.TestCase):
    def test_account_options_order(self):
        accounts = pd.DataFrame(
            [{"帳戶名稱": "自訂帳戶A"}, {"帳戶名稱": "第一證券"}, {"帳戶名稱": "自訂帳戶B"}]
        )
        opts = account_options(accounts)
        self.assertEqual(opts[: len(ACCOUNT_CHOICES)], ACCOUNT_CHOICES)
        self.assertIn("自訂帳戶A", opts)
        self.assertIn("自訂帳戶B", opts)
        self.assertEqual(opts.count("第一證券"), 1)

    def test_default_account_routes(self):
        positions = pd.DataFrame([_stock("現貨主帳戶", "台股", "2330", 1000, 500, 500)])
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0},
                {"帳戶名稱": "元大期貨", "現金餘額": 200000, "融資/質押借款": 0},
                {"帳戶名稱": "IB", "現金餘額": 50000, "融資/質押借款": 0},
            ]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        # Held account override
        self.assertEqual(default_account(snap, "台股", "口袋證券"), "口袋證券")

        # Futures / options -> 元大期貨
        self.assertEqual(default_account(snap, "期貨", None), "元大期貨")
        self.assertEqual(default_account(snap, "選擇權", None), "元大期貨")

        # US stock -> IB or 嘉信
        self.assertEqual(default_account(snap, "美股", None), "IB")

        # TW stock -> 第一證券 fallback if preference missing
        self.assertEqual(default_account(snap, "台股", None), "第一證券")

    def test_account_kind_warnings(self):
        w1 = account_kind_warnings("第一證券", "期貨")
        self.assertTrue(any("元大期貨" in msg for msg in w1))

        w2 = account_kind_warnings("元大期貨", "台股")
        self.assertTrue(any("不建議" in msg for msg in w2))

        w3 = account_kind_warnings("第一證券", "美股")
        self.assertTrue(any("複委託" in msg for msg in w3))

    def test_account_view(self):
        positions = pd.DataFrame([_stock("現貨主帳戶", "台股", "2330", 1000, 500, 500)])
        accounts = pd.DataFrame(
            [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 20000}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        v_exist = account_view(snap, "現貨主帳戶")
        self.assertTrue(v_exist["exists"])
        self.assertEqual(v_exist["cash"], 100000)
        self.assertEqual(v_exist["debt"], 20000)

        v_none = account_view(snap, "不存在帳戶")
        self.assertFalse(v_none["exists"])
        self.assertEqual(v_none["nav"], 0.0)

    def test_beta_split_change_leveraged(self):
        positions = pd.DataFrame(
            [
                _stock("現貨主帳戶", "台股", "00663L", 1000, 100, 100, lev=2.0, beta=1.9),
            ]
        )
        accounts = pd.DataFrame(
            [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        # 00663L MV = 100,000; Lev=2 -> TW exposure = 200,000
        self.assertAlmostEqual(snap.metrics["tw_exposure"], 200000.0)

        before = snap.metrics
        after = dict(before)
        after["tw_beta"] = 2.5

        splits = beta_split_change(before, after)
        tw_split = next(s for s in splits if s["label"] == "台股")
        self.assertAlmostEqual(tw_split["delta"], 2.5 - before["tw_beta"])

    def test_leveraged_beta_note(self):
        note = leveraged_beta_note(lev=2.0, beta=1.9)
        self.assertIsNotNone(note)
        self.assertIn("標的槓桿為 2x", note)

        no_note = leveraged_beta_note(lev=1.0, beta=1.0)
        self.assertIsNone(no_note)

    def test_rs_raw_score_synthetic(self):
        dates = pd.date_range("2023-01-01", periods=300, freq="B")
        prices = np.linspace(100.0, 200.0, 300)
        s = pd.Series(prices, index=dates)

        raw = rs_raw_score(s)
        self.assertIsNotNone(raw)
        self.assertGreater(raw, 0.0)

    def test_rs_rating_from_raws_extremes(self):
        universe = [float(i) for i in range(1, 13)]  # 1..12
        r_top = rs_rating_from_raws(15.0, universe)
        self.assertEqual(r_top, 99)

        r_bottom = rs_rating_from_raws(0.0, universe)
        self.assertEqual(r_bottom, 1)

        r_mid = rs_rating_from_raws(6.5, universe)
        self.assertAlmostEqual(r_mid, 50, delta=2)

    def test_rs_short_history(self):
        dates = pd.date_range("2023-01-01", periods=50, freq="B")
        s = pd.Series(np.linspace(100, 110, 50), index=dates)
        self.assertIsNone(rs_raw_score(s))

    def test_rs_symbol(self):
        self.assertEqual(rs_symbol("2330", "台股"), "2330.TW")
        self.assertEqual(rs_symbol("AAPL", "美股"), "AAPL")
        self.assertEqual(rs_symbol("TX", "期貨"), "")

    def test_futures_margin_call_level(self):
        # Long 1 TX contract: initial margin 701,000, maint margin 538,000. Price = 20,000.
        positions = pd.DataFrame(
            [
                _stock("元大期貨", "期貨", "TX", 1, 20000, 20000),
            ]
        )
        accounts = pd.DataFrame(
            [{"帳戶名稱": "元大期貨", "現金餘額": 600000, "融資/質押借款": 0}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        res = futures_margin_call_level(snap)
        self.assertIsNotNone(res)
        self.assertFalse(res["already"])
        self.assertEqual(res["direction"], "下跌")

        # No futures snapshot
        snap_no_fut = Snapshot(
            pd.DataFrame([_stock("現貨主帳戶", "台股", "2330", 1000, 500, 500)]),
            pd.DataFrame(
                [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0}]
            ),
            32.0,
            20000.0,
        )
        self.assertIsNone(futures_margin_call_level(snap_no_fut))

    def test_margin_account_call_levels(self):
        positions = pd.DataFrame([_stock("現貨主帳戶", "台股", "2330", 100, 1000, 1000)])
        accounts = pd.DataFrame(
            [{"帳戶名稱": "現貨主帳戶", "現金餘額": 0, "融資/質押借款": 85000}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        # MV = 100,000, Debt = 85,000. Ratio = 1.176 < 1.30 -> already in call
        levels = margin_account_call_levels(snap)
        self.assertEqual(len(levels), 1)
        self.assertTrue(levels[0]["already_call"])

    def test_hedge_suggestion(self):
        positions = pd.DataFrame([_stock("現貨主帳戶", "台股", "2330", 1000, 1000, 1000)])
        accounts = pd.DataFrame(
            [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        # tw_long = 1,000,000; index = 20,000.
        # lots_full = 1,000,000 / (20,000 * 50) = 1.0
        h = hedge_suggestion(snap, put_delta=-0.5, target_ratio=1.0)
        self.assertAlmostEqual(h["lots_full"], 1.0)
        self.assertAlmostEqual(h["lots_delta"], 2.0)
        self.assertEqual(h["suggested_lots"], 2)

    def test_triggered_stop_losses_and_priority_list(self):
        positions = pd.DataFrame(
            [
                _stock(
                    "現貨主帳戶",
                    "台股",
                    "2330",
                    1000,
                    1000,
                    800,
                    max_loss=-15.0,
                ),  # ROI = -20% <= -15% -> stop loss
            ]
        )
        accounts = pd.DataFrame(
            [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0}]
        )
        snap = Snapshot(positions, accounts, 32.0, 20000.0)

        stops = triggered_stop_losses(snap)
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["ticker"], "2330")

        rules = Rules()
        plist = priority_list(snap, rules)
        self.assertTrue(any(p["category"] == "觸發停損" for p in plist))


if __name__ == "__main__":
    unittest.main()
