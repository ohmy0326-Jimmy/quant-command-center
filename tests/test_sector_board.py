"""第二頁相對強弱、進場建議與強勢股。不連網。"""

import unittest
from fractions import Fraction

import numpy as np
import pandas as pd

import sector_board as board


def _series(values, start="2026-01-01"):
    index = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=index)


class AlignTests(unittest.TestCase):
    def test_drops_dates_the_benchmark_does_not_have(self):
        asset = _series([10, 11, 12, 13])
        bench = _series([100, 100, 100])
        aligned = board.align_prices(asset, bench)
        self.assertEqual(len(aligned), 3)
        self.assertNotIn(asset.index[-1], aligned.index)

    def test_four_and_twelve_week_need_enough_common_days(self):
        asset = _series(np.linspace(100, 120, 30))
        bench = _series(np.linspace(100, 110, 30))
        result = board.relative_strength(asset, bench)
        self.assertIsNone(result["rows"]["12週"]["excess"])
        self.assertEqual(result["rows"]["12週"]["direction"], "未知")
        self.assertIsNotNone(result["rows"]["4週"]["excess"])

    def test_excess_is_return_difference(self):
        asset = _series([100] * 20 + [121])
        bench = _series([100] * 20 + [110])
        result = board.relative_strength(asset, bench)
        # 21 個點才能做 20 日報酬。
        self.assertAlmostEqual(result["rows"]["4週"]["asset_return"], 0.21)
        self.assertAlmostEqual(result["rows"]["4週"]["bench_return"], 0.10)
        self.assertAlmostEqual(result["rows"]["4週"]["excess"], 0.11)
        self.assertEqual(result["rows"]["4週"]["direction"], "轉強")


class GateTests(unittest.TestCase):
    def test_entry_has_price_and_relative_only(self):
        frame = pd.DataFrame({"Close": np.linspace(10, 12, 30), "Volume": np.full(30, 1000)})
        bench = _series(np.linspace(100, 110, 30))
        entry = board.evaluate_entry(frame, bench)
        self.assertEqual([gate["name"] for gate in entry["gates"]], ["價格結構", "相對大盤連兩週"])
        self.assertNotIn("總經", entry["text"])
        self.assertFalse(hasattr(board, "macro_gate"))

    def test_short_history_does_not_fail_the_price_gate(self):
        frame = pd.DataFrame({"Close": np.linspace(10, 12, 30), "Volume": np.full(30, 1000)})
        self.assertEqual(board.price_gate(frame)["state"], board.UNKNOWN)

    def test_two_up_weeks_pass_and_short_sample_is_unknown(self):
        asset = _series(np.linspace(100, 130, 16))
        bench = _series(np.full(16, 100.0))
        self.assertEqual(board.relative_gate(asset, bench)["state"], board.PASS)
        short = board.relative_gate(_series(np.linspace(100, 110, 8)), _series(np.full(8, 100.0)))
        self.assertEqual(short["state"], board.UNKNOWN)
        summary = board.summarize_gates(
            [{"name": "價格結構", "state": board.UNKNOWN, "detail": ""}, board.relative_gate(asset, bench)]
        )
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["unknown"], 1)
        self.assertEqual(summary["entry_stage"], "probe")
        self.assertEqual(summary["text"], "🟡 試單（1/3 部位，已過相對大盤）")
        self.assertNotIn("未知沒有算成通過或未過", summary["text"])
        self.assertNotIn("3/3", summary["text"])
        both_unknown = board.summarize_gates(
            [{"name": "價格結構", "state": board.UNKNOWN, "detail": ""}, {"name": "相對大盤連兩週", "state": board.UNKNOWN, "detail": ""}]
        )
        self.assertEqual(both_unknown["entry_stage"], "unknown")
        self.assertEqual(both_unknown["text"], "⚪ 未知（缺價格資料）")
        both_pass = board.summarize_gates(
            [{"name": "價格結構", "state": board.PASS, "detail": ""}, {"name": "相對大盤連兩週", "state": board.PASS, "detail": ""}]
        )
        self.assertEqual(both_pass["entry_stage"], "full")
        self.assertEqual(both_pass["text"], "✅ 加碼至滿")

    def test_roles_and_unmapped_stance(self):
        self.assertEqual(board.role_of("XLK"), "攻擊")
        self.assertEqual(board.role_of("XLP"), "防禦")
        self.assertEqual(board.role_of("SOXX"), "主題（不歸攻防）")
        self.assertEqual(board.stance_text("XLC", {"industries": []}), "未對應第五頁產業")
        self.assertEqual(board.stance_text("UFO", {"industries": []}), "未對應第五頁產業")
        view = {"industries": [{"name": "AI 算力", "stance": "加碼", "example": True}]}
        self.assertIn("示例", board.stance_text("NVDA", view))

    def test_page2_etfs_use_existing_industries(self):
        import json
        from pathlib import Path

        from sector_map import PAGE2_INDUSTRIES, industries_for_board

        payload = json.loads((Path(__file__).resolve().parents[1] / "data" / "macro_cycle.json").read_text(encoding="utf-8"))
        known = {item["name"] for item in payload["industries"]["items"]}
        for names in PAGE2_INDUSTRIES.values():
            self.assertTrue(names)
            self.assertTrue(set(names) <= known)
        view = {
            "industries": [
                {"name": "AI 算力", "stance": "中性"},
                {"name": "軟體雲端", "stance": "加碼"},
                {"name": "消費電子", "stance": "減碼"},
                {"name": "金融", "stance": "中性"},
            ]
        }
        self.assertEqual(industries_for_board("SOXX"), ("AI 算力",))
        self.assertEqual(industries_for_board("IGV"), ("軟體雲端",))
        self.assertEqual(board.stance_text("SOXX", view), "AI 算力 · 中性")
        self.assertEqual(board.stance_text("IGV", view), "軟體雲端 · 加碼")
        self.assertEqual(board.stance_text("XLF", view), "金融 · 中性")
        xlk = board.stance_text("XLK", view)
        self.assertEqual(xlk, "AI 算力 · 中性；軟體雲端 · 加碼；消費電子 · 減碼")
        for ticker in ("XLC", "XLY", "XLI", "XLE", "XLB", "XLP", "XLU", "XLRE", "XLV", "UFO"):
            self.assertEqual(industries_for_board(ticker), ())
            self.assertEqual(board.stance_text(ticker, view), "未對應第五頁產業")
        summary = board.summarize_gates(
            [{"name": "價格結構", "state": board.FAIL, "detail": ""}, {"name": "相對大盤連兩週", "state": board.FAIL, "detail": ""}]
        )
        self.assertEqual(summary["unknown"], 0)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["entry_stage"], "none")
        self.assertEqual(summary["text"], "❌ 不進場")
        self.assertNotIn("總經允許", summary["text"])
        self.assertNotIn("未對應", summary["text"])

    def test_staged_advice_names_the_passed_gate_and_keeps_unknown_separate(self):
        self.assertEqual(board.ENTRY_PROBE_FRACTION, Fraction(1, 3))
        price_only = board.summarize_gates(
            [{"name": "價格結構", "state": board.PASS, "detail": ""}, {"name": "相對大盤連兩週", "state": board.FAIL, "detail": ""}]
        )
        self.assertEqual(price_only["entry_stage"], "probe")
        self.assertEqual(price_only["text"], "🟡 試單（1/3 部位，已過價格結構）")
        relative_only = board.summarize_gates(
            [{"name": "價格結構", "state": board.UNKNOWN, "detail": ""}, {"name": "相對大盤連兩週", "state": board.PASS, "detail": ""}]
        )
        self.assertEqual(relative_only["text"], "🟡 試單（1/3 部位，已過相對大盤）")
        mixed = board.summarize_gates(
            [{"name": "價格結構", "state": board.UNKNOWN, "detail": ""}, {"name": "相對大盤連兩週", "state": board.FAIL, "detail": ""}]
        )
        self.assertEqual(mixed["passed"], 0)
        self.assertEqual(mixed["entry_stage"], "unknown")
        self.assertEqual(mixed["text"], "⚪ 未知（價格結構未知、相對大盤未轉強）")
        self.assertNotIn("不進場", mixed["text"])
        self.assertNotIn("試單", mixed["text"])
        original = board.ENTRY_PROBE_FRACTION
        try:
            board.ENTRY_PROBE_FRACTION = Fraction(1, 4)
            shifted = board.gate_status_text(
                [{"name": "價格結構", "state": board.PASS, "detail": ""}, {"name": "相對大盤連兩週", "state": board.FAIL, "detail": ""}]
            )
            self.assertIn("1/4", shifted)
            self.assertIn("1/4", board.entry_caption())
        finally:
            board.ENTRY_PROBE_FRACTION = original
        self.assertEqual(board.entry_caption(), "進場只看價格結構與相對大盤兩關：過一關先試單 1/3，兩關都過再加碼至滿。未知不算通過也不算未過。")
        self.assertFalse(hasattr(board, "macro_gate"))
        self.assertEqual(len(board.evaluate_entry(pd.DataFrame({"Close": [1], "Volume": [1]}), _series([1]))["gates"]), 2)


class StrongStockTests(unittest.TestCase):
    def test_lists_only_real_holdings_with_enough_history(self):
        holdings = board.holdings_from_frame(
            pd.DataFrame({"Holding Percent": [0.08, 0.01]}, index=["AAA", "BBB"])
        )
        self.assertEqual([item["symbol"] for item in holdings], ["AAA", "BBB"])
        closes = {
            "AAA": _series([100, 100, 100, 112]),
            "BBB": _series([100, 101]),
        }
        result = board.strong_holdings(holdings, closes)
        self.assertEqual([item["symbol"] for item in result["rows"]], ["AAA"])
        self.assertIn("BBB", result["skipped"])
        self.assertIn("不是漲 0", result["note"])
        empty = board.strong_holdings([], {})
        self.assertEqual(empty["rows"], [])
        self.assertIn("沒有真實前十大", empty["note"])

    def test_download_without_rows_is_empty_not_zero(self):
        raw = pd.DataFrame(
            {("AAA", "Close"): [10, 11], ("BBB", "Close"): [float("nan"), float("nan")]},
            index=pd.bdate_range("2026-01-01", periods=2),
        )
        raw.columns = pd.MultiIndex.from_tuples(raw.columns)
        found = board.closes_from_download(raw, ("AAA", "BBB", "CCC"))
        self.assertEqual(len(found["AAA"]), 2)
        self.assertTrue(found["BBB"].empty)
        self.assertTrue(found["CCC"].empty)


class SummaryRenderTests(unittest.TestCase):
    def test_apptest_says_unknown_is_not_a_pass(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        script = """
import streamlit as st
import sector_board
rows = [{
    "代號": "XLK", "名稱": "科技", "類型": "攻擊",
    "4週報酬": "未知", "4週相對大盤": "未知", "4週方向": "未知",
    "12週報酬": "未知", "12週相對大盤": "未知", "12週方向": "未知",
    "第五頁立場": "未對應第五頁產業", "進場關卡": "⚪ 未知（缺價格資料）",
    "對齊天數": 0,
}]
sector_board.render_sector_summary(st, rows, {"XLK": {"rows": [], "skipped": [], "note": "沒有真實前十大，不列強勢股。"}})
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(str(item.value) for item in list(at.caption) + list(at.markdown))
        self.assertIn("不是資金流", blob)
        self.assertIn("未知不算通過也不算未過", blob)
        self.assertIn("過一關先試單 1/3", blob)
        self.assertNotIn("未知不算通過，也不算未過", blob)
        self.assertNotIn("總經允許", blob)
        self.assertIn("不列強勢股", blob)


if __name__ == "__main__":
    unittest.main()
