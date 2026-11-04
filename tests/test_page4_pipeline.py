"""第四頁持倉估價流程的回歸測試。不連網、不讀金鑰。

背景：第四頁曾把只有代號的 tuple（"FMF", "00663L.TW", ...）丟給
cached_quote_bundle，而它用 `for t, k in pairs` 拆 (標的, 種類)，
真實持倉就噴出 too many values to unpack (expected 2)。
"""

import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import portfolio_risk
from portfolio_risk import (
    coerce_ticker_kind_pairs,
    normalize_positions,
    ticker_kind_pairs,
    value_positions_safely,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 欄位與 Quant_Database「持倉統計」相同，數字取自真實表的一部分。
SHEET_POSITIONS = pd.DataFrame(
    [
        ["元大期貨", "台股期貨", "FMF", "微台", 2, 46733, 934660, -10, 1],
        ["投資先生", "台股", "00663L.TW", "國泰臺灣加權正2", 19000, 51.66, 981540, -10, 2],
        ["第一證券", "美股", "SOXL", "SOXL", 200, 144.52, 28904, -10, 3],
        ["嘉信理財", "美股", "MUU", "MUU", 470, 41.82, 19655.4, -10, 2],
        ["嘉信理財", "美股選擇權", "MUU261030C00045000", "MUU261030C00045000", -1, 0.39, -39, -10, 1],
        ["嘉信理財", "美股", "SOXL", "SOXL", 186, 141.56, 26330.16, -10, 3],
    ],
    columns=["帳戶", "種類", "標的", "名稱", "持有數量", "平均成本", "投入總金額", "最大虧損比例", "標的槓桿"],
)

SHEET_ACCOUNTS = pd.DataFrame(
    [
        ["第一證券", 0, 132654.72, 0, 0],
        ["嘉信理財", 0, 166357.12, 0, 0],
        ["投資先生", 1593, 348166, 0, 0],
        ["元大期貨", 168308, 0, 0, 0],
    ],
    columns=["帳戶名稱", "現金餘額", "融資/質押借款", "名目曝險", "帳戶淨值"],
)

PRICES = {"FMF": 46000.0, "00663L.TW": 50.0, "SOXL": 70.0, "MUU": 25.0, "MUU261030C00045000": 0.1}


def _positions():
    from datetime import date

    return normalize_positions(SHEET_POSITIONS, today=date(2026, 10, 1))


class QuotePairTests(unittest.TestCase):
    def test_plain_ticker_strings_do_not_unpack_into_characters(self):
        # 舊的第四頁呼叫方式：只有代號
        pairs = coerce_ticker_kind_pairs(("FMF", "00663L.TW", "MUU", "MUU261030C00045000"))
        self.assertEqual(pairs, (("FMF", ""), ("00663L.TW", ""), ("MUU", ""), ("MUU261030C00045000", "")))
        # 原本 cached_quote_bundle 的寫法必須能吃整理後的結果
        frame = pd.DataFrame([{"標的": t, "種類": k} for t, k in pairs])
        self.assertEqual(list(frame["標的"]), ["FMF", "00663L.TW", "MUU", "MUU261030C00045000"])

    def test_pairs_keep_kind_and_dedupe(self):
        pairs = coerce_ticker_kind_pairs([("FMF", "台股期貨"), ["MUU", "美股"], ("FMF", "台股期貨"), ("", "x"), ("TX",)])
        self.assertEqual(pairs, (("FMF", "台股期貨"), ("MUU", "美股"), ("TX", "")))

    def test_ticker_kind_pairs_from_real_sheet_rows(self):
        pairs = ticker_kind_pairs(_positions())
        self.assertIn(("FMF", "台股期貨"), pairs)
        self.assertIn(("MUU261030C00045000", "美股選擇權"), pairs)
        self.assertEqual(len(pairs), len(set(pairs)))
        for item in pairs:
            self.assertEqual(len(item), 2)

    def test_page4_passes_ticker_kind_pairs_to_quote_bundle(self):
        with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
            dashboard = handle.read()
        page4 = dashboard.split('elif page == "第四頁：部位監控與量化風控":', 1)[1].split("第五頁", 1)[0]
        self.assertIn("cached_quote_bundle(_ticker_kind_pairs(df))", page4)
        self.assertNotIn('cached_quote_bundle(tuple(dict.fromkeys(df["標的"]', page4)
        self.assertIn("value_positions_safely(", page4)
        bundle = dashboard.split("def cached_quote_bundle", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("coerce_ticker_kind_pairs(pairs)", bundle)


class SafeValuationTests(unittest.TestCase):
    def test_real_rows_value_without_warnings(self):
        valued, accounts, metrics, warnings = value_positions_safely(
            _positions(), SHEET_ACCOUNTS, 32.6, PRICES, {}, {}, extras={"FMF": {"合約乘數": 10.0}}
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(valued), len(SHEET_POSITIONS))
        self.assertGreater(metrics["nav"], 0)

    def test_one_bad_row_is_dropped_and_named(self):
        real = portfolio_risk._futures_margin_row

        def boom(ticker, qty_abs, notional_abs, *args):
            if ticker == "FMF":
                raise ValueError("too many values to unpack (expected 2)")
            return real(ticker, qty_abs, notional_abs, *args)

        with patch.object(portfolio_risk, "_futures_margin_row", side_effect=boom):
            valued, _accounts, metrics, warnings = value_positions_safely(_positions(), SHEET_ACCOUNTS, 32.6, PRICES, {}, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("FMF", warnings[0])
        self.assertIn("元大期貨", warnings[0])
        self.assertIn("第 2 列", warnings[0])
        self.assertNotIn("FMF", list(valued["標的"]))
        self.assertEqual(len(valued), len(SHEET_POSITIONS) - 1)
        self.assertTrue(np.isfinite(metrics["nav"]))

    def test_error_not_tied_to_a_row_still_raises(self):
        with patch.object(portfolio_risk, "finalize_valuation", side_effect=RuntimeError("accounts broken")):
            with self.assertRaises(RuntimeError):
                value_positions_safely(_positions(), SHEET_ACCOUNTS, 32.6, PRICES, {}, {})


if __name__ == "__main__":
    unittest.main()
