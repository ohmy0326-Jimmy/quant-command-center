import pytest

import stock_futures_margin


@pytest.fixture(autouse=True)
def _offline_taifex_stock_margin(monkeypatch):
    """測試不連期交所：保證金一覽表一律抓不到，走預設比例，除非個別測試自己換掉。"""
    def _fail(*_a, **_k):
        raise OSError("offline in tests")

    stock_futures_margin.clear_cache()
    monkeypatch.setattr(stock_futures_margin, "_http_get", _fail)
    yield
    stock_futures_margin.clear_cache()
