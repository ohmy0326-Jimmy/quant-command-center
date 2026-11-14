"""熊市預警的每一條規則。缺資料是未知，不算通過，也不改 REGIME_RISK。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from api.schemas import PortfolioResponse
from bear_warning import (
    CAPTION,
    LEADERS,
    THRESHOLDS,
    assess,
    breadth_from_closes,
    breadth_percent,
    cached_price_bundle,
    clear_bear_cache,
    closes_to_bundle,
    curve_indicator,
    defensive_indicator,
    high_beta_indicator,
    hy_widening_indicator,
    index_trend_indicator,
    leaders_indicator,
    load_bear_warning,
    overall_state,
    parse_symbol_column,
    read_stored_breadth,
    record_breadth,
    render_bear_panel,
    vix_indicator,
)
from macro_cycle import REGIME_RISK
from portfolio_briefing import portfolio_payload

AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _rising(n=250, start=100.0, step=0.2):
    return [start + step * index for index in range(n)]


class RuleTests(unittest.TestCase):
    def test_regime_table_is_unchanged(self):
        self.assertEqual(
            {name: item["target_leverage"] for name, item in REGIME_RISK.items()},
            {"牛": 2.0, "偏多": 1.8, "中性": 1.5, "偏空": 1.2, "熊": 1.0},
        )

    def test_missing_inputs_are_unknown_and_not_a_pass(self):
        report = assess({"regime": "偏多"})
        self.assertEqual(report["caption"], CAPTION)
        self.assertTrue(all(layer["state"] == "unknown" for layer in report["layers"]))
        self.assertEqual(report["suggested_cap"], 1.8)
        self.assertEqual(report["suggested_action"], "預警未亮，沿用牛熊分數的槓桿上限。")
        self.assertIsNone(assess({})["suggested_cap"])
        self.assertEqual(curve_indicator("利差", "T10Y2Y", None)["state"], "unknown")
        self.assertEqual(defensive_indicator({})["state"], "unknown")
        self.assertEqual(leaders_indicator([True, None])["state"], "unknown")
        self.assertEqual(high_beta_indicator(None, -0.01)["state"], "unknown")
        self.assertEqual(index_trend_indicator("指數", [1, 2, 3], "測試")["state"], "unknown")
        self.assertEqual(hy_widening_indicator(None)["state"], "unknown")
        self.assertEqual(vix_indicator([20.0] * 3)["state"], "unknown")

    def test_each_trigger(self):
        self.assertEqual(curve_indicator("利差", "T10Y2Y", -0.2)["state"], "red")
        self.assertEqual(curve_indicator("利差", "T10Y2Y", 0.1)["state"], "yellow")
        self.assertEqual(curve_indicator("利差", "T10Y2Y", 1.0)["state"], "green")
        self.assertEqual(hy_widening_indicator(100)["state"], "red")
        self.assertEqual(hy_widening_indicator(50)["state"], "yellow")
        self.assertEqual(hy_widening_indicator(0)["state"], "green")
        self.assertEqual(vix_indicator([30.0] * 10)["state"], "red")
        self.assertEqual(vix_indicator([10.0] * 9 + [26.0])["state"], "yellow")
        self.assertEqual(vix_indicator([12.0] * 10)["state"], "green")
        self.assertEqual(leaders_indicator([True, True, True, False])["state"], "red")
        self.assertEqual(leaders_indicator([True, True, False, False, False])["state"], "yellow")
        self.assertEqual(leaders_indicator([False, False, False, False])["state"], "green")
        self.assertEqual(high_beta_indicator(-0.05, 0.0)["state"], "red")
        self.assertEqual(high_beta_indicator(-0.01, 0.0)["state"], "yellow")
        self.assertEqual(high_beta_indicator(0.02, 0.0)["state"], "green")
        falling = [200.0] * 220 + [100.0]
        self.assertEqual(index_trend_indicator("指數", falling, "測試")["state"], "red")
        rising = _rising(250)
        self.assertEqual(index_trend_indicator("指數", rising, "測試")["state"], "green")

    def test_breadth_divergence_and_proxy_label(self):
        closes = {"A": _rising(200, 10, 0.1), "B": [5.0] * 200, "C": [1.0] * 10}
        percent, counted = breadth_percent(closes)
        self.assertEqual(counted, 2)
        self.assertAlmostEqual(percent, 50.0)
        report = assess({
            "regime": "牛",
            "breadth_us": 40.0,
            "breadth_us_prior": 60.0,
            "spx_last": 99.0,
            "spx_high": 100.0,
            "breadth_tw": 70.0,
            "breadth_tw_prior": 70.0,
            "twii_last": 100.0,
            "twii_high": 100.0,
        })
        us = report["breadth"]["us"]
        tw = report["breadth"]["tw"]
        self.assertEqual(us["state"], "red")
        self.assertEqual(tw["state"], "green")
        self.assertIn("0050", report["breadth"]["tw_universe"])
        self.assertIn("標普 500", report["breadth"]["us_universe"])
        self.assertEqual(THRESHOLDS["near_high_pct"], 2.0)

    def test_defensive_rotation(self):
        def series(base, weekly):
            values = [100.0]
            for change in weekly:
                for _ in range(5):
                    values.append(values[-1] * (1 + change / 5))
            return values
        prices = {
            "SPY": series(100, [0.0, 0.0]),
            "XLV": series(100, [0.02, 0.02]),
            "XLP": series(100, [0.02, 0.02]),
            "XLU": series(100, [0.02, 0.02]),
            "XLK": series(100, [-0.02, -0.02]),
        }
        self.assertEqual(defensive_indicator(prices)["state"], "red")

    def test_caps_take_the_more_conservative(self):
        quiet = assess({"regime": "偏多", "t10y2y": 1.0, "t10y3m": 1.0})
        self.assertEqual(quiet["layers"][0]["state"] in {"yellow", "red", "unknown", "green"}, True)
        leading = assess({
            "regime": "偏多",
            "credit": {"drtscilm_light": "red", "hy_light": "green", "hy_change_bp": 0},
            "t10y2y": 1.0,
            "t10y3m": 1.0,
            "prices": {"SPY": _rising(20), "XLV": _rising(20), "XLP": _rising(20), "XLU": _rising(20), "XLK": _rising(20)},
        })
        self.assertEqual(leading["layers"][0]["state"], "red")
        self.assertEqual(leading["warning_cap"], 1.5)
        self.assertEqual(leading["suggested_cap"], 1.5)
        both = assess({
            "regime": "偏多",
            "credit": {"drtscilm_light": "red", "hy_light": "green", "hy_change_bp": 0},
            "t10y2y": 1.0,
            "t10y3m": 1.0,
            "prices": {"SPY": _rising(40), "XLV": _rising(40), "XLP": _rising(40), "XLU": _rising(40), "XLK": _rising(40)},
            "breadth_us": 30,
            "breadth_us_prior": 50,
            "spx_last": 80,
            "spx_high": 100,
            "breadth_tw": 40,
            "breadth_tw_prior": 40,
            "twii_last": 80,
            "twii_high": 100,
            "leader_below": [False, False, False, False],
            "sphb_return": 0.01,
            "splv_return": 0.0,
        })
        self.assertEqual(both["suggested_cap"], 1.2)
        self.assertIn("加避險", both["suggested_action"])
        confirmed = assess({"regime": "牛", "credit": {"hy_change_bp": 120}})
        self.assertEqual(confirmed["layers"][2]["state"], "red")
        self.assertEqual(confirmed["suggested_cap"], 1.0)
        self.assertIn("只留核心與避險", confirmed["suggested_action"])
        self.assertEqual(LEADERS[0], "AAPL")

    def test_api_object(self):
        payload = portfolio_payload(
            pd.DataFrame(),
            {"nav": 3_140_000, "exposure": 6_500_000, "leverage": 2.07, "cash_pct": 10, "beta": 1, "exposure_complete": True},
            usdtwd=32.0,
            as_of=AS_OF,
            regime="偏多",
        )
        self.assertEqual(payload["schema_version"], "1.15.0")
        self.assertEqual(payload["bear_warning"]["caption"], CAPTION)
        self.assertEqual(payload["bear_warning"]["suggested_cap"], 1.8)
        self.assertEqual(payload["bear_warning"]["overall"], "unknown")
        PortfolioResponse(**payload)


class PriceAndLightTests(unittest.TestCase):
    def tearDown(self):
        clear_bear_cache()

    def test_missing_fetcher_result_is_not_locked_as_a_full_day_success(self):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            if calls["n"] == 1:
                return {}
            closes = {symbol: _rising(260) for symbol in ("SPY", "XLV", "XLP", "XLU", "XLK", "^GSPC", "^VIX")}
            return closes_to_bundle(closes)

        from datetime import date

        import bear_warning

        day = date(2026, 11, 14)
        first = cached_price_bundle(day, fetch)
        self.assertFalse(first.get("SPY"))
        self.assertFalse(bear_warning._PRICE_CACHE.get("ok"))
        bear_warning._PRICE_CACHE["fail_at"] = 0
        second = cached_price_bundle(day, fetch)
        self.assertTrue(second.get("SPY"))
        self.assertEqual(calls["n"], 2)

    def test_core_prices_feed_defensive_and_vix(self):
        from datetime import date

        clear_bear_cache()
        closes = {symbol: _rising(260) for symbol in ("SPY", "XLV", "XLP", "XLU", "XLK", "SPHB", "SPLV", "^GSPC", "^TWII", *LEADERS)}
        closes["^VIX"] = [12.0] * 260
        report = load_bear_warning(
            date(2026, 11, 14),
            regime="偏多",
            credit_board={"rows": []},
            fetch_fred=lambda series_id: [],
            fetch_prices=lambda: closes_to_bundle(closes),
            regime_reader=lambda: None,
            breadth_reader=lambda today: {},
        )
        names = {item["name"]: item["state"] for layer in report["layers"] for item in layer["indicators"]}
        self.assertNotEqual(names["防禦股相對強、科技股相對弱"], "unknown")
        self.assertNotEqual(names["VIX 持續偏高"], "unknown")
        self.assertNotEqual(names["大型權值跌破 60 日線"], "unknown")
        self.assertEqual(names["美股廣度"], "unknown")
        self.assertEqual(report["regime"], "偏多")
        self.assertEqual(report["suggested_cap"], 1.8)

    def test_sheet_regime_is_used_when_memory_view_is_not_the_sheet(self):
        from datetime import date

        from macro_cycle_live import clear_live_view

        clear_bear_cache()
        clear_live_view()
        report = load_bear_warning(
            date(2026, 11, 14),
            regime=None,
            fetch_fred=lambda series_id: [],
            fetch_prices=lambda: {},
            regime_reader=lambda: "熊",
            breadth_reader=lambda today: {},
        )
        self.assertEqual(report["regime"], "熊")
        self.assertEqual(report["regime_cap"], 1.0)

    def test_overall_light_uses_the_worst_known_layer(self):
        self.assertEqual(overall_state([{"state": "unknown"}, {"state": "unknown"}, {"state": "unknown"}]), "unknown")
        self.assertEqual(overall_state([{"state": "green"}, {"state": "unknown"}, {"state": "yellow"}]), "yellow")
        self.assertEqual(overall_state([{"state": "green"}, {"state": "red"}, {"state": "unknown"}]), "red")
        confirmed = assess({"regime": "牛", "credit": {"hy_change_bp": 120}})
        self.assertEqual(confirmed["overall"], "red")
        self.assertEqual(confirmed["layers"][0]["state"], "unknown")

    def test_panel_paints_grey_unknown_and_a_red_overall(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        script = """
import streamlit as st
from bear_warning import assess, render_bear_panel
render_bear_panel(st, assess({"regime": "牛", "credit": {"hy_change_bp": 120}}))
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(
            str(getattr(item, "value", "") or "")
            for item in list(at.error) + list(at.warning) + list(at.success) + list(at.markdown) + list(at.subheader)
        )
        self.assertIn("熊市預警燈", blob)
        self.assertIn("總燈", blob)
        self.assertIn("紅", blob)
        self.assertIn("未知", blob)
        self.assertIn("第一層", blob)
        self.assertIn("第三層", blob)

    def test_page1_panel_is_outside_the_chart_error(self):
        from pathlib import Path

        text = Path("Dashboard.py").read_text(encoding="utf-8")
        chart_error = text.index("總經圖表載入失敗")
        panel = text.index("render_bear_panel")
        self.assertGreater(panel, chart_error)
        self.assertIn("下面的燈是未知，不是通過", text)

    def test_breadth_round_trip_skips_blanks_and_rejects_stale(self):
        from datetime import date

        from tests.test_nav_snapshot import FakeBook

        book = FakeBook({})
        skipped = record_breadth(book, "2026-11-14", {"breadth_us": None, "breadth_tw": None}, "t")
        self.assertEqual(skipped["status"], "skipped")
        self.assertNotIn("Bear_Breadth", book.tabs)
        written = record_breadth(
            book,
            "2026-11-14",
            {"breadth_us": 42.5, "breadth_us_prior": 55.0, "breadth_us_count": 480, "breadth_tw": None, "breadth_tw_prior": None, "breadth_tw_count": 0},
            "2026-11-14 15:00:00",
        )
        self.assertEqual(written["status"], "written")
        found = read_stored_breadth(book, date(2026, 11, 16))
        self.assertAlmostEqual(found["breadth_us"], 42.5)
        self.assertIsNone(found["breadth_tw"])
        self.assertEqual(read_stored_breadth(book, date(2026, 11, 20)), {})
        current, prior, counted = breadth_from_closes({"A": _rising(220), "B": [1.0] * 220})
        self.assertEqual(counted, 2)
        self.assertIsNotNone(current)
        self.assertIsNotNone(prior)

    def test_symbol_csv_uses_yahoo_tickers(self):
        text = "Symbol,Security\nBRK.B,Berkshire\nAAPL,Apple\n"
        self.assertEqual(parse_symbol_column(text), ["BRK-B", "AAPL"])


if __name__ == "__main__":
    unittest.main()
