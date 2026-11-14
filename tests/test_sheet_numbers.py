"""Sheet 貨幣格式要讀成數字。借款讀失敗時不可當成 0。"""

import math
import unittest

import pandas as pd

from portfolio_briefing import SCHEMA_VERSION, portfolio_payload
from portfolio_risk import finalize_valuation, normalize_positions, value_positions_safely
from sheet_numbers import classify_sheet_number, parse_sheet_number, read_worksheet_records


AS_OF = __import__("datetime").datetime(2026, 11, 14, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei"))


class ParseTests(unittest.TestCase):
    def test_currency_commas_parentheses_and_fullwidth(self):
        self.assertEqual(parse_sheet_number("NT$305,430"), 305430.0)
        self.assertEqual(parse_sheet_number("NT$214,006"), 214006.0)
        self.assertEqual(parse_sheet_number("$1,234.5"), 1234.5)
        self.assertEqual(parse_sheet_number("US$1,234"), 1234.0)
        self.assertEqual(parse_sheet_number("(1,234)"), -1234.0)
        self.assertEqual(parse_sheet_number("（1,234）"), -1234.0)
        self.assertEqual(parse_sheet_number("ＮＴ＄305,430"), 305430.0)
        self.assertEqual(parse_sheet_number("305,430"), 305430.0)
        self.assertEqual(parse_sheet_number(305430), 305430.0)
        self.assertIsNone(parse_sheet_number("－"))
        self.assertIsNone(parse_sheet_number(""))
        self.assertIsNone(parse_sheet_number(None))
        self.assertEqual(classify_sheet_number("－"), ("blank", None))
        self.assertEqual(classify_sheet_number("待補")[0], "invalid")
        self.assertIsNone(parse_sheet_number(True))

    def test_reads_unformatted_cell_values(self):
        seen = {}

        class Sheet:
            def get_all_records(self, value_render_option=None):
                seen["option"] = value_render_option
                return [{"現金餘額": 305430}]

        self.assertEqual(read_worksheet_records(Sheet()), [{"現金餘額": 305430}])
        self.assertEqual(seen["option"], "UNFORMATTED_VALUE")

        class OldSheet:
            def get_all_records(self):
                return [{"現金餘額": "NT$305,430"}]

        self.assertEqual(read_worksheet_records(OldSheet())[0]["現金餘額"], "NT$305,430")


def _stock(**extra):
    row = {
        "帳戶": "投資先生",
        "種類": "台股",
        "標的": "2330",
        "名稱": "台積電",
        "持有數量": 1000,
        "平均成本": 100,
        "投入總金額": 100_000,
        "標的槓桿": 1,
        "即時報價": 90,
        "Beta值": 1.0,
        "產業別": "先進製程",
    }
    row.update(extra)
    return row


class NavAmountTests(unittest.TestCase):
    def test_pledge_balance_is_deducted_from_taiwan_nav(self):
        accounts = pd.DataFrame([
            {"帳戶名稱": "投資先生", "現金餘額": "NT$0", "融資/質押借款": "NT$305,430"},
        ])
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame([_stock()]), accounts, 32.0)
        # 1000 × 90 － 305,430。這格是股票質押借款，淨值仍要扣。
        self.assertAlmostEqual(float(accounts_out.iloc[0]["融資/質押借款"]), 305430.0)
        self.assertAlmostEqual(metrics["nav"], 90_000.0 - 305_430.0)
        self.assertAlmostEqual(metrics["tw_nav"], 90_000.0 - 305_430.0)
        self.assertAlmostEqual(metrics["debt"], 305430.0)
        self.assertEqual(metrics["sheet_number_warnings"], [])

    def test_unreadable_debt_is_not_treated_as_zero(self):
        accounts = pd.DataFrame([
            {"帳戶名稱": "投資先生", "現金餘額": 0, "融資/質押借款": "NT$待補"},
            {"帳戶名稱": "口袋證券", "現金餘額": "1,424", "融資/質押借款": 0},
        ])
        positions = pd.DataFrame([
            _stock(),
            _stock(帳戶="口袋證券", 標的="2308", 名稱="台達電", 持有數量=4, 即時報價=100, 平均成本=100, 投入總金額=400),
        ])
        _valued, accounts_out, metrics = finalize_valuation(positions, accounts, 32.0)
        investor = accounts_out.loc[accounts_out["帳戶名稱"] == "投資先生"].iloc[0]
        pocket = accounts_out.loc[accounts_out["帳戶名稱"] == "口袋證券"].iloc[0]
        self.assertTrue(math.isnan(float(investor["帳戶淨值"])))
        self.assertTrue(math.isnan(float(investor["融資/質押借款"])))
        # 口袋：現金 1,424 ＋ 4×100。投資先生整戶未計入，不是只漏扣借款。
        self.assertAlmostEqual(float(pocket["帳戶淨值"]), 1424.0 + 400.0)
        self.assertAlmostEqual(metrics["nav"], 1424.0 + 400.0)
        self.assertNotEqual(metrics["nav"], 90_000.0 + 1424.0 + 400.0)
        text = " ".join(metrics["sheet_number_warnings"])
        self.assertIn("投資先生", text)
        self.assertIn("融資/質押借款", text)
        self.assertIn("未計入", text)
        self.assertIn("NT$待補", text)

        _v, _a, again, warnings = value_positions_safely(positions, accounts, 32.0)
        self.assertTrue(any("投資先生" in item for item in warnings))
        payload = portfolio_payload(_v, again, usdtwd=32.0, as_of=AS_OF)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertTrue(any("沒有把這欄當成 0" in item for item in payload["warnings"]))

        from order_assistant import _coerce_accounts

        coerced = _coerce_accounts(accounts)
        _again, twice, metrics_twice = finalize_valuation(positions, coerced, 32.0)
        self.assertTrue(math.isnan(float(twice.loc[twice["帳戶名稱"] == "投資先生", "帳戶淨值"].iloc[0])))
        self.assertAlmostEqual(metrics_twice["nav"], 1424.0 + 400.0)

    def test_quantity_with_commas_is_kept_and_garbage_is_dropped(self):
        frame = normalize_positions(pd.DataFrame([
            _stock(持有數量="1,000"),
            _stock(標的="2317", 名稱="鴻海", 持有數量="兩張"),
        ]))
        self.assertEqual(frame["標的"].tolist(), ["2330"])
        self.assertEqual(float(frame.iloc[0]["持有數量"]), 1000.0)
        self.assertTrue(any("兩張" in item for item in frame.attrs["sheet_number_warnings"]))


if __name__ == "__main__":
    unittest.main()
