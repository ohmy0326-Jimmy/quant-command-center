import pandas as pd

import portfolio_risk
from sector_map import industry_for_ticker, sector_for_ticker


def test_guoju_and_its_futures_are_passive_components():
    for t in ("2327", "2327.TW", "國巨", "國巨期", "小國巨期", "小型國巨期貨", "2327F", "S2327"):
        assert sector_for_ticker(t) == "被動元件", t
        assert industry_for_ticker(t) == "被動元件", t


def test_stock_future_inherits_underlying_sector():
    assert sector_for_ticker("台積電期") == sector_for_ticker("2330") == "先進製程"
    assert sector_for_ticker("小型台積電期貨") == "先進製程"


def test_index_futures_unchanged():
    assert sector_for_ticker("MTX") == "小型臺指"
    assert industry_for_ticker("TX") is None


def test_stock_future_quote_path_sets_sector(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "_fetch_yahoo_price", lambda *a, **k: 449.0)

    def beta_fn(ticker, is_tw):
        return 1.7, "其他"

    out = portfolio_risk._quote_one_ex("小國巨期", 23000.0, beta_fn, "台股期貨")
    assert out[1] == 449.0
    assert out[3] == "被動元件"
