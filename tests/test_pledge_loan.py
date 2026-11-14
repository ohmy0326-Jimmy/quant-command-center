"""質押借款從淨值扣除，維持率用質押品，不套融資整戶 130%。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from api.schemas import PortfolioResponse
from order_insights import priority_list
from pledge_loan import PLEDGE_THRESHOLDS, pledge_light, pledge_view
from portfolio_briefing import SCHEMA_VERSION, portfolio_payload
from portfolio_risk import finalize_valuation

TAIPEI = ZoneInfo("Asia/Taipei")
AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=TAIPEI)


def _stock(account="投資先生", ticker="2330", qty=1000, price=90.0, **extra):
    row = {
        "帳戶": account,
        "種類": "台股",
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": price,
        "投入總金額": qty * price,
        "標的槓桿": 1,
        "即時報價": price,
        "Beta值": 1.0,
        "產業別": "其他",
    }
    row.update(extra)
    return row


class PledgeNavTests(unittest.TestCase):
    def test_pledge_is_deducted_and_there_is_no_margin_loan(self):
        accounts = pd.DataFrame([{"帳戶名稱": "投資先生", "現金餘額": 0, "融資/質押借款": 305_430}])
        _valued, _accounts, metrics = finalize_valuation(pd.DataFrame([_stock()]), accounts, 32.0)
        self.assertAlmostEqual(metrics["nav"], 90_000 - 305_430)
        self.assertAlmostEqual(metrics["tw_nav"], 90_000 - 305_430)
        self.assertAlmostEqual(metrics["debt"], 305_430)
        self.assertAlmostEqual(metrics["pledge_loan"], 305_430)
        self.assertEqual(metrics["margin_financing"]["label"], "無融資")
        self.assertEqual(metrics["margin_financing"]["status"], "none")
        self.assertEqual(metrics["pledge"]["light"], "red")
        self.assertTrue(metrics["pledge"]["estimated"])
        self.assertAlmostEqual(metrics["pledge"]["maintenance_pct"], 90_000 / 305_430 * 100.0)

    def test_real_margin_loan_is_separate_and_also_deducted(self):
        accounts = pd.DataFrame([
            {"帳戶名稱": "投資先生", "現金餘額": 0, "融資/質押借款": 305_430, "融資": 1_000},
        ])
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame([_stock()]), accounts, 32.0)
        self.assertAlmostEqual(metrics["nav"], 90_000 - 305_430 - 1_000)
        self.assertAlmostEqual(float(accounts_out.iloc[0]["融資/質押借款"]), 305_430)
        self.assertEqual(metrics["margin_financing"]["status"], "active")
        self.assertAlmostEqual(metrics["margin_financing"]["balance"], 1_000)

    def test_lights_and_drop_room(self):
        self.assertEqual(pledge_light(129.9), "red")
        self.assertEqual(pledge_light(130.0), "yellow")
        self.assertEqual(pledge_light(160.0), "yellow")
        self.assertEqual(pledge_light(160.1), "green")
        self.assertEqual(PLEDGE_THRESHOLDS["call_below_pct"], 130.0)
        green = pledge_view(
            pd.DataFrame([{"帳戶名稱": "投資先生", "融資/質押借款": 50_000}]),
            pd.DataFrame([_stock(qty=100, price=1000)]),
        )
        self.assertAlmostEqual(green["maintenance_pct"], 200.0)
        self.assertEqual(green["light"], "green")
        self.assertAlmostEqual(green["drop_to_call_pct"], 35.0)

    def test_marked_collateral_is_not_an_estimate(self):
        positions = pd.DataFrame([
            _stock(ticker="2330", qty=100, price=1000, 質押="是"),
            _stock(ticker="2317", qty=100, price=1000, 質押=""),
        ])
        view = pledge_view(
            pd.DataFrame([{"帳戶名稱": "投資先生", "融資/質押借款": 50_000}]),
            positions,
        )
        self.assertEqual(view["collateral_basis"], "sheet")
        self.assertFalse(view["estimated"])
        self.assertAlmostEqual(view["collateral_value"], 100_000)
        self.assertEqual(view["accounts"][0]["collateral_basis"], "sheet")

    def test_priority_uses_existing_call_category(self):
        from order_assistant import Rules, Snapshot

        snap = Snapshot(
            pd.DataFrame([_stock(qty=100, price=1000)]),
            pd.DataFrame([{"帳戶名稱": "投資先生", "現金餘額": 0, "融資/質押借款": 85_000}]),
            32.0,
            20000.0,
        )
        items = [item for item in priority_list(snap, Rules()) if item["ticker"] == "質押借款"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "追繳風險")
        self.assertNotIn("融資追繳", items[0]["detail"])
        self.assertIn("估算", items[0]["detail"])

    def test_api_schema(self):
        _valued, _accounts, metrics = finalize_valuation(
            pd.DataFrame([_stock(qty=100, price=1000)]),
            pd.DataFrame([{"帳戶名稱": "投資先生", "現金餘額": 0, "融資/質押借款": 50_000}]),
            32.0,
        )
        payload = portfolio_payload(_valued, metrics, usdtwd=32.0, as_of=AS_OF)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, "1.11.0")
        self.assertEqual(payload["margin_financing"]["label"], "無融資")
        self.assertAlmostEqual(payload["pledge"]["maintenance_pct"], 200.0)
        self.assertAlmostEqual(payload["pledge"]["drop_to_call_pct"], 35.0)
        PortfolioResponse(**payload)


if __name__ == "__main__":
    unittest.main()
