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


def report_text() -> str:
    from bear_warning import NOTE, TW_BREADTH_UNIVERSE

    return NOTE + TW_BREADTH_UNIVERSE


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
        self.assertIn("預警未亮", report["suggested_action"])
        self.assertIn("遲滯資料不足", report["layers"][0]["hysteresis"]["note"])
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

    def test_breadth_uses_listed_advance_share_not_etf_proxy(self):
        closes = {"A": _rising(200, 10, 0.1), "B": [5.0] * 200, "C": [1.0] * 10}
        percent, counted = breadth_percent(closes)
        self.assertEqual(counted, 2)
        self.assertAlmostEqual(percent, 50.0)
        self.assertIn("MI_INDEX", report_text())
        self.assertNotIn("0051", report_text())

    def test_postmortem_rules_and_hysteresis(self):
        from datetime import date, timedelta

        from bear_warning import (
            apply_hysteresis,
            breadth_percent_from_counts,
            foreign_sum,
            hy_gap,
            nfci_rise,
            parse_mi_index_breadth,
            ratio_return,
            tactical_dma_advice,
            tw_breadth_on,
        )

        self.assertIn("2026 覆盤", THRESHOLDS and report_text() or "")
        self.assertEqual(LEADERS, ("NVDA", "2330.TW", "MU", "AVGO"))
        flat = [3.0] * 59 + [3.2]
        self.assertAlmostEqual(hy_gap(flat), 0.2)
        self.assertIsNone(hy_gap([3.0] * 10))
        wide = [3.0] * 59 + [3.4]
        confirmed = [3.0] * 59 + [3.8]
        start = date(2026, 1, 1)
        nfci = [(start + timedelta(days=7 * index), 0.0) for index in range(6)]
        nfci[-1] = (nfci[-1][0], 0.04)
        self.assertAlmostEqual(nfci_rise(nfci), 0.04)
        self.assertIsNone(nfci_rise([(start, 0.1)]))
        self.assertIsNone(foreign_sum([-1e8] * 9))
        self.assertLess(foreign_sum([-200e8] * 10), -1500 * 1e8)
        spy = [100.0] * 30
        rsp = [100.0] * 10 + [97.0] * 20
        self.assertLess(ratio_return(rsp, spy), -0.02)
        self.assertTrue(tw_breadth_on(35, 50))
        self.assertFalse(tw_breadth_on(55, 58))
        self.assertIsNone(tw_breadth_on(55, None))
        self.assertAlmostEqual(breadth_percent_from_counts(30, 70), 30.0)
        parsed = parse_mi_index_breadth({
            "tables": [{"title": "漲跌證券數合計", "fields": ["類型", "整體市場", "股票"], "data": [["上漲(漲停)", "1", "811(28)"], ["下跌(跌停)", "1", "199(3)"]]}],
        })
        self.assertEqual(parsed["up"], 811)
        self.assertEqual(parsed["down"], 199)
        unknown_hyst = apply_hysteresis([False, False], False)
        self.assertFalse(unknown_hyst["on"])
        self.assertFalse(unknown_hyst["sufficient"])
        self.assertEqual(unknown_hyst["note"], "遲滯資料不足")
        turned = apply_hysteresis([True, True], True)
        self.assertTrue(turned["on"])
        self.assertEqual(turned["state"], "red")
        still = apply_hysteresis([True, True, True] + [False] * 8, False)
        self.assertTrue(still["on"])
        off = apply_hysteresis([True, True, True] + [False] * 9, False)
        # 3 on then 10 off: the last False is the 10th if prior has 9 falses.
        self.assertFalse(off["on"])
        self.assertEqual(off["state"], "green")
        gap = apply_hysteresis([True, None], True)
        self.assertTrue(gap["provisional"])
        self.assertEqual(gap["state"], "yellow")
        self.assertEqual(gap["note"], "疑似亮燈（遲滯資料不足）")
        self.assertFalse(gap["sufficient"])
        prices = {"^TWII": _rising(260), "^SOX": [100.0] * 252 + [90.0], "SPY": spy, "RSP": rsp, "SPHB": rsp, "SPLV": spy, "NVDA": [100.0] * 80, "MU": [100.0] * 80, "AVGO": _rising(80), "2330.TW": _rising(80)}
        report = assess({
            "regime": "偏多",
            "hy": wide,
            "nfci": nfci,
            "foreign_nets": [-200e8] * 10,
            "prices": prices,
            "history": {"leading": [True, True], "internal": [False, False], "confirm": [False, False]},
        })
        self.assertEqual(report["layers"][0]["state"], "red")
        self.assertEqual(report["layers"][0]["indicators"][0]["role"], "main")
        self.assertEqual(report["suggested_cap"], 1.5)
        self.assertTrue(all(item["role"] == "watch" for item in report["watches"]))
        self.assertIn("DRTSCILM", " ".join(item["name"] for item in report["watches"]))
        quiet = assess({"regime": "偏多", "hy": flat, "foreign_nets": [0.0] * 10, "prices": {"^TWII": _rising(260), "^SOX": _rising(260), "SPY": _rising(40), "RSP": _rising(40), "SPHB": _rising(40), "SPLV": _rising(40), "NVDA": _rising(80), "MU": _rising(80), "AVGO": _rising(80), "2330.TW": _rising(80)}, "tw_breadth": 70, "tw_breadth_prior": 72})
        self.assertEqual(quiet["layers"][0]["raw_on"], False)
        self.assertEqual(quiet["layers"][0]["state"], "unknown")
        deep = assess({
            "regime": "牛",
            "hy": confirmed,
            "history": {"confirm": [True, True]},
        })
        self.assertEqual(deep["layers"][2]["state"], "red")
        self.assertEqual(deep["suggested_cap"], 1.0)
        self.assertIn("只留核心與避險", deep["suggested_action"])
        advice = tactical_dma_advice(["SOXL", "2330"], {"SOXX": [100.0] * 19 + [90.0]})
        self.assertEqual(len(advice), 1)
        self.assertEqual(advice[0]["underlying"], "SOXX")
        self.assertEqual(advice[0]["advice"], "收破 20 日線減 1/3")
        self.assertEqual(tactical_dma_advice(["00663L"], {})[0]["advice"], "未知")
        provisional = assess({
            "regime": "偏多",
            "hy": [3.0] * 59 + [3.4],
            "foreign_nets": [-200e8] * 10,
        })
        self.assertTrue(provisional["layers"][0]["provisional"])
        self.assertEqual(provisional["layers"][0]["state"], "yellow")
        self.assertTrue(provisional["provisional"])
        self.assertIn("暫行", provisional["suggested_action"])
        self.assertIn("疑似亮燈", provisional["layers"][0]["hysteresis"]["note"])
        self.assertEqual(provisional["suggested_cap"], 1.5)
        self.assertNotIn("預警未亮", provisional["suggested_action"])

    def test_backfill_ignores_same_day_fred_and_skips_weekends(self):
        from datetime import date, timedelta

        from bear_warning import build_backfill_rows
        from tests.test_nav_snapshot import FakeBook
        from bear_warning import record_warning_rows

        today = date(2026, 11, 13)
        days = []
        cursor = today
        while len(days) < 70:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        days = list(reversed(days))
        twii = [(day, 18000.0 + index) for index, day in enumerate(days)]
        hy = [(day, 3.0) for day in days[:-1]] + [(today, 9.0)]
        visible = [value for item_day, value in hy if item_day <= today - timedelta(days=1)]
        self.assertNotIn(9.0, visible)
        rows = build_backfill_rows(today, {"prices": {"^TWII": twii, "^SOX": twii}, "hy": hy, "nfci": [], "foreign": [], "breadth": []}, trading_days=60)
        self.assertGreaterEqual(len(rows), 60)
        self.assertTrue(all(date.fromisoformat(row["date"]).weekday() < 5 for row in rows))
        self.assertNotEqual(rows[-1].get("leading"), True)
        book = FakeBook({})
        record_warning_rows(book, rows[:2], "t")
        again = record_warning_rows(book, rows[:2], "t")
        self.assertEqual(again["status"], "written")
        self.assertEqual(len(book.worksheet("Bear_Warning_Daily").rows), 3)

    def test_chunk_writes_each_day_before_the_next_fetch(self):
        from datetime import date, timedelta

        from bear_warning import backfill_warning_chunk
        from tests.test_nav_snapshot import FakeBook

        today = date(2026, 11, 13)
        days = []
        cursor = today
        while len(days) < 4:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        days = list(reversed(days))
        prices = {"^TWII": [(day, 100.0 + index) for index, day in enumerate(days)]}
        book = FakeBook({})
        seen = {"n": 0}

        def fetch(day):
            seen["n"] += 1
            if seen["n"] >= 2:
                self.assertGreaterEqual(len(book.worksheet("Bear_Warning_Daily").rows), 2)
            if seen["n"] == 3:
                raise TimeoutError("slow")
            return {"foreign_net": -1e9, "up": 100, "down": 80, "errors": []}

        first = backfill_warning_chunk(
            today, book, max_days=8, budget_seconds=60, pause=0,
            prices=prices, hy=[], nfci=[], fetch_day=fetch,
        )
        self.assertEqual(first["rows"], 3)
        self.assertFalse(first["done"])
        self.assertEqual(first["remaining_days"], 1)
        self.assertEqual(first["next_date"], days[2].isoformat())
        self.assertTrue(any("TimeoutError" in item for item in first["failures"]))
        ticks = {"n": 0}

        def clock():
            ticks["n"] += 1
            return 0 if ticks["n"] < 3 else 100

        second_book = FakeBook({})
        second = backfill_warning_chunk(
            today, second_book, max_days=8, budget_seconds=5, pause=0,
            prices=prices, hy=[], nfci=[], fetch_day=lambda day: {"foreign_net": -1.0, "up": 1, "down": 1, "errors": []},
            clock=clock,
        )
        self.assertLess(second["rows"], 4)
        self.assertFalse(second["done"])
        from tests.test_nav_snapshot import FakeBook
        from bear_warning import record_warning_rows, warning_history_from_sheet

        book = FakeBook({})
        written = record_warning_rows(book, [{"date": "2026-11-13", "foreign_net": -20e8, "up": 400, "down": 600}], "t")
        self.assertEqual(written["status"], "written")
        stored = warning_history_from_sheet(book, date(2026, 11, 14))
        self.assertIsNone(stored["foreign_nets"])
        self.assertAlmostEqual(stored["tw_breadth"], 40.0)

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
        both = assess({
            "regime": "偏多",
            "hy": [3.0] * 59 + [3.4],
            "foreign_nets": [-200e8] * 10,
            "prices": {
                "SPY": [100.0] * 30,
                "RSP": [100.0] * 10 + [97.0] * 20,
                "NVDA": [100.0] * 60 + [50.0] * 20,
                "MU": [100.0] * 60 + [50.0] * 20,
                "^TWII": _rising(260),
                "^SOX": _rising(260),
            },
            "history": {"leading": [True, True], "internal": [True, True]},
        })
        self.assertEqual(both["layers"][0]["state"], "red")
        self.assertEqual(both["layers"][1]["state"], "red")
        self.assertEqual(both["suggested_cap"], 1.2)
        self.assertIn("加避險", both["suggested_action"])
        self.assertEqual(LEADERS[0], "NVDA")

    def test_api_object(self):
        payload = portfolio_payload(
            pd.DataFrame(),
            {"nav": 3_140_000, "exposure": 6_500_000, "leverage": 2.07, "cash_pct": 10, "beta": 1, "exposure_complete": True},
            usdtwd=32.0,
            as_of=AS_OF,
            regime="偏多",
        )
        self.assertEqual(payload["schema_version"], "1.17.1")
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
            history_reader=lambda today: {},
        )
        names = {item["name"]: item["state"] for item in report["watches"]}
        self.assertEqual(names["VIX 高於 25"], "green")
        self.assertEqual(report["regime"], "偏多")
        self.assertEqual(report["suggested_cap"], 1.8)
        self.assertTrue(report["layers"][0]["hysteresis"]["note"])

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
            history_reader=lambda today: {},
        )
        self.assertEqual(report["regime"], "熊")
        self.assertEqual(report["regime_cap"], 1.0)

    def test_overall_light_uses_the_worst_known_layer(self):
        self.assertEqual(overall_state([{"state": "unknown"}, {"state": "unknown"}, {"state": "unknown"}]), "unknown")
        self.assertEqual(overall_state([{"state": "green"}, {"state": "unknown"}, {"state": "yellow"}]), "yellow")
        self.assertEqual(overall_state([{"state": "green"}, {"state": "red"}, {"state": "unknown"}]), "red")
        confirmed = assess({"regime": "牛", "hy": [1.0] * 59 + [2.0], "history": {"confirm": [True, True]}})
        self.assertEqual(confirmed["overall"], "red")
        self.assertEqual(confirmed["layers"][0]["state"], "unknown")

    def test_layer_title_counts_only_red_main_signals(self):
        from bear_warning import cap_headline, hysteresis_status_line, layer_light_counts, layer_title

        layer = {
            "id": "leading",
            "name": "第一層 領先",
            "need": 2,
            "indicators": [
                {"role": "main", "state": "red"},
                {"role": "main", "state": "unknown"},
                {"role": "main", "state": "green"},
                {"role": "watch", "state": "red"},
            ],
        }
        self.assertEqual(layer_light_counts(layer), {"lit": 1, "unknown": 1, "total": 3})
        self.assertEqual(layer_title(layer), "第一層 領先｜需≥2 亮｜已亮 1/3｜動作：上限降一級｜未知 1")
        internal = {"id": "internal", "name": "第二層 內部", "need": 2, "indicators": [{"state": "red"}, {"state": "red"}, {"state": "green"}]}
        self.assertEqual(layer_title(internal), "第二層 內部｜需≥2 亮｜已亮 2/3｜動作：再降一級＋避險")
        confirm = {"id": "confirm", "name": "第三層 確認", "need": 1, "indicators": [{"state": "red"}, {"state": "unknown"}]}
        self.assertEqual(layer_title(confirm), "第三層 確認｜需≥1 亮｜已亮 1/2｜動作：只留核心與避險｜未知 1")
        line = hysteresis_status_line({"raw_on": True, "on_streak": 2, "on_days": 3, "state": "yellow", "off_streak": 0, "off_days": 10})
        self.assertEqual(line, "今日成立｜已連續 2 日（需 3 日生效）")
        lit = hysteresis_status_line({"raw_on": True, "on_streak": 3, "on_days": 3, "state": "red", "off_streak": 0, "off_days": 10})
        self.assertIn("熄燈需連續 10 日不成立，目前 0 日", lit)
        self.assertEqual(cap_headline({"suggested_cap": 1.5}), "建議槓桿上限 1.5 倍")
        self.assertEqual(cap_headline({"suggested_cap": None}), "建議槓桿上限 未知")

    def test_layer_block_writes_into_the_container(self):
        from bear_warning import render_bear_panel

        def _texts(node):
            found = []
            for name, args, _kwargs in node.calls:
                found.append(str(name))
                found.extend(str(arg) for arg in args)
            return found

        class Node:
            def __init__(self):
                self.calls = []
                self.children = []

            def _record(self, name, *args, **kwargs):
                self.calls.append((name, args, kwargs))
                return self

            subheader = caption = markdown = warning = __call__ = _record

            def container(self, border=False):
                child = Node()
                child.border = border
                self.children.append(child)
                return child

            def expander(self, label, expanded=False):
                child = Node()
                child.label = label
                child.expanded = expanded
                self.children.append(child)
                return child

        parent = Node()
        render_bear_panel(parent, {
            "overall": "yellow",
            "suggested_cap": 1.2,
            "regime_cap": 1.8,
            "warning_cap": 1.2,
            "suggested_action": "暫行。",
            "layers": [{
                "id": "internal",
                "name": "第二層 內部",
                "need": 2,
                "state": "yellow",
                "provisional": True,
                "hysteresis": {"raw_on": True, "on_streak": 1, "on_days": 3, "state": "yellow", "off_streak": 0, "off_days": 10, "provisional": True},
                "indicators": [{"role": "main", "state": "red", "name": "寬度合成", "value_text": "成立", "threshold": "門檻", "source": "來源"}],
            }],
            "watches": [{"state": "green", "name": "VIX 高於 25", "value_text": "18"}],
        })
        boxes = [child for child in parent.children if getattr(child, "border", False)]
        self.assertEqual(len(boxes), 1)
        self.assertTrue(any("第二層 內部" in text for text in _texts(boxes[0])))
        self.assertTrue(any("寬度合成" in text for text in _texts(boxes[0])))
        self.assertFalse(any("第二層 內部" in text for text in _texts(parent)))
        watch = next(child for child in parent.children if getattr(child, "label", "") == "觀察指標（不升級）")
        self.assertFalse(watch.expanded)
        self.assertTrue(any("VIX" in text for text in _texts(watch)))

    def test_panel_paints_grey_unknown_and_a_red_overall(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        script = """
import streamlit as st
from bear_warning import assess, render_bear_panel
render_bear_panel(st, assess({"regime": "牛", "hy": [1.0] * 59 + [2.0], "history": {"confirm": [True, True]}}))
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(
            str(getattr(item, "value", "") or "")
            for item in list(at.error) + list(at.warning) + list(at.success) + list(at.markdown) + list(at.subheader) + list(at.caption)
        )
        self.assertIn("熊市預警燈", blob)
        self.assertIn("牛熊分數決定槓桿上限", blob)
        self.assertIn("建議槓桿上限", blob)
        self.assertIn("第一層 領先｜需≥2 亮", blob)
        self.assertIn("第三層 確認｜需≥1 亮", blob)
        self.assertIn("今日", blob)
        labels = [str(getattr(item, "label", "") or "") for item in at.expander]
        self.assertIn("觀察指標（不升級）", labels)

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
