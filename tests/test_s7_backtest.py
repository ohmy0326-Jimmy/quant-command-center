"""S7 只列序列裡真正符合條件的日子。"""

from datetime import date, timedelta

from s7_backtest import s7_events


def _rows(start: date, values: list[float]) -> list[dict]:
    return [{"date": start + timedelta(days=index), "close": value} for index, value in enumerate(values)]


def test_drop_from_peak_is_an_event_and_short_horizon_is_null():
    start = date(2024, 1, 1)
    vix = [20] * 9 + [40] + [33]
    price = [100 + index for index in range(len(vix))]
    # 40 then 33 is a 17.5% drop. Need the peak inside the last 10 days.
    report = s7_events(_rows(start, vix), _rows(start, price), market="測試")
    assert report["ok"] is True
    assert len(report["events"]) == 1
    event = report["events"][0]
    assert event["date"] == (start + timedelta(days=10)).isoformat()
    assert event["horizons"]["5"]["return"] is None
    assert event["horizons"]["5"]["max_drawdown"] is None
    assert event["horizons"]["5"]["note"] == "資料不足"
    assert event["horizons"]["60"]["return"] is None


def test_forward_return_and_drawdown_use_only_available_days():
    start = date(2024, 3, 1)
    vix = [20] * 8 + [40, 40, 30] + [20] * 30
    price = [100] * 11 + [110, 80, 90, 100, 130] + [130] * 25
    report = s7_events(_rows(start, vix), _rows(start, price), market="測試")
    event = report["events"][0]
    assert event["date"] == (start + timedelta(days=10)).isoformat()
    assert event["horizons"]["5"]["return"] == 130 / 100 - 1
    assert event["horizons"]["5"]["max_drawdown"] < 0
    assert event["horizons"]["60"]["return"] is None
    assert event["horizons"]["60"]["note"] == "資料不足"


def test_empty_series_does_not_invent_dates():
    report = s7_events([], [], market="美股")
    assert report["ok"] is False
    assert report["events"] == []
    assert "資料不足" in report["note"]


def test_high_vix_without_a_drop_is_not_an_event():
    start = date(2024, 6, 1)
    rows = _rows(start, [40] * 12)
    report = s7_events(rows, rows, market="測試")
    assert report["ok"] is True
    assert report["events"] == []
