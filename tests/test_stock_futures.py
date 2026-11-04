"""個股期：解析、乘數、現股對標報價與 Beta。不連網。"""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import portfolio_risk
import stock_futures
from futures_ledger import contract_multiplier, symbol_root
from stock_futures import resolve_stock_future
from tests.test_beta_leverage import _book, _row


class ResolverTests(unittest.TestCase):
    def test_small_names_and_codes(self):
        for s in ("小國巨期", "小型國巨期", "國巨小型期貨", "S2327", "s2327", "小2327期"):
            sf = resolve_stock_future(s)
            self.assertIsNotNone(sf, s)
            self.assertEqual((sf.code, sf.small, sf.multiplier), ("2327", True, 100.0), s)

    def test_regular_names_and_codes(self):
        for s in ("國巨期", "國巨期貨", "2327F", "2327期", "台積電期"):
            sf = resolve_stock_future(s)
            self.assertIsNotNone(sf, s)
            self.assertFalse(sf.small, s)
            self.assertEqual(sf.multiplier, 2000.0, s)
        self.assertEqual(resolve_stock_future("台積電期").code, "2330")

    def test_not_stock_futures(self):
        with mock.patch.object(stock_futures, "lookup_code_by_name", return_value=None):
            for s in ("TX", "MTX", "FMF", "小台", "微台", "台指期", "小型臺指", "2330", "SOXL", "", "未知期"):
                self.assertIsNone(resolve_stock_future(s), s)

    def test_taifex_dispcname_fallback(self):
        items = {"Items": [{"DispCName": "小型奇鋐期126", "SpotID": "3017", "CID": "XXX"}]}
        with mock.patch("taifex_quotes._post", return_value=items):
            sf = resolve_stock_future("小大同奇鋐期".replace("大同", ""))
        self.assertEqual((sf.code, sf.multiplier), ("3017", 100.0))
        items = {"Items": [{"DispCName": "冷門公司期126", "SpotID": "9999"}]}
        with mock.patch("taifex_quotes._post", return_value=items):
            self.assertEqual(resolve_stock_future("冷門公司期").code, "9999")


class MultiplierTests(unittest.TestCase):
    def test_portfolio_and_ledger_multipliers(self):
        self.assertEqual(portfolio_risk.futures_multiplier("小國巨期"), 100.0)
        self.assertEqual(portfolio_risk.futures_multiplier("國巨期"), 2000.0)
        self.assertEqual(portfolio_risk.futures_multiplier("FMF"), 10.0)
        self.assertEqual(contract_multiplier("小國巨期"), 100.0)
        self.assertEqual(contract_multiplier("國巨期"), 2000.0)
        self.assertEqual(symbol_root("小國巨期"), symbol_root("S2327"))
        self.assertEqual(symbol_root("小台"), "MTX")


class SpotBenchmarkQuoteTests(unittest.TestCase):
    def _quote(self, ticker, ok=True):
        calls = []

        def beta_fn(t, is_tw):
            calls.append(t)
            return (1.35, "被動元件") if ok else (1.0, "其他")

        with mock.patch.object(portfolio_risk, "_fetch_yahoo_price", side_effect=lambda t, tw: 450.0 if t == "2327.TW" else np.nan), \
             mock.patch.object(portfolio_risk.taifex_quotes, "get_futures_quote", side_effect=AssertionError("no TAIFEX quote")):
            out = portfolio_risk._quote_one_ex(ticker, 23000.0, beta_fn, kind="台股期貨")
        return out, calls

    def test_small_future_uses_spot_and_underlying_beta(self):
        (_t, px, b, s, ex), calls = self._quote("小國巨期")
        self.assertEqual(px, 450.0)
        self.assertEqual(b, 1.35)
        self.assertEqual(calls, ["2327.TW"])
        self.assertEqual(ex["報價來源"], "現股 2327 對標")
        self.assertEqual(ex["合約乘數"], 100.0)
        self.assertEqual(ex["標的代號"], "2327.TW")
        self.assertIn("一年回歸", ex["Beta來源"])

    def test_regular_future_uses_spot(self):
        (_t, px, _b, _s, ex), _ = self._quote("國巨期")
        self.assertEqual(px, 450.0)
        self.assertEqual(ex["合約乘數"], 2000.0)
        self.assertEqual(ex["報價來源"], "現股 2327 對標")


class ValuationTests(unittest.TestCase):
    def _val(self, ticker, mult, qty=1):
        rows = [_row("元大期貨", "台股期貨", ticker, qty, 448.5, 450.0, beta=1.35,
                     und_beta=1.35, und_src="2327 一年回歸（對加權指數）", 合約乘數=mult,
                     Beta來源="2327 一年回歸（對加權指數）", 標的代號="2327.TW")]
        acc = pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": 137049, "融資/質押借款": 0}])
        df, _a, m = _book(rows, acc)
        return df.iloc[0], m

    def test_small_future_notional_and_beta(self):
        row, m = self._val("小國巨期", 100.0)
        self.assertAlmostEqual(row["實質曝險(台幣)"], 45000.0)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], 45000.0 * 1.35)
        self.assertAlmostEqual(row["有效Beta"], 1.35)
        self.assertEqual(row["標的代號"], "2327.TW")
        self.assertAlmostEqual(row["淨未實現損益(原幣)"], 150.0)
        self.assertAlmostEqual(m["tw_notional_exposure"], 45000.0)

    def test_small_future_without_listed_multiplier(self):
        row, _m = self._val("小國巨期", np.nan)
        self.assertAlmostEqual(row["實質曝險(台幣)"], 45000.0)

    def test_regular_future_notional(self):
        row, _m = self._val("國巨期", np.nan, qty=-1)
        self.assertAlmostEqual(row["實質曝險(台幣)"], 900000.0)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], -900000.0 * 1.35)


if __name__ == "__main__":
    unittest.main()
