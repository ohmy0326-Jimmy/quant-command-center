"""可轉債／CBAS 計價與曝險。不連網、不讀金鑰、不下單。"""

from datetime import date

import numpy as np
import pytest
import pandas as pd

from cb_holdings import (
    bond_floor_delta,
    conversion_value_amount,
    conversion_value_points,
    mark_convertible,
    parity_delta,
    product_kind,
)
from nav_scenario_sim import build_book
from order_assistant import bs_price_greeks
from portfolio_briefing import position_records
from portfolio_risk import _quote_one_ex, attach_quotes, finalize_valuation


def _accounts(cash=50_000.0):
    return pd.DataFrame([{"帳戶名稱": "第一證券", "現金餘額": cash, "融資/質押借款": 0.0}])


def _cb(**extra):
    row = {
        "帳戶": "第一證券",
        "種類": "可轉債",
        "標的": "13164",
        "名稱": "上曜四",
        "持有數量": 2,
        "平均成本": 110,
        "投入總金額": 0,
        "最大虧損比例": -100,
        "標的槓桿": 2,
        "即時報價": 120,
        "Beta值": 1.2,
        "產業別": "其他",
        "轉換價格": 50,
        "標的股價": 80,
        "標的代號": "2330",
    }
    row.update(extra)
    return row


def test_product_kind_cbas_is_not_a_plain_cb():
    assert product_kind("可轉債") == "cb"
    assert product_kind("CB") == "cb"
    assert product_kind("可轉債資產交換") == "cbas"
    assert product_kind("CBAS") == "cbas"
    assert product_kind("台股") is None


def test_conversion_value_and_deltas():
    assert conversion_value_points(80, 50) == 160
    assert conversion_value_amount(80, 50, 100_000, 2) == 320_000
    assert parity_delta(160, 120) == 1
    assert parity_delta(80, 105) == 80 / 105
    assert parity_delta(80, 0) is None
    assert bond_floor_delta(100, 108, 98) == 0.2
    assert bond_floor_delta(90, 102, 98) == 0
    assert bond_floor_delta(160, 102, 98) == 1


def test_cb_mark_to_market_joins_nav_and_delta_joins_exposure():
    valued, accounts, metrics = finalize_valuation(pd.DataFrame([_cb()]), _accounts(), 32.0)
    row = valued.iloc[0]
    # 120 點 × 1,000 × 2 張。損益不含 0.3% 證交稅。
    assert row["即時市值(台幣)"] == 240_000
    assert row["淨未實現損益(原幣)"] == 20_000
    assert row["轉換價值(百元)"] == 160
    assert row["轉換價值(台幣)"] == 320_000
    assert row["轉換溢價(%)"] == (120 / 160 - 1) * 100
    assert row["Delta"] == 1
    assert row["Delta來源"] == "估算(轉換價值/市價)"
    assert row["實質曝險(台幣)"] == 320_000
    assert row["Beta曝險(台幣)"] == 320_000 * 1.2
    assert row["標的槓桿"] == 1
    assert accounts.iloc[0]["帳戶淨值"] == 50_000 + 240_000
    assert metrics["nav"] == 290_000
    assert metrics["exposure"] == 320_000
    assert metrics["leverage"] == 320_000 / 290_000
    assert metrics["cash"] == 50_000
    assert metrics["beta"] == (320_000 * 1.2) / 290_000
    assert metrics["cb_missing_delta"] == []
    assert metrics["cb_missing_terms"] == []
    assert "可轉債 Delta 曝險" in str(row["Beta來源"])


def test_sheet_delta_overrides_the_estimate_and_cash_price_is_scaled():
    row = _cb(Delta=0.25, 即時報價=120_000, 平均成本=110_000, 純債價值=98)
    valued, _accounts_out, metrics = finalize_valuation(pd.DataFrame([row]), _accounts(), 32.0)
    got = valued.iloc[0]
    assert got["即時報價"] == 120
    assert got["平均成本"] == 110
    assert got["Delta"] == 0.25
    assert got["Delta來源"] == "試算表"
    assert got["實質曝險(台幣)"] == 0.25 * 320_000
    assert metrics["exposure"] == 0.25 * 320_000


def test_bond_floor_delta_when_sheet_has_no_delta():
    valued, _a, _m = finalize_valuation(pd.DataFrame([_cb(即時報價=108, 標的股價=50, 純債價值=98, Delta=None)]), _accounts(), 32.0)
    # 轉換價值百元 = 50/50×100 = 100；權利金 = 108−98 = 10；內含 = 2 → Delta 0.2
    assert valued.iloc[0]["Delta"] == 0.2
    assert valued.iloc[0]["Delta來源"] == "估算(債底)"


def test_cbas_premium_is_in_nav_and_sheet_delta_is_the_exposure():
    row = _cb(種類="CBAS", 名稱="上曜四 CBAS", 持有數量=1, 平均成本=12, 即時報價=15, Delta=0.4)
    valued, accounts, metrics = finalize_valuation(pd.DataFrame([row]), _accounts(), 32.0)
    got = valued.iloc[0]
    assert got["即時市值(台幣)"] == 15_000
    assert got["轉換溢價(%)"] != got["轉換溢價(%)"]  # CBAS 不計算轉換溢價率
    assert got["實質曝險(台幣)"] == 0.4 * 80 * (100_000 / 50)
    assert accounts.iloc[0]["帳戶淨值"] == 50_000 + 15_000
    assert metrics["exposure"] == got["實質曝險(台幣)"]
    record = position_records(valued)[0]
    assert record["product"] == "cbas"
    assert record["delta"] == 0.4
    assert record["conversion_value"] == 160_000
    assert record["conversion_value_points"] == 160
    assert record["parity_premium_pct"] is None


def test_cbas_without_delta_keeps_market_value_and_zero_exposure():
    row = _cb(種類="可轉債資產交換", 持有數量=1, 即時報價=15, 平均成本=12)
    row.pop("Delta", None)
    valued, _a, metrics = finalize_valuation(pd.DataFrame([row]), _accounts(), 32.0)
    assert valued.iloc[0]["即時市值(台幣)"] == 15_000
    assert metrics["exposure"] == 0
    assert metrics["nav"] == 65_000
    assert metrics["cb_missing_delta"] == ["13164"]


def test_cbas_black_scholes_delta_when_vol_and_expiry_exist():
    today = date(2026, 11, 6)
    expiry = date(2027, 11, 6)
    _price, expected, _theta = bs_price_greeks(80, 50, 1.0, 0.0, 0.2, "call")
    mark = mark_convertible(
        {
            "種類": "CBAS",
            "持有數量": 1,
            "平均成本": 10,
            "即時報價": 15,
            "轉換價格": 50,
            "標的股價": 80,
            "波動率": 0.2,
            "到期日": expiry.isoformat(),
            "Beta值": 1,
        },
        today=today,
    )
    assert mark.delta == expected
    assert mark.delta_source == "估算(Black-Scholes)"


def test_missing_stock_price_still_marks_the_bond():
    row = _cb()
    row.pop("標的股價")
    valued, _a, metrics = finalize_valuation(pd.DataFrame([row]), _accounts(), 32.0)
    assert valued.iloc[0]["即時市值(台幣)"] == 240_000
    assert metrics["exposure"] == 0
    assert metrics["cb_missing_terms"] == ["13164"]
    assert metrics["nav"] == 290_000


def test_short_cb_exposure_is_negative_and_gross_is_positive():
    valued, _a, metrics = finalize_valuation(pd.DataFrame([_cb(持有數量=-2, Delta=0.5)]), _accounts(), 32.0)
    row = valued.iloc[0]
    assert row["Beta曝險(台幣)"] < 0
    assert row["實質曝險(台幣)"] == abs(row["Beta曝險(台幣)"]) / 1.2
    assert metrics["exposure"] == row["實質曝險(台幣)"]
    assert metrics["tw_exposure"] == row["Beta曝險(台幣)"]


def test_stock_beside_cb_keeps_the_stock_formula():
    stock = {
        "帳戶": "第一證券",
        "種類": "台股",
        "標的": "2330",
        "名稱": "台積電",
        "持有數量": 1000,
        "平均成本": 100,
        "投入總金額": 100_000,
        "最大虧損比例": -100,
        "標的槓桿": 1,
        "即時報價": 110,
        "Beta值": 1.2,
        "產業別": "先進製程",
    }
    valued, accounts, metrics = finalize_valuation(pd.DataFrame([stock, _cb()]), _accounts(), 32.0)
    tw = valued[valued["標的"] == "2330"].iloc[0]
    assert tw["即時市值(台幣)"] == 110_000
    assert tw["淨未實現損益(原幣)"] == pytest.approx(10_000 - 110_000 * 0.003)
    assert tw["實質曝險(台幣)"] == 110_000
    cb = valued[valued["標的"] == "13164"].iloc[0]
    assert metrics["exposure"] == 110_000 + cb["實質曝險(台幣)"]
    assert accounts.iloc[0]["帳戶淨值"] == 50_000 + 110_000 + 240_000


def test_sheet_quote_is_kept_when_there_is_no_live_bond_price():
    frame = pd.DataFrame([_cb(即時報價=np.nan, 百元報價=118)])
    out = attach_quotes(frame, {"13164": np.nan}, {}, {})
    assert out.iloc[0]["即時報價"] == 118
    assert out.iloc[0]["報價來源"] == "持倉統計百元報價"


def test_quote_fetches_the_stock_not_the_bond_code():
    calls = []

    def fake_price(ticker, is_tw):
        calls.append((ticker, is_tw))
        return 80.0

    def beta_fn(ticker, is_tw):
        return 1.25, "先進製程"

    import portfolio_risk

    original = portfolio_risk._fetch_yahoo_price
    portfolio_risk._fetch_yahoo_price = fake_price
    try:
        _t, price, beta, sector, extras = _quote_one_ex("13164", 23000, beta_fn, kind="可轉債", underlying="2330")
    finally:
        portfolio_risk._fetch_yahoo_price = original
    assert np.isnan(price)
    assert calls == [("2330.TW", True)]
    assert extras["標的現價"] == 80
    assert beta == 1.25
    assert sector == "先進製程"


def test_scenario_book_uses_delta_notional_and_keeps_cash_identity():
    valued, _a, metrics = finalize_valuation(pd.DataFrame([_cb()]), _accounts(), 32.0)
    book = build_book(valued, metrics)
    assert book.cb_tw_mv == 240_000
    assert book.cb_tw_equity == 320_000
    assert book.cb_tw_beta_exp == 320_000 * 1.2
    assert book.gross_exposure == metrics["exposure"]
    assert book.cash_identity_gap == 0
    assert book.amounts["tw_stock"] == 0
    assert "跨市場" in " ".join(book.notes)
