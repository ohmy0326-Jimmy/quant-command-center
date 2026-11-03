"""元大餘額自動推算、Trade_Log 既有欄位、Daily_NAV 日期、TXO 隱含波動率。"""

import math
import os
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from futures_ledger import (
    APPENDED_TRADE_HEADERS,
    EXISTING_TRADE_HEADERS,
    apply_yuanta_ledger,
    contract_multiplier,
    quote_yuanta_ledger,
    template_ledger_frame,
)
from order_assistant import (
    bs_price_greeks,
    hedge_leg_vols,
    implied_vol_black76,
    implied_vol_bs,
    iv_percentile,
    iv_traffic_light,
)
from portfolio_risk import finalize_valuation
from sheet_dates import format_sheet_date, nav_growth_series, parse_sheet_date, write_daily_nav
from taiex_vix import decode_vix_bytes, fetch_taiex_vix, parse_vix_table

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL_DAY = date(2026, 11, 3)


def _accounts(cash=168308):
    return pd.DataFrame([{"帳戶名稱": "元大期貨", "現金餘額": cash, "融資/質押借款": 0}])


def _ledger(avg=46733):
    frame = template_ledger_frame()
    frame.loc[frame["類型"] == "校正", "均價"] = avg
    return frame


def _notion_trade(**overrides):
    row = {
        "成交時間": "2026-11-04 01:30:00",
        "帳戶": "元大期貨",
        "標的": "FMF",
        "名稱": "微台",
        "商品種類": "台股期貨",
        "動作": "賣出",
        "數量": 1,
        "成交價": 45027,
        "合約乘數": 10,
        "標的槓桿": 1,
        "最大虧損比例": -10,
        "覆盤筆記": "",
    }
    row.update(overrides)
    return row


class TradeLogTests(unittest.TestCase):
    def test_existing_headers_match_the_live_sheet(self):
        self.assertEqual(
            EXISTING_TRADE_HEADERS,
            (
                "時間",
                "帳戶",
                "種類",
                "動作",
                "標的",
                "名稱",
                "數量",
                "成交價",
                "合約乘數",
                "總金額",
                "最大虧損比例",
                "覆盤筆記",
                "標的槓桿",
                "類型",
                "長短線",
                "停損價",
                "已實現損益",
                "交易後帳戶槓桿",
            ),
        )
        self.assertEqual(APPENDED_TRADE_HEADERS, EXISTING_TRADE_HEADERS[13:])

    def test_real_headers_count_micro_close_only_after_calibration(self):
        def row(**overrides):
            blank = {name: "" for name in EXISTING_TRADE_HEADERS}
            blank.update(overrides)
            return blank

        before = row(
            時間="2026-10-30 11:36:00",
            帳戶="元大期貨",
            種類="台股期貨",
            動作="賣出",
            標的="FMF",
            名稱="微台",
            數量=1,
            成交價=45027,
            合約乘數=10,
            總金額=450270,
            已實現損益=-99999,
        )
        same_day = row(
            時間="2026-11-03",
            帳戶="元大期貨",
            種類="台股期貨",
            動作="賣出",
            標的="FMF",
            名稱="微台",
            數量=1,
            成交價=40000,
            合約乘數=10,
            總金額=400000,
            已實現損益=-99999,
        )
        close_after = row(
            時間="2026-11-04 13:45:00",
            帳戶="元大期貨",
            種類="台股期貨",
            動作="賣出",
            標的="FMF",
            名稱="微台",
            數量=1,
            成交價=46000,
            合約乘數=10,
            總金額=460000,
            最大虧損比例=-10,
            標的槓桿=1,
        )
        stock = row(
            時間="2026-11-05",
            帳戶="元大期貨",
            種類="台股",
            動作="賣出",
            標的="2330",
            名稱="台積電",
            數量=100,
            成交價=1000,
            總金額=1_000_000,
            已實現損益=-50000,
        )
        quote = quote_yuanta_ledger(_ledger(46734), pd.DataFrame([before, same_day, close_after, stock]), today=CAL_DAY)
        gross = (46000 - 46734) * 10
        tax = 46000 * 10 * 0.00002
        self.assertAlmostEqual(quote.realized, gross, places=4)
        self.assertEqual(quote.cashflow, 0)
        self.assertAlmostEqual(quote.fees, 15, places=4)
        self.assertAlmostEqual(quote.balance, 137094 + gross - 15 - tax, places=4)

    def test_blank_realized_column_uses_average_cost(self):
        trades = pd.DataFrame([_notion_trade(已實現損益="")])
        quote = quote_yuanta_ledger(_ledger(), trades, today=CAL_DAY)
        gross = (45027 - 46733) * 1 * 10
        tax = 45027 * 10 * 0.00002
        self.assertTrue(quote.ok)
        self.assertAlmostEqual(quote.realized, gross, places=4)
        self.assertAlmostEqual(quote.fees, 15, places=4)
        self.assertAlmostEqual(quote.tax, tax, places=4)
        self.assertAlmostEqual(quote.balance, 137094 + gross - 15 - tax, places=4)

    def test_missing_appended_columns_still_calculate(self):
        trades = pd.DataFrame([_notion_trade()])
        self.assertNotIn("已實現損益", trades.columns)
        quote = quote_yuanta_ledger(_ledger(), trades, today=CAL_DAY)
        gross = (45027 - 46733) * 10
        self.assertAlmostEqual(quote.realized, gross, places=4)

    def test_column_q_overrides_the_formula_and_skips_default_fees(self):
        trades = pd.DataFrame([_notion_trade(已實現損益=-16000, 類型="", 長短線="", 停損價="", 交易後帳戶槓桿="")])
        quote = quote_yuanta_ledger(_ledger(), trades, today=CAL_DAY)
        self.assertAlmostEqual(quote.realized, -16000, places=4)
        self.assertEqual(quote.fees, 0)
        self.assertEqual(quote.tax, 0)
        self.assertAlmostEqual(quote.balance, 137094 - 16000, places=4)

    def test_sheets_serial_trade_date_and_ignores_same_day_as_calibration(self):
        serial_next = (date(2026, 11, 4) - date(1899, 12, 30)).days
        later = quote_yuanta_ledger(_ledger(), pd.DataFrame([_notion_trade(成交時間=serial_next, 已實現損益=-100)]), today=CAL_DAY)
        same_day = quote_yuanta_ledger(_ledger(), pd.DataFrame([_notion_trade(成交時間=46329, 已實現損益=-100)]), today=CAL_DAY)
        self.assertAlmostEqual(later.realized, -100, places=4)
        self.assertEqual(same_day.realized, 0)

    def test_stock_rows_and_other_brokers_do_not_change_yuanta_cash(self):
        trades = pd.DataFrame(
            [
                _notion_trade(帳戶="嘉信理財", 標的="SOXL", 商品種類="美股", 動作="賣出", 已實現損益=-999),
                _notion_trade(成交時間="2026-10-30 11:36:00", 已實現損益=-999),
            ]
        )
        quote = quote_yuanta_ledger(_ledger(), trades, today=CAL_DAY)
        self.assertAlmostEqual(quote.balance, 137094, places=4)

    def test_listed_multiplier_overrides_the_default_table(self):
        self.assertEqual(contract_multiplier("S2330"), 100)
        self.assertEqual(contract_multiplier("2330"), 2000)
        trades = pd.DataFrame(
            [
                _notion_trade(標的="2330", 名稱="個股期", 動作="買進", 數量=1, 成交價=100, 合約乘數=100, 成交時間="2026-11-04"),
                _notion_trade(標的="2330", 名稱="個股期", 動作="賣出", 數量=1, 成交價=110, 合約乘數=100, 成交時間="2026-11-05", 手續費=0, 稅=0),
            ]
        )
        quote = quote_yuanta_ledger(_ledger(), trades, today=CAL_DAY)
        self.assertAlmostEqual(quote.realized, (110 - 100) * 100, places=4)

    def test_missing_ledger_keeps_b7_and_finalize_uses_the_same_cash(self):
        kept = apply_yuanta_ledger(_accounts(168308), None, None, today=CAL_DAY)
        self.assertEqual(float(kept.iloc[0]["現金餘額"]), 168308)
        self.assertEqual(kept.iloc[0]["_yuanta_source"], "b7")
        adjusted = apply_yuanta_ledger(_accounts(168308), _ledger(), pd.DataFrame([_notion_trade(已實現損益=-500)]), today=CAL_DAY)
        _valued, accounts_out, metrics = finalize_valuation(pd.DataFrame(), adjusted, 32)
        self.assertAlmostEqual(metrics["futures_cash_ledger"], 137094 - 500, places=4)
        self.assertAlmostEqual(accounts_out.iloc[0]["現金餘額"], 137094 - 500, places=4)
        self.assertEqual(metrics["futures_cash_source"], "ledger")
        self.assertIn("自動推算（最後校正日 2026-11-03）", metrics["futures_cash_label"])

    def test_stale_calibration_still_uses_the_balance(self):
        quote = quote_yuanta_ledger(_ledger(), None, today=date(2026, 12, 15))
        self.assertTrue(quote.stale)
        self.assertAlmostEqual(quote.balance, 137094, places=4)
        fresh = quote_yuanta_ledger(_ledger(), None, today=CAL_DAY)
        self.assertFalse(fresh.stale)


class SheetDateTests(unittest.TestCase):
    def test_sheets_serial_becomes_the_calendar_date(self):
        serial = (date(2026, 11, 3) - date(1899, 12, 30)).days
        self.assertEqual(serial, 46329)
        self.assertEqual(parse_sheet_date(46329), date(2026, 11, 3))
        self.assertEqual(parse_sheet_date("46329"), date(2026, 11, 3))
        self.assertEqual(parse_sheet_date(46329.75), date(2026, 11, 3))
        self.assertEqual(format_sheet_date(46329), "2026-11-03")

    def test_nav_chart_keeps_history_in_order(self):
        nav = pd.DataFrame({"日期": [46327, 46328, "2026-11-02"], "總淨值": [100, 110, 90]})
        series = nav_growth_series(nav, 120, today=date(2026, 11, 3))
        self.assertEqual(list(series.index), ["2026-11-01", "2026-11-02", "2026-11-03"])
        self.assertEqual(float(series.iloc[-1]), 120)
        self.assertNotIn("1970-01-01", list(series.index))

    def test_write_uses_iso_date_not_serial(self):
        class Sheet:
            def __init__(self):
                self.calls = []
                self.values = [["日期", "總淨值"], [46329, 100]]

            def get_all_values(self):
                return self.values

            def update(self, cell_range, values, value_input_option=None):
                self.calls.append((cell_range, values, value_input_option))

            def format(self, cell_range, fmt):
                self.calls.append(("format", cell_range, fmt))

        sheet = Sheet()
        written = write_daily_nav(sheet, date(2026, 11, 4), 120)
        self.assertEqual(written["values"], ["2026-11-04", 120.0])
        self.assertEqual(written["value_input_option"], "USER_ENTERED")
        self.assertEqual(sheet.calls[0][1], [["2026-11-04", 120.0]])
        self.assertEqual(sheet.calls[0][2], "USER_ENTERED")
        self.assertEqual(sheet.calls[1][2]["numberFormat"]["pattern"], "yyyy-mm-dd")


class ImpliedVolTests(unittest.TestCase):
    def test_bs_and_black76_round_trip(self):
        spot, strike, tenor, rate, sigma = 23000.0, 22000.0, 30 / 365, 0.015, 0.22
        price, _delta, _theta = bs_price_greeks(spot, strike, tenor, rate, sigma, "put")
        self.assertAlmostEqual(implied_vol_bs(price, spot, strike, tenor, rate, "put"), sigma, places=3)
        recovered = implied_vol_black76(_black76_price(spot, strike, tenor, sigma, "put"), spot, strike, tenor, "put")
        self.assertAlmostEqual(recovered, sigma, places=3)

    def test_percentile_lights_and_otm_put_skew(self):
        history = list(range(10, 40))
        self.assertLess(iv_percentile(15, history), 30)
        self.assertEqual(iv_traffic_light(29.9)["color"], "green")
        self.assertEqual(iv_traffic_light(30)["color"], "yellow")
        self.assertEqual(iv_traffic_light(70)["color"], "yellow")
        self.assertEqual(iv_traffic_light(70.1)["color"], "red")
        skewed = hedge_leg_vols(23000, 21000, "put", 0, 0.20, 30 / 365, 0.015, 36.83)
        self.assertEqual(skewed["source"], "skew")
        self.assertAlmostEqual(skewed["iv_percent"], 39.83, places=2)
        self.assertIn("VIX", skewed["note"])
        priced = hedge_leg_vols(100, 100, "put", 5, 0.2, 1, 0.01, 20)
        self.assertEqual(priced["source"], "premium")
        self.assertAlmostEqual(priced["iv"], implied_vol_bs(5, 100, 100, 1, 0.01, "put"), places=6)
        self.assertIsNotNone(priced["black76_percent"])

    def test_vix_parser_reads_taifex_text(self):
        text = "交易日期\t時間\t臺指選擇權波動率指數\n--------\t----\t----\n20261103\t13450000\t36.83\n20261103\tLast 1 min AVG\t36.83\n"
        rows = parse_vix_table(decode_vix_bytes(text.encode("big5")))
        self.assertEqual(rows, [(date(2026, 11, 3), "13450000", 36.83)])
        body = "20261102\t13450000\t31.30\n"
        quote = fetch_taiex_vix(date(2026, 11, 3), get=lambda url: body if "202611" in url and "getVixData" not in url else "", use_cache=False)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["source"].startswith("期交所"), True)
        empty = fetch_taiex_vix(date(2026, 11, 3), get=lambda _url: "", use_cache=False)
        self.assertFalse(empty["ok"])
        self.assertIn("無報價", empty["source"])


class PageCopyTests(unittest.TestCase):
    def test_page4_and_hedge_copy(self):
        with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
            dashboard = handle.read()
        with open(os.path.join(ROOT, "order_assistant_ui.py"), encoding="utf-8") as handle:
            hedge = handle.read()
        self.assertIn("元大明細：", dashboard)
        self.assertIn("nav_growth_series", dashboard)
        self.assertNotIn("pd.to_datetime(nav_data['日期'])", dashboard)
        self.assertIn("以加權指數×曝險估算，正2等槓桿 ETF 有每日再平衡耗損。", hedge)
        self.assertIn("台指 VIX", hedge)
        self.assertNotIn("y=1.12", hedge)
        self.assertIn('y=-0.22', hedge)


def _black76_price(forward, strike, tenor, vol, kind):
    disc_free = math.exp(0)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * tenor) / (vol * math.sqrt(tenor))
    d2 = d1 - vol * math.sqrt(tenor)
    n1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    n2 = 0.5 * (1 + math.erf(d2 / math.sqrt(2)))
    if str(kind).startswith("c"):
        return disc_free * (forward * n1 - strike * n2)
    return disc_free * (strike * (1 - n2) - forward * (1 - n1))


class FetchSheetsTests(unittest.TestCase):
    def test_fetch_reads_trade_log_without_requiring_new_columns(self):
        tabs = {
            "Active_Positions": [{"帳戶名稱": "元大期貨", "現金餘額": 168308, "融資/質押借款": 0}],
            "Daily_NAV": [{"日期": 46328, "總淨值": 500000}],
            "持倉統計": [],
            "Futures_Ledger": _ledger().to_dict(orient="records"),
            "Trade_Log": [_notion_trade(已實現損益=-250)],
        }

        class Worksheet:
            def __init__(self, records):
                self.records = records

            def get_all_records(self):
                return self.records

        class Book:
            def worksheet(self, title):
                if title not in tabs:
                    raise KeyError(title)
                return Worksheet(tabs[title])

        class Client:
            def open(self, name):
                self.name = name
                return Book()

        with patch("gspread.authorize", return_value=Client()), patch(
            "oauth2client.service_account.ServiceAccountCredentials.from_json_keyfile_dict",
            return_value=object(),
        ):
            from portfolio_risk import fetch_quant_sheets

            accounts, nav, _positions = fetch_quant_sheets({"client_email": "x"})
        self.assertEqual(str(nav.iloc[0]["日期"]), "2026-11-02")
        self.assertAlmostEqual(float(accounts.iloc[0]["現金餘額"]), 137094 - 250, places=4)


if __name__ == "__main__":
    unittest.main()
