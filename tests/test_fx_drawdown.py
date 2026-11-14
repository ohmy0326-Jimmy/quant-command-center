"""匯率退回值要看得到、NAV_Snapshots 舊表頭補欄、回撤用扣除出入金的淨值指數。"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nav_snapshot as ns  # noqa: E402
import portfolio_risk as pr  # noqa: E402
import risk_performance as rp  # noqa: E402
from sheet_dates import nav_flow_map  # noqa: E402
from tests.test_nav_snapshot import FakeBook, FakeWorksheet, _metrics  # noqa: E402

TPE = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)


def _boom():
    raise RuntimeError("offline")


class ResolveUsdtwdTests(unittest.TestCase):
    def setUp(self):
        pr._last_good_usdtwd.clear()

    tearDown = setUp

    def test_live(self):
        q = pr.resolve_usdtwd(live=lambda: 31.25, now=NOW)
        self.assertEqual((q.rate, q.source, q.is_fallback, q.warning()), (31.25, "live", False, None))

    def test_cached_then_last_known_then_default(self):
        pr.resolve_usdtwd(live=lambda: 31.0, now=NOW)
        cached = pr.resolve_usdtwd(live=_boom, last_known=(30.0, "2026-11-01"), now=NOW + timedelta(hours=5))
        self.assertEqual((cached.rate, cached.source), (31.0, "cached"))
        self.assertIn("最近一次成功", cached.warning())
        stale = pr.resolve_usdtwd(live=_boom, last_known=(30.0, "2026-11-01"), now=NOW + timedelta(days=4))
        self.assertEqual((stale.rate, stale.source, stale.as_of), (30.0, "last_known", "2026-11-01"))
        self.assertIn("NAV_Snapshots", stale.warning())
        pr._last_good_usdtwd.clear()
        hard = pr.resolve_usdtwd(live=_boom, now=NOW)
        self.assertEqual((hard.rate, hard.source), (32.5, "fallback"))
        self.assertIn("32.50", hard.warning())

    def test_insane_live_value_is_a_failure(self):
        q = pr.resolve_usdtwd(live=lambda: 0.031, now=NOW)
        self.assertEqual(q.source, "fallback")
        self.assertEqual(pr._last_good_usdtwd, {})

    def test_fetch_usdtwd_still_returns_float(self):
        with patch("portfolio_risk.fetch_usdtwd_live", side_effect=RuntimeError("x")):
            self.assertEqual(pr.fetch_usdtwd(), 32.5)


class LastKnownTests(unittest.TestCase):
    def test_only_live_rows_count(self):
        frame = pd.DataFrame([
            {"日期": "2026-11-01", "USDTWD": 30.5, "匯率來源": "live"},
            {"日期": "2026/11/03", "USDTWD": "31.1", "匯率來源": "live"},
            {"日期": "2026-11-04", "USDTWD": 32.5, "匯率來源": "fallback"},
            {"日期": "2026-11-02", "USDTWD": 99, "匯率來源": "live"},
        ])
        self.assertEqual(ns.last_known_usdtwd(frame), (31.1, "2026-11-03"))
        self.assertIsNone(ns.last_known_usdtwd(pd.DataFrame({"日期": ["2026-11-01"], "總淨值": [1]})))
        self.assertIsNone(ns.last_known_usdtwd(None))


class LegacyWorksheet(FakeWorksheet):
    def __init__(self, title, rows, col_count):
        super().__init__(title, rows)
        self.col_count = col_count
        self.added_cols = 0

    def add_cols(self, n):
        self.added_cols += n
        self.col_count += n


class HeaderMigrationTests(unittest.TestCase):
    def _row(self, day, source="live"):
        return ns.snapshot_row(day, _metrics(), 0.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE), usdtwd=31.5, fx_source=source)

    def test_legacy_header_gets_two_columns_without_touching_data(self):
        old_row = ["2026-11-03", 900000, 1, 2, 3, 1.1, 0.9, 0, "2026-11-03 14:00:00"]
        ws = LegacyWorksheet(ns.SNAPSHOT_SHEET, [ns.LEGACY_SNAPSHOT_HEADER, old_row], col_count=9)
        book = FakeBook({ns.SNAPSHOT_SHEET: ws})
        from datetime import date

        out = ns.upsert_snapshot(book, self._row(date(2026, 11, 4)))
        self.assertTrue(out["header_migrated"])
        self.assertEqual(ws.added_cols, 2)
        self.assertEqual(ws.rows[0], ns.SNAPSHOT_HEADER)
        self.assertEqual(ws.rows[1], old_row)
        self.assertEqual(ws.rows[2][-2:], [31.5, "live"])
        self.assertEqual(ws.updates[0][0], "J1:K1")
        again = ns.upsert_snapshot(book, self._row(date(2026, 11, 4), "fallback"))
        self.assertFalse(again["header_migrated"])
        self.assertEqual(again["action"], "updated")
        self.assertEqual(ws.rows[2][-1], "fallback")

    def test_unknown_header_still_refuses(self):
        from datetime import date

        book = FakeBook({ns.SNAPSHOT_SHEET: [ns.LEGACY_SNAPSHOT_HEADER + ["自訂欄"]]})
        with self.assertRaises(RuntimeError):
            ns.upsert_snapshot(book, self._row(date(2026, 11, 4)))


class SnapshotFxTests(unittest.TestCase):
    def setUp(self):
        pr._last_good_usdtwd.clear()

    tearDown = setUp

    def _run(self, book, nav_history, **kwargs):
        kwargs.setdefault("breadth_compute", False)
        fake_valuation = (pd.DataFrame(), pd.DataFrame(), _metrics(), [])
        with patch("portfolio_risk.read_quant_sheets", return_value=(pd.DataFrame(), nav_history, pd.DataFrame())), \
                patch("portfolio_risk.value_positions_safely", return_value=fake_valuation) as valued:
            out = ns.take_snapshot(now=datetime(2026, 11, 4, 14, 30, tzinfo=TPE), spreadsheet=book,
                                   market=lambda p: ({}, {}, {}, {}), **kwargs)
        return out, valued.call_args.args[2]

    def test_fx_failure_uses_last_known_and_flags_row(self):
        history = pd.DataFrame([{"日期": "2026-11-03", "總淨值": 1, "USDTWD": 31.7, "匯率來源": "live"}])
        with patch("portfolio_risk.fetch_usdtwd_live", side_effect=RuntimeError("offline")):
            out, used_rate = self._run(FakeBook({}), history)
        self.assertEqual(used_rate, 31.7)
        self.assertEqual(out["usdtwd_source"], "last_known")
        self.assertEqual(out["row"]["匯率來源"], "last_known")
        self.assertEqual(out["row"]["USDTWD"], 31.7)
        self.assertTrue(any("匯率來源＝last_known" in w for w in out["warnings"]))

    def test_hard_fallback_is_flagged(self):
        with patch("portfolio_risk.fetch_usdtwd_live", side_effect=RuntimeError("offline")):
            out, used_rate = self._run(FakeBook({}), pd.DataFrame())
        self.assertEqual((used_rate, out["usdtwd_source"], out["row"]["匯率來源"]), (32.5, "fallback", "fallback"))
        self.assertIn("32.50", out["warnings"][0])

    def test_live_has_no_fx_warning(self):
        with patch("portfolio_risk.fetch_usdtwd_live", return_value=31.9):
            out, used_rate = self._run(FakeBook({}), pd.DataFrame())
        self.assertEqual((used_rate, out["usdtwd_source"]), (31.9, "live"))
        self.assertEqual(out["warnings"], [])
        self.assertEqual(list(out["row"]), ns.SNAPSHOT_HEADER)


class ApiFxTests(unittest.TestCase):
    def test_snapshot_response_carries_source(self):
        from fastapi.testclient import TestClient

        from api.main import app, get_snapshot_writer

        def writer():
            row = ns.snapshot_row(datetime(2026, 11, 4).date(), _metrics(), 0.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE), 32.5, "fallback")
            return {"date": row["日期"], "action": "inserted", "sheet": ns.SNAPSHOT_SHEET, "sheet_created": False,
                    "header_migrated": True, "row_number": 3, "row": row, "flow_sources": {}, "usdtwd": 32.5,
                    "usdtwd_source": "fallback", "warnings": ["美元／台幣即時匯率抓不到"]}

        app.dependency_overrides[get_snapshot_writer] = lambda: writer
        os.environ["QCC_SNAPSHOT_KEY"] = "write-test-key"
        try:
            r = TestClient(app).post("/v1/nav-snapshot", headers={"X-Snapshot-Key": "write-test-key"})
        finally:
            app.dependency_overrides.clear()
            os.environ.pop("QCC_SNAPSHOT_KEY", None)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["usdtwd_source"], "fallback")
        self.assertTrue(body["header_migrated"])
        self.assertEqual(body["row"]["匯率來源"], "fallback")

    def test_live_book_flags_fallback_fx(self):
        import portfolio_briefing as pb

        pr._last_good_usdtwd.clear()
        history = pd.DataFrame([{"日期": "2026-11-03", "總淨值": 1, "USDTWD": 31.4, "匯率來源": "live"}])
        with patch("portfolio_risk.load_service_account_info", return_value={}), \
                patch("portfolio_risk.fetch_quant_sheets", return_value=(pd.DataFrame(), history, pd.DataFrame())), \
                patch("portfolio_risk.fetch_usdtwd_live", side_effect=RuntimeError("offline")):
            book = pb.load_live_book(as_of=datetime(2026, 11, 4, 9, 0, tzinfo=TPE))
        self.assertEqual(book.payload["usdtwd"], 31.4)
        self.assertEqual(book.payload["usdtwd_source"], "last_known")
        self.assertEqual(len(book.payload["warnings"]), 1)
        from api.schemas import PortfolioResponse

        PortfolioResponse.model_validate(book.payload)


def _s(values):
    return pd.Series(values, index=[f"2026-10-{i + 1:02d}" for i in range(len(values))])


class FlowAdjustedDrawdownTests(unittest.TestCase):
    def test_withdrawal_is_not_a_drawdown(self):
        nav = _s([100.0, 102.0, 60.0, 61.0])
        flows = {"2026-10-03": -42.0}
        raw = rp.drawdown_stats(nav)
        self.assertLess(raw["max_dd_pct"], -40)
        s = rp.risk_performance_summary({"nav": 61.0}, nav, flows)
        self.assertGreater(s["drawdown"]["max_dd_pct"], -0.01)
        self.assertEqual(s["brake_light"], "🟢")
        self.assertTrue(s["drawdown"]["flow_adjusted"])

    def test_deposit_does_not_hide_a_loss(self):
        nav = _s([100.0, 85.0, 185.0])
        flows = {"2026-10-03": 100.0}
        s = rp.risk_performance_summary({"nav": 185.0}, nav, flows)
        self.assertAlmostEqual(s["drawdown"]["current_dd_pct"], -15.0)
        self.assertEqual(s["brake_light"], "🔴")  # -15% 已過紅燈線 -12%
        self.assertEqual(rp.risk_performance_summary({"nav": 185.0}, nav)["brake_light"], "🟢")

    def test_index_math_and_no_flows_identity(self):
        nav = _s([100.0, 110.0, 160.0])
        idx = rp.flow_adjusted_index(nav, {"2026-10-03": 50.0})
        self.assertEqual(idx.round(6).tolist(), [100.0, 110.0, 110.0])
        self.assertEqual(rp.flow_adjusted_index(nav, {}).tolist(), nav.tolist())
        self.assertIsNone(rp.flow_adjusted_index(None, {}))

    def test_nav_flow_map(self):
        frame = pd.DataFrame({"日期": ["2026/10/03", "2026-10-04", "bad"], "總淨值": [1, 2, 3], "出入金": ["1,000", "", 5]})
        self.assertEqual(nav_flow_map(frame), {"2026-10-03": 1000.0, "2026-10-04": 0.0})
        self.assertEqual(nav_flow_map(pd.DataFrame({"日期": ["2026-10-03"], "總淨值": [1]})), {})


def test_card_caption_mentions_flow_adjustment():
    import textwrap

    from streamlit.testing.v1 import AppTest

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        import pandas as pd
        import streamlit as st
        from order_assistant import build_sample_snapshot
        from risk_performance import render_risk_performance_card

        snap = build_sample_snapshot()
        nav = snap.metrics["nav"]
        series = pd.Series([nav * 1.5, nav], index=["2026-10-01", "2026-10-02"])
        render_risk_performance_card(st, snap.metrics, series, None, {{"2026-10-02": -nav * 0.5}})
        """
    )
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    values = {m.label: m.value for m in at.metric}
    assert values["最大回撤"] == "0.00%"
    assert values["回撤煞車"] == "🟢"
    captions = " ".join(str(c.value) for c in at.caption)
    assert "扣除出入金的淨值指數（已扣除出入金）" in captions


if __name__ == "__main__":
    unittest.main()
