"""避險與劇本共用結果，以及實際 Beta 對預設區間。"""

import pandas as pd

from sim_panel import beta_status, shared_result_rows


def test_missing_beta_is_incomplete_not_zero():
    status = beta_status({}, "中性", "TW")
    assert status["beta"] is None
    assert status["complete"] is False
    assert status["band"] == (0.70, 1.00)
    assert status["note"] == "不完整"


def test_actual_beta_uses_market_and_default_band():
    tw = beta_status({"tw_beta": 1.25, "us_beta": 0.4}, "牛", "TW")
    us = beta_status({"tw_beta": 1.25, "us_beta": 0.4}, "牛", "US")
    assert tw["beta"] == 1.25
    assert tw["band"] == (1.10, 1.40)
    assert us["beta"] == 0.4
    assert us["complete"] is True
    unknown = beta_status({"tw_beta": 1.0}, "不存在", "TW")
    assert unknown["band"] is None
    assert unknown["complete"] is False


def test_shared_table_keeps_both_and_skips_blanks():
    hedge = pd.DataFrame({"期末淨值": [100.0], "最大回撤": [None]}, index=["期貨避險（觸發）"])
    scenario = pd.DataFrame([{"策略": "A", "期末淨值中位數": 90.0, "中位最大回撤": -0.08}])
    rows = shared_result_rows(hedge, scenario)
    assert [row["面板"] for row in rows] == ["台股避險試算", "淨值劇本"]
    assert rows[0]["最大回撤"] is None
    assert rows[0]["工具"] == "台指期／小台／TXO"
    assert shared_result_rows(None, None) == []
