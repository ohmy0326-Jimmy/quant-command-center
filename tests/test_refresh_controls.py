import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from streamlit.testing.v1 import AppTest

from refresh_controls import (
    TAIPEI,
    clear_session_keys,
    ensure_refresh_stamp,
    format_refresh_time,
    refresh_page_data,
    run_clear_functions,
    taipei_now,
)


class TestRefreshControls(unittest.TestCase):
    def test_taipei_now_naive_and_aware(self):
        naive = datetime(2026, 11, 3, 14, 30, 5)
        aware_utc = datetime(2026, 11, 3, 14, 30, 5, tzinfo=timezone.utc)
        t1 = taipei_now(naive)
        t2 = taipei_now(aware_utc)
        self.assertEqual(t1.tzinfo, TAIPEI)
        self.assertEqual(t2.tzinfo, TAIPEI)
        self.assertEqual(t1, t2)
        self.assertEqual(t1.hour, 22)
        self.assertEqual(t1.minute, 30)
        self.assertEqual(t1.second, 5)

        default_now = taipei_now()
        self.assertEqual(default_now.tzinfo, TAIPEI)

    def test_format_refresh_time(self):
        moment = datetime(2026, 11, 3, 14, 30, 5, tzinfo=timezone.utc)
        text = format_refresh_time(moment)
        self.assertEqual(text, "2026-11-03 22:30:05（台北時間）")

    def test_clear_session_keys(self):
        state = {"a": 1, "b": 2, "c": 3}
        removed = clear_session_keys(state, ("a", "c", "missing"))
        self.assertEqual(removed, ["a", "c"])
        self.assertEqual(state, {"b": 2})

    def test_run_clear_functions(self):
        called = []

        def good():
            called.append(True)

        def bad():
            raise RuntimeError("cache failed")

        errors = run_clear_functions([good, None, bad])
        self.assertEqual(called, [True])
        self.assertEqual(len(errors), 1)
        self.assertIn("cache failed", errors[0])

    @patch("refresh_controls.st.cache_data.clear")
    def test_refresh_page_data(self, mock_clear):
        state = {"k1": "val1", "remain": "ok"}
        called = []
        fixed_now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

        errors = refresh_page_data(
            state,
            stamp_key="stamp",
            clear_fns=[lambda: called.append("ran")],
            session_keys=["k1"],
            now=fixed_now,
            clear_all_cache=True,
        )

        mock_clear.assert_called_once()
        self.assertEqual(called, ["ran"])
        self.assertNotIn("k1", state)
        self.assertEqual(state["remain"], "ok")
        self.assertEqual(state["stamp"], datetime(2026, 5, 1, 8, 0, 0, tzinfo=TAIPEI))
        self.assertEqual(errors, [])

    def test_ensure_refresh_stamp_idempotent(self):
        state = {}
        t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)

        first = ensure_refresh_stamp(state, "s", now=t1)
        second = ensure_refresh_stamp(state, "s", now=t2)
        self.assertEqual(first, second)
        self.assertEqual(state["s"], taipei_now(t1))


class TestRefreshBarAppTest(unittest.TestCase):
    def test_render_refresh_bar_smoke(self):
        script = """
import streamlit as st
from refresh_controls import render_refresh_bar

def _clear_counter():
    st.session_state["clear_invoked"] = st.session_state.get("clear_invoked", 0) + 1

render_refresh_bar("t", clear_fns=(_clear_counter,))
"""
        at = AppTest.from_string(script).run()
        self.assertFalse(at.exception)
        btn = at.button(key="t_refresh_btn")
        self.assertIsNotNone(btn)
        btn.click().run()
        self.assertFalse(at.exception)
        captions = [c.value for c in at.caption]
        self.assertTrue(any("最後更新" in c for c in captions), f"Captions found: {captions}")
        self.assertEqual(at.session_state.get("clear_invoked"), 1)


if __name__ == "__main__":
    unittest.main()


def test_page5_refresh_bar_smoke():
    from streamlit.testing.v1 import AppTest

    script = '''
from unittest.mock import patch
import macro_cycle_ui
with patch("macro_cycle_ui.load_service_key", side_effect=RuntimeError("no key")), \\
        patch("macro_cycle_ui.fetch_auto_results", return_value={}):
    macro_cycle_ui.render_macro_cycle_page(load_book=lambda: {"ok": False}, clear_caches=lambda: None)
'''
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    assert any("最後更新" in c.value for c in at.caption)
    at.button(key="p5_refresh_btn").click().run()
    assert not at.exception
    assert any("台北時間" in c.value for c in at.caption)
