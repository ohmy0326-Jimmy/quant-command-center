"""每日淨值快照：出入金、upsert、API 金鑰、捕獲率、NAV_Snapshots 退回 Daily_NAV、卡片。"""
import os
import sys
import textwrap
import unittest
import unittest.mock
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nav_snapshot as ns  # noqa: E402
from futures_ledger import ledger_cash_flows  # noqa: E402

TPE = ZoneInfo("Asia/Taipei")
DAY = date(2026, 11, 4)


class FakeWorksheet:
    def __init__(self, title, rows=None):
        self.title = title
        self.rows = [list(r) for r in (rows or [])]
        self.updates = []

    def _cell_rows(self, n):
        while len(self.rows) < n:
            self.rows.append([])

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def col_values(self, n):
        vals = [r[n - 1] if len(r) >= n else "" for r in self.rows]
        while vals and not str(vals[-1]).strip():
            vals.pop()
        return vals

    def get_all_records(self):
        if not self.rows:
            return []
        head = self.rows[0]
        return [dict(zip(head, r)) for r in self.rows[1:]]

    def update(self, range_name, values, value_input_option=None):
        self.updates.append((range_name, value_input_option))
        start = range_name.split(":")[0]
        row = int("".join(c for c in start if c.isdigit()))
        col = 0
        for ch in (c for c in start if c.isalpha()):
            col = col * 26 + (ord(ch) - 64)
        self._cell_rows(row)
        current = list(self.rows[row - 1])
        while len(current) < col - 1:
            current.append("")
        self.rows[row - 1] = current[: col - 1] + list(values[0]) + current[col - 1 + len(values[0]):]


class FakeBook:
    def __init__(self, tabs):
        self.tabs = {k: (v if isinstance(v, FakeWorksheet) else FakeWorksheet(k, v)) for k, v in tabs.items()}
        self.added = []

    def worksheet(self, title):
        if title not in self.tabs:
            from gspread.exceptions import WorksheetNotFound

            raise WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        self.added.append(title)
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]


def _metrics(nav=1_000_000.0):
    return {"nav": nav, "tw_nav": 600_000.0, "us_nav": 400_000.0, "notional_exposure": 1_500_000.0,
            "total_leverage": 1.5, "account_beta": 1.234567, "leverage": 9.9}


class FlowTests(unittest.TestCase):
    def test_ledger_flows_signs(self):
        ledger = pd.DataFrame([
            {"日期": "2026/11/04", "類型": "入金", "金額": -50000},
            {"日期": "2026-11-04", "類型": "出金", "金額": 20000},
            {"日期": "2026-11-03", "類型": "出入金", "金額": -1000},
            {"日期": "2026-11-04", "類型": "平倉", "金額": 9999},
        ])
        flows = ledger_cash_flows(ledger)
        self.assertEqual(ns.net_flow_on(DAY, flows), 30000.0)
        self.assertEqual(ns.net_flow_on(date(2026, 11, 3), flows), -1000.0)
        self.assertEqual(ledger_cash_flows(None), [])

    def test_cash_flows_tab_converts_usd_and_skips_unknown(self):
        tab = pd.DataFrame([
            {"日期": "2026-11-04", "帳戶": "美股", "金額": "1,000", "幣別": "USD", "備註": "電匯"},
            {"日期": "2026-11-04", "帳戶": "台股", "金額": -5000, "幣別": "", "備註": ""},
            {"日期": "2026-11-04", "帳戶": "x", "金額": 1, "幣別": "JPY", "備註": ""},
            {"日期": "", "帳戶": "", "金額": "", "幣別": "", "備註": ""},
        ])
        flows, warnings = ns.cash_flows_tab_flows(tab, 32.0)
        self.assertAlmostEqual(ns.net_flow_on(DAY, flows), 32000.0 - 5000.0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("JPY", warnings[0])
        self.assertEqual(ns.cash_flows_tab_flows(None, 32.0), ([], []))


class UpsertTests(unittest.TestCase):
    def test_creates_tab_then_updates_same_day(self):
        book = FakeBook({})
        row = ns.snapshot_row(DAY, _metrics(), 1000.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE))
        self.assertEqual(list(row), ns.SNAPSHOT_HEADER)
        self.assertEqual(row["槓桿"], 1.5)
        self.assertEqual(row["帳戶Beta"], 1.2346)
        first = ns.upsert_snapshot(book, row)
        self.assertEqual(first, {"action": "inserted", "row_number": 2, "sheet_created": True, "header_migrated": False})
        ws = book.tabs[ns.SNAPSHOT_SHEET]
        self.assertEqual(ws.rows[0], ns.SNAPSHOT_HEADER)
        again = dict(row, 總淨值=2_000_000)
        second = ns.upsert_snapshot(book, again)
        self.assertEqual(second["action"], "updated")
        self.assertEqual(second["row_number"], 2)
        self.assertEqual(len(ws.rows), 2)
        self.assertEqual(ws.rows[1][1], 2_000_000)
        nxt = ns.snapshot_row(date(2026, 11, 5), _metrics(), 0.0, datetime(2026, 11, 5, 14, 0, tzinfo=TPE))
        self.assertEqual(ns.upsert_snapshot(book, nxt)["row_number"], 3)
        self.assertTrue(all(opt == "USER_ENTERED" for _r, opt in ws.updates))

    def test_matches_sheet_formatted_dates(self):
        book = FakeBook({ns.SNAPSHOT_SHEET: [ns.SNAPSHOT_HEADER, ["2026/11/4"] + [0] * 8]})
        row = ns.snapshot_row(DAY, _metrics(), 0.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE))
        self.assertEqual(ns.upsert_snapshot(book, row)["action"], "updated")

    def test_wrong_header_refuses(self):
        book = FakeBook({ns.SNAPSHOT_SHEET: [["date", "nav"]]})
        row = ns.snapshot_row(DAY, _metrics(), 0.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE))
        with self.assertRaises(RuntimeError):
            ns.upsert_snapshot(book, row)


class TakeSnapshotTests(unittest.TestCase):
    def test_end_to_end_with_fake_book(self):
        from unittest.mock import patch

        book = FakeBook({
            "Active_Positions": [["帳戶", "現金"], ["台股", 100]],
            "持倉統計": [["標的"]],
            "Futures_Ledger": [["日期", "類型", "金額"], ["2026-11-04", "入金", 10000]],
            "Cash_Flows": [ns.CASH_FLOWS_HEADER, ["2026-11-04", "美股", 100, "USD", ""]],
        })
        fake_valuation = (pd.DataFrame(), pd.DataFrame(), _metrics(), ["一列報價失敗"])
        with patch("portfolio_risk.read_quant_sheets", return_value=(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())), \
                patch("portfolio_risk.value_positions_safely", return_value=fake_valuation):
            out = ns.take_snapshot(now=datetime(2026, 11, 4, 14, 30, tzinfo=TPE), spreadsheet=book, usdtwd=30.0,
                                   market=lambda p: ({}, {}, {}, {}), breadth_compute=False)
        self.assertEqual(out["action"], "inserted")
        self.assertEqual(out["row"]["出入金"], 13000)
        self.assertEqual(out["flow_sources"], {"futures_ledger": 10000, "cash_flows": 3000})
        self.assertEqual(out["row"]["寫入時間"], "2026-11-04 14:30:00")
        self.assertEqual(out["warnings"], ["一列報價失敗"])
        self.assertIn(ns.SNAPSHOT_SHEET, book.added)

    def test_refuses_non_positive_nav(self):
        from unittest.mock import patch

        book = FakeBook({})
        with patch("portfolio_risk.read_quant_sheets", return_value=(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())), \
                patch("portfolio_risk.value_positions_safely", return_value=(None, None, {"nav": 0.0}, [])):
            with self.assertRaises(RuntimeError):
                ns.take_snapshot(spreadsheet=book, usdtwd=30.0, market=lambda p: ({}, {}, {}, {}), breadth_compute=False)
        self.assertEqual(book.added, [])


class NavHistoryFallbackTests(unittest.TestCase):
    def test_prefers_snapshots_then_daily_nav(self):
        from portfolio_risk import nav_history_frame

        snaps = [ns.SNAPSHOT_HEADER, ["2026/11/04", 100, 60, 40, 150, 1.5, 1, 0, "x"]]
        both = FakeBook({"NAV_Snapshots": snaps, "Daily_NAV": [["日期", "總淨值"], ["2026-01-01", 5]]})
        self.assertEqual(nav_history_frame(both).attrs["source"], "NAV_Snapshots")
        self.assertEqual(nav_history_frame(both)["日期"].tolist(), ["2026-11-04"])
        legacy = FakeBook({"NAV_Snapshots": [ns.SNAPSHOT_HEADER], "Daily_NAV": [["日期", "總淨值"], ["2026-01-01", 5]]})
        self.assertEqual(nav_history_frame(legacy).attrs["source"], "Daily_NAV")
        none = nav_history_frame(FakeBook({}))
        self.assertTrue(none.empty)
        self.assertEqual(none.attrs["source"], "")


def _history(n, start="2026-01-01", flows=None, seed=0):
    days = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(seed)
    bench = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    nav = 1_000_000 * bench / bench[0]
    frame = pd.DataFrame({
        "日期": [d.date().isoformat() for d in days],
        "總淨值": nav,
        "台股淨值": nav * 0.5,
        "美股淨值": nav * 0.5,
        "出入金": 0.0,
    })
    for i, amount in (flows or {}).items():
        # 入金當天之後整個部位等比例放大（錢也一起投入），出入金欄記當天金額
        scale = 1 + amount / frame.loc[i, "總淨值"]
        frame.loc[i:, ["總淨值", "台股淨值", "美股淨值"]] *= scale
        frame.loc[i, "出入金"] = amount
    close = pd.Series(bench, index=days)
    return frame, close


class CaptureTests(unittest.TestCase):
    def test_flow_adjusted_return_formula(self):
        hist = ns.history_frame(pd.DataFrame({"日期": ["2026-11-02", "2026-11-03"], "總淨值": [100.0, 160.0], "出入金": [0, 50]}))
        r = ns.flow_adjusted_returns(hist)
        self.assertAlmostEqual(r.iloc[0], (160 - 50) / 100 - 1)

    def test_insufficient_sample_skips_benchmarks(self):
        frame, _ = _history(40)
        out = ns.capture_summary(frame, closes=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["days"], 39)
        self.assertIn("樣本不足", ns.capture_status_text(out))
        self.assertIn("39", ns.capture_status_text(out))
        self.assertIn("樣本不足", ns.capture_status_text(None))

    def test_tracking_book_has_100_capture_even_with_deposit(self):
        frame, close = _history(130, flows={70: 250_000.0})
        # 美股收盤日期比台北快照早一天（台北週二下午看到的是美國週一收盤）
        out = ns.capture_summary(frame, {"^TWII": close, "^GSPC": close.shift(-1, freq="D")})
        self.assertTrue(out["ok"])
        rows = {r["基準"]: r for r in out["rows"]}
        self.assertEqual(set(rows), {"台股 ^TWII", "美股 ^GSPC", "台／美淨值加權"})
        for label in rows:
            self.assertAlmostEqual(rows[label]["上檔捕獲"], 100.0, places=6)
            self.assertAlmostEqual(rows[label]["下檔捕獲"], 100.0, places=6)
            self.assertGreaterEqual(rows[label]["月數"], 6)

    def test_double_leverage_gives_about_200(self):
        days = pd.bdate_range("2026-01-01", periods=150)
        rng = np.random.default_rng(3)
        rb = rng.normal(0, 0.01, len(days))
        close = pd.Series(100 * np.cumprod(1 + rb), index=days)
        nav = 1e6 * np.cumprod(1 + 2 * np.r_[0, rb[1:]])
        frame = pd.DataFrame({"日期": [d.date().isoformat() for d in days], "總淨值": nav})
        out = ns.capture_summary(frame, {"^TWII": close, "^GSPC": close})
        tw = out["rows"][0]
        self.assertGreater(tw["上檔捕獲"], 180)
        self.assertGreater(tw["下檔捕獲"], 180)
        # 沒有台／美淨值欄（舊 Daily_NAV）→ 加權列沒有月份
        self.assertEqual(out["rows"][2]["月數"], 0)

    def test_fetch_benchmark_raises_on_empty(self):
        with self.assertRaises(RuntimeError):
            ns.fetch_benchmark_closes("2026-01-01", history=lambda t, s: pd.DataFrame())
        got = ns.fetch_benchmark_closes("2026-01-10", history=lambda t, s: pd.DataFrame({"Close": [1.0, 2.0]}))
        self.assertEqual(set(got), {"^TWII", "^GSPC"})


class SnapshotApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from api.main import app, get_snapshot_writer

        self.app = app
        self.calls = {"n": 0}

        def writer():
            self.calls["n"] += 1
            row = ns.snapshot_row(DAY, _metrics(), 0.0, datetime(2026, 11, 4, 14, 0, tzinfo=TPE))
            return {"date": row["日期"], "action": "inserted", "sheet": ns.SNAPSHOT_SHEET, "sheet_created": False,
                    "row_number": 5, "row": row, "flow_sources": {"futures_ledger": 0, "cash_flows": 0},
                    "usdtwd": 32.0, "warnings": []}

        self.writer = writer
        app.dependency_overrides[get_snapshot_writer] = lambda: self.writer
        os.environ["READONLY_API_KEY"] = "read-only-test-key"
        os.environ.pop("QCC_SNAPSHOT_KEY", None)
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        os.environ.pop("QCC_SNAPSHOT_KEY", None)
        os.environ.pop("READONLY_API_KEY", None)

    def test_disabled_without_env(self):
        r = self.client.post("/v1/nav-snapshot", headers={"X-Snapshot-Key": "anything"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.calls["n"], 0)

    def test_wrong_key_and_readonly_key_rejected(self):
        os.environ["QCC_SNAPSHOT_KEY"] = "write-test-key"
        for headers in ({}, {"X-Snapshot-Key": "nope"}, {"X-Snapshot-Key": "read-only-test-key"}, {"X-API-Key": "write-test-key"}):
            self.assertEqual(self.client.post("/v1/nav-snapshot", headers=headers).status_code, 401)
        self.assertEqual(self.calls["n"], 0)
        self.assertEqual(self.client.get("/v1/nav-snapshot", headers={"X-Snapshot-Key": "write-test-key"}).status_code, 405)

    def test_success_returns_row(self):
        os.environ["QCC_SNAPSHOT_KEY"] = "write-test-key"
        r = self.client.post("/v1/nav-snapshot", headers={"X-Snapshot-Key": "write-test-key"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "written")
        self.assertEqual(body["row"]["總淨值"], 1_000_000)
        self.assertEqual(list(body["row"]), ns.SNAPSHOT_HEADER)
        self.assertTrue(body["as_of"].endswith("+08:00"))
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_failure_hides_detail(self):
        os.environ["QCC_SNAPSHOT_KEY"] = "write-test-key"

        def boom():
            raise RuntimeError("SENTINEL write-test-key")

        self.writer = boom
        r = self.client.post("/v1/nav-snapshot", headers={"X-Snapshot-Key": "write-test-key"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["detail"], "snapshot failed")
        self.assertNotIn("SENTINEL", r.text)

    def test_macro_history_is_idempotent_and_does_not_invent_scores(self):
        from macro_cycle import CANONICAL_IDS

        book = FakeBook({})
        missing = ns.macro_candidate_from_sheet(book, "2026-11-04")
        self.assertIsNone(missing["total"])
        self.assertIsNone(missing["regime"])
        first = ns.record_macro_history(book, "2026-11-04", missing)
        self.assertEqual(first["status"], "written")
        self.assertIsNone(first["total"])
        sheet = book.worksheet("Macro_Score_History")
        self.assertEqual(sheet.rows[1][1], "")
        self.assertNotEqual(sheet.rows[1][1], 0)
        second = ns.record_macro_history(book, "2026-11-04", missing)
        self.assertEqual(second["status"], "kept")
        self.assertEqual(len(sheet.rows), 2)

        partial = FakeBook({
            "Macro_Score": [["indicator_id", "name", "score", "source", "value", "trend", "note", "updated_at"],
                            ["vix", "VIX", 1, "manual", "", "", "", "2026-11-04"]],
        })
        self.assertIsNone(ns.macro_candidate_from_sheet(partial, "2026-11-04")["total"])

        scores = [["indicator_id", "name", "score", "source", "value", "trend", "note", "updated_at"]]
        scores.extend([[item_id, item_id, 0, "manual", "", "", "", "2026-11-04"] for item_id in CANONICAL_IDS])
        scored = FakeBook({"Macro_Score": scores})
        candidate = ns.macro_candidate_from_sheet(scored, "2026-11-04")
        self.assertEqual(candidate["total"], 0)
        self.assertEqual(candidate["regime"], "中性")
        written = ns.record_macro_history(scored, "2026-11-04", candidate)
        self.assertEqual(written["status"], "written")
        changed = dict(candidate, total=4, regime="偏多")
        updated = ns.record_macro_history(scored, "2026-11-04", changed)
        self.assertEqual(updated["status"], "written")
        self.assertEqual(updated["action"], "update")
        history = scored.worksheet("Macro_Score_History")
        self.assertEqual(len(history.rows), 2)
        self.assertEqual(history.rows[1][1], 4)
        kept = ns.record_macro_history(scored, "2026-11-04", {"week": "2026-11-04", "total": None, "regime": None})
        self.assertEqual(kept["status"], "kept")
        self.assertEqual(history.rows[1][1], 4)

    def test_macro_failure_does_not_fail_snapshot_summary(self):
        def boom(_spreadsheet, _today):
            raise RuntimeError("history down")

        payload = {"date": "2026-11-04", "action": "inserted", "sheet": "NAV_Snapshots"}
        with unittest.mock.patch.object(ns, "record_macro_history", boom):
            out = ns.attach_macro_history(payload, object(), "2026-11-04")
        self.assertEqual(out["action"], "inserted")
        self.assertEqual(out["sheet"], "NAV_Snapshots")
        self.assertEqual(out["macro_history"]["status"], "failed")
        self.assertIsNone(out["macro_history"]["total"])
        self.assertNotIn("history down", out["macro_history"]["detail"])

    def test_warning_backfill_cannot_fail_the_snapshot(self):
        payload = {"date": "2026-11-04", "action": "inserted", "sheet": "NAV_Snapshots", "row": {"寫入時間": "t"}}

        def boom(*_args, **_kwargs):
            raise RuntimeError("backfill hung")

        with unittest.mock.patch("bear_warning.backfill_warning_chunk", boom):
            out = ns.attach_warning_history(payload, FakeBook({}), "2026-11-04")
        self.assertEqual(out["action"], "inserted")
        self.assertEqual(out["sheet"], "NAV_Snapshots")
        self.assertEqual(out["bear_warning_history"]["status"], "failed")
        self.assertNotIn("backfill hung", out["bear_warning_history"]["detail"])

    def test_snapshot_key_is_redacted(self):
        from api.auth import redact_secrets

        os.environ["QCC_SNAPSHOT_KEY"] = "write-test-key-123456"
        self.assertNotIn("write-test-key-123456", redact_secrets("oops write-test-key-123456"))


def _card_script(capture_expr):
    return textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        import streamlit as st
        from order_assistant import build_sample_snapshot
        from risk_performance import render_risk_performance_card

        snap = build_sample_snapshot()
        render_risk_performance_card(st, snap.metrics, None, {capture_expr})
        """
    )


def test_card_shows_insufficient_sample():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_card_script('{"ok": False, "days": 12, "need": 60, "rows": []}'), default_timeout=60).run()
    assert not at.exception
    captions = " ".join(str(c.value) for c in at.caption)
    assert "樣本不足（目前 12 個交易日，需 60）" in captions
    assert len(at.dataframe) == 3


def test_card_shows_capture_table():
    from streamlit.testing.v1 import AppTest

    cap = ('{"ok": True, "days": 80, "need": 60, "start": "2026-07-01", "rows": ['
           '{"基準": "台股 ^TWII", "上檔捕獲": 120.4, "下檔捕獲": 85.0, "月數": 4, "上漲月": 3, "下跌月": 1},'
           '{"基準": "美股 ^GSPC", "上檔捕獲": None, "下檔捕獲": 90.0, "月數": 4, "上漲月": 0, "下跌月": 4}]}')
    at = AppTest.from_string(_card_script(cap), default_timeout=60).run()
    assert not at.exception
    assert len(at.dataframe) == 4
    table = at.dataframe[3].value
    assert table["上檔捕獲"].tolist() == ["120%", "—"]
    assert table["下檔捕獲"].tolist() == ["85%", "90%"]


if __name__ == "__main__":
    unittest.main()
