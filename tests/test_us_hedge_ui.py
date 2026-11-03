from unittest import TestCase


class TestUsHedgeUI(TestCase):
    def test_apptest_smoke(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")

        script = """
from unittest.mock import patch
import numpy as np
import pandas as pd
import streamlit as st

dates = pd.date_range("2023-01-01", periods=300, freq="D")
def fake_fetch_close(symbols, period="13mo"):
    out = {}
    for sym in symbols:
        out[sym] = pd.Series(np.linspace(100, 200, 300), index=dates)
    return out

mock_vix = {
    "ok": True,
    "value": 18.0,
    "as_of": "2026-11-02",
    "source": "測試",
    "history": [float(x) for x in range(10, 40)],
    "history_days": 30,
    "history_label": "近一年美股 VIX 日收盤（30 個交易日）",
}
mock_idx = {"ok": True, "SPX": 6000.0, "SPY": 598.0, "source": "測試"}
mock_taiex = {
    "ok": False,
    "value": None,
    "as_of": "",
    "source": "無報價",
    "history": [],
    "history_days": 0,
    "history_label": "無",
}

class DummyCallable:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("Should not be called")
    def clear(self):
        pass

def dummy_send(title, text):
    pass

with patch("us_hedge.fetch_us_vix", return_value=mock_vix), \\
     patch("us_hedge.fetch_us_index_prices", return_value=mock_idx), \\
     patch("taiex_vix.fetch_taiex_vix", return_value=mock_taiex), \\
     patch("order_assistant_ui.fetch_taiex_vix", return_value=mock_taiex), \\
     patch("order_insights.fetch_close_history", side_effect=fake_fetch_close):
    import order_assistant_ui
    order_assistant_ui._cached_us_vix.clear()
    order_assistant_ui._cached_us_index.clear()
    from order_assistant_ui import render_order_assistant
    render_order_assistant(dummy_send, DummyCallable(), DummyCallable())
"""

        at = AppTest.from_string(script, default_timeout=60).run()
        self.assertFalse(at.exception)

        markdown_texts = [m.value for m in at.markdown]
        self.assertTrue(any("美股選擇權避險" in txt for txt in markdown_texts))

        metric_labels = [m.label for m in at.metric]
        self.assertIn("美股 S&P Beta 曝險", metric_labels)
        self.assertIn("美股部位平均 Beta", metric_labels)
        self.assertIn("美股帳戶 Beta（÷ 美股淨值）", metric_labels)

        warning_texts = [w.value for w in at.warning]
        self.assertTrue(any("無報價" in txt for txt in warning_texts))

        self.assertTrue(len(at.get("plotly_chart")) > 0)

        at.number_input(key="oa_us_prem_put_SPY").set_value(12.0)
        at.run()
        self.assertFalse(at.exception)

        all_feedback_texts = (
            [s.value for s in at.success]
            + [w.value for w in at.warning]
            + [e.value for e in at.error]
        )
        self.assertTrue(any("百分位" in txt for txt in all_feedback_texts))

        at.selectbox(key="oa_us_inst").set_value("SPX")
        at.run()
        self.assertFalse(at.exception)
        spot_val = at.number_input(key="oa_us_spot_SPX").value
        self.assertEqual(spot_val, 6000.0)
