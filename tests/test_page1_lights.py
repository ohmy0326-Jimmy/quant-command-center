"""第一頁新增燈號：約定門檻、缺資料不當綠燈、讀數不混進舊分數。"""

import unittest
from datetime import date
from pathlib import Path

import page1_lights as lights
import macro_risk as risk

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 6)


def _obs(day, value):
    return [(day, value)]


def _macro_item(key="vix", points=0, status=risk.STATUS_OK):
    item = risk._base(key)
    item.update(status=status, points=points, value=18.0, value_text="18.00", date="2026-11-06")
    return item


class FredParseTests(unittest.TestCase):
    def test_skips_missing_dots_and_keeps_dates(self):
        text = "observation_date,DFII10\n2026-11-04,2.40\n2026-11-05,.\n2026-11-06,3.10\n"
        found = lights.parse_fred_observations(text)
        self.assertEqual(found, [(date(2026, 11, 4), 2.4), (date(2026, 11, 6), 3.1)])

    def test_empty_csv_is_not_a_zero(self):
        self.assertEqual(lights.parse_fred_observations("observation_date,DFII10\n"), [])


class DfiiTests(unittest.TestCase):
    def test_agreed_thresholds_use_percent_not_tenths(self):
        self.assertEqual(lights.score_dfii10(2.49, risk.STATUS_OK)["light"], "green")
        self.assertEqual(lights.score_dfii10(2.5, risk.STATUS_OK)["light"], "yellow")
        self.assertEqual(lights.score_dfii10(2.99, risk.STATUS_OK)["light"], "yellow")
        self.assertEqual(lights.score_dfii10(3.0, risk.STATUS_OK)["light"], "red")
        self.assertEqual(lights.score_dfii10(3.0, risk.STATUS_OK)["points"], 0)

    def test_missing_and_stale_are_unknown(self):
        self.assertEqual(lights.score_dfii10(None, risk.STATUS_MISSING)["light"], "unknown")
        self.assertEqual(lights.score_dfii10(1.0, risk.STATUS_STALE)["light"], "unknown")
        board = lights.assemble_board([_macro_item()], {"dfii10": None, "dgs30": None, "hy_oas": None, "ig_oas": None}, TODAY)
        self.assertEqual(board["dfii10"]["light"], "unknown")
        self.assertFalse(board["complete"])
        self.assertIn("美國 10 年實質利率", board["unknown_names"])
        text = "\n".join(lights.summary_lines(board, {"score_text": "0 / 11 分", "label": "綠", "level": "green"}))
        self.assertIn("不是綠燈", text)


class ReadingTests(unittest.TestCase):
    def test_high_spread_has_no_invented_light(self):
        board = lights.assemble_board(
            [_macro_item()],
            {
                "dfii10": _obs(TODAY, 2.0),
                "dgs30": _obs(TODAY, 5.5),
                "hy_oas": _obs(TODAY, 8.0),
                "ig_oas": _obs(TODAY, 2.0),
            },
            TODAY,
        )
        by_name = {row["name"]: row for row in board["readings"]}
        self.assertEqual(by_name["高收益債利差"]["light"], "reading")
        self.assertEqual(by_name["高收益債利差"]["light_text"], "無約定門檻")
        self.assertEqual(by_name["高收益債利差"]["unit"], "百分點")
        self.assertIn("BAMLH0A0HYM2", by_name["高收益債利差"]["source"])
        self.assertIn("BAMLC0A0CM", by_name["投資級公司債利差"]["source"])
        self.assertIn("DGS30", by_name["美國 30 年公債殖利率"]["source"])
        self.assertTrue(board["readings_complete"])
        self.assertNotIn("MOVE", " ".join(row["name"] for row in board["rows"] + board["readings"]))

    def test_stale_reading_is_unknown_not_clear(self):
        board = lights.assemble_board(
            [_macro_item()],
            {"dfii10": _obs(TODAY, 2.0), "dgs30": _obs(date(2026, 10, 1), 4.0), "hy_oas": _obs(TODAY, 3.0), "ig_oas": _obs(TODAY, 1.0)},
            TODAY,
        )
        dgs = next(row for row in board["readings"] if row["name"].startswith("美國 30"))
        self.assertEqual(dgs["light"], "unknown")
        self.assertFalse(board["complete"])


class IsolationTests(unittest.TestCase):
    def test_new_lights_do_not_change_telegram_score_module(self):
        text = (ROOT / "alert_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("page1_lights", text)
        self.assertNotIn("DFII10", text)
        self.assertIn("rule_macro", text)

    def test_not_done_lists_real_gaps(self):
        names = [item["name"] for item in lights.NOT_DONE]
        self.assertIn("MOVE 債市波動", names)
        self.assertIn("融資餘額與維持率", names)
        self.assertIn("四大雲端廠資本支出下修", names)


class SummaryRenderTests(unittest.TestCase):
    def test_apptest_shows_unknown_instead_of_green(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        script = """
import streamlit as st
from datetime import date
import page1_lights
board = page1_lights.assemble_board(
    [],
    {"dfii10": None, "dgs30": [(date(2026, 11, 6), 4.5)], "hy_oas": None, "ig_oas": None},
    date(2026, 11, 6),
)
page1_lights.render_page1_summary(st, board, {"score_text": "0 / 11 分", "label": "綠", "level": "green"})
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(str(item.value) for item in list(at.warning) + list(at.error) + list(at.caption))
        self.assertIn("未知", blob)
        self.assertIn("DFII10", blob)
        self.assertIn("不另送 Telegram", blob)


if __name__ == "__main__":
    unittest.main()
