"""第四頁「風險與績效」卡片、第六頁現況健檢摘要、AI 壓力測試搬到第六頁、Daily_NAV 可不存在。"""
import os
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import risk_performance as rp  # noqa: E402
from nav_scenario_sim import Strategy  # noqa: E402
from order_assistant import build_sample_snapshot  # noqa: E402
from portfolio_risk import beta_leverage_rows, equity_ratio_pct  # noqa: E402
from sheet_dates import nav_growth_series  # noqa: E402


def _series(values):
    return pd.Series(values, index=[f"2026-10-{i + 1:02d}" for i in range(len(values))])


class DrawdownTests(unittest.TestCase):
    def test_no_history_is_unavailable_not_zero(self):
        for series in (None, _series([]), _series([100.0])):
            stats = rp.drawdown_stats(series)
            self.assertFalse(stats["available"])
            self.assertIsNone(stats["max_dd_pct"])
            self.assertIsNone(stats["current_dd_pct"])
        self.assertEqual(rp.brake_light(None)[0], "⚪")

    def test_max_and_current_drawdown(self):
        stats = rp.drawdown_stats(_series([100.0, 120.0, 90.0, 108.0]))
        self.assertTrue(stats["available"])
        self.assertAlmostEqual(stats["max_dd_pct"], -25.0)
        self.assertAlmostEqual(stats["current_dd_pct"], -10.0)
        self.assertEqual(stats["start"], "2026-10-01")

    def test_brake_thresholds_follow_nav_scenario_defaults(self):
        self.assertAlmostEqual(rp.BRAKE_PAUSE_PCT, Strategy.brake_pause * 100)
        self.assertAlmostEqual(rp.BRAKE_CUT_PCT, Strategy.brake_cut * 100)
        self.assertEqual(rp.brake_light(-5.0)[0], "🟢")
        self.assertEqual(rp.brake_light(-12.0)[0], "🟡")
        self.assertEqual(rp.brake_light(-14.0)[0], "🟡")
        self.assertEqual(rp.brake_light(-16.0)[0], "🔴")
        self.assertEqual(rp.brake_light(-30.0)[0], "🔴")

    def test_nav_growth_without_daily_nav_sheet_has_only_today(self):
        empty = pd.DataFrame(columns=["日期", "總淨值"])
        series = nav_growth_series(empty, 1_000_000)
        self.assertEqual(len(series), 1)
        self.assertFalse(rp.drawdown_stats(series)["available"])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.snap = build_sample_snapshot()
        self.m = self.snap.metrics

    def test_definitions_unchanged(self):
        s = rp.risk_performance_summary(self.m)
        nav = self.m["nav"]
        self.assertAlmostEqual(s["leverage"], self.m["notional_exposure"] / nav, places=9)
        self.assertAlmostEqual(s["account_beta"], self.m["beta_exposure"] / nav, places=9)
        self.assertEqual(s["equity_ratio_pct"], equity_ratio_pct(self.m.get("futures_equity"), self.m.get("futures_initial")))
        total = [r for r in beta_leverage_rows(self.m) if r["市場"] == "合計"][0]
        self.assertAlmostEqual(total["槓桿"], s["leverage"], places=9)
        self.assertAlmostEqual(total["帳戶Beta"], s["account_beta"], places=9)

    def test_page6_line_points_to_page4(self):
        line = rp.page6_health_line(self.m, 1.3)
        self.assertIn("詳見第四頁「風險與績效」", line)
        self.assertIn(f"槓桿 {rp.risk_performance_summary(self.m)['leverage']:.2f}x（上限 1.30x）", line)
        self.assertIn("帳戶 Beta", line)
        self.assertEqual(line.count("\n"), 0)

    def test_neutrality(self):
        self.assertIn("高度偏多", rp.neutrality_label(0.8))
        self.assertIn("市場中性", rp.neutrality_label(0.0))
        self.assertIn("偏空", rp.neutrality_label(-0.5))


def _card_script(with_history: bool) -> str:
    return textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        import pandas as pd
        import streamlit as st
        from order_assistant import build_sample_snapshot
        from risk_performance import render_risk_performance_card
        from sheet_dates import nav_growth_series

        snap = build_sample_snapshot()
        hist = pd.DataFrame({{"日期": ["2026-01-02", "2026-02-02"], "總淨值": [snap.metrics["nav"] * 1.25, snap.metrics["nav"] * 1.1]}})
        nav_data = hist if {with_history!r} else pd.DataFrame(columns=["日期", "總淨值"])
        render_risk_performance_card(st, snap.metrics, nav_growth_series(nav_data, snap.metrics["nav"]))
        """
    )


def test_card_renders_without_history():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_card_script(False), default_timeout=60).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert labels == ["現金水位", "融資／質押借款", "最大回撤", "回撤煞車"]
    assert [m.value for m in at.metric][2:] == ["—", "⚪"]
    captions = " ".join(str(c.value) for c in at.caption)
    assert "淨值歷史無法取得" in captions
    assert len(at.dataframe) == 1  # Beta／槓桿三列表只出現一次


def test_card_renders_with_history():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_card_script(True), default_timeout=60).run()
    assert not at.exception
    values = {m.label: m.value for m in at.metric}
    assert values["最大回撤"] == "-20.00%"
    assert values["回撤煞車"] == "🔴"


def test_page6_health_and_ai_stress_tab():
    from streamlit.testing.v1 import AppTest

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        import streamlit as st
        from order_assistant_ui import render_order_assistant

        class Dummy:
            def __call__(self, *a, **k):
                raise RuntimeError("offline")
            def clear(self):
                pass

        render_order_assistant(lambda title, text: None, Dummy(), Dummy())
        """
    )
    dates = pd.date_range("2025-01-01", periods=300, freq="D")

    def fake_close(symbols, period="13mo"):
        return {s: pd.Series(np.linspace(100, 200, 300), index=dates) for s in symbols}

    fake = MagicMock()
    fake.models.generate_content.return_value.text = "三點建議"
    with patch("order_insights.fetch_close_history", side_effect=fake_close), patch.dict(os.environ, {"GEMINI_API_KEY": "not-a-real-key"}), patch("google.genai.Client", return_value=fake):
        at = AppTest.from_string(script, default_timeout=90).run()
        assert not at.exception
        infos = " ".join(str(i.value) for i in at.info)
        assert "詳見第四頁「風險與績效」" in infos
        health = at.tabs[0]
        assert health.label == "現況健檢"
        assert "詳見第四頁「風險與績效」" in " ".join(str(i.value) for i in health.info)
        health_labels = [m.label for m in health.metric]
        for gone in ("淨值", "實質曝險", "合併槓桿", "現金水位", "總 Beta", "權益比率（權益數 ÷ 原始保證金）"):
            assert gone not in health_labels
        assert len([d for d in health.dataframe]) >= 1
        subheaders = [str(h.value) for h in at.subheader]
        assert "處置優先順序" in subheaders
        assert "超過單一部位虧損上限的持倉" in subheaders
        assert any("AI 避險基金經理人壓力測試" in h for h in subheaders)
        at.button(key="oa_ai_stress").click().run()
        assert not at.exception
        assert at.session_state["ai_memo_p4"]["text"] == "三點建議"
        prompt = fake.models.generate_content.call_args.kwargs["contents"]
        assert "合併總槓桿率" in prompt and "請給予 3 點風控建議。" in prompt


class FetchSheetsWithoutDailyNavTests(unittest.TestCase):
    def test_missing_daily_nav_tab_does_not_crash(self):
        tabs = {
            "Active_Positions": [{"帳戶名稱": "現貨主帳戶", "現金餘額": 100000, "融資/質押借款": 0}],
            "持倉統計": [],
        }

        class Worksheet:
            def __init__(self, records):
                self.records = records

            def get_all_records(self):
                return self.records

        class Book:
            def worksheet(self, title):
                if title not in tabs:
                    raise KeyError(title)  # gspread 會丟 WorksheetNotFound
                return Worksheet(tabs[title])

        class Client:
            def open(self, name):
                return Book()

        with patch("gspread.authorize", return_value=Client()), patch(
            "oauth2client.service_account.ServiceAccountCredentials.from_json_keyfile_dict", return_value=object()
        ):
            from portfolio_risk import fetch_quant_sheets

            accounts, nav, positions = fetch_quant_sheets({"client_email": "x"})
        self.assertTrue(nav.empty)
        self.assertEqual(list(nav.columns), ["日期", "總淨值"])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(len(nav_growth_series(nav, 123.0)), 1)


class DashboardWiringTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.page4 = text.split('elif page == "第四頁：部位監控與量化風控":', 1)[1].split('elif page == "第五頁', 1)[0]

    def test_page4_has_single_card_and_no_duplicates(self):
        self.assertEqual(self.page4.count("render_risk_performance_card(st, metrics, nav_series, capture, nav_flows)"), 1)
        for gone in ("投資組合 Beta 係數與中性化戰情計分板", "合併總槓桿率", "總現金水位", "歷史資金成長曲線", "render_beta_leverage_table", "啟動 AI 經理人風控與壓力測試"):
            self.assertNotIn(gone, self.page4)
        self.assertIn("AI 壓力測試已移到第六頁", self.page4)
        # 元大權益比率與距追繳仍在帳戶區
        self.assertIn("equity_ratio_pct", self.page4)
        self.assertIn("距追繳", self.page4)


if __name__ == "__main__":
    unittest.main()
