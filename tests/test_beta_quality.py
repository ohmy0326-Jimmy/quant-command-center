"""Beta／曝險品質的合成回歸。不連網、不讀金鑰，也不代表線上持倉。"""

import math

import numpy as np
import pandas as pd
import pytest

from beta_rules import effective_beta, occ_root
from portfolio_risk import (
    estimate_beta_from_closes,
    finalize_valuation,
    market_is_tw,
)
from us_hedge import us_spx_exposure
from order_assistant import Snapshot


FX = 30.0


def _row(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, **extra):
    data = {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": 0.0 if ("期" in kind and "選擇" not in kind) else cost * qty,
        "最大虧損比例": -100,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": "其他",
    }
    data.update(extra)
    return data


def _book(rows, cash=0.0, account="美股戶"):
    accounts = pd.DataFrame([{"帳戶名稱": account, "現金餘額": cash, "融資/質押借款": 0}])
    return finalize_valuation(pd.DataFrame(rows), accounts, FX)


def test_market_uses_kind_not_three_letters():
    assert market_is_tw("美股", "SPY") is False
    assert market_is_tw("美股", "TSM") is False
    assert market_is_tw("美股", "AMD") is False
    assert market_is_tw("美股", "MUU") is False
    assert market_is_tw("台股", "2330") is True
    assert market_is_tw("", "2330.TW") is True
    assert market_is_tw("", "SPY") is None
    assert market_is_tw("", "SOXL") is None


def test_common_sample_ddof_and_minimum():
    index = pd.bdate_range("2024-01-01", periods=300)
    bench_r = np.full(299, 0.01)
    bench_r[::2] = -0.005
    bench = [100.0]
    for value in bench_r:
        bench.append(bench[-1] * (1.0 + value))
    bench = pd.Series(bench, index=index)
    asset = [50.0]
    for value in bench_r:
        asset.append(asset[-1] * (1.0 + 1.25 * value))
    asset = pd.Series(asset, index=index)
    full = estimate_beta_from_closes(asset, bench)
    assert full["status"] == "estimated"
    assert full["n"] == 251
    assert full["beta"] == pytest.approx(1.25)
    assert full["beta"] != pytest.approx(1.25 * full["n"] / (full["n"] - 1))

    short = asset.iloc[-41:]
    missed = estimate_beta_from_closes(short, bench)
    assert missed["status"] == "insufficient"
    assert missed["beta"] is None
    assert missed["n"] < 60

    tail = asset.iloc[-81:]
    enough = estimate_beta_from_closes(tail, bench)
    assert enough["status"] == "estimated"
    assert enough["beta"] == pytest.approx(1.25)
    assert enough["n"] == 80


def test_soxl_counts_three_once_and_soxs_is_negative_gross():
    mv = 100 * 10 * FX
    soxl, _a, m = _book([
        _row("美股戶", "美股", "SOXL", 100, 10, 10, lev=6, beta=8.0, 標的Beta=2.0, 標的Beta來源="SOXX 一年回歸（對 S&P 500）"),
    ])
    row = soxl.iloc[0]
    assert row["實質曝險(台幣)"] == pytest.approx(mv * 3)
    assert row["Beta曝險(台幣)"] == pytest.approx(mv * 3 * 2.0)
    assert row["Beta曝險(台幣)"] != pytest.approx(mv * 3 * 8.0)
    assert row["Beta曝險(台幣)"] != pytest.approx(mv * 9 * 2.0)
    assert m["us_avg_beta"] == pytest.approx(2.0)
    assert m["us_account_beta"] == pytest.approx(m["us_leverage"] * m["us_avg_beta"])

    soxs, _a, metrics = _book([
        _row("美股戶", "美股", "SOXS", 100, 10, 10, lev=3, beta=-8.16, 標的Beta=2.0, 標的Beta來源="SOXX 一年回歸（對 S&P 500）"),
    ])
    inverse = soxs.iloc[0]
    assert inverse["採用槓桿"] == pytest.approx(3.0)
    assert inverse["有效Beta"] == pytest.approx(-6.0)
    assert inverse["實質曝險(台幣)"] == pytest.approx(mv * 3)
    assert inverse["Beta曝險(台幣)"] == pytest.approx(mv * -6.0)
    assert metrics["us_account_beta"] == pytest.approx(-6.0)
    rule = effective_beta("SOXS", "美股", 3, -8.16, 2.0, "SOXX 一年回歸（對 S&P 500）")
    assert rule["signed_lev"] == pytest.approx(-3.0)
    assert rule["sens"] == pytest.approx(-6.0)


def test_missing_beta_is_not_one():
    frame, _a, metrics = _book([_row("美股戶", "美股", "NVDA", 10, 10, 10, beta=np.nan)])
    row = frame.iloc[0]
    assert math.isnan(float(row["Beta曝險(台幣)"]))
    assert row["實質曝險(台幣)"] == pytest.approx(10 * 10 * FX)
    assert metrics["beta_complete"] is False
    assert metrics["unknown_beta_symbols"] == ["NVDA"]
    assert metrics["us_beta_exposure"] == pytest.approx(0.0)
    assert metrics["us_exposure"] == pytest.approx(0.0)


def test_option_never_uses_premium_as_underlying():
    premium = 0.10
    spot = 25.0
    missing, _a, metrics = _book([
        _row("美股戶", "美股選擇權", "MUU261030C00045000", -1, 0.39, premium, Delta=0.40),
    ])
    assert math.isnan(float(missing.iloc[0]["實質曝險(台幣)"]))
    assert metrics["exposure_complete"] is False
    snap = Snapshot(pd.DataFrame([
        _row("美股戶", "美股選擇權", "MUU261030C00045000", -1, 0.39, premium, Delta=0.40),
    ]), pd.DataFrame([{"帳戶名稱": "美股戶", "現金餘額": 0, "融資/質押借款": 0}]), FX, 20000)
    hedge = us_spx_exposure(snap)
    assert hedge["notional_twd"] == pytest.approx(0.0)
    assert hedge["incomplete_rows"] == ["MUU261030C00045000"]

    priced, _a, _m = _book([
        _row(
            "美股戶", "美股選擇權", "MUU261030C00045000", -1, 0.39, premium,
            Delta=0.40, 標的現價=spot, 標的代號="MUU", 標的Beta=1.8,
            標的Beta來源="MU 一年回歸（對 S&P 500）",
        ),
    ])
    delta_notional = 0.40 * -1 * 100 * spot * FX
    row = priced.iloc[0]
    assert row["實質曝險(台幣)"] == pytest.approx(abs(delta_notional) * 2)
    assert row["Beta曝險(台幣)"] == pytest.approx(delta_notional * 2 * 1.8)
    assert abs(row["Beta曝險(台幣)"]) != pytest.approx(abs(0.40 * -1 * 100 * premium * FX))


def test_short_equity_nav_is_signed_and_gross_notional_stays_positive():
    flat, _a, calm = _book([_row("美股戶", "美股", "NVDA", -100, 10, 10, beta=1.5)], cash=0)
    up, _a, higher = _book([_row("美股戶", "美股", "NVDA", -100, 10, 12, beta=1.5)], cash=0)
    assert calm["nav"] == pytest.approx(-30000.0)
    assert higher["nav"] < calm["nav"]
    assert flat.iloc[0]["Beta曝險(台幣)"] == pytest.approx(-45000.0)
    assert flat.iloc[0]["實質曝險(台幣)"] == pytest.approx(30000.0)
    _flat_cash, _a, funded = _book([_row("美股戶", "美股", "NVDA", -100, 10, 10, beta=1.5)], cash=30000)
    assert funded["nav"] == pytest.approx(0.0)
    assert funded["us_exposure"] == pytest.approx(-45000.0)


def test_mixed_account_does_not_invent_a_cash_split():
    rows = [
        _row("混合戶", "美股", "NVDA", 10, 10, 10, beta=2.0),
        _row("混合戶", "台股", "2330", 10, 100, 100, beta=1.0),
    ]
    accounts = pd.DataFrame([{"帳戶名稱": "混合戶", "現金餘額": 10000, "融資/質押借款": 0}])
    _df, _a, metrics = finalize_valuation(pd.DataFrame(rows), accounts, FX)
    assert metrics["nav"] == pytest.approx(14000.0)
    assert metrics["us_nav"] == pytest.approx(0.0)
    assert metrics["tw_nav"] == pytest.approx(0.0)
    assert metrics["unallocated_nav"] == pytest.approx(14000.0)
    assert metrics["sleeve_nav_complete"] is False
    assert metrics["beta_basis"] == "total_nav"
    assert metrics["us_beta"] == pytest.approx(6000 / 14000)
    assert metrics["tw_beta"] == pytest.approx(1000 / 14000)
    assert metrics["beta"] == pytest.approx(7000 / 14000)


def test_occ_root():
    assert occ_root("MUU261030C00045000") == "MUU"
    assert occ_root("2330") is None
