import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from order_assistant import Snapshot, apply_order, build_sample_snapshot
from order_insights import compute_rs, default_put_delta, rs_for_ticker


def _stock(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, sector="其他", max_loss=-100):
    return {"帳戶": account, "種類": kind, "標的": ticker, "名稱": ticker, "持有數量": qty, "平均成本": cost,
            "投入總金額": 0.0 if "期" in kind else cost * qty, "最大虧損比例": max_loss, "標的槓桿": lev,
            "即時報價": price, "Beta值": beta, "產業別": sector}


class TestOrderV2(unittest.TestCase):
    def _snap(self):
        positions = pd.DataFrame(
            [
                _stock("第一證券", "台股", "2330", 1000, 900, 1000, beta=1.15, sector="晶圓代工"),
                _stock("元大期貨", "期貨", "TX", 1, 23000, 23000, sector="臺指期貨"),
            ]
        )
        accounts = pd.DataFrame(
            [
                {"帳戶名稱": "第一證券", "現金餘額": 1_000_000.0, "融資/質押借款": 0.0},
                {"帳戶名稱": "元大期貨", "現金餘額": 800_000.0, "融資/質押借款": 0.0},
            ]
        )
        return Snapshot(positions, accounts, 32.0, 23000.0)

    @staticmethod
    def _cash(snap, name):
        frame = snap.accounts
        return float(frame.loc[frame["帳戶名稱"].astype(str).str.strip() == name, "現金餘額"].iloc[0])

    def test_apply_order_explicit_account_new(self):
        snap = self._snap()
        after, warnings = apply_order(
            snap, {"ticker": "AAPL", "side": "buy", "qty": 10, "price": 150.0, "kind": "美股", "account": "IB"}
        )
        names = set(after.accounts["帳戶名稱"].astype(str).str.strip())
        self.assertEqual(names, {"第一證券", "元大期貨", "IB"})
        self.assertTrue(any("不在試算表" in w for w in warnings))
        self.assertEqual(self._cash(after, "第一證券"), 1_000_000.0)

    def test_apply_order_explicit_account_existing(self):
        snap = self._snap()
        after, _warnings = apply_order(
            snap, {"ticker": "2330", "side": "buy", "qty": 1000, "price": 500.0, "kind": "台股", "account": "第一證券"}
        )
        self.assertAlmostEqual(self._cash(after, "第一證券"), 1_000_000.0 - 500_000.0)
        self.assertEqual(set(after.accounts["帳戶名稱"].astype(str).str.strip()), {"第一證券", "元大期貨"})

    def test_leveraged_etf_counts_leverage_once(self):
        snap = self._snap()
        qty, price = 2000, 100.0
        after, _warnings = apply_order(
            snap,
            {"ticker": "00663L", "side": "buy", "qty": qty, "price": price, "kind": "台股", "account": "第一證券", "leverage": 2.0, "beta": 1.9},
        )
        self.assertAlmostEqual(after.metrics["tw_exposure"] - snap.metrics["tw_exposure"], qty * price * 2.0, places=4)

    def test_compute_rs_excludes_target(self):
        dates = pd.date_range("2023-01-01", periods=300, freq="D")
        s1 = pd.Series(np.linspace(100, 200, 300), index=dates)
        s2 = pd.Series(np.linspace(100, 200, 300), index=dates)
        s3 = pd.Series(np.linspace(100, 150, 300), index=dates)

        universe = {"2330.TW": s1, "2317.TW": s2, "2454.TW": s3}
        res = compute_rs(s1, universe, target_symbol="2330.TW")
        self.assertEqual(res["universe_size"], 2)

    def test_rs_for_ticker(self):
        dates = pd.date_range("2023-01-01", periods=300, freq="D")

        def fake_fetch(symbols):
            out = {}
            for idx, sym in enumerate(symbols):
                mult = 1.0 + idx * 0.1
                out[sym] = pd.Series(np.linspace(100, 100 * mult, 300), index=dates)
            return out

        res_fut = rs_for_ticker("TX", "期貨", fetch=fake_fetch)
        self.assertIn("期貨", res_fut["error"])

        res = rs_for_ticker("2330", "台股", fetch=fake_fetch)
        self.assertIsNone(res["error"])
        self.assertIsNotNone(res["rating"])

        def empty_fetch(symbols):
            return {}

        res_err = rs_for_ticker("9999", "台股", fetch=empty_fetch)
        self.assertIn("抓不到", res_err["error"])

    def test_default_put_delta(self):
        delta = default_put_delta(23000.0)
        self.assertLess(delta, 0.0)
        self.assertGreater(delta, -0.5)

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

            at.text_input(key="oa_ticker").set_value("2330")
            at.number_input(key="oa_qty").set_value(1000.0)
            at.run()
            self.assertFalse(at.exception)
            all_text = " ".join(
                [str(getattr(m, "value", "")) for m in at.markdown] + [str(getattr(h, "value", "")) for h in at.subheader]
            )
            self.assertIn("處置優先順序", all_text)


if __name__ == "__main__":
    unittest.main()
