"""第五頁 4 週分數趨勢：週對齊、缺週、示例、去重、樣本不足、黃燈邊界。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path

from macro_cycle import load_macro_cycle, regime_for_score, week_change
from macro_cycle_sheet import compose_macro_view, parse_history_rows
from macro_cycle_ui import _paint_quadrant_change
from macro_score_trend import assess_trend, chart_rows, plan_history_upsert
from risk_performance import BRAKE_CUT_PCT, BRAKE_PAUSE_PCT

ROOT = Path(__file__).resolve().parents[1]


def _weeks(start: str, totals: list[int]):
    day = date.fromisoformat(start)
    return [
        {"week": (day + timedelta(days=7 * index)).isoformat(), "total": total, "regime": regime_for_score(total)}
        for index, total in enumerate(totals)
    ]


class AlignmentTests(unittest.TestCase):
    def test_five_weekly_points_are_four_weeks(self):
        rows = _weeks("2026-01-05", [6, 6, 6, 6, 3])
        trend = assess_trend(rows, "2026-02-02", origin="sheet", example=False)
        self.assertEqual(trend["delta"], -3)
        self.assertEqual(trend["light"], "黃燈")
        self.assertEqual(trend["arrow"], "下降")
        self.assertEqual(trend["count"], 5)
        self.assertAlmostEqual(trend["average"], 5.4)
        self.assertEqual([row["week"] for row in trend["window"]], [row["week"] for row in rows])

    def test_drop_of_two_is_not_yellow(self):
        trend = assess_trend(_weeks("2026-01-05", [6, 6, 6, 6, 4]), "2026-02-02", origin="sheet")
        self.assertEqual(trend["delta"], -2)
        self.assertEqual(trend["light"], "未達黃燈")

    def test_flat_and_up(self):
        flat = assess_trend(_weeks("2026-01-05", [3, 3, 3, 3, 3]), "2026-02-02", origin="sheet")
        up = assess_trend(_weeks("2026-01-05", [1, 1, 1, 1, 4]), "2026-02-02", origin="sheet")
        self.assertEqual(flat["arrow"], "持平")
        self.assertEqual(flat["light"], "未達黃燈")
        self.assertEqual(up["arrow"], "上升")
        self.assertEqual(up["delta"], 3)

    def test_four_sparse_rows_are_not_four_weeks(self):
        rows = [
            {"week": "2026-01-05", "total": 8, "regime": "偏多"},
            {"week": "2026-02-16", "total": 8, "regime": "偏多"},
            {"week": "2026-03-30", "total": 1, "regime": "中性"},
            {"week": "2026-05-04", "total": 1, "regime": "中性"},
        ]
        trend = assess_trend(rows, "2026-05-04", origin="sheet")
        self.assertIsNone(trend["delta"])
        self.assertEqual(trend["light"], "未知")
        self.assertIn("樣本不足", trend["reason"])
        self.assertIsNone(trend["average"])

    def test_missing_week_is_not_filled(self):
        rows = _weeks("2026-01-05", [6, 6, 6, 3])
        rows = [rows[0], rows[1], rows[3]]
        trend = assess_trend(rows, "2026-02-02", origin="sheet")
        self.assertEqual(trend["light"], "未知")
        self.assertTrue(trend["gaps"])
        chart = chart_rows(rows, "2026-02-02")
        self.assertEqual(len(chart), 3)
        self.assertTrue(all(row["average"] is None for row in chart))
        self.assertNotIn(0, [row["total"] for row in chart])
        self.assertNotIn("2026-01-19", [row["week"] for row in chart])

    def test_duplicate_future_invalid_and_scale(self):
        rows = [
            {"week": "2026-01-05", "total": 1, "regime": "中性"},
            {"week": "2026-01-05", "total": 9, "regime": "牛"},
            {"week": "2026-03-01", "total": 1, "regime": "中性"},
            {"week": "不是日期", "total": 1, "regime": "中性"},
            {"week": "2026-01-12", "total": 19, "regime": "牛"},
            {"week": "2026-01-12", "total": 1.5, "regime": "中性"},
            {"week": "2026-01-19", "total": 4, "regime": "熊"},
        ]
        parsed, warnings = parse_history_rows(rows)
        self.assertTrue(any("重複" in warning for warning in warnings))
        trend = assess_trend(rows, "2026-02-01", origin="sheet")
        self.assertNotIn("2026-03-01", [point["week"] for point in trend["window"]])
        self.assertIn("2026-03-01", trend["future"])
        self.assertTrue(any("重複" in note for note in trend["notes"]))
        self.assertNotEqual(trend["light"], "黃燈")
        kept = [point for point in prepare_visible(trend)]
        self.assertNotIn(9, kept)

    def test_repo_example_is_not_a_backtest(self):
        view = load_macro_cycle()
        self.assertEqual([row["week"] for row in view["history"]], ["2026-10-19", "2026-10-26"])
        trend = assess_trend(view["history"], "2026-11-07", origin="json", example=True)
        self.assertEqual(trend["count"], 2)
        self.assertEqual(trend["light"], "未知")
        self.assertTrue(trend["example"])
        self.assertFalse(trend["backtest"]["ready"])
        self.assertEqual(trend["backtest"]["label"], "不可回測")
        self.assertIsNone(trend["backtest"]["performance"])
        self.assertTrue(any("52" in item for item in trend["backtest"]["missing"]))
        self.assertIsNone(view["total"])
        self.assertIsNone(view["week_change"])
        self.assertEqual(week_change(1, view["history"], view["score_updated_at"]), 1)

    def test_sheet_history_stays_separate_from_example(self):
        base = load_macro_cycle()
        sheet = {
            "ok": True,
            "scores": [],
            "industries": [],
            "history": _weeks("2026-01-05", [2, 2, 2, 2, 2]),
            "history_error": "",
        }
        for row in sheet["history"]:
            row["regime"] = "中性"
        view = compose_macro_view(base, sheet, {}, today="2026-02-02")
        self.assertEqual(view["history_origin"], "json")
        self.assertTrue(view["history_example"])
        real_scores = []
        for item in base["indicators"]:
            real_scores.append(
                {
                    "indicator_id": item["id"],
                    "name": item["name"],
                    "score": 0,
                    "source": "manual",
                    "value": "",
                    "trend": "",
                    "note": "",
                    "updated_at": "2026-02-02",
                }
            )
        sheet["scores"] = real_scores
        view = compose_macro_view(base, sheet, {}, today="2026-02-02")
        self.assertEqual(view["history_origin"], "sheet")
        self.assertFalse(view["history_example"])
        self.assertEqual(view["score_trend"]["light"], "未達黃燈")
        self.assertEqual(view["history"][0]["total"], 2)
        self.assertNotEqual(view["history"], base["history"])
        blank = dict(sheet)
        blank["history"] = [{"week": "2026-02-02", "total": "", "regime": ""}]
        unused = compose_macro_view(base, blank, {}, today="2026-02-02")
        self.assertEqual(unused["history_origin"], "sheet")
        self.assertEqual(unused["history"], [])
        self.assertFalse(unused["history_example"])
        self.assertEqual(unused["score_trend"]["count"], 0)
        self.assertEqual(unused["score_trend"]["light"], "未知")

    def test_upsert_does_not_overwrite_and_is_not_enabled(self):
        existing = [{"week": "2026-02-02", "total": 4, "regime": "偏多"}]
        kept = plan_history_upsert(existing, {"week": "2026-02-02", "total": -5, "regime": "偏空"}, "2026-02-02")
        self.assertEqual(kept["action"], "keep")
        self.assertEqual(kept["rows"][0]["total"], 4)
        self.assertFalse(kept["enabled"])
        same = plan_history_upsert(existing, {"week": "2026-02-02", "total": 4, "regime": "偏多"}, "2026-02-02")
        self.assertEqual(same["action"], "keep")
        added = plan_history_upsert(existing, {"week": "2026-02-09", "total": 4, "regime": "偏多"}, "2026-02-09")
        self.assertEqual(added["action"], "insert")
        self.assertFalse(added["enabled"])
        self.assertEqual(len(existing), 1)
        future = plan_history_upsert(existing, {"week": "2026-03-01", "total": 1, "regime": "中性"}, "2026-02-09")
        self.assertEqual(future["action"], "reject")

    def test_missing_score_is_null_and_same_day_rerun_updates_one_row(self):
        blank = plan_history_upsert([], {"week": "2026-02-02", "total": None, "regime": None}, "2026-02-02")
        self.assertEqual(blank["action"], "insert")
        self.assertIsNone(blank["rows"][0]["total"])
        self.assertIsNone(blank["rows"][0]["regime"])
        self.assertNotEqual(blank["rows"][0]["total"], 0)
        kept = plan_history_upsert(blank["rows"], {"week": "2026-02-02", "total": None}, "2026-02-02")
        self.assertEqual(kept["action"], "keep")
        self.assertEqual(len(kept["rows"]), 1)
        filled = plan_history_upsert(blank["rows"], {"week": "2026-02-02", "total": 4, "regime": "偏多"}, "2026-02-02", replace_same_day=True)
        self.assertEqual(filled["action"], "update")
        self.assertEqual(filled["rows"], [{"week": "2026-02-02", "total": 4, "regime": "偏多"}])
        again = plan_history_upsert(filled["rows"], {"week": "2026-02-02", "total": 4, "regime": "偏多"}, "2026-02-02", replace_same_day=True)
        self.assertEqual(again["action"], "keep")
        self.assertEqual(len(again["rows"]), 1)
        preserve = plan_history_upsert(filled["rows"], {"week": "2026-02-02", "total": None}, "2026-02-02", replace_same_day=True)
        self.assertEqual(preserve["action"], "keep")
        self.assertEqual(preserve["total"], 4)

    def test_brake_and_page3_buttons_stay(self):
        self.assertEqual(BRAKE_PAUSE_PCT, -8)
        self.assertEqual(BRAKE_CUT_PCT, -12)
        page = (ROOT / "calendar_page.py").read_text(encoding="utf-8")
        self.assertIn("🧠 AI 解析", page)
        self.assertIn("📲 推播TG", page)
        self.assertIn("📤 推播此事件建議到 TG", page)
        trend = (ROOT / "macro_score_trend.py").read_text(encoding="utf-8")
        self.assertNotIn("send_to_telegram", trend)
        self.assertNotIn("gspread", trend)

    def test_quadrant_change_color(self):
        self.assertIn("#ef6c00", _paint_quadrant_change("成長→趨緩"))
        self.assertEqual(_paint_quadrant_change("成長→成長"), "")
        self.assertEqual(_paint_quadrant_change("—"), "")


def prepare_visible(trend: dict) -> list[int]:
    return [point["total"] for point in trend["window"]]


class TrendAppTest(unittest.TestCase):
    def test_apptest_shows_yellow_and_blocks_backtest(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        yellow = assess_trend(_weeks("2026-01-05", [6, 6, 6, 6, 3]), "2026-02-02", origin="sheet", example=False)
        short = assess_trend(
            [{"week": "2026-10-19", "total": 0, "regime": "中性"}, {"week": "2026-10-26", "total": 1, "regime": "中性"}],
            "2026-11-07",
            origin="json",
            example=True,
        )
        script = f"""
import streamlit as st
from macro_cycle_ui import render_score_trend
render_score_trend(st, {yellow!r}, "來源是試算表 Macro_Score_History。程式只讀，不會用今天重算的分數覆蓋舊週。")
render_score_trend(st, {short!r}, "試算表沒有可用的歷史列，改用倉庫示例。")
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(str(item.value) for item in list(at.caption) + list(at.warning) + list(at.metric))
        self.assertIn("4 週趨勢燈", blob)
        self.assertIn("不是淨值回撤", blob)
        self.assertIn("-8%", blob)
        self.assertIn("-12%", blob)
        self.assertIn("黃燈", blob)
        self.assertIn("不可回測", blob)
        self.assertIn("倉庫示例", blob)
        self.assertIn("樣本不足", blob)
        self.assertNotIn("年化", blob)


if __name__ == "__main__":
    unittest.main()
