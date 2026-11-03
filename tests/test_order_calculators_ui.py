from unittest import TestCase
from unittest.mock import patch

import numpy as np
import pandas as pd


class TestOrderCalculatorsUI(TestCase):
    def test_apptest_smoke(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")

        script = """
import streamlit as st
from order_assistant_ui import render_order_assistant

class DummyCallable:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("Should not be called")
    def clear(self):
        pass

def dummy_send(title, text):
    pass

render_order_assistant(dummy_send, DummyCallable(), DummyCallable())
"""
        dates = pd.date_range("2023-01-01", periods=300, freq="D")

        def fake_fetch_close(symbols, period="13mo"):
            out = {}
            for sym in symbols:
                out[sym] = pd.Series(np.linspace(100, 200, 300), index=dates)
            return out

        with patch(
            "order_insights.fetch_close_history", side_effect=fake_fetch_close
        ):
            at = AppTest.from_string(script, default_timeout=60).run()
            self.assertFalse(at.exception)

            at.number_input(key="oa_leaps_premium").set_value(30.0)
            at.run()
            self.assertFalse(at.exception)
            labels = [m.label for m in at.metric]
            self.assertIn("Delta", labels)
            self.assertIn("約當股數", labels)

            cur_price = at.number_input(key="oa_fut_price").value
            at.number_input(key="oa_fut_lots").set_value(2)
            at.number_input(key="oa_fut_stop").set_value(float(cur_price - 100))
            at.run()
            self.assertFalse(at.exception)
            labels = [m.label for m in at.metric]
            self.assertIn("所需原始保證金", labels)
            self.assertIn("名目曝險", labels)
            self.assertIn("合併槓桿", labels)
            self.assertIn("停損上限可下口數", labels)
