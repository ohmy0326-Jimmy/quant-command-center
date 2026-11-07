"""缺 Delta 不得當成 0，停損空白才套商品預設。不連網、不寫試算表。"""

import json
import math
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from alert_engine import Record, rule_leverage, rule_stop_loss
from portfolio_briefing import portfolio_payload
from portfolio_risk import finalize_valuation
from stop_rules import UNRECORDED_BREACH, apply_stop_defaults, breach_age_label, explicit_stop

TAIPEI = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=TAIPEI)


def _accounts():
    return pd.DataFrame([{"帳戶名稱": "第一證券", "現金餘額": 100_000.0, "融資/質押借款": 0.0}])


def _stock(**extra):
    row = {
        "帳戶": "第一證券",
        "種類": "台股",
        "標的": "2330",
        "名稱": "台積電",
        "持有數量": 1000,
        "平均成本": 100,
        "投入總金額": 100_000,
        "標的槓桿": 1,
        "即時報價": 90,
        "Beta值": 1.1,
        "產業別": "先進製程",
    }
    row.update(extra)
    return row


def _option(**extra):
    row = {
        "帳戶": "第一證券",
        "種類": "台股選擇權",
        "標的": "TXO2411",
        "名稱": "台指買權",
        "持有數量": 2,
        "平均成本": 100,
        "投入總金額": 0,
        "最大虧損比例": -100,
        "標的槓桿": 1,
        "即時報價": 80,
        "標的現價": 20000,
        "Beta值": 1,
        "產業別": "臺指期貨",
    }
    row.update(extra)
    return row


class MissingDeltaTests(unittest.TestCase):
    def test_option_zero_delta_is_known_zero_and_missing_is_not(self):
        zero, _a, zero_metrics = finalize_valuation(pd.DataFrame([_option(Delta=0)]), _accounts(), 32.0)
        missing, _b, missing_metrics = finalize_valuation(pd.DataFrame([_option()]), _accounts(), 32.0)
        self.assertEqual(float(zero.iloc[0]["實質曝險(台幣)"]), 0.0)
        self.assertEqual(zero.iloc[0]["Delta狀態"], "known")
        self.assertTrue(bool(zero.iloc[0]["曝險已知"]))
        self.assertTrue(zero_metrics["exposure_complete"])
        self.assertEqual(zero_metrics["exposure"], 0.0)

        self.assertTrue(math.isnan(float(missing.iloc[0]["實質曝險(台幣)"])))
        self.assertEqual(missing.iloc[0]["Delta狀態"], "missing")
        self.assertFalse(bool(missing.iloc[0]["曝險已知"]))
        self.assertFalse(missing_metrics["exposure_complete"])
        self.assertEqual(missing_metrics["exposure_scope"], "partial")
        self.assertEqual(missing_metrics["unknown_delta_symbols"], ["TXO2411"])
        self.assertGreater(missing_metrics["nav"], 0)

    def test_mixed_book_keeps_known_stock_and_labels_partial(self):
        _valued, _accounts_out, metrics = finalize_valuation(
            pd.DataFrame([_stock(最大虧損比例=-8), _option()]),
            _accounts(),
            32.0,
        )
        self.assertAlmostEqual(metrics["exposure"], 90_000.0, places=4)
        self.assertFalse(metrics["exposure_complete"])
        payload = portfolio_payload(_valued, metrics, usdtwd=32.0, as_of=NOW)
        self.assertFalse(payload["exposure_complete"])
        self.assertEqual(payload["exposure_scope"], "partial")
        self.assertIn("TXO2411", payload["unknown_delta_symbols"])
        self.assertTrue(any("不是 0" in line for line in payload["warnings"]))
        by_symbol = {row["symbol"]: row for row in payload["positions"]}
        self.assertTrue(by_symbol["2330"]["exposure_known"])
        self.assertEqual(by_symbol["2330"]["delta_status"], "not_applicable")
        self.assertIsNone(by_symbol["TXO2411"]["notional_exposure"])
        self.assertEqual(by_symbol["TXO2411"]["delta_status"], "missing")
        self.assertAlmostEqual(payload["exposure"], 90_000.0, places=4)


class StopDefaultTests(unittest.TestCase):
    def test_explicit_sheet_value_beats_product_default(self):
        frame = apply_stop_defaults(pd.DataFrame([
            _stock(最大虧損比例=-8),
            _stock(標的="SOXL", 種類="美股", 標的槓桿=3, 最大虧損比例=-12),
            _stock(標的="00663L", 標的槓桿=2, 最大虧損比例=""),
            _stock(標的="TQQQ", 種類="美股", 標的槓桿=1, 最大虧損比例=float("nan")),
            _option(),
            _stock(標的="13164", 種類="可轉債", 最大虧損比例=-100),
            _stock(標的="TX", 種類="台股期貨", 最大虧損比例=None),
        ]))
        by_ticker = {row["標的"]: row for _, row in frame.iterrows()}
        self.assertEqual(by_ticker["2330"]["最大虧損比例"], -8)
        self.assertEqual(by_ticker["2330"]["停損來源"], "試算表")
        self.assertEqual(by_ticker["SOXL"]["最大虧損比例"], -12)
        self.assertEqual(by_ticker["SOXL"]["停損來源"], "試算表")
        self.assertEqual(by_ticker["00663L"]["最大虧損比例"], -15)
        self.assertIn("2 倍", by_ticker["00663L"]["停損來源"])
        self.assertEqual(by_ticker["TQQQ"]["最大虧損比例"], -20)
        self.assertIn("3 倍", by_ticker["TQQQ"]["停損來源"])
        self.assertEqual(by_ticker["TXO2411"]["最大虧損比例"], -100)
        self.assertEqual(by_ticker["TXO2411"]["停損來源"], "未設定")
        self.assertEqual(by_ticker["13164"]["停損來源"], "未設定")
        self.assertEqual(by_ticker["TX"]["停損來源"], "未設定")

    def test_invalid_values_do_not_become_a_stop_for_derivatives(self):
        self.assertIsNone(explicit_stop("不是數字"))
        self.assertIsNone(explicit_stop(float("nan")))
        self.assertIsNone(explicit_stop(5))
        self.assertIsNone(explicit_stop(-100))
        self.assertEqual(explicit_stop(0), 0.0)
        self.assertEqual(explicit_stop(" -8% "), -8.0)
        frame = apply_stop_defaults(pd.DataFrame([_option(最大虧損比例="壞掉")]))
        self.assertEqual(frame.iloc[0]["停損來源"], "未設定")
        equity = apply_stop_defaults(pd.DataFrame([_stock(最大虧損比例="壞掉")]))
        self.assertEqual(equity.iloc[0]["最大虧損比例"], -10)
        self.assertIn("個股", equity.iloc[0]["停損來源"])


class BreachAgeTests(unittest.TestCase):
    def test_label_needs_a_real_date(self):
        today = date(2026, 11, 7)
        self.assertEqual(breach_age_label(None, today), UNRECORDED_BREACH)
        self.assertEqual(breach_age_label("", today), UNRECORDED_BREACH)
        self.assertEqual(breach_age_label("2026-11-08", today), UNRECORDED_BREACH)
        self.assertEqual(breach_age_label("2026-11-07", today), "今天首次觀察到，尚未跨日")
        self.assertEqual(breach_age_label("2026-11-04", today), "已觀察 3 天（自 2026-11-04）")

    def test_old_alert_state_does_not_invent_a_start_date(self):
        previous = Record(state=json.dumps(["A|2330"]), value="1")
        held = {"ticker": "2330", "name": "台積電", "account": "A", "roi": -11, "max_loss_pct": -10}
        fresh = {"ticker": "2317", "name": "鴻海", "account": "A", "roi": -12, "max_loss_pct": -10}
        state, value, alert = rule_stop_loss({"positions": [held, fresh]}, previous, NOW)
        self.assertEqual(json.loads(state), ["A|2317", "A|2330"])
        dates = json.loads(value)
        self.assertNotIn("A|2330", dates)
        self.assertEqual(dates["A|2317"], "2026-11-07")
        self.assertIn("今天首次觀察到", alert.body)
        self.assertNotIn("2330", alert.body)

    def test_incomplete_leverage_is_not_reported_as_safe(self):
        state, _value, alert = rule_leverage(
            {"value": 1.1, "exposure_complete": False, "unknown_delta_symbols": ["TXO2411"]},
            None,
            NOW,
        )
        self.assertEqual(state, "incomplete")
        self.assertIn("不完整", alert.title)
        again, _v, quiet = rule_leverage(
            {"value": 1.1, "exposure_complete": False},
            Record(state="incomplete"),
            NOW,
        )
        self.assertEqual(again, "incomplete")
        self.assertIsNone(quiet)


if __name__ == "__main__":
    unittest.main()
