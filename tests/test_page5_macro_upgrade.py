import unittest
from datetime import date

from macro_cycle import manual_indicator_schedule, next_release_date, quadrant_change_text
from macro_cycle_sheet import exposure_by_industry
from macro_cycle_ui import marker_size
from portfolio_risk import DETAILED_SECTOR_MAP
from sector_map import ETF_INDUSTRY_MAP, industry_for_ticker, sector_for_ticker

INDUSTRIES = [
    {"name": "記憶體", "tickers": ["MU"]},
    {"name": "AI 算力", "tickers": ["NVDA"]},
    {"name": "先進製程", "tickers": ["2330.TW"]},
]


class SharedSectorMapTest(unittest.TestCase):
    def test_etfs_map_to_page5_industries(self):
        self.assertEqual(industry_for_ticker("MUU"), "記憶體")
        self.assertEqual(industry_for_ticker("soxl"), "AI 算力")
        self.assertIsNone(industry_for_ticker("00663L"))
        self.assertEqual(sector_for_ticker("00663L"), "台股槓桿ETF")
        self.assertEqual(sector_for_ticker("2330.TW"), "先進製程")

    def test_page4_uses_same_table(self):
        for ticker, name in ETF_INDUSTRY_MAP.items():
            self.assertEqual(DETAILED_SECTOR_MAP[ticker], name)

    def test_exposure_falls_back_to_shared_map(self):
        positions = [{"ticker": "MUU", "mv": 300}, {"ticker": "MU", "mv": 100}, {"ticker": "00663L", "mv": 600}]
        exposure, grand = exposure_by_industry(positions, INDUSTRIES)
        self.assertEqual(grand, 1000)
        self.assertEqual(exposure["記憶體"]["mv"], 400)
        self.assertNotIn("AI 算力", exposure)


class QuadrantTest(unittest.TestCase):
    def test_change_text(self):
        self.assertEqual(quadrant_change_text({"cycle": "趨緩", "prev_cycle": "成長"}), "成長→趨緩")
        self.assertEqual(quadrant_change_text({"cycle": "成長", "prev_cycle": "成長"}), "成長→成長")
        self.assertEqual(quadrant_change_text({"cycle": "成長"}), "—")

    def test_marker_size_scales(self):
        exposure = {"a": {"mv": 400}, "b": {"mv": 100}}
        self.assertEqual(marker_size(0, exposure), 12.0)
        self.assertEqual(marker_size(400, exposure), 46.0)
        self.assertLess(marker_size(100, exposure), marker_size(400, exposure))
        self.assertEqual(marker_size(100, {}), 12.0)


class ReleaseScheduleTest(unittest.TestCase):
    def test_pmi_first_business_day(self):
        # 2026-11-01 是週日，下一個月第一個工作日 12/1（週二）
        self.assertEqual(next_release_date("pmi", date(2026, 11, 4)), "2026-12-01")
        self.assertEqual(next_release_date("pmi", date(2026, 10, 30)), "2026-11-02")

    def test_tw_activity_and_unknown(self):
        self.assertEqual(next_release_date("tw_activity", date(2026, 11, 4)), "2026-11-27")
        self.assertEqual(next_release_date("valuation", date(2026, 11, 4)), "未知")

    def test_schedule_rows_skip_auto(self):
        rows = manual_indicator_schedule(
            [
                {"id": "vix", "name": "VIX", "source": "auto", "updated_at": "2026-11-01"},
                {"id": "pmi", "name": "製造業 PMI", "source": "manual", "updated_at": "2026-10-26"},
                {"id": "valuation", "name": "評價", "source": "manual", "updated_at": ""},
            ],
            date(2026, 11, 4),
        )
        self.assertEqual([r["指標"] for r in rows], ["製造業 PMI", "評價"])
        self.assertEqual(rows[1]["最近更新"], "未知")
        self.assertEqual(rows[1]["下次公布（預估）"], "未知")


if __name__ == "__main__":
    unittest.main()
