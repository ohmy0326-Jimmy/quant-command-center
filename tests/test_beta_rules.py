import math
import pytest



from beta_rules import effective_beta, fetch_underlying_beta
from portfolio_risk import attach_quotes, market_beta_and_sector
from us_hedge import us_spx_exposure
from order_assistant import Snapshot, build_sample_frames, build_sample_snapshot


def test_effective_beta():
    # SOXL lev 3 with live underlying 1.7 -> sens 5.1, is_fallback False
    res = effective_beta("SOXL", "美股", 3.0, None, underlying_beta=1.7)
    assert res["sens"] == pytest.approx(5.1)
    assert not res["is_fallback"]
    
    # without underlying -> 3 * 1.6 = 4.8, is_fallback True, "預設值" in source
    res2 = effective_beta("SOXL", "美股", 3.0, None)
    assert res2["sens"] == pytest.approx(4.8)
    assert res2["is_fallback"]
    assert "預設值" in res2["source"]
    
    # overrides {"SOXX": 2.0} -> 6.0 "手動"
    res3 = effective_beta("SOXL", "美股", 3.0, None, overrides={"SOXX": 2.0})
    assert res3["sens"] == pytest.approx(6.0)
    assert "手動" in res3["source"]
    
    # TW 00631L (kind 台股, lev 2) -> 2.0, source contains "定義"
    res4 = effective_beta("00631L", "台股", 2.0, None)
    assert res4["sens"] == pytest.approx(2.0)
    assert "定義" in res4["source"]
    
    # plain stock beta 1.3 -> 1.3
    res5 = effective_beta("AAPL", "美股", 1.0, 1.3)
    assert res5["sens"] == pytest.approx(1.3)
    
    # 一般股票沒有 Beta 時不假設 1
    res6 = effective_beta("AAPL", "美股", 1.0, None)
    assert res6["sens"] is None
    assert res6["quality"] == "missing"


def test_fetch_underlying_beta():
    class FakeBetaFn:
        def __init__(self, b, s, ok):
            self.b = b
            self.s = s
            self.ok = ok
        def info(self, u, is_tw):
            return self.b, self.s, self.ok
            
    # fake beta_fn having .info returning ok False -> 標的Beta nan
    res1 = fetch_underlying_beta("SOXL", FakeBetaFn(1.5, "Sector", False))
    assert math.isnan(res1["標的Beta"])
    
    # plain 2-tuple fake -> value
    def plain_fake(u, is_tw):
        return 1.5, "Sector"
    res2 = fetch_underlying_beta("SOXL", plain_fake)
    assert res2["標的Beta"] == pytest.approx(1.5)
    
    # non-mapped ticker -> {}
    assert fetch_underlying_beta("AAPL", plain_fake) == {}


def _sample_quoted(extras=None, beta_fallbacks=None):
    positions, accounts, usdtwd, twii = build_sample_frames()
    prices = dict(zip(positions["標的"], positions["即時報價"]))
    betas = dict(zip(positions["標的"], positions["Beta值"]))
    sectors = dict(zip(positions["標的"], positions["產業別"]))
    quoted = attach_quotes(positions, prices, betas, sectors, extras=extras, beta_fallbacks=beta_fallbacks)
    return Snapshot(quoted, accounts, usdtwd, twii)


def test_sample_snapshot_uses_labelled_fallback():
    snap = build_sample_snapshot()
    valued = snap.valued
    soxl = valued[valued["標的"] == "SOXL"].iloc[0]
    assert soxl["有效Beta"] == pytest.approx(4.8)
    assert "預設值" in soxl["Beta來源"]
    assert soxl["Beta曝險(台幣)"] == pytest.approx(soxl["即時市值(台幣)"] * 4.8)
    # 實質曝險（槓桿）不變：市值 × 槓桿
    assert soxl["實質曝險(台幣)"] == pytest.approx(soxl["即時市值(台幣)"] * 3)
    nvda = valued[valued["標的"] == "NVDA"].iloc[0]
    assert nvda["有效Beta"] == pytest.approx(1.7)
    assert [f["標的"] for f in snap.metrics["beta_fallbacks_used"]] == ["SOXL"]
    tw_lev = valued[valued["標的"] == "00663L"].iloc[0]
    assert tw_lev["有效Beta"] == pytest.approx(2.0)
    assert "定義" in tw_lev["Beta來源"]


def test_page4_us_beta_equals_hedge_us_beta():
    """同一組持倉：第四頁（metrics）的美股 Beta 與美股避險區塊一致。"""
    for snap in (
        build_sample_snapshot(),
        _sample_quoted(extras={"SOXL": {"標的代號": "SOXX", "標的Beta": 1.75, "標的Beta來源": "SOXX 一年回歸（對 S&P 500）"}}),
        _sample_quoted(beta_fallbacks={"SOXX": 2.2}),
    ):
        hedge = us_spx_exposure(snap)
        assert hedge["us_beta_spx"] == pytest.approx(snap.metrics["us_beta"])
        assert hedge["exposure_twd"] == pytest.approx(snap.metrics["us_exposure"])
        assert hedge["page4_us_exposure"] == pytest.approx(hedge["exposure_twd"])


def test_attach_quotes_live_and_override():
    live = _sample_quoted(extras={"SOXL": {"標的代號": "SOXX", "標的Beta": 1.5, "標的Beta來源": "SOXX 一年回歸（對 S&P 500）"}})
    soxl = live.valued[live.valued["標的"] == "SOXL"].iloc[0]
    assert soxl["有效Beta"] == pytest.approx(4.5)
    assert "預設值" not in soxl["Beta來源"]
    assert live.metrics["beta_fallbacks_used"] == []

    manual = _sample_quoted(beta_fallbacks={"SOXX": 2.0})
    soxl = manual.valued[manual.valued["標的"] == "SOXL"].iloc[0]
    assert soxl["有效Beta"] == pytest.approx(6.0)
    assert "手動" in soxl["Beta來源"]


def test_revaluation_keeps_fallback_label():
    """Snapshot 再估價（例如試算下單）時，預設值不會被誤標成即時值。"""
    snap = build_sample_snapshot()
    again = Snapshot(snap.valued, snap.accounts, snap.usdtwd, snap.twii)
    soxl = again.valued[again.valued["標的"] == "SOXL"].iloc[0]
    assert "預設值" in soxl["Beta來源"]
    assert again.metrics["us_beta"] == pytest.approx(snap.metrics["us_beta"])


def test_market_beta_and_sector_info():
    assert hasattr(market_beta_and_sector, "info")
