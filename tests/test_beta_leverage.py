"""槓桿／部位平均 Beta／帳戶 Beta 的口徑測試。不連網、不讀金鑰。

數字仿 2026-11-04 線上 /v1/portfolio：SOXL 200 股 @ 73.12、標的 SOXX 一年 Beta 2.72、
微台 1 口 @ 43798（均價 46733，未實現 −29,350，名目 437,980）。
"""

import unittest

import numpy as np
import pandas as pd

from portfolio_briefing import beta_leverage_block, portfolio_payload, position_records
from portfolio_risk import beta_leverage_breakdown, beta_leverage_rows, finalize_valuation
from datetime import datetime
from zoneinfo import ZoneInfo

USDTWD = 32.785
AS_OF = datetime(2026, 11, 4, 7, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _row(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, und_beta=np.nan, und_src="", **extra):
    row = {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": 0.0 if "期" in kind else cost * qty,
        "最大虧損比例": -100,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": "其他",
        "標的Beta": und_beta,
        "標的Beta來源": und_src,
    }
    row.update(extra)
    return row


def _book(rows=None, accounts=None):
    rows = rows or [
        _row("元大期貨", "台股期貨", "FMF", 1, 46733, 43798),
        _row("投資先生", "台股", "00663L.TW", 19000, 51.66, 89.95, lev=2, beta=2.07),
        _row("投資先生", "台股", "2308.TW", 4, 1900, 1270, lev=1, beta=1.69),
        # SOXL 自己的一年 Beta 8.05 已含槓桿；有效 Beta 必須是 3 × SOXX 2.72，不能用 8.05 再乘 3。
        _row("第一證券", "美股", "SOXL", 200, 144.52, 73.12, lev=3, beta=8.05, und_beta=2.72, und_src="SOXX 一年回歸（對 S&P 500）"),
        _row("嘉信理財", "美股", "AVGO", 5, 345, 332.13, lev=1, beta=2.22),
    ]
    accounts = accounts if accounts is not None else pd.DataFrame(
        [
            {"帳戶名稱": "元大期貨", "現金餘額": 137094, "融資/質押借款": 0},
            {"帳戶名稱": "投資先生", "現金餘額": 1593, "融資/質押借款": 348166},
            {"帳戶名稱": "第一證券", "現金餘額": 0, "融資/質押借款": 132654.72},
            {"帳戶名稱": "嘉信理財", "現金餘額": 5000, "融資/質押借款": 0},
        ]
    )
    return finalize_valuation(pd.DataFrame(rows), accounts, USDTWD)


class LeveragedEtfTests(unittest.TestCase):
    def test_soxl_leverage_applied_exactly_once(self):
        df, _acc, _m = _book()
        soxl = df[df["標的"] == "SOXL"].iloc[0]
        mv = 200 * 73.12 * USDTWD
        self.assertAlmostEqual(soxl["即時市值(台幣)"], mv, places=4)
        self.assertAlmostEqual(soxl["實質曝險(台幣)"], mv * 3, places=4)
        self.assertAlmostEqual(soxl["有效Beta"], 3 * 2.72, places=9)
        self.assertAlmostEqual(soxl["曝險Beta"], 2.72, places=9)
        self.assertAlmostEqual(soxl["Beta曝險(台幣)"], mv * 3 * 2.72, places=4)
        # 沒有用自身 Beta 8.05，也沒有 MV × 3 × (3 × 2.72)
        self.assertNotAlmostEqual(soxl["Beta曝險(台幣)"], mv * 3 * 8.05, places=0)
        self.assertNotAlmostEqual(soxl["Beta曝險(台幣)"], mv * 9 * 2.72, places=0)

    def test_sheet_leverage_missing_uses_mapping_for_both_exposure_and_beta(self):
        rows = [_row("第一證券", "美股", "SOXL", 10, 20, 25, lev=1, beta=8.0, und_beta=2.0, und_src="SOXX")]
        acc = pd.DataFrame([{"帳戶名稱": "第一證券", "現金餘額": 0, "融資/質押借款": 0}])
        df, _a, m = _book(rows, acc)
        row = df.iloc[0]
        mv = 10 * 25 * USDTWD
        self.assertAlmostEqual(row["採用槓桿"], 3.0)
        self.assertAlmostEqual(row["實質曝險(台幣)"], mv * 3, places=6)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], mv * 3 * 2.0, places=6)
        self.assertAlmostEqual(m["us_avg_beta"], 2.0, places=9)

    def test_tw_leveraged_etf_uses_index_beta_one(self):
        df, _a, _m = _book()
        row = df[df["標的"] == "00663L.TW"].iloc[0]
        mv = 19000 * 89.95
        self.assertAlmostEqual(row["實質曝險(台幣)"], mv * 2, places=4)
        self.assertAlmostEqual(row["曝險Beta"], 1.0, places=9)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], mv * 2, places=4)


class FuturesTests(unittest.TestCase):
    def test_micro_taiex_market_value_is_notional_not_pnl(self):
        df, acc, _m = _book()
        fmf = df[df["標的"] == "FMF"].iloc[0]
        notional = 1 * 43798 * 10
        self.assertAlmostEqual(fmf["即時市值(台幣)"], notional, places=6)
        self.assertAlmostEqual(fmf["顯示_即時市值(原幣)"], notional, places=6)
        self.assertAlmostEqual(fmf["實質曝險(台幣)"], notional, places=6)
        self.assertAlmostEqual(fmf["曝險Beta"], 1.0)
        self.assertAlmostEqual(fmf["Beta曝險(台幣)"], notional, places=6)
        # 權益市值保留舊口徑：保證金 35,050 + 未實現 −29,350 = 5,700
        self.assertAlmostEqual(fmf["權益市值(台幣)"], 35050 - 29350, places=6)
        # 元大淨值仍是本日餘額 + 未實現損益，名目不進淨值
        yuanta = acc[acc["帳戶名稱"] == "元大期貨"].iloc[0]
        self.assertAlmostEqual(yuanta["帳戶淨值"], 137094 - 29350, places=6)

    def test_api_market_value_for_futures_is_notional(self):
        df, _a, m = _book()
        recs = {r["symbol"]: r for r in position_records(df)}
        self.assertAlmostEqual(recs["FMF"]["market_value"], 437980, places=6)
        self.assertAlmostEqual(recs["FMF"]["equity_value"], 5700, places=6)
        self.assertAlmostEqual(recs["FMF"]["pnl"], -29350, places=6)
        self.assertAlmostEqual(recs["FMF"]["notional_exposure"], 437980, places=6)
        self.assertAlmostEqual(recs["FMF"]["beta"], 1.0)
        self.assertAlmostEqual(recs["SOXL"]["leverage"], 3.0)
        self.assertEqual(recs["SOXL"]["underlying"], "SOXX")
        self.assertAlmostEqual(recs["SOXL"]["underlying_beta"], 2.72)
        self.assertAlmostEqual(recs["SOXL"]["effective_beta"], 8.16, places=9)
        self.assertAlmostEqual(recs["SOXL"]["beta_exposure"], recs["SOXL"]["market_value"] * 8.16, places=4)

    def test_futures_in_stock_account_do_not_inflate_nav(self):
        rows = [_row("股票戶", "台股期貨", "FMF", 1, 46733, 43798), _row("股票戶", "台股", "2330", 1000, 100, 110, beta=1.2)]
        acc = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 50000, "融資/質押借款": 0}])
        _df, a, m = _book(rows, acc)
        # 淨值 = 現金 + 股票市值 + 期貨權益市值（保證金 + 損益），不是 + 名目
        self.assertAlmostEqual(a.iloc[0]["帳戶淨值"], 50000 + 110000 + 5700, places=6)
        self.assertAlmostEqual(m["exposure"], 110000 + 437980, places=6)

    def test_short_non_taiex_future_uses_notional(self):
        rows = [_row("期貨戶", "台股期貨", "CDF", -1, 1000, 1100, beta=1.5, 合約乘數=2000)]
        acc = pd.DataFrame([{"帳戶名稱": "期貨戶", "現金餘額": 500000, "融資/質押借款": 0}])
        df, _a, _m = _book(rows, acc)
        row = df.iloc[0]
        self.assertAlmostEqual(row["實質曝險(台幣)"], 1100 * 2000, places=6)
        self.assertAlmostEqual(row["Beta曝險(台幣)"], -1100 * 2000 * 1.5, places=6)


class ThreeNumbersTests(unittest.TestCase):
    def test_totals_reconcile_and_identities_hold(self):
        df, _a, m = _book()
        self.assertAlmostEqual(m["tw_notional_exposure"] + m["us_notional_exposure"], m["exposure"], places=4)
        self.assertAlmostEqual(m["notional_exposure"], m["exposure"], places=4)
        self.assertAlmostEqual(m["tw_beta_exposure"] + m["us_beta_exposure"], m["beta_exposure"], places=4)
        self.assertAlmostEqual(m["tw_nav"] + m["us_nav"], m["nav"], places=4)
        self.assertAlmostEqual(m["total_leverage"], m["leverage"], places=12)
        self.assertAlmostEqual(m["account_beta"], m["total_beta"], places=12)
        self.assertAlmostEqual(m["tw_account_beta"], m["tw_beta"], places=12)
        self.assertAlmostEqual(m["us_account_beta"], m["us_beta"], places=12)
        for p in ("tw_", "us_", ""):
            lev = m[f"{p}leverage"] if p else m["total_leverage"]
            self.assertAlmostEqual(m[f"{p}account_beta"], lev * m[f"{p}avg_beta"], places=10)
        # 舊欄位 tw_exposure 是 Beta 曝險
        self.assertAlmostEqual(m["tw_exposure"], m["tw_beta_exposure"], places=6)
        # 部位平均 Beta 不再用自身 Beta × 名目（舊 beta_weighted_avg 會把 SOXL 算成 3 × 8.05）
        self.assertAlmostEqual(m["beta_weighted_avg"], m["avg_beta"], places=12)
        expected_avg = float(df["Beta曝險(台幣)"].sum() / df["實質曝險(台幣)"].sum())
        self.assertAlmostEqual(m["avg_beta"], expected_avg, places=12)

    def test_account_not_in_sheet_excluded_consistently(self):
        rows = [_row("股票戶", "台股", "2330", 1000, 100, 110, beta=1.2), _row("不明帳戶", "台股", "2317", 1000, 100, 100, beta=1.0)]
        acc = pd.DataFrame([{"帳戶名稱": "股票戶", "現金餘額": 0, "融資/質押借款": 0}])
        _df, _a, m = _book(rows, acc)
        self.assertAlmostEqual(m["notional_exposure"], m["exposure"], places=6)

    def test_breakdown_formula(self):
        b = beta_leverage_breakdown(4_000_000, 4_000_000, 4_500_000, 12_000_000, 2_000_000, 1_500_000, 3_500_000)
        self.assertAlmostEqual(b["tw_leverage"], 2.0)
        self.assertAlmostEqual(b["us_leverage"], 4_000_000 / 1_500_000)
        self.assertAlmostEqual(b["total_leverage"], 8_000_000 / 3_500_000)
        self.assertAlmostEqual(b["tw_avg_beta"], 1.125)
        self.assertAlmostEqual(b["us_avg_beta"], 3.0)
        self.assertAlmostEqual(b["avg_beta"], 16_500_000 / 8_000_000)
        self.assertAlmostEqual(b["account_beta"], 16_500_000 / 3_500_000)
        zero = beta_leverage_breakdown(0, 0, 0, 0, 0, 0, 0)
        self.assertEqual(zero["account_beta"], 0.0)

    def test_rows_and_api_block(self):
        _df, _a, m = _book()
        rows = beta_leverage_rows(m)
        self.assertEqual([r["市場"] for r in rows], ["台股", "美股", "合計"])
        self.assertAlmostEqual(rows[0]["名目曝險"] + rows[1]["名目曝險"], rows[2]["名目曝險"], places=4)
        block = beta_leverage_block(m)
        self.assertAlmostEqual(block["total"]["leverage"], m["leverage"], places=12)
        self.assertAlmostEqual(block["tw"]["notional_exposure"] + block["us"]["notional_exposure"], block["total"]["notional_exposure"], places=4)
        payload = portfolio_payload(_df, m, usdtwd=USDTWD, as_of=AS_OF)
        self.assertEqual(payload["schema_version"], "1.6.0")
        self.assertAlmostEqual(payload["beta_leverage"]["total"]["account_beta"], payload["beta"], places=12)
        from api.schemas import PortfolioResponse
        PortfolioResponse(**payload)


if __name__ == "__main__":
    unittest.main()
