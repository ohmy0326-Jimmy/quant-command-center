import os
import sys
import textwrap
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chart_history  # noqa: E402


def _frame(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="D")
    return pd.DataFrame({"Close": values}, index=idx)


class FetchStrictTests(unittest.TestCase):
    def test_success_returns_frame(self):
        frame = chart_history.fetch_history_strict("A", "1y", downloader=lambda t, p: _frame([1.0, 2.0]))
        self.assertEqual(len(frame), 2)

    def test_empty_frame_raises(self):
        with self.assertRaises(chart_history.EmptyHistoryError):
            chart_history.fetch_history_strict("A", "1y", downloader=lambda t, p: pd.DataFrame())

    def test_none_and_nan_close_raise(self):
        with self.assertRaises(chart_history.EmptyHistoryError):
            chart_history.fetch_history_strict("A", "1y", downloader=lambda t, p: None)
        with self.assertRaises(chart_history.EmptyHistoryError):
            chart_history.fetch_history_strict("A", "1y", downloader=lambda t, p: _frame([float("nan")]))

    def test_downloader_exception_propagates(self):
        def boom(t, p):
            raise OSError("network")

        with self.assertRaises(OSError):
            chart_history.fetch_history_strict("A", "1y", downloader=boom)


class FetchMapTests(unittest.TestCase):
    def test_failures_become_empty_without_breaking_others(self):
        def loader(t, p):
            if t == "BAD":
                raise chart_history.EmptyHistoryError("x")
            return _frame([1.0, 2.0])

        found = chart_history.fetch_history_map((("OK", "1y"), ("BAD", "1y"), ("OK", "5d")), loader)
        self.assertEqual(set(found), {("OK", "1y"), ("BAD", "1y"), ("OK", "5d")})
        self.assertTrue(found[("BAD", "1y")].empty)
        self.assertFalse(found[("OK", "1y")].empty)

    def test_empty_pairs(self):
        self.assertEqual(chart_history.fetch_history_map((), lambda t, p: _frame([1.0])), {})


def test_cache_keeps_success_but_not_failure():
    """真的跑 st.cache_data：成功的 5 分鐘內不重抓；失敗的下一次重新執行會重抓。"""
    from streamlit.testing.v1 import AppTest

    script = textwrap.dedent(
        f"""
        import sys, threading
        sys.path.insert(0, {ROOT!r})
        import pandas as pd
        import streamlit as st
        from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
        import chart_history

        calls = st.session_state.setdefault("calls", {{}})
        fail = st.session_state.setdefault("fail", {{"BAD"}})

        def downloader(t, p):
            calls[t] = calls.get(t, 0) + 1
            if t in fail:
                return pd.DataFrame()
            return pd.DataFrame({{"Close": [1.0, 2.0]}})

        @st.cache_data(ttl=300, show_spinner=False)
        def cached_history(ticker, period):
            return chart_history.fetch_history_strict(ticker, period, downloader=downloader)

        ctx = get_script_run_ctx()

        def load(t, p):
            add_script_run_ctx(threading.current_thread(), ctx)
            return cached_history(t, p)

        found = chart_history.fetch_history_map((("OK", "1y"), ("BAD", "1y")), load)
        st.write("ok_empty", found[("OK", "1y")].empty, "bad_empty", found[("BAD", "1y")].empty)
        """
    )
    at = AppTest.from_string(script, default_timeout=30)
    at.run()
    assert not at.exception
    at.run()
    assert at.session_state["calls"] == {"OK": 1, "BAD": 2}
    # 資料源恢復：下一次執行就拿到資料，不會卡在快取的空結果
    at.session_state["fail"] = set()
    at.run()
    assert not at.exception
    assert at.session_state["calls"] == {"OK": 1, "BAD": 3}
    at.run()
    assert at.session_state["calls"] == {"OK": 1, "BAD": 3}


class DashboardWiringTests(unittest.TestCase):
    def test_dashboard_uses_per_ticker_strict_cache(self):
        with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("chart_history.fetch_history_strict(ticker, period)", text)
        self.assertIn("chart_history.fetch_history_map(pairs, load)", text)
        self.assertIn("cached_history_map.clear = cached_history.clear", text)
        # 舊寫法：在快取函式裡吞掉例外並回空表 → 失敗會被快取 5 分鐘
        self.assertNotIn("return pair, yf.Ticker(ticker).history(period=period)", text)


if __name__ == "__main__":
    unittest.main()
