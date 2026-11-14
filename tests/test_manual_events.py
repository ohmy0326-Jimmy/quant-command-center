"""手動事件併入第三頁。不自編官方沒有的日期，結算不重複。"""

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

import calendar_events as cal
import calendar_view


FETCHED = datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


class ManualEventTests(unittest.TestCase):
    def test_seed_rows_and_msci_tag_without_a_second_settlement(self):
        model = cal.build_calendar([], "", {}, None, date(2026, 11, 14), FETCHED)
        titles = [event["title"] for event in model["events"]]
        msci_manual = next(event for event in model["events"] if "11/30 收盤" in event["title"])
        self.assertEqual(msci_manual["date"], date(2026, 11, 30))
        self.assertEqual(msci_manual["scenario_card"], "S18")
        self.assertEqual(msci_manual["source"], "使用者提供")
        official = next(event for event in model["events"] if event["title"] == "MSCI 2026年11月指數檢討生效")
        self.assertEqual(official["date"], date(2026, 12, 1))
        self.assertEqual(official["scenario_card"], "S18")
        nvidia = next(event for event in model["events"] if event["title"] == "NVIDIA 財報")
        self.assertEqual(nvidia["date"], date(2026, 11, 18))
        self.assertIn("05:20", nvidia["when_text"])
        minutes = next(event for event in model["events"] if event["title"] == "FOMC 會議紀要")
        self.assertIsNone(minutes["date"])
        self.assertEqual(minutes["certainty"], "官方日期待確認")
        settlements = [event for event in model["events"] if event["date"] == date(2026, 11, 18) and event["kind"] in {"settlement", "weekly"}]
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0]["kind"], "settlement")
        self.assertIn("MSCI 指數檢討生效（11/30 收盤）", titles)

    def test_undated_minutes_stay_visible(self):
        model = cal.build_calendar([], "", {}, None, date(2026, 11, 14), FETCHED)
        view = calendar_view.shape_calendar(model, date(2026, 11, 14))
        row = next(item for item in view["rows"] if item["事件"] == "FOMC 會議紀要")
        self.assertEqual(row["日期"], "待確認")
        self.assertEqual(row["確定度"], "待確認")
        self.assertEqual(row["劇本卡"], "—")
        tagged = next(item for item in view["rows"] if "11/30" in item["事件"])
        self.assertEqual(tagged["劇本卡"], "S18")


if __name__ == "__main__":
    unittest.main()
