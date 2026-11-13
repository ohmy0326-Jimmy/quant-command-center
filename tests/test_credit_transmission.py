"""信用傳導燈號與階段。不連網。"""

import unittest
from datetime import date, timedelta

import credit_transmission as credit


TODAY = date(2026, 11, 13)


def _weekly(values, end=date(2026, 11, 6)):
    start = end - timedelta(weeks=len(values) - 1)
    return [(start + timedelta(weeks=i), float(value)) for i, value in enumerate(values)]


def _daily(values, end=date(2026, 11, 11)):
    start = end - timedelta(days=len(values) - 1)
    return [(start + timedelta(days=i), float(value)) for i, value in enumerate(values)]


def _levels(n, latest, anchors):
    """anchors：距今幾期 → 水準。其餘填 latest，燈號只讀指定期。"""
    values = [latest] * n
    for steps, level in anchors.items():
        values[-1 - steps] = level
    return values


class StandardsTests(unittest.TestCase):
    def test_level_and_jump_boundaries(self):
        rule = credit.THRESHOLDS["drtscilm"]
        green = credit.score_drtscilm([(TODAY, rule["green_below"] - 0.1)], TODAY)
        yellow_low = credit.score_drtscilm([(TODAY, 0.0)], TODAY)
        yellow_high = credit.score_drtscilm([(TODAY, rule["yellow_high"])], TODAY)
        red = credit.score_drtscilm([(TODAY, rule["yellow_high"] + 0.1)], TODAY)
        self.assertEqual([green["light"], yellow_low["light"], yellow_high["light"], red["light"]], ["green", "yellow", "yellow", "red"])
        jumped = credit.score_drtscilm([(TODAY - timedelta(days=90), -20.0), (TODAY, -20.0 + rule["jump_red"])], TODAY)
        self.assertEqual(jumped["light"], "red")
        almost = credit.score_drtscilm([(TODAY - timedelta(days=90), -20.0), (TODAY, -20.0 + rule["jump_red"] - 0.1)], TODAY)
        self.assertEqual(almost["light"], "green")

    def test_stale_is_not_a_green_and_keeps_the_date(self):
        old = TODAY - timedelta(days=credit.THRESHOLDS["drtscilm"]["stale_days"] + 1)
        row = credit.score_drtscilm([(old, -10.0)], TODAY)
        self.assertEqual(row["light"], "stale")
        self.assertEqual(row["date"], old.isoformat())
        self.assertEqual(row["value"], -10.0)
        self.assertNotEqual(row["light"], "green")


class LendingTests(unittest.TestCase):
    def _row(self, latest, past52, past13):
        values = _levels(54, latest, {52: past52, 13: past13})
        return credit.score_lending(credit.SERIES[1], _weekly(values), TODAY)

    def test_yoy_and_deceleration_use_the_constant_table(self):
        rule = credit.THRESHOLDS["lending"]
        green = self._row(106.0, 100.0, 106.0 / (1.05 ** (13 / 52)))
        self.assertGreater(green["value"], rule["yoy_green_above"])
        self.assertEqual(green["light"], "green")
        edge = self._row(103.0, 100.0, 103.0 / (1.03 ** (13 / 52)))
        self.assertEqual(edge["light"], "yellow")
        self.assertAlmostEqual(edge["value"], 3.0, places=6)
        zero = self._row(100.0, 100.0, 100.0)
        self.assertEqual(zero["light"], "yellow")
        red = self._row(99.0, 100.0, 99.0)
        self.assertLess(red["value"], rule["yoy_red_below"])
        self.assertEqual(red["light"], "red")
        slowing = self._row(106.0, 100.0, 106.0 / (1.02 ** (13 / 52)))
        self.assertGreater(slowing["value"] - 2.0, rule["deceleration_gap"])
        self.assertEqual(slowing["light"], "yellow")

    def test_monthly_fallback_uses_twelve_and_three_not_fifty_two_weeks(self):
        values = _levels(16, 110.0, {12: 100.0, 3: 110.0 / (1.10 ** (3 / 12))})
        points = []
        cursor = date(2025, 6, 1)
        for value in values:
            points.append((cursor, float(value)))
            month = cursor.month + 1
            year = cursor.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            cursor = date(year, month, 1)
        self.assertEqual(points[-1][0], date(2026, 9, 1))
        spec = {"id": "totci", "name": "工商放款（BUSLOANS，月資料備援）", "series_id": "BUSLOANS"}
        row = credit.score_lending(spec, points, TODAY, frequency="month")
        self.assertEqual(row["light"], "stale")
        self.assertIn("月資料", row["change_text"] + row["reason"])
        self.assertAlmostEqual(row["value"], 10.0, places=4)
        fresh = credit.score_lending(spec, points, points[-1][0] + timedelta(days=10), frequency="month")
        self.assertEqual(fresh["light"], "green")
        self.assertIn("3個月年化", fresh["value_text"])
        self.assertEqual(credit.THRESHOLDS["lending"]["totci_series_id"], "TOTCI")
        self.assertEqual(credit.THRESHOLDS["lending"]["week_stale_days"], 21)
        self.assertEqual(credit.THRESHOLDS["lending"]["week_yoy_steps"], 52)


class PriceTests(unittest.TestCase):
    def test_hy_bp_thresholds_and_one_month_jump(self):
        rule = credit.THRESHOLDS["hy_oas"]

        def hy(level, past=None):
            values = [level] * (rule["jump_sessions"] + 1)
            if past is not None:
                values[0] = past
            return credit.score_oas("hy_oas", _daily(values), TODAY)

        self.assertEqual(hy(rule["green_below_bp"] / 100 - 0.01)["light"], "green")
        self.assertEqual(hy(rule["green_below_bp"] / 100)["light"], "yellow")
        self.assertEqual(hy(rule["yellow_high_bp"] / 100)["light"], "yellow")
        self.assertEqual(hy(rule["yellow_high_bp"] / 100 + 0.01)["light"], "red")
        jumped = hy(2.0, past=1.0)
        self.assertEqual(jumped["light"], "red")
        self.assertGreaterEqual(jumped["change"], rule["jump_red_bp"])
        self.assertLess(jumped["value"], rule["green_below_bp"])

    def test_ig_nfci_and_stlfsi(self):
        ig = credit.THRESHOLDS["ig_oas"]
        nfci = credit.THRESHOLDS["nfci"]
        stress = credit.THRESHOLDS["stlfsi4"]

        def ig_row(percent):
            return credit.score_oas("ig_oas", _daily([percent] * 22), TODAY)

        self.assertEqual(ig_row(ig["green_below_bp"] / 100 - 0.01)["light"], "green")
        self.assertEqual(ig_row(ig["green_below_bp"] / 100)["light"], "yellow")
        self.assertEqual(ig_row(ig["yellow_high_bp"] / 100)["light"], "yellow")
        self.assertEqual(ig_row(ig["yellow_high_bp"] / 100 + 0.01)["light"], "red")
        self.assertIn("21", ig_row(0.9)["change_text"])

        def weekly_index(kind, value):
            return credit.score_index(kind, _weekly([value, value]), TODAY)

        self.assertEqual(weekly_index("nfci", nfci["yellow_low"] - 0.01)["light"], "green")
        self.assertEqual(weekly_index("nfci", nfci["yellow_low"])["light"], "yellow")
        self.assertEqual(weekly_index("nfci", nfci["red_above"])["light"], "yellow")
        self.assertEqual(weekly_index("nfci", nfci["red_above"] + 0.01)["light"], "red")
        self.assertEqual(weekly_index("stlfsi4", stress["red_above"])["light"], "green")
        self.assertEqual(weekly_index("stlfsi4", stress["red_above"] + 0.01)["light"], "red")

    def test_daily_stale_and_fetch_failure_do_not_reuse_a_number(self):
        old = _daily([3.0] * 22, end=TODAY - timedelta(days=credit.THRESHOLDS["hy_oas"]["stale_days"] + 1))
        stale = credit.score_oas("hy_oas", old, TODAY)
        self.assertEqual(stale["light"], "stale")
        secret = "super-secret-fred-key"

        def fetch(_series_id):
            raise RuntimeError(f"https://api.stlouisfed.org/fred/series/observations?api_key={secret}")

        board = credit.load_board(TODAY, fetch=fetch)
        hy = next(row for row in board["rows"] if row["id"] == "hy_oas")
        self.assertEqual(hy["light"], "missing")
        self.assertIsNone(hy["value"])
        self.assertNotIn(secret, str(board))
        self.assertIn("RuntimeError", hy["reason"])
        self.assertIn("資料缺失", next(row["燈號"] for row in board["table_rows"] if row["指標"].startswith("高收益")))


class StageTests(unittest.TestCase):
    def test_combinations(self):
        cases = [
            ("green", "green", "green", credit.STAGE_EQUITY, "equity_only"),
            ("green", "green", None, credit.STAGE_EQUITY, "equity_only"),
            ("green", "green", "stale", credit.STAGE_EQUITY, "equity_only"),
            ("green", "yellow", "green", credit.STAGE_PRICE, "price_first"),
            ("green", "red", None, credit.STAGE_PRICE, "price_first"),
            ("green", "green", "red", credit.STAGE_PRICE, "price_first"),
            ("green", "green", "yellow", credit.STAGE_PRICE, "price_first"),
            ("yellow", "yellow", "green", credit.STAGE_CONFIRMED, "confirmed"),
            ("red", "red", None, credit.STAGE_CONFIRMED, "confirmed"),
            ("red", "yellow", "green", credit.STAGE_CONFIRMED, "confirmed"),
            ("red", "green", "red", credit.STAGE_VOLUME, "volume_only"),
            ("yellow", "green", None, credit.STAGE_VOLUME, "volume_only"),
            (None, "red", "red", credit.STAGE_UNKNOWN, "insufficient"),
            ("green", None, "green", credit.STAGE_UNKNOWN, "insufficient"),
            (None, None, None, credit.STAGE_UNKNOWN, "insufficient"),
        ]
        for volume, price, standards, text, code in cases:
            with self.case(f"{volume}-{price}-{standards}"):
                self.assertEqual(credit.judge_stage(volume, price, standards), (text, code))

    def case(self, name):
        return self.subTest(name)

    def test_board_requires_both_lending_series_and_ignores_optional_stress_when_missing(self):
        def flat(level, n=60, end=date(2026, 11, 6)):
            return _weekly([level] * n, end)

        series = {
            "drtscilm": [(TODAY, -5.0)],
            "totll": _weekly(_levels(54, 110.0, {52: 100.0, 13: 110.0 / (1.08 ** (13 / 52))})),
            "totci": None,
            "hy_oas": _daily([2.0] * 22),
            "ig_oas": _daily([0.8] * 22),
            "nfci": _weekly([-0.4, -0.4]),
            "stlfsi4": None,
        }
        board = credit.build_board(series, {"totci": "FRED 公開 CSV 連不上"}, TODAY)
        self.assertIsNone(board["lending_light"])
        self.assertEqual(board["price_light"], "green")
        self.assertEqual(board["stage"], credit.STAGE_UNKNOWN)
        self.assertEqual(next(row["name"] for row in board["rows"] if row["id"] == "totci"), "工商放款（TOTCI）")
        series["totci"] = _weekly(_levels(54, 108.0, {52: 100.0, 13: 108.0 / (1.06 ** (13 / 52))}))
        series["stlfsi4"] = _weekly([0.4, 0.4])
        tight = credit.build_board(series, {}, TODAY)
        self.assertEqual(tight["price_light"], "red")
        self.assertEqual(tight["lending_light"], "green")
        self.assertEqual(tight["stage"], credit.STAGE_PRICE)
        cds = next(row for row in tight["rows"] if row["id"] == "cds")
        self.assertEqual(cds["reason"], credit.CDS_TEXT)
        self.assertIsNone(cds["value"])
        source = open(credit.__file__, encoding="utf-8").read()
        self.assertNotIn("score_hy_spread(", source)
        self.assertIn("fetch_fred_observations", source)

    def test_totci_failure_falls_back_to_labeled_monthly_busloans(self):
        values = _levels(16, 110.0, {12: 100.0, 3: 110.0 / (1.10 ** (3 / 12))})
        points = []
        cursor = date(2025, 6, 1)
        for value in values:
            points.append((cursor, float(value)))
            month = cursor.month + 1
            year = cursor.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            cursor = date(year, month, 1)
        calls = []

        def fetch(series_id):
            calls.append(series_id)
            if series_id == "TOTCI":
                raise RuntimeError("totci down")
            if series_id == "BUSLOANS":
                return points
            raise AssertionError(series_id)

        data, reason, fallback = credit.resolve_totci(fetch)
        self.assertEqual(calls, ["TOTCI", "BUSLOANS"])
        self.assertEqual(fallback, "BUSLOANS")
        self.assertEqual(reason, "")
        self.assertEqual(data[-1][0], date(2026, 9, 1))
        board = credit.build_board(
            {"totci": data, "totll": _weekly(_levels(54, 108.0, {52: 100.0, 13: 108.0 / (1.06 ** (13 / 52))}))},
            {},
            TODAY,
            fallbacks={"totci": fallback},
        )
        row = next(item for item in board["rows"] if item["id"] == "totci")
        self.assertEqual(row["series_id"], "BUSLOANS")
        self.assertIn("月資料", row["name"])
        self.assertEqual(row["light"], "stale")

        calls.clear()

        def weekly_only(series_id):
            calls.append(series_id)
            if series_id == "BUSLOANS":
                raise AssertionError("TOTCI 成功時不該抓 BUSLOANS")
            if series_id == "TOTCI":
                return _weekly(_levels(54, 108.0, {52: 100.0, 13: 108.0 / (1.06 ** (13 / 52))}))
            if series_id in {"BAMLH0A0HYM2", "BAMLC0A0CM"}:
                return _daily([2.0] * 22)
            if series_id == "DRTSCILM":
                return [(TODAY, -5.0)]
            if series_id == "NFCI":
                return _weekly([-0.5, -0.5, -0.5, -0.5])
            if series_id == "STLFSI4":
                return _weekly([-0.4, -0.4, -0.4, -0.4])
            if series_id == "TOTLL":
                return _weekly(_levels(54, 108.0, {52: 100.0, 13: 108.0 / (1.06 ** (13 / 52))}))
            raise AssertionError(series_id)

        live = credit.load_board(TODAY, fetch=weekly_only)
        self.assertNotIn("BUSLOANS", calls)
        totci = next(item for item in live["rows"] if item["id"] == "totci")
        self.assertEqual(totci["series_id"], "TOTCI")
        self.assertEqual(totci["name"], "工商放款（TOTCI）")
        self.assertEqual(totci["light"], "green")
        self.assertEqual(live["lending_light"], "green")


class CacheTests(unittest.TestCase):
    def test_success_is_cached_and_failure_is_not(self):
        calls = {"n": 0}

        def fetch(series_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("down")
            return [(TODAY, 1.0)]

        now = {"t": 0.0}
        wrapped = credit.memoize_fetch(fetch, ttl=10, clock=lambda: now["t"])
        with self.assertRaises(RuntimeError):
            wrapped("DFII10")
        found = wrapped("DFII10")
        again = wrapped("DFII10")
        self.assertEqual(found, again)
        self.assertEqual(calls["n"], 2)
        now["t"] = 11
        wrapped("DFII10")
        self.assertEqual(calls["n"], 3)


class PanelTests(unittest.TestCase):
    def test_render_states_the_rule_and_does_not_score(self):
        try:
            from streamlit.testing.v1 import AppTest
        except ImportError:
            self.skipTest("streamlit.testing.v1 not available")
        board = credit.build_board({}, {}, TODAY)
        script = """
import streamlit as st
import credit_transmission as credit
from datetime import date
board = credit.build_board({}, {}, date(2026, 11, 13))
credit.render_credit_panel(st, board)
"""
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assertFalse(at.exception)
        blob = "\n".join(str(item.value) for item in list(at.caption) + list(at.info) + list(at.subheader))
        self.assertIn("13 週年化比年增率低 3", blob)
        self.assertIn(credit.CDS_TEXT, blob)
        self.assertIn("不計入牛熊總分", blob)
        self.assertIn(credit.STAGE_UNKNOWN, blob)
        self.assertIs(board, board)


if __name__ == "__main__":
    unittest.main()
