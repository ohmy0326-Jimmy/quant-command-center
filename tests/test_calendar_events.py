"""第三頁行事曆：休市、時區、代號、缺金鑰、來源失敗、部位對照。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import calendar_events as cal
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FETCHED = datetime(2026, 11, 7, 9, 0, tzinfo=cal.TAIPEI)


def _row(**kwargs):
    base = {"帳戶": "甲", "種類": "台股", "標的": "", "名稱": ""}
    base.update(kwargs)
    return base


class HolidayTests(unittest.TestCase):
    def test_february_2026_settlement_is_unknown(self):
        item = cal.monthly_settlement(2026, 2)
        self.assertEqual(item["anchor"], date(2026, 2, 18))
        self.assertIsNone(item["date"])
        self.assertEqual(item["status"], "未知")
        self.assertIn("不自行指定", item["note"])

    def test_non_holiday_third_wednesday_is_estimate(self):
        item = cal.monthly_settlement(2026, 1)
        self.assertEqual(item["date"], date(2026, 1, 21))
        self.assertEqual(item["status"], "規則推估")

    def test_year_without_holiday_table_is_not_a_valid_wednesday(self):
        item = cal.monthly_settlement(2027, 1)
        self.assertIsNone(item["date"])
        self.assertEqual(item["status"], "未知")
        weekly, notes = cal.weekly_settlement_dates(date(2027, 1, 1), date(2027, 1, 31))
        self.assertEqual(weekly, [])
        self.assertTrue(any("2027" in note for note in notes))

    def test_february_window_does_not_invent_a_shifted_settlement(self):
        events, notes = cal.institutional_events(date(2026, 2, 1), date(2026, 2, 28))
        dates = {event["date"] for event in events}
        self.assertNotIn(date(2026, 2, 18), dates)
        self.assertNotIn(date(2026, 2, 23), dates)
        self.assertIn(date(2026, 2, 11), dates)
        self.assertNotIn(date(2026, 2, 25), {event["date"] for event in events if "月選" in event["title"] or "台指期" in event["title"]})
        self.assertTrue(any(event["date"] == date(2026, 2, 25) and event["kind"] == "weekly" for event in events))
        self.assertTrue(any("2026-02-18" in note for note in notes))
        self.assertFalse(any("台積電" in event["title"] or "合約負債" in event["title"] for event in events))

    def test_holiday_wednesday_is_omitted_from_weeklies(self):
        weekly, notes = cal.weekly_settlement_dates(date(2026, 2, 1), date(2026, 2, 28))
        self.assertNotIn(date(2026, 2, 18), {item["date"] for item in weekly})
        self.assertTrue(any("2026-02-18" in note and "不改到" in note for note in notes))

    def test_business_signal_on_holiday_is_unknown(self):
        item = cal.business_indicator_date(2026, 2)
        self.assertEqual(item["anchor"], date(2026, 2, 27))
        self.assertIsNone(item["date"])
        self.assertEqual(item["status"], "未知")


class TimezoneTests(unittest.TestCase):
    def test_after_close_converts_to_taipei(self):
        moment = datetime(2026, 1, 28, 21, 0, tzinfo=timezone.utc)
        clock = cal.session_from_timestamp(moment)
        self.assertEqual(clock["taipei_date"], date(2026, 1, 29))
        self.assertEqual(clock["session"], "美股盤後")
        self.assertIn("美東", clock["note"])

    def test_before_open_is_premarket(self):
        moment = datetime(2026, 1, 28, 12, 0, tzinfo=timezone.utc)
        clock = cal.session_from_timestamp(moment)
        self.assertEqual(clock["taipei_date"], date(2026, 1, 28))
        self.assertEqual(clock["session"], "美股盤前")

    def test_naive_timestamp_is_not_converted(self):
        clock = cal.session_from_timestamp(datetime(2026, 1, 28, 21, 0))
        self.assertIsNone(clock["taipei_date"])
        self.assertEqual(clock["session"], "時段未知")

    def test_date_only_earnings_do_not_invent_session(self):
        event = cal.parse_yahoo_calendar("AAPL", {"Earnings Date": date(2026, 1, 29)}, "US")
        self.assertEqual(event["date"], date(2026, 1, 29))
        self.assertEqual(event["session"], "時段未知")
        self.assertEqual(event["certainty"], "估計")
        self.assertIsNone(event["actual"])


class SymbolTests(unittest.TestCase):
    def test_normalize_tw_and_us(self):
        self.assertEqual(cal.normalize_symbol("2330"), ("2330.TW", "TW"))
        self.assertEqual(cal.normalize_symbol("2330.tw"), ("2330.TW", "TW"))
        self.assertEqual(cal.normalize_symbol(" AAPL "), ("AAPL", "US"))
        self.assertEqual(cal.normalize_symbol("13164"), ("", ""))

    def test_etf_and_stock_future_and_cb(self):
        rows = [
            _row(標的="0050", 名稱="元大台灣50"),
            _row(種類="期貨", 標的="2330期", 名稱="台積電期"),
            _row(種類="可轉債", 標的="13164", 名稱="某轉", 標的代號="2330"),
            _row(種類="可轉債", 標的="15123", 名稱="沒有標的"),
            _row(種類="期貨", 標的="小台", 名稱="小型台指"),
            _row(種類="期貨", 標的="TX", 名稱="台指期"),
            _row(種類="美股", 標的="SPY", 名稱="標普"),
            _row(種類="美股", 標的="AAPL", 名稱="蘋果"),
        ]
        held, notes = cal.holdings_from_positions(rows)
        by_display = {item["display"]: item for item in held}
        self.assertFalse(by_display["元大台灣50"]["earnings"])
        self.assertEqual(by_display["台積電期"]["key"], "2330.TW")
        self.assertTrue(by_display["台積電期"]["earnings"])
        self.assertEqual(by_display["某轉"]["key"], "2330.TW")
        self.assertFalse(by_display["沒有標的"]["earnings"])
        self.assertEqual(by_display["小型台指"]["market"], "TW")
        self.assertFalse(by_display["小型台指"]["earnings"])
        self.assertEqual(by_display["台指期"]["market"], "TW")
        self.assertFalse(by_display["台指期"]["earnings"])
        self.assertFalse(by_display["標普"]["earnings"])
        self.assertTrue(by_display["蘋果"]["earnings"])
        self.assertTrue(any("ETF" in note or "基金" in note for note in notes))

    def test_watchlist_empty_and_deduped(self):
        watches, notes = cal.watch_from_text("")
        self.assertEqual(watches, [])
        self.assertEqual(notes, [])
        watches, notes = cal.watch_from_text("AAPL，0050、TX")
        self.assertEqual([item["key"] for item in watches if item["earnings"]], ["AAPL"])
        self.assertTrue(any("ETF" in note for note in notes))
        self.assertTrue(any("TX" in note for note in notes))
        held, _notes = cal.holdings_from_positions([_row(種類="美股", 標的="AAPL", 名稱="蘋果")])
        symbols = cal.earnings_symbols(held + watches)
        self.assertEqual(symbols, ["AAPL"])


class SourceTests(unittest.TestCase):
    def test_missing_fmp_key_does_not_invent_events(self):
        model = cal.build_calendar([], "", {}, None, date(2026, 11, 7), FETCHED)
        blob = " ".join(event["title"] for event in model["events"])
        self.assertNotIn("FOMC", blob)
        self.assertNotIn("M7", blob)
        self.assertTrue(any("FMP_API_KEY" in note for note in model["notes"]))
        self.assertTrue(any(event["kind"] == "settlement" for event in model["events"]))

    def test_fmp_nulls_are_not_zero(self):
        event = cal.parse_fmp_event({
            "event": "CPI",
            "country": "US",
            "date": "2026-11-12",
            "estimate": "None",
            "previous": None,
            "actual": "0",
        })
        self.assertIsNone(event["estimate"])
        self.assertIsNone(event["previous"])
        self.assertEqual(event["actual"], 0.0)
        self.assertEqual(event["session"], "時段未知")

    def test_fmp_aware_time_converts(self):
        event = cal.parse_fmp_event({
            "event": "Fed Interest Rate Decision",
            "country": "United States",
            "date": "2026-12-10T21:30:00Z",
            "estimate": 4.5,
            "previous": 4.25,
            "actual": None,
        })
        self.assertEqual(event["date"], date(2026, 12, 11))
        self.assertEqual(event["session"], "美股盤後")
        self.assertEqual(event["estimate"], 4.5)
        self.assertIsNone(event["actual"])

    def test_fmp_and_yahoo_failures_leave_the_rest(self):
        rows = [_row(種類="美股", 標的="NVDA", 名稱="輝達"), _row(標的="2330", 名稱="台積電")]
        model = cal.build_calendar(
            rows,
            "",
            {"NVDA": {"error": "來源失敗"}, "2330.TW": {"Earnings Date": date(2026, 11, 12)}},
            [],
            date(2026, 11, 7),
            FETCHED,
            fmp_error="FMP 來源失敗，這一段缺漏。",
        )
        self.assertTrue(any("NVDA" in gap for gap in model["gaps"]))
        self.assertTrue(any(event["symbol"] == "2330.TW" for event in model["events"]))
        self.assertTrue(any("FMP 來源失敗" in note for note in model["notes"]))
        self.assertFalse(any("利多" in line or "利空" in line for event in model["events"] for line in event["impacts"]))

    def test_fetch_helpers_raise(self):
        with self.assertRaises(cal.CalendarSourceError):
            cal.fetch_fmp_calendar("", date(2026, 11, 7), date(2026, 12, 7), get=lambda *args, **kwargs: None)

        class Response:
            status_code = 500

            def json(self):
                return []

        with self.assertRaises(cal.CalendarSourceError):
            cal.fetch_fmp_calendar("key", date(2026, 11, 7), date(2026, 12, 7), get=lambda *args, **kwargs: Response())

        class Broken:
            @property
            def calendar(self):
                raise RuntimeError("down")

        with self.assertRaises(cal.CalendarSourceError):
            cal.fetch_yahoo_calendar("AAPL", factory=lambda symbol: Broken())


class ImpactTests(unittest.TestCase):
    def test_markets_stay_separate(self):
        rows = [
            _row(標的="2330", 名稱="台積電"),
            _row(種類="期貨", 標的="TX", 名稱="台指期"),
            _row(種類="美股", 標的="AAPL", 名稱="蘋果"),
        ]
        model = cal.build_calendar(
            rows,
            "NVDA",
            {
                "AAPL": {"Earnings Date": [datetime(2026, 11, 12, 21, 0, tzinfo=timezone.utc)]},
                "2330.TW": {"Earnings Date": date(2026, 11, 13)},
                "NVDA": {"Earnings Date": date(2026, 11, 19)},
            },
            [{
                "event": "CPI",
                "country": "US",
                "date": "2026-11-12",
                "estimate": None,
                "previous": 0.2,
                "actual": None,
            }, {
                "event": "USD/TWD",
                "country": "Global",
                "date": "2026-11-20",
                "estimate": None,
                "previous": None,
                "actual": None,
            }],
            date(2026, 11, 7),
            FETCHED,
        )
        earnings = {event["symbol"]: event for event in model["events"] if event["kind"] == "earnings"}
        self.assertIn("直接標的（持倉，美股）", earnings["AAPL"]["impacts"][0])
        self.assertIn("美股盤後", earnings["AAPL"]["session"])
        self.assertIn("05:00 台北", earnings["AAPL"]["when_text"])
        self.assertTrue(any("觀察清單" in line and "不是持倉" in line for line in earnings["NVDA"]["impacts"]))
        settlement = next(event for event in model["events"] if event["kind"] == "settlement")
        joined = " ".join(settlement["impacts"])
        self.assertIn("台積電", joined)
        self.assertIn("台指期", joined)
        self.assertNotIn("蘋果", joined)
        cpi = next(event for event in model["events"] if event["kind"] == "us_macro")
        self.assertIn("蘋果", cpi["impacts"][0])
        self.assertNotIn("台積電", cpi["impacts"][0])
        self.assertIsNone(cpi["actual"])
        self.assertEqual(cpi["previous"], 0.2)
        fx = next(event for event in model["events"] if event["kind"] == "fx")
        fx_text = " ".join(fx["impacts"])
        self.assertIn("台股：", fx_text)
        self.assertIn("美股：", fx_text)
        self.assertNotIn("利多", fx_text)
        self.assertNotIn("利空", fx_text)

    def test_positions_unavailable_still_lists_tw_events(self):
        model = cal.build_calendar(None, "", {}, None, date(2026, 11, 7), FETCHED)
        self.assertTrue(any("持倉讀不到" in note for note in model["notes"]))
        self.assertTrue(any(event["kind"] == "settlement" for event in model["events"]))


class PageTests(unittest.TestCase):
    def test_dashboard_drops_hardcoded_calendar(self):
        text = (ROOT / "Dashboard.py").read_text(encoding="utf-8")
        start = text.index('elif page == "第三頁：重大行事曆與會展"')
        end = text.index('elif page == "第四頁：部位監控與量化風控"')
        page = text[start:end]
        self.assertNotIn("2026-10-29", page)
        self.assertNotIn("get_m7_earnings", page)
        self.assertNotIn("generate_content", page)
        self.assertNotIn("send_to_telegram(", page)
        self.assertIn("send=send_to_telegram", page)
        self.assertIn("ai_memo_p3", page)
        self.assertIn("complete_event_analysis", page)
        self.assertIn("calendar_watchlist", page)
        self.assertIn("load_calendar_model", page)
        actions = (ROOT / "calendar_page.py").read_text(encoding="utf-8")
        self.assertIn("🧠 AI 解析", actions)
        self.assertIn("📲 推播TG", actions)
        self.assertIn("📤 推播此事件建議到 TG", actions)

    def test_apptest_shows_gap_and_unknown_settlement(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        rendered = cal.build_calendar(
            [_row(標的="2330", 名稱="台積電")],
            "",
            {"2330.TW": {"error": "來源失敗"}},
            None,
            date(2026, 2, 1),
            FETCHED,
            horizon_days=27,
        )
        import json
        payload = json.dumps({
            "notes": rendered["notes"],
            "gaps": rendered["gaps"],
            "symbols": rendered["symbols"],
            "not_done": rendered["not_done"],
            "events": [
                {
                    "when_text": event["when_text"],
                    "title": event["title"],
                    "certainty": event["certainty"],
                    "session": event["session"],
                    "source": event["source"],
                    "estimate": event["estimate"],
                    "previous": event["previous"],
                    "actual": event["actual"],
                    "note": event["note"],
                    "impacts": event["impacts"],
                    "updated_at": None,
                }
                for event in rendered["events"]
            ],
        }, ensure_ascii=False)
        script = f"""
import json
import streamlit as st
import calendar_page
calendar_page.render_calendar(st, json.loads({payload!r}))
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(str(item.value) for item in list(at.caption) + list(at.markdown) + list(at.info))
        self.assertIn("缺漏", blob)
        self.assertIn("2330.TW", blob)
        self.assertIn("未知", blob)
        self.assertIn("不寫入", blob)
        self.assertNotIn("利多", blob)
        self.assertNotIn("利空", blob)


class DataFrameTests(unittest.TestCase):
    def test_nan_symbol_is_skipped(self):
        frame = pd.DataFrame([{"種類": "台股", "標的": float("nan"), "名稱": ""}])
        held, notes = cal.holdings_from_positions(frame)
        self.assertEqual(held, [])
        self.assertEqual(notes, [])


def _two_earnings():
    return cal.build_calendar(
        [
            _row(種類="美股", 標的="AAPL", 名稱="蘋果"),
            _row(標的="2330", 名稱="台積電"),
        ],
        "",
        {
            "AAPL": {"Earnings Date": date(2026, 11, 12)},
            "2330.TW": {"Earnings Date": date(2026, 11, 13)},
        },
        [],
        date(2026, 11, 7),
        FETCHED,
    )


class ActionTests(unittest.TestCase):
    def test_missing_key_and_incomplete_event_do_not_call(self):
        from unittest.mock import MagicMock, patch

        import calendar_actions

        event = {"title": "財報 AAPL", "when_text": "2026-11-12", "fingerprint": "a"}
        self.assertIn("GEMINI_API_KEY", calendar_actions.analysis_block_reason(event, ""))
        self.assertIn("缺漏", calendar_actions.analysis_block_reason({"title": "只有標題"}, "key"))
        self.assertIsNone(calendar_actions.event_notice({"title": "", "when_text": "2026-11-12"}))
        with patch("google.genai.Client") as ctor, patch("requests.post") as post:
            with self.assertRaises(RuntimeError):
                calendar_actions.complete_event_analysis("提示", "")
            ctor.assert_not_called()
            post.assert_not_called()
            fake = MagicMock()
            fake.models.generate_content.return_value.text = "   "
            ctor.return_value = fake
            with self.assertRaises(RuntimeError) as caught:
                calendar_actions.complete_event_analysis("提示", "fake-key")
            self.assertIn("解析未完成", str(caught.exception))
            post.assert_not_called()

    def test_mock_analysis_uses_the_same_model_and_does_not_post(self):
        from unittest.mock import MagicMock, patch

        import calendar_actions

        fake = MagicMock()
        fake.models.generate_content.return_value.text = " 只談這一則 "
        with patch("google.genai.Client", return_value=fake), patch("requests.post") as post:
            text = calendar_actions.complete_event_analysis("提示", "fake-key")
        self.assertEqual(text, "只談這一則")
        fake.models.generate_content.assert_called_once_with(model="gemini-3.8-flash", contents="提示")
        post.assert_not_called()

    def test_stale_memo_cannot_be_sent_as_another_event(self):
        import calendar_actions

        model = _two_earnings()
        aapl = next(event for event in model["events"] if event["symbol"] == "AAPL")
        tw = next(event for event in model["events"] if event["symbol"] == "2330.TW")
        self.assertNotEqual(aapl["id"], tw["id"])
        self.assertNotEqual(aapl["fingerprint"], tw["fingerprint"])
        memo = {"text": "只談蘋果", "fingerprint": aapl["fingerprint"]}
        self.assertEqual(calendar_actions.memo_status(memo, aapl), "ready")
        self.assertIsNone(calendar_actions.advice_notice(tw, memo))
        changed = dict(aapl)
        changed["impacts"] = ["直接標的換了"]
        changed["fingerprint"] = cal.event_fingerprint(changed)
        self.assertEqual(calendar_actions.memo_status(memo, changed), "stale")
        self.assertIsNone(calendar_actions.advice_notice(changed, memo))
        title, body = calendar_actions.event_notice(tw)
        self.assertIn("財報 2330.TW", title)
        self.assertIn("台積電", body)
        self.assertNotIn("只談蘋果", body)
        self.assertNotIn("AAPL", body)
        prompt = calendar_actions.analysis_prompt(aapl)
        self.assertIn("財報 AAPL", prompt)
        self.assertIn("估計", prompt)
        self.assertIn("—", prompt)
        self.assertNotIn("2330.TW", prompt)

    def test_apptest_buttons_are_manual(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        from unittest.mock import patch

        import calendar_page

        model = _two_earnings()
        aapl = next(event for event in model["events"] if event["symbol"] == "AAPL")
        tw = next(event for event in model["events"] if event["symbol"] == "2330.TW")
        script = """
import streamlit as st
from datetime import date, datetime
import calendar_events as cal
import calendar_page

if "log" not in st.session_state:
    st.session_state.log = []
    st.session_state.memos = {}
    st.session_state.gkey = ""
    st.session_state.mode = "ok"

def complete(prompt, api_key):
    st.session_state.log.append(("ai", api_key, prompt))
    if st.session_state.mode == "fail":
        raise RuntimeError("boom")
    if st.session_state.mode == "empty":
        return "  "
    return "建議只談這一則"

def send(title, text):
    st.session_state.log.append(("tg", title, text))

model = cal.build_calendar(
    [
        {"帳戶": "甲", "種類": "美股", "標的": "AAPL", "名稱": "蘋果"},
        {"帳戶": "甲", "種類": "台股", "標的": "2330", "名稱": "台積電"},
    ],
    "",
    {
        "AAPL": {"Earnings Date": date(2026, 11, 12)},
        "2330.TW": {"Earnings Date": date(2026, 11, 13)},
    },
    [],
    date(2026, 11, 7),
    datetime(2026, 11, 7, 9, 0, tzinfo=cal.TAIPEI),
)
calendar_page.render_calendar(
    st,
    model,
    memos=st.session_state.memos,
    gemini_key=st.session_state.gkey,
    complete_analysis=complete,
    send=send,
)
"""

        def blocked(*args, **kwargs):
            raise AssertionError("不該連網")

        with patch("requests.post", side_effect=blocked), patch("google.genai.Client", side_effect=blocked):
            at = AppTest.from_string(script, default_timeout=60).run()
            self.assertFalse(at.exception)
            labels = [button.label for button in at.button]
            self.assertIn("🧠 AI 解析", labels)
            self.assertIn("📲 推播TG", labels)
            self.assertNotIn("📤 推播此事件建議到 TG", labels)
            self.assertEqual(at.session_state["log"], [])

            at = at.button(key=calendar_page.widget_key("ai", aapl["id"])).click().run()
            self.assertFalse(at.exception)
            warnings = "\n".join(str(item.value) for item in at.warning)
            self.assertIn("沒有 GEMINI_API_KEY", warnings)
            self.assertEqual(at.session_state["log"], [])

            at.session_state["gkey"] = "fake-key"
            at.session_state["mode"] = "empty"
            at = at.button(key=calendar_page.widget_key("ai", aapl["id"])).click().run()
            self.assertFalse(at.exception)
            warnings = "\n".join(str(item.value) for item in at.warning)
            self.assertIn("解析未完成", warnings)
            self.assertNotIn(aapl["id"], at.session_state["memos"])

            at.session_state["mode"] = "fail"
            at = at.button(key=calendar_page.widget_key("ai", aapl["id"])).click().run()
            self.assertFalse(at.exception)
            errors = "\n".join(str(item.value) for item in at.error)
            self.assertIn("生成失敗", errors)
            self.assertNotIn(aapl["id"], at.session_state["memos"])

            at.session_state["mode"] = "ok"
            at = at.button(key=calendar_page.widget_key("ai", aapl["id"])).click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state["memos"][aapl["id"]]["text"], "建議只談這一則")
            self.assertEqual(at.session_state["log"][-1][0], "ai")
            self.assertIn("財報 AAPL", at.session_state["log"][-1][2])
            self.assertNotIn("2330.TW", at.session_state["log"][-1][2])
            self.assertIn("📤 推播此事件建議到 TG", [button.label for button in at.button])

            at = at.button(key=calendar_page.widget_key("tg", tw["id"])).click().run()
            self.assertFalse(at.exception)
            kind, title, body = at.session_state["log"][-1]
            self.assertEqual(kind, "tg")
            self.assertIn("財報 2330.TW", title)
            self.assertIn("台積電", body)
            self.assertNotIn("建議只談這一則", body)
            self.assertNotIn("AAPL", title)

            at.session_state["memos"][aapl["id"]]["fingerprint"] = "別列舊解析"
            at = at.run()
            self.assertFalse(at.exception)
            warnings = "\n".join(str(item.value) for item in at.warning)
            self.assertIn("較早的事件內容", warnings)
            self.assertNotIn(calendar_page.widget_key("advice", aapl["id"]), [button.key for button in at.button])

            at = at.button(key=calendar_page.widget_key("ai", aapl["id"])).click().run()
            at = at.button(key=calendar_page.widget_key("advice", aapl["id"])).click().run()
            self.assertFalse(at.exception)
            kind, title, body = at.session_state["log"][-1]
            self.assertEqual(kind, "tg")
            self.assertIn("行事曆戰術建議：財報 AAPL", title)
            self.assertEqual(body, "建議只談這一則")
            self.assertNotIn("2330", title)


if __name__ == "__main__":
    unittest.main()
