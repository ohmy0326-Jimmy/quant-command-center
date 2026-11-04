"""第一頁宏觀風險計分板：刻度、缺漏、過期、燈號與今日情境。不連網。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import macro_risk as m

ROOT = Path(__file__).resolve().parents[1]
# 2026-11-04 21:55 台北 = 08:55 紐約，美股尚未開盤
NOW = datetime(2026, 11, 4, 13, 55, tzinfo=timezone.utc)
TODAY = date(2026, 11, 4)


def daily(closes, end="2026-11-04", lows=None, tz=None):
    idx = pd.bdate_range(end=end, periods=len(closes), tz=tz)
    frame = pd.DataFrame({"Close": closes}, index=idx)
    frame["Low"] = lows if lows is not None else frame["Close"]
    return frame


def today_like_loaders():
    vix = daily([17.20, 19.42, 21.15, 20.83, 18.35], tz="America/Chicago")
    tnx = daily([4.989, 4.929, 4.894, 4.952, 4.925], tz="America/Chicago")
    usd = daily([32.2] * 15 + [32.4, 32.43, 32.5, 32.55, 32.585, 32.855], tz="UTC")
    tw_closes = [47000.0] * 15 + [46368.33, 45211.83, 44920.14, 43307.54, 42948.91]
    tw_closes = [47000.0] + tw_closes  # 21 筆
    tw_lows = list(tw_closes)
    tw_lows[-1] = 41855.82
    twii = daily(tw_closes, lows=tw_lows, tz="Asia/Taipei")
    flows = {
        "days": [
            (date(2026, 11, 4), -149_899_834_076.0),
            (date(2026, 11, 3), -107_306_720_357.0),
            (date(2026, 11, 2), -60e9),
            (date(2026, 10, 30), -30e9),
            (date(2026, 10, 29), -20e9),
            (date(2026, 10, 28), 5e9),
        ],
        "complete": True,
    }
    return {
        "vix": lambda: vix,
        "tnx": lambda: tnx,
        "foreign": lambda: flows,
        "usdtwd": lambda: usd,
        "taiex": lambda: twii,
    }


class TnxScaleTests(unittest.TestCase):
    def test_modern_percent_quote_not_divided(self):
        self.assertAlmostEqual(m.tnx_quote_to_yield_percent(4.925), 4.925)

    def test_legacy_index_quote_divided(self):
        self.assertAlmostEqual(m.tnx_quote_to_yield_percent(49.25), 4.925)

    def test_scale_bug_now_triggers(self):
        item = m.score_tnx(daily([4.9, 4.925]), TODAY)
        self.assertAlmostEqual(item["value"], 4.925)
        self.assertEqual(item["points"], 2)
        self.assertEqual(item["status"], m.STATUS_OK)

    def test_below_threshold(self):
        self.assertEqual(m.score_tnx(daily([4.2, 4.3]), TODAY)["points"], 0)


class VixTests(unittest.TestCase):
    def test_pre_open_uses_previous_close_when_higher(self):
        item = m.score_vix(daily([21.15, 20.83, 18.35], tz="America/Chicago"), NOW)
        self.assertAlmostEqual(item["value"], 20.83)
        self.assertEqual(item["points"], 2)
        self.assertEqual(item["date"], "2026-11-03")

    def test_pre_open_keeps_latest_when_higher(self):
        item = m.score_vix(daily([19.0, 26.0], tz="America/Chicago"), NOW)
        self.assertEqual(item["points"], 4)

    def test_during_session_uses_latest(self):
        session = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)  # 11:00 紐約
        item = m.score_vix(daily([20.83, 18.35], tz="America/Chicago"), session)
        self.assertAlmostEqual(item["value"], 18.35)
        self.assertEqual(item["points"], 0)

    def test_after_close_uses_latest_completed_close(self):
        after = datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc)  # 17:00 紐約
        item = m.score_vix(daily([25.5, 22.0], tz="America/Chicago"), after)
        self.assertAlmostEqual(item["value"], 22.0)
        self.assertEqual(item["points"], 2)

    def test_thresholds(self):
        session = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(m.score_vix(daily([20.0]), session)["points"], 0)
        self.assertEqual(m.score_vix(daily([20.01]), session)["points"], 2)
        self.assertEqual(m.score_vix(daily([25.01]), session)["points"], 4)


class MissingAndStaleTests(unittest.TestCase):
    def test_empty_frame_is_missing_not_zero(self):
        item = m.score_vix(pd.DataFrame(), NOW)
        self.assertEqual(item["status"], m.STATUS_MISSING)
        self.assertIsNone(item["value"])

    def test_all_nan_is_missing(self):
        item = m.score_tnx(daily([np.nan, np.nan]), TODAY)
        self.assertEqual(item["status"], m.STATUS_MISSING)

    def test_trailing_nan_uses_last_valid(self):
        item = m.score_tnx(daily([4.8, np.nan]), TODAY)
        self.assertEqual(item["status"], m.STATUS_OK)
        self.assertAlmostEqual(item["value"], 4.8)

    def test_stale_data_not_scored(self):
        item = m.score_tnx(daily([4.9, 4.95], end="2026-10-27"), TODAY)
        self.assertEqual(item["status"], m.STATUS_STALE)
        self.assertEqual(item["points"], 0)

    def test_three_business_days_still_ok(self):
        item = m.score_tnx(daily([4.9, 4.95], end="2026-10-30"), TODAY)
        self.assertEqual(item["status"], m.STATUS_OK)

    def test_loader_exception_becomes_missing(self):
        loaders = today_like_loaders()
        loaders["vix"] = lambda: (_ for _ in ()).throw(ConnectionError("boom"))
        items, summary = m.evaluate(loaders, NOW)
        vix = next(i for i in items if i["key"] == "vix")
        self.assertEqual(vix["status"], m.STATUS_MISSING)
        self.assertIn("ConnectionError", vix["note"])
        self.assertEqual(summary["missing_count"], 1)
        self.assertEqual(summary["usable_max"], 7)
        self.assertIn("（1 項資料缺漏）", summary["score_text"])

    def test_all_missing_shows_grey_not_green(self):
        failing = {key: (lambda: (_ for _ in ()).throw(ValueError("x"))) for key in m.INDICATOR_ORDER}
        items, summary = m.evaluate(failing, NOW)
        self.assertEqual(summary["level"], "grey")
        self.assertIn("資料不足", summary["label"])
        self.assertEqual(summary["score_text"], "0 / 0 分（5 項資料缺漏）")

    def test_calm_but_one_missing_is_grey(self):
        session = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
        calm = {
            "vix": lambda: daily([14.0, 14.5]),
            "tnx": lambda: daily([4.0, 4.1]),
            "foreign": lambda: {"days": [(TODAY, 3e9)], "complete": True},
            "usdtwd": lambda: daily([30.0] * 25),
            "taiex": lambda: (_ for _ in ()).throw(TimeoutError()),
        }
        _, summary = m.evaluate(calm, session)
        self.assertEqual(summary["score"], 0)
        self.assertEqual(summary["level"], "grey")

    def test_calm_complete_is_green(self):
        session = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
        calm = {
            "vix": lambda: daily([14.0, 14.5]),
            "tnx": lambda: daily([4.0, 4.1]),
            "foreign": lambda: {"days": [(TODAY, 3e9)], "complete": True},
            "usdtwd": lambda: daily([30.0] * 25),
            "taiex": lambda: daily([20000.0] * 25),
        }
        _, summary = m.evaluate(calm, session)
        self.assertEqual(summary["level"], "green")
        self.assertEqual(summary["score_text"], "0 / 11 分")

    def test_short_history_missing(self):
        self.assertEqual(m.score_usdtwd(daily([32.0] * 10), TODAY)["status"], m.STATUS_MISSING)
        self.assertEqual(m.score_taiex(daily([20000.0] * 10), TODAY)["status"], m.STATUS_MISSING)


class TaiwanIndicatorTests(unittest.TestCase):
    def test_parse_bfi82u(self):
        payload = {
            "stat": "OK",
            "data": [
                ["投信", "1", "2", "27,258,458,843"],
                ["外資及陸資(不含外資自營商)", "455", "604", "-149,899,834,076"],
            ],
        }
        self.assertEqual(m.parse_bfi82u(payload), -149_899_834_076.0)
        self.assertIsNone(m.parse_bfi82u({"stat": "很抱歉，沒有符合條件的資料!"}))
        self.assertIsNone(m.parse_bfi82u(None))

    def test_single_big_sell_triggers(self):
        item = m.score_foreign({"days": [(TODAY, -60e9), (date(2026, 11, 3), 10e9)]}, TODAY)
        self.assertEqual(item["points"], 2)

    def test_streak_triggers(self):
        days = [(TODAY, -1e9), (date(2026, 11, 3), -2e9), (date(2026, 11, 2), -3e9), (date(2026, 10, 30), 1e9)]
        item = m.score_foreign({"days": days}, TODAY)
        self.assertEqual(item["points"], 2)
        self.assertIn("連續賣超 3 天", item["value_text"])

    def test_small_sell_no_streak(self):
        days = [(TODAY, -10e9), (date(2026, 11, 3), 2e9)]
        self.assertEqual(m.score_foreign({"days": days}, TODAY)["points"], 0)

    def test_foreign_empty_missing(self):
        self.assertEqual(m.score_foreign({"days": []}, TODAY)["status"], m.STATUS_MISSING)

    def test_fetch_foreign_flows_skips_holidays_and_raises_when_empty(self):
        class Resp:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        class Session:
            def __init__(self, table):
                self.table = table
                self.calls = []

            def get(self, url, params=None, **kwargs):
                self.calls.append(params["dayDate"])
                net = self.table.get(params["dayDate"])
                if net is None:
                    return Resp({"stat": "很抱歉，沒有符合條件的資料!"})
                return Resp({"stat": "OK", "data": [[m.FOREIGN_ROW_NAME, "0", "0", f"{net:,.0f}"]]})

        session = Session({"20261104": -5e9, "20261102": -6e9})
        flows = m.fetch_foreign_flows(TODAY, trading_days=2, session=session, pause=0)
        self.assertEqual([d for d, _ in flows["days"]], [date(2026, 11, 4), date(2026, 11, 2)])
        self.assertNotIn("20261101", session.calls)  # 週日不打 API
        with self.assertRaises(ValueError):
            m.fetch_foreign_flows(TODAY, session=Session({}), pause=0)

    def test_usdtwd_rules(self):
        flat = m.score_usdtwd(daily([32.0] * 25), TODAY)
        self.assertEqual(flat["points"], 0)
        jump = m.score_usdtwd(daily([32.0] * 24 + [32.5]), TODAY)
        self.assertEqual(jump["points"], 1)

    def test_taiex_intraday_drop_only(self):
        closes = [20000.0] * 25
        lows = list(closes)
        lows[-1] = 19300.0  # -3.5%
        item = m.score_taiex(daily(closes, lows=lows), TODAY)
        self.assertEqual(item["points"], 2)


class SummaryTests(unittest.TestCase):
    def test_today_like_scenario_scores_nine_red(self):
        items, summary = m.evaluate(today_like_loaders(), NOW)
        points = {i["key"]: i["points"] for i in items}
        self.assertEqual(points, {"vix": 2, "tnx": 2, "foreign": 2, "usdtwd": 1, "taiex": 2})
        self.assertEqual(summary["score"], 9)
        self.assertEqual(summary["score_text"], "9 / 11 分")
        self.assertEqual(summary["level"], "red")
        self.assertTrue(all(i["status"] == m.STATUS_OK for i in items))

    def test_cutoffs(self):
        self.assertEqual(m.MAX_SCORE, 11)

        def level_for(score):
            item = m._base("vix")
            item.update(status=m.STATUS_OK, points=score, max_points=11)
            return m.summarize([item])["level"]

        self.assertEqual([level_for(s) for s in (0, 2, 3, 6, 7, 11)], ["green", "green", "yellow", "yellow", "red", "red"])

    def test_table_rows_have_columns(self):
        items, _ = m.evaluate(today_like_loaders(), NOW)
        rows = m.table_rows(items)
        self.assertEqual(len(rows), 5)
        self.assertEqual(set(rows[0]), {"指標", "數值", "資料日期", "門檻", "得分", "來源", "狀態"})


class DashboardWiringTests(unittest.TestCase):
    def test_dashboard_no_silent_defaults(self):
        text = (ROOT / "Dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn("/ 10.0 if tnx_df", text)
        self.assertNotIn("else 15.0", text)
        self.assertIn("load_macro_risk()", text)
        self.assertIn("macro_risk.table_rows", text)
        self.assertIn("clear_macro_risk_caches()", text)


if __name__ == "__main__":
    unittest.main()
