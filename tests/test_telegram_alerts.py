"""Telegram 即時警報：狀態轉換、遲滯、靜音時段、每日上限、金鑰。不連網。"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import alert_engine as ae
from alert_engine import CRITICAL, DAILY_KEY, MemoryAlertStore, Record, run_check
from api.main import app, get_alert_runner
from telegram_html import markdown_to_html, send_message, titled_message

TPE = ae.TAIPEI
DAY = datetime(2026, 11, 4, 14, 0, tzinfo=TPE)  # 非靜音時段
NIGHT = datetime(2026, 11, 5, 2, 0, tzinfo=TPE)  # 靜音時段


class Sender:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, alert, silent):
        self.calls.append((alert.key, alert.severity, silent))
        return self.ok


def step(readings, store, now, sender=None):
    sender = sender or Sender()
    result = run_check(readings, store.load(), sender, now)
    store.save(result.states)
    return result, sender


class MarginTests(unittest.TestCase):
    def test_near_then_call_then_hourly_remind_then_clear(self):
        store = MemoryAlertStore()
        r, s = step({"margin": {"already": False, "move_pct": -12.0, "index_level": 20000}}, store, DAY)
        self.assertEqual(s.calls, [])
        r, s = step({"margin": {"already": False, "move_pct": -4.5, "index_level": 21000}}, store, DAY)
        self.assertEqual(s.calls, [("margin", CRITICAL, False)])
        self.assertEqual(store.records["margin"].state, "near")
        r, s = step({"margin": {"already": False, "move_pct": -4.0}}, store, DAY + timedelta(minutes=15))
        self.assertEqual(s.calls, [])  # 狀態沒變不推
        r, s = step({"margin": {"already": True, "move_pct": 0.0}}, store, DAY + timedelta(minutes=30))
        self.assertEqual(s.calls, [("margin", CRITICAL, False)])
        r, s = step({"margin": {"already": True}}, store, DAY + timedelta(minutes=45))
        self.assertEqual(s.calls, [])  # 未滿一小時
        r, s = step({"margin": {"already": True}}, store, DAY + timedelta(minutes=90))
        self.assertEqual(len(s.calls), 1)  # 每小時再提醒
        self.assertIn("每小時", r.sent[0].title)
        r, s = step({"margin": {"already": False, "move_pct": -5.5}}, store, DAY + timedelta(minutes=105))
        self.assertEqual(store.records["margin"].state, "near")  # 遲滯：5.5% 仍在解除線 6% 內
        r, s = step({"margin": {"already": False, "move_pct": -6.5, "direction": "下跌"}}, store, DAY + timedelta(minutes=120))
        self.assertEqual(store.records["margin"].state, "ok")
        self.assertEqual(s.calls[0][1], ae.NORMAL)

    def test_near_hysteresis_no_flapping(self):
        store = MemoryAlertStore()
        moves = [-4.9, -5.2, -4.8, -5.4, -4.9]
        total = 0
        for i, m in enumerate(moves):
            _r, s = step({"margin": {"already": False, "move_pct": m}}, store, DAY + timedelta(minutes=15 * i))
            total += len(s.calls)
        self.assertEqual(total, 1)

    def test_no_futures_is_ok(self):
        store = MemoryAlertStore()
        _r, s = step({"margin": {"already": False, "move_pct": None}}, store, DAY)
        self.assertEqual(s.calls, [])


class DrawdownMacroTests(unittest.TestCase):
    def test_drawdown_levels_and_hysteresis(self):
        store = MemoryAlertStore()
        seq = [(-5, "green", 0), (-12.5, "yellow", 1), (-16.2, "red", 1), (-15.5, "red", 0), (-14.8, "yellow", 1),
               (-11.5, "yellow", 0), (-10.9, "green", 1)]
        for i, (dd, state, n) in enumerate(seq):
            _r, s = step({"drawdown": {"dd_pct": dd}}, store, DAY + timedelta(minutes=15 * i))
            self.assertEqual(store.records["drawdown"].state, state, dd)
            self.assertEqual(len(s.calls), n, dd)
            if state == "red" and n:
                self.assertEqual(s.calls[0][1], CRITICAL)

    def test_macro_levels(self):
        store = MemoryAlertStore()
        y, r = ae.macro_risk.GREEN_MAX + 1, ae.macro_risk.YELLOW_MAX + 1
        _r, s = step({"macro": {"score": y}}, store, DAY)
        self.assertEqual(store.records["macro"].state, "yellow")
        _r, s = step({"macro": {"score": r}}, store, DAY)
        self.assertEqual(s.calls[0][1], CRITICAL)
        _r, s = step({"macro": {"score": r - 1}}, store, DAY)
        self.assertEqual((store.records["macro"].state, s.calls), ("red", []))
        _r, s = step({"macro": {"score": r - 2}}, store, DAY)
        self.assertEqual(store.records["macro"].state, "yellow")
        _r, s = step({"macro": {"score": y - 1}}, store, DAY)
        self.assertEqual(store.records["macro"].state, "yellow")
        _r, s = step({"macro": {"score": 0}}, store, DAY)
        self.assertEqual(store.records["macro"].state, "green")


class OtherRuleTests(unittest.TestCase):
    def test_hedge_needs_release_to_turn_off(self):
        store = MemoryAlertStore()
        _r, s = step({"hedge": {"alert": True, "lights": {"ma60": True}}}, store, DAY)
        self.assertEqual(len(s.calls), 1)
        _r, s = step({"hedge": {"alert": False, "release": False}}, store, DAY)
        self.assertEqual((store.records["hedge"].state, s.calls), ("on", []))
        _r, s = step({"hedge": {"alert": False, "release": True}}, store, DAY)
        self.assertEqual(store.records["hedge"].state, "off")
        self.assertEqual(len(s.calls), 1)

    def test_stop_loss_only_new(self):
        store = MemoryAlertStore()
        a = {"ticker": "2330", "name": "台積電", "account": "A", "roi": -11, "max_loss_pct": -10}
        b = {"ticker": "2317", "name": "鴻海", "account": "A", "roi": -21, "max_loss_pct": -20}
        _r, s = step({"stop_loss": {"positions": [a]}}, store, DAY)
        self.assertEqual(len(s.calls), 1)
        _r, s = step({"stop_loss": {"positions": [a]}}, store, DAY)
        self.assertEqual(s.calls, [])
        # a 回升到 -9（停損線 +1pp）仍留名單；b 新觸發
        _r, s = step({"stop_loss": {"positions": [dict(a, roi=-9), b]}}, store, DAY)
        self.assertEqual(len(s.calls), 1)
        self.assertIn("鴻海", _r.sent[0].body)
        self.assertEqual(json.loads(store.records["stop_loss"].state), ["A|2317", "A|2330"])
        _r, s = step({"stop_loss": {"positions": [dict(a, roi=-11)]}}, store, DAY)
        self.assertEqual(s.calls, [])  # a 沒離開名單，不重複推

    def test_leverage_and_cash_hysteresis(self):
        store = MemoryAlertStore()
        counts = []
        for lev in (1.4, 1.52, 1.48, 1.51, 1.44):
            _r, s = step({"leverage": {"value": lev}}, store, DAY)
            counts.append(len(s.calls))
        self.assertEqual(counts, [0, 1, 0, 0, 1])
        counts = []
        for cash in (45, 39, 41, 38, 42.5):
            _r, s = step({"cash": {"value": cash}}, store, DAY)
            counts.append(len(s.calls))
        self.assertEqual(counts, [0, 1, 0, 0, 1])

    def test_regime_and_quadrant_baseline_then_change(self):
        store = MemoryAlertStore()
        _r, s = step({"regime": {"regime": "中性", "total": 0}, "quadrant": {"items": {"半導體": "成長", "航運": "衰退"}}}, store, DAY)
        self.assertEqual(s.calls, [])  # 第一次只記錄
        _r, s = step({"regime": {"regime": "偏多", "total": 5}, "quadrant": {"items": {"半導體": "趨緩", "航運": "衰退"}}}, store, DAY)
        self.assertEqual(sorted(k for k, *_ in s.calls), ["quadrant", "regime"])
        self.assertIn("半導體：成長 → 趨緩", [a for a in _r.sent if a.key == "quadrant"][0].body)

    def test_missing_reading_keeps_state(self):
        store = MemoryAlertStore()
        step({"leverage": {"value": 1.6}}, store, DAY)
        r, s = step({"leverage": None}, store, DAY)
        self.assertEqual((store.records["leverage"].state, s.calls), ("high", []))
        self.assertIn("leverage", r.skipped)


class QuietCapTests(unittest.TestCase):
    def test_quiet_hours_only_critical_has_sound(self):
        store = MemoryAlertStore()
        _r, s = step({"margin": {"already": True}, "leverage": {"value": 2.0}, "drawdown": {"dd_pct": -13}}, store, NIGHT)
        by_key = {k: silent for k, _sev, silent in s.calls}
        self.assertEqual(by_key, {"margin": False, "leverage": True, "drawdown": True})
        self.assertEqual(s.calls[0][0], "margin")  # 緊急的排前面

    def test_quiet_boundaries(self):
        self.assertTrue(ae.is_quiet(datetime(2026, 11, 4, 0, 0, tzinfo=TPE)))
        self.assertTrue(ae.is_quiet(datetime(2026, 11, 4, 6, 59, tzinfo=TPE)))
        self.assertFalse(ae.is_quiet(datetime(2026, 11, 4, 7, 0, tzinfo=TPE)))
        self.assertTrue(ae.is_quiet(datetime(2026, 11, 3, 17, 30, tzinfo=ae.ZoneInfo("UTC"))))  # 台北 01:30

    def test_daily_cap_and_reset(self):
        store = MemoryAlertStore({DAILY_KEY: Record(state=DAY.date().isoformat(), value="11")})
        r, s = step({"leverage": {"value": 2.0}, "cash": {"value": 10}}, store, DAY)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(len(r.suppressed), 1)
        self.assertEqual(store.records[DAILY_KEY].value, "12")
        suppressed_key = r.suppressed[0].key
        self.assertNotIn(suppressed_key, store.records)  # 被擋下的不推進狀態
        r, s = step({"leverage": {"value": 2.0}, "cash": {"value": 10}}, store, DAY + timedelta(minutes=15))
        self.assertEqual(s.calls, [])
        r, s = step({"leverage": {"value": 2.0}, "cash": {"value": 10}}, store, DAY + timedelta(days=1))
        self.assertEqual([k for k, *_ in s.calls], [suppressed_key])  # 隔天補送
        self.assertEqual(store.records[DAILY_KEY].value, "1")

    def test_cap_prefers_critical(self):
        store = MemoryAlertStore({DAILY_KEY: Record(state=DAY.date().isoformat(), value="11")})
        _r, s = step({"leverage": {"value": 2.0}, "margin": {"already": True}}, store, DAY)
        self.assertEqual([k for k, *_ in s.calls], ["margin"])

    def test_send_failure_retries_next_run(self):
        store = MemoryAlertStore()
        r, s = step({"leverage": {"value": 2.0}}, store, DAY, Sender(ok=False))
        self.assertEqual(len(r.failed), 1)
        r, s = step({"leverage": {"value": 2.0}}, store, DAY)
        self.assertEqual(len(s.calls), 1)


class TelegramHtmlTests(unittest.TestCase):
    def test_bold_and_escape(self):
        self.assertEqual(markdown_to_html("**注意** a<b & c"), "<b>注意</b> a&lt;b &amp; c")
        msg = titled_message("AI <晨報>", "**一**")
        self.assertIn("<b>【AI &lt;晨報&gt;】</b>", msg)
        self.assertNotIn("**", msg)

    def test_send_uses_html_parse_mode(self):
        post = MagicMock()
        send_message("TOKEN", "1", "<b>x</b>", disable_notification=True, post=post)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["disable_notification"])

    def test_alert_html_escapes(self):
        html = ae.Alert("x", ae.NORMAL, "a<b", "c&d").html(DAY)
        self.assertIn("a&lt;b", html)
        self.assertIn("c&amp;d", html)


class AlertEndpointTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0

        def runner():
            self.calls += 1
            return {"sent": [], "suppressed": [], "failed": [], "skipped": [], "states": {}, "sent_today": 0}

        app.dependency_overrides[get_alert_runner] = lambda: runner
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        os.environ.pop("QCC_PUSH_KEY", None)

    def test_disabled_without_key(self):
        os.environ.pop("QCC_PUSH_KEY", None)
        r = self.client.post("/v1/alert-check", headers={"X-Push-Key": "x"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.calls, 0)

    def test_wrong_or_missing_key(self):
        os.environ["QCC_PUSH_KEY"] = "push-key-not-a-secret"
        self.assertEqual(self.client.post("/v1/alert-check").status_code, 401)
        self.assertEqual(self.client.post("/v1/alert-check", headers={"X-Push-Key": "nope"}).status_code, 401)
        os.environ["READONLY_API_KEY"] = "ro-key"
        self.assertEqual(self.client.post("/v1/alert-check", headers={"X-API-Key": "ro-key"}).status_code, 401)
        self.assertEqual(self.calls, 0)

    def test_ok(self):
        os.environ["QCC_PUSH_KEY"] = "push-key-not-a-secret"
        r = self.client.post("/v1/alert-check", headers={"X-Push-Key": "push-key-not-a-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "checked")
        self.assertNotIn("push-key-not-a-secret", r.text)

    def test_telegram_not_configured(self):
        from alert_inputs import TelegramNotConfigured

        def runner():
            raise TelegramNotConfigured()

        app.dependency_overrides[get_alert_runner] = lambda: runner
        os.environ["QCC_PUSH_KEY"] = "k"
        self.assertEqual(self.client.post("/v1/alert-check", headers={"X-Push-Key": "k"}).status_code, 503)

    def test_sheet_store_roundtrip(self):
        ws = MagicMock()
        ws.get_all_records.return_value = [{"key": "cash", "state": "low", "value": 38.5, "last_sent_at": "", "updated_at": "t"}]
        store = ae.SheetAlertStore.__new__(ae.SheetAlertStore)
        store.worksheet = ws
        self.assertEqual(store.load()["cash"].state, "low")
        store.save({"cash": Record("ok", "41", "", "t")})
        rows = ws.update.call_args.kwargs["values"]
        self.assertEqual(rows[0], ae.ALERT_STATE_HEADERS)
        self.assertEqual(rows[1][:2], ["cash", "ok"])


if __name__ == "__main__":
    unittest.main()
