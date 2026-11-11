"""自動歷史回補不填手動分數，黃燈只數次數。"""

from datetime import date, timedelta

from macro_history_backfill import append_backfill, plan_backfill_inserts, reconstruct_fridays, yellow_threshold_backtest
from reference_feeds import parse_institution_net, parse_margin_balance, parse_tsmc_revenue, reference_rows


def _fridays(n, start=date(2024, 1, 5)):
    return [start + timedelta(days=7 * index) for index in range(n)]


def test_reconstructed_week_leaves_manual_scores_empty():
    days = _fridays(1)
    bundle = {"vix": [(days[0], 12.0)]}
    row = reconstruct_fridays(bundle, days)[0]
    assert row["source"] == "backfill"
    assert row["total"] is not None
    assert row["total"] != 0
    assert row["coverage"] < 1
    assert row["regime"]


def test_backfill_does_not_overwrite_daily_rows():
    proposed = [
        {"week": "2026-11-07", "total": 4, "regime": "偏多", "coverage": 0.5},
        {"week": "2026-11-14", "total": None, "regime": None, "coverage": 0.2},
    ]
    plan = plan_backfill_inserts([{"week": "2026-11-07", "total": 1, "regime": "中性"}], proposed)
    assert plan["skipped"] == ["2026-11-07"]
    assert [row["week"] for row in plan["insert"]] == ["2026-11-14"]
    assert plan["insert"][0]["source"] == "backfill"
    assert plan["insert"][0]["total"] is None

    class Sheet:
        def __init__(self):
            self.rows = []

        def append_row(self, values, value_input_option=None):
            self.rows.append(values)

    sheet = Sheet()
    append_backfill(sheet, [{"week": "2026-11-07", "total": 1}], proposed)
    assert sheet.rows == [["2026-11-14", "", "", 0.2, "backfill"]]


def test_yellow_counts_a_three_point_drop_without_a_return():
    days = _fridays(60)
    rows = []
    for index, day in enumerate(days):
        total = 8 if index < 40 else 4
        rows.append({"week": day.isoformat(), "total": total, "regime": "偏多"})
    report = yellow_threshold_backtest(rows)
    assert report["weeks"] == 60
    assert report["windows"] > 0
    assert report["yellow"] > 0
    assert report["performance"] is None


def test_reference_missing_is_not_zero_and_unavailable_stay_blank():
    rows = reference_rows({"t10y2y": {"value": -0.2, "as_of": "2026-11-11"}})
    by_name = {row["name"]: row for row in rows}
    assert by_name["美債 10 年減 2 年"]["value"] == -0.2
    assert by_name["MOVE 指數"]["value"] is None
    assert by_name["MOVE 指數"]["note"] == "不完整"
    assert by_name["八大官股"]["value"] is None
    assert "估計" in by_name["外銷訂單"]["note"]
    margin = parse_margin_balance({
        "date": "20261111",
        "tables": [{"fields": ["項目", "今日餘額"], "data": [["融資(交易單位)", "1,234"]]}],
    })
    assert margin["value"] == 1234
    net = parse_institution_net({
        "date": "20261111",
        "fields": ["單位名稱", "買賣差額"],
        "data": [["合計", "-20"]],
    })
    assert net["value"] == -20
    revenue = parse_tsmc_revenue([{"公司代號": "2330", "資料年月": "11509", "營業收入-當月營收": "100"}])
    assert revenue["value"] == 100
    assert parse_tsmc_revenue([{"公司代號": "1101"}]) is None
