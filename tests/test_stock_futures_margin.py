import pandas as pd

import portfolio_risk
import stock_futures_margin as sfm

HTML = """<table><tr><td>57</td><td>LXF</td><td>2327 </td><td>國巨期貨 </td><td>國巨股份有限公司</td>
<td>級距3</td><td>15.00%</td><td>15.53%</td><td>20.25%</td></tr>
<tr><td>58</td><td>QEF</td><td>2327 </td><td>小型國巨期貨 </td><td>國巨股份有限公司</td>
<td>級距3</td><td>15.00%</td><td>15.53%</td><td>20.25%</td></tr>
<tr><td>1</td><td>DFF</td><td>1101 </td><td>台泥期貨 </td><td>臺灣水泥股份有限公司</td>
<td>級距1</td><td>10.00%</td><td>10.35%</td><td>13.50%</td></tr></table>"""


class _Resp:
    status_code = 200
    encoding = "utf-8"
    text = HTML


def _online(monkeypatch, calls=None):
    def get(*a, **k):
        if calls is not None:
            calls.append(1)
        return _Resp()
    monkeypatch.setattr(sfm, "_http_get", get)
    sfm.clear_cache()


def test_parse_table():
    t = sfm.parse_margin_table(HTML)
    assert t["2327"].tier == "級距3"
    assert abs(t["2327"].initial - 0.2025) < 1e-9
    assert abs(t["1101"].maintenance - 0.1035) < 1e-9


def test_fetch_is_cached(monkeypatch):
    calls = []
    _online(monkeypatch, calls)
    sfm.fetch_margin_table()
    sfm.fetch_margin_table()
    assert len(calls) == 1


def test_small_guoju_margin_from_taifex(monkeypatch):
    _online(monkeypatch)
    # 期交所以前一日結算價計：497.2 × 100 × 20.25% ≈ 10,068
    ini, mnt, src = portfolio_risk._futures_margin_row("小國巨期", 1, 497.2 * 100)
    assert round(ini) == 10068
    assert abs(mnt - 497.2 * 100 * 0.1553) < 1e-6
    assert "期交所" in src and "級距3" in src


def test_fallback_default_when_offline():
    ini, _m, src = portfolio_risk._futures_margin_row("小國巨期", 1, 44850)
    assert abs(ini - 44850 * sfm.DEFAULT_INITIAL_RATIO) < 1e-6
    assert "預設" in src


def test_ledger_override_wins(monkeypatch):
    _online(monkeypatch)
    ini, mnt, src = portfolio_risk._futures_margin_row("小國巨期", 2, 89700, {"S2327": (10068.0, "設定列每口原始保證金")})
    assert ini == 20136
    assert 0 < mnt < ini
    assert "10,068" in src


def test_finalize_uses_ledger_setting_and_shows_source():
    from futures_ledger import _encode_margin
    pos = pd.DataFrame([{"帳戶": "元大期貨", "種類": "台股期貨", "標的": "小國巨期", "名稱": "", "持有數量": 1,
                         "平均成本": 448.5, "投入總金額": 44850, "最大虧損比例": 0, "標的槓桿": 1,
                         "即時報價": 448.5, "Beta值": 1.7, "產業別": "被動元件"}])
    acc = pd.DataFrame([{"帳戶名稱": "元大期貨", "幣別": "TWD", "現金餘額": 100000, "融資負債": 0,
                         "_yuanta_margin_per_lot": _encode_margin({"S2327": 10068.0}, {"S2327": "設定列每口原始保證金"})}])
    valued, _a, _m = portfolio_risk.finalize_valuation(pos, acc, 32.0)
    row = valued.iloc[0]
    assert row["顯示_投入總金額(原幣)"] == 10068
    assert "10,068" in row["保證金來源"]
