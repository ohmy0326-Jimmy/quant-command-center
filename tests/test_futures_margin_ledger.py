"""期貨下單試算：每口原始保證金從 Futures_Ledger 推，權益比率用本日餘額＋Futures_Ledger 保證金。"""

import unittest
from datetime import date

import pandas as pd

from futures_ledger import (
    apply_yuanta_ledger,
    derive_margin_per_lot,
    ledger_metrics_from_accounts,
    quote_yuanta_ledger,
    template_ledger_frame,
)
from order_assistant import Snapshot
from order_calculators import (
    MARGIN_SOURCE_DEFAULT,
    MARGIN_SOURCE_LEDGER,
    futures_order_impact,
    futures_trial,
    initial_margin_with_ledger,
    ledger_margin_per_lot,
)
from portfolio_risk import FUTURES_MARGIN

CAL_DAY = date(2026, 11, 3)


def _calibration(**overrides):
    row = {
        "類型": "校正",
        "日期": "2026-11-03",
        "本日餘額": 137094,
        "商品代號": "FMF",
        "買賣": "買",
        "口數": 1,
        "均價": 46734,
        "原始保證金": 35050,
    }
    row.update(overrides)
    return row


def _positions(lots=1, ticker="FMF"):
    return pd.DataFrame(
        [
            {"帳戶": "第一證券", "種類": "台股", "標的": "2330", "名稱": "2330", "持有數量": 1000, "平均成本": 900, "投入總金額": 900000, "最大虧損比例": -100, "標的槓桿": 1.0, "即時報價": 1000, "Beta值": 1.15, "產業別": "晶圓代工"},
            {"帳戶": "元大期貨", "種類": "台股期貨", "標的": ticker, "名稱": "微台", "持有數量": lots, "平均成本": 46734, "投入總金額": 0.0, "最大虧損比例": -100, "標的槓桿": 1.0, "即時報價": 46734, "Beta值": 1.0, "產業別": "臺指期貨"},
        ]
    )


def _accounts():
    return pd.DataFrame(
        [
            {"帳戶名稱": "第一證券", "現金餘額": 1_000_000.0, "融資/質押借款": 0.0},
            {"帳戶名稱": "元大期貨", "現金餘額": 168308.0, "融資/質押借款": 0.0},
        ]
    )


class DeriveMarginTests(unittest.TestCase):
    def test_single_contract_divides_by_lots(self):
        per_lot, basis = derive_margin_per_lot(35050, {"FMF": 1})
        self.assertEqual(per_lot, {"FMF": 35050.0})
        self.assertIn("÷ 1 口", basis["FMF"])
        per_lot, _ = derive_margin_per_lot(70100, {"FMF": 2})
        self.assertEqual(per_lot["FMF"], 35050.0)

    def test_mixed_contracts_need_explicit_settings(self):
        per_lot, _ = derive_margin_per_lot(210300, {"FMF": 1, "MTX": 1})
        self.assertEqual(per_lot, {})
        per_lot, basis = derive_margin_per_lot(210300, {"FMF": 1, "MTX": 1}, {"MTX": 175250})
        self.assertEqual(per_lot["MTX"], 175250.0)
        self.assertAlmostEqual(per_lot["FMF"], 35050.0)
        self.assertIn("其他商品", basis["FMF"])

    def test_missing_or_bad_total(self):
        self.assertEqual(derive_margin_per_lot(None, {"FMF": 1})[0], {})
        self.assertEqual(derive_margin_per_lot(0, {"FMF": 1})[0], {})
        self.assertEqual(derive_margin_per_lot(35050, {})[0], {})
        self.assertEqual(derive_margin_per_lot(35050, {"TXO": 2})[0], {})


class LedgerQuoteMarginTests(unittest.TestCase):
    def test_seed_template_gives_fmf_per_lot(self):
        quote = quote_yuanta_ledger(template_ledger_frame(), None, today=CAL_DAY)
        self.assertTrue(quote.ok)
        self.assertEqual(quote.margin_note, 35050)
        self.assertEqual(quote.margin_per_lot, {"FMF": 35050.0})

    def test_uses_calibration_lots_not_later_trades(self):
        trades = pd.DataFrame(
            [{"成交時間": "2026-11-04 09:00:00", "帳戶": "元大期貨", "標的": "FMF", "種類": "台股期貨", "動作": "買進", "數量": 1, "成交價": 46800, "合約乘數": 10}]
        )
        ledger = pd.DataFrame([_calibration(口數=2, 原始保證金=72000)])
        quote = quote_yuanta_ledger(ledger, trades, today=CAL_DAY)
        self.assertEqual(quote.margin_per_lot["FMF"], 36000.0)

    def test_explicit_setting_row(self):
        ledger = pd.DataFrame(
            [
                _calibration(),
                {"類型": "設定", "商品代號": "MTX", "原始保證金": 180000},
                {"類型": "設定", "商品代號": "TX", "手續費預設": 50},
            ]
        )
        quote = quote_yuanta_ledger(ledger, None, today=CAL_DAY)
        self.assertEqual(quote.margin_per_lot, {"FMF": 35050.0, "MTX": 180000.0})
        self.assertEqual(quote.margin_basis["MTX"], "設定列每口原始保證金")

    def test_tmf_alias_and_text_number(self):
        ledger = pd.DataFrame([_calibration(商品代號="微台", 原始保證金="35,050")])
        quote = quote_yuanta_ledger(ledger, None, today=CAL_DAY)
        self.assertEqual(quote.margin_per_lot, {"FMF": 35050.0})

    def test_missing_margin_column(self):
        ledger = pd.DataFrame([_calibration()]).drop(columns=["原始保證金"])
        quote = quote_yuanta_ledger(ledger, None, today=CAL_DAY)
        self.assertTrue(quote.ok)
        self.assertEqual(quote.margin_per_lot, {})

    def test_round_trip_through_accounts(self):
        out = apply_yuanta_ledger(_accounts(), template_ledger_frame(), None, today=CAL_DAY)
        metrics = ledger_metrics_from_accounts(out)
        self.assertEqual(metrics["futures_ledger_margin_total"], 35050.0)
        self.assertEqual(metrics["futures_ledger_margin_per_lot"], {"FMF": 35050.0})
        self.assertIn("35,050", metrics["futures_ledger_margin_basis"]["FMF"])

    def test_unreadable_sheet_gives_empty_map(self):
        out = apply_yuanta_ledger(_accounts(), None, None, today=CAL_DAY)
        metrics = ledger_metrics_from_accounts(out)
        self.assertEqual(metrics["futures_ledger_margin_per_lot"], {})
        self.assertIsNone(metrics["futures_ledger_margin_total"])


class CalculatorMarginTests(unittest.TestCase):
    def test_prefill_from_ledger_and_fallback(self):
        metrics = {"futures_ledger_margin_per_lot": {"FMF": 36000.0}, "futures_ledger_margin_basis": {"FMF": "校正 36,000 ÷ 1 口"}}
        info = ledger_margin_per_lot("FMF", metrics)
        self.assertEqual(info["value"], 36000.0)
        self.assertEqual(info["source"], MARGIN_SOURCE_LEDGER)
        self.assertEqual(info["source"], "Futures_Ledger")
        self.assertEqual(ledger_margin_per_lot("TMF", {"futures_ledger_margin_per_lot": {"TMF": 34000}})["value"], 34000.0)
        tx = ledger_margin_per_lot("TX", metrics)
        self.assertEqual(tx["source"], MARGIN_SOURCE_DEFAULT)
        self.assertEqual(tx["source"], "預設值")
        self.assertEqual(tx["value"], FUTURES_MARGIN["TX"][0])
        for broken in (None, {}, {"futures_ledger_margin_per_lot": "garbage"}, {"futures_ledger_margin_per_lot": {"FMF": "n/a"}}, {"futures_ledger_margin_per_lot": {"FMF": -1}}):
            with self.subTest(metrics=broken):
                self.assertEqual(ledger_margin_per_lot("FMF", broken)["source"], MARGIN_SOURCE_DEFAULT)

    def test_snapshot_carries_ledger_margin(self):
        accounts = apply_yuanta_ledger(_accounts(), pd.DataFrame([_calibration(原始保證金=36000)]), None, today=CAL_DAY)
        snap = Snapshot(_positions(), accounts, 32.0, 23000.0)
        info = ledger_margin_per_lot("FMF", snap.metrics)
        self.assertEqual(info["value"], 36000.0)
        self.assertEqual(info["source"], MARGIN_SOURCE_LEDGER)

    def test_equity_ratio_uses_ledger_balance_and_margin(self):
        accounts = apply_yuanta_ledger(_accounts(), pd.DataFrame([_calibration(原始保證金=36000)]), None, today=CAL_DAY)
        snap = Snapshot(_positions(), accounts, 32.0, 23000.0)
        equity = snap.metrics["futures_equity"]
        # 本日餘額來自 Futures_Ledger 校正 137,094（不是 B7 的 168,308），報價＝均價所以未實現 0。
        self.assertAlmostEqual(equity, 137094.0)
        trial = futures_trial("FMF", 1, 46734, side="buy", margin_per_lot=36000.0)
        impact = futures_order_impact(snap, trial, account="元大期貨")
        self.assertAlmostEqual(impact["initial_margin_before"], 36000.0)
        self.assertAlmostEqual(impact["initial_margin_after"], 72000.0)
        self.assertAlmostEqual(impact["equity_ratio_before"], 137094.0 / 36000.0 * 100.0)
        self.assertAlmostEqual(impact["equity_ratio_after"], impact["equity_after"] / 72000.0 * 100.0)
        self.assertEqual(impact["equity_source"], "Futures_Ledger 本日餘額")
        self.assertAlmostEqual(impact["excess_margin_before"], 137094.0 - 36000.0)

    def test_ui_edited_margin_overrides_ledger(self):
        accounts = apply_yuanta_ledger(_accounts(), template_ledger_frame(), None, today=CAL_DAY)
        snap = Snapshot(_positions(), accounts, 32.0, 23000.0)
        trial = futures_trial("FMF", 1, 46734, side="buy", margin_per_lot=40000.0)
        impact = futures_order_impact(snap, trial, account="元大期貨")
        self.assertAlmostEqual(impact["initial_margin_after"], 80000.0)

    def test_sell_releases_margin(self):
        accounts = apply_yuanta_ledger(_accounts(), template_ledger_frame(), None, today=CAL_DAY)
        snap = Snapshot(_positions(lots=2), accounts, 32.0, 23000.0)
        trial = futures_trial("FMF", 1, 46734, side="sell", margin_per_lot=35050.0)
        impact = futures_order_impact(snap, trial, account="元大期貨")
        self.assertAlmostEqual(impact["initial_margin_before"], 70100.0)
        self.assertAlmostEqual(impact["initial_margin_after"], 35050.0)

    def test_without_ledger_matches_previous_defaults(self):
        snap = Snapshot(_positions(), _accounts(), 32.0, 23000.0)
        trial = futures_trial("FMF", 1, 46734, side="buy")
        impact = futures_order_impact(snap, trial, account="元大期貨")
        self.assertAlmostEqual(impact["initial_margin_before"], snap.metrics["futures_initial"])
        self.assertAlmostEqual(impact["excess_margin_before"], snap.metrics["excess_margin"])

    def test_initial_margin_helper_handles_empty(self):
        self.assertEqual(initial_margin_with_ledger(None, {"FMF": 1.0}, fallback=5.0), 5.0)
        self.assertEqual(initial_margin_with_ledger(pd.DataFrame(), {}, fallback=0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
