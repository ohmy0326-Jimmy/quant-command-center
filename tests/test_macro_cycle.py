"""總經週期檔的 schema、載入與多空狀態對應。不連網。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema.validators import Draft202012Validator

from macro_cycle import (
    AUTO_INDICATOR_IDS,
    quadrant_shift,
    week_change,
    DATA_PATH,
    REGIME_BANDS,
    REGIME_RISK,
    canonical_ticker,
    format_holding_tag,
    holding_tag,
    industry_quadrant_points,
    load_macro_cycle,
    load_schema,
    regime_for_score,
    risk_params_for_regime,
    validate_macro_cycle,
)
from macro_cycle_live import clear_live_view, macro_view_for_orders, read_macro_tabs, remember_live_view
from macro_cycle_sheet import (
    CYCLES,
    DIRECTIONS,
    HISTORY_HEADERS,
    INDUSTRY_CYCLE_HEADERS,
    MACRO_SCORE_HEADERS,
    STANCES,
    compose_macro_view,
    exposure_by_industry,
    headers_missing,
    parse_fred_csv,
    parse_history_rows,
    parse_industry_rows,
    underweight_warnings,
    score_auto_from_markets,
    score_hy_spread,
    score_ma_gap,
    score_real_yield_change,
    score_vs_ma200,
    score_vix,
)


OFFICIAL_STANCE = {
    "半導體設備": "加碼",
    "光通訊/CPO": "加碼",
    "重電電網": "加碼",
    "先進製程": "加碼",
    "AI 算力": "中性",
    "消費電子": "減碼",
    "生技醫療": "減碼",
    "記憶體": "減碼",
}


def _raw() -> dict:
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def _in_quadrant(cycle: str, x: float, y: float) -> bool:
    if cycle == "復甦":
        return x < 0 and y > 0
    if cycle == "成長":
        return x > 0 and y > 0
    if cycle == "趨緩":
        return x > 0 and y < 0
    if cycle == "衰退":
        return x < 0 and y < 0
    return False


class SchemaTests(unittest.TestCase):
    def test_schema_document_is_draft_2020(self):
        schema = load_schema()
        Draft202012Validator.check_schema(schema)
        industry = schema["$defs"]["industry"]["properties"]
        self.assertEqual(industry["cycle"]["enum"], ["復甦", "成長", "趨緩", "衰退"])
        self.assertEqual(industry["direction"]["enum"], ["上行", "持平", "下行"])
        self.assertEqual(industry["stance"]["enum"], ["加碼", "中性", "減碼"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.0.0")
        score = schema["properties"]["score"]["properties"]
        industries = schema["properties"]["industries"]["properties"]
        self.assertEqual(score["cadence"]["const"], "weekly")
        self.assertEqual(score["indicators"]["minItems"], 9)
        self.assertEqual(score["indicators"]["maxItems"], 9)
        self.assertEqual(industries["cadence"]["const"], "monthly")
        self.assertEqual(industries["as_of_day"]["const"], 12)
        self.assertEqual(industries["items"]["minItems"], 20)
        self.assertEqual(industries["items"]["maxItems"], 20)
        self.assertEqual(schema["$defs"]["indicator"]["properties"]["score"]["minimum"], -2)
        self.assertEqual(schema["$defs"]["indicator"]["properties"]["score"]["maximum"], 2)

    def test_seed_sums_to_neutral_and_marks_placeholders(self):
        raw = _raw()
        self.assertNotIn("total", raw)
        self.assertNotIn("total", raw["score"])
        view = load_macro_cycle()
        self.assertTrue(view["ok"], view["errors"])
        self.assertFalse(view["fallback"])
        self.assertEqual(view["schema_version"], "1.0.0")
        self.assertEqual(view["score_updated_at"], "2026-10-26")
        self.assertEqual(view["score_cadence"], "weekly")
        self.assertEqual(view["industry_updated_at"], "2026-10-12")
        self.assertEqual(view["industry_cadence"], "monthly")
        self.assertEqual(view["industry_as_of_day"], 12)
        self.assertEqual(view["total"], 1)
        self.assertEqual(view["regime"], "中性")
        self.assertEqual(view["risk"]["target_leverage"], 1.5)
        self.assertEqual(view["risk"]["min_cash_pct"], 15.0)
        self.assertEqual(len(view["indicators"]), 9)
        self.assertEqual([item["id"] for item in view["indicators"]], [
            "us_real_yield",
            "hy_spread",
            "vix",
            "equity_ma",
            "sox_trend",
            "pmi",
            "earnings_revisions",
            "valuation",
            "tw_activity",
        ])
        self.assertTrue(all(-2 <= item["score"] <= 2 for item in view["indicators"]))
        self.assertTrue(all(item["example"] and "示例" in item["note"] for item in view["indicators"]))
        self.assertEqual(sum(item["score"] for item in view["indicators"]), 1)

        by_name = {item["name"]: item for item in view["industries"]}
        self.assertEqual(len(by_name), 20)
        for name, stance in OFFICIAL_STANCE.items():
            self.assertEqual(by_name[name]["stance"], stance)
            self.assertFalse(by_name[name]["example"])
            self.assertIn("目前配置", by_name[name]["note"])
            self.assertIn("示例", by_name[name]["note"])
        examples = [item for item in view["industries"] if item["example"]]
        self.assertEqual(len(examples), 12)
        self.assertTrue(all("示例" in item["note"] for item in examples))
        self.assertEqual({item["cycle"] for item in view["industries"]}, {"復甦", "成長", "趨緩", "衰退"})

    def test_invalid_score_version_and_duplicate_ticker_fall_back(self):
        raw = _raw()
        raw["score"]["indicators"][0]["score"] = 3
        self.assertTrue(validate_macro_cycle(raw))
        view = _load_copy(raw)
        self.assertFalse(view["ok"])
        self.assertTrue(view["fallback"])
        self.assertIsNone(view["total"])
        self.assertEqual(view["regime"], "中性")
        self.assertEqual(view["risk"]["target_leverage"], 1.5)
        self.assertEqual(view["risk"]["min_cash_pct"], 15.0)
        self.assertIsNone(holding_tag(view, "2330"))

        bumped = _raw()
        bumped["schema_version"] = "9.9.9"
        self.assertFalse(_load_copy(bumped)["ok"])

        duplicated = _raw()
        duplicated["industries"]["items"][7]["tickers"] = ["NVDA", "2408.TW", "2337.TW", "2344.TW"]
        errors = validate_macro_cycle(duplicated)
        self.assertTrue(any("NVDA" in message for message in errors))

    def test_impossible_calendar_date_is_rejected(self):
        raw = _raw()
        raw["industries"]["updated_at"] = "2026-13-12"
        errors = validate_macro_cycle(raw)
        self.assertTrue(any("有效日期" in message for message in errors))
        self.assertFalse(_load_copy(raw)["ok"])

    def test_missing_and_broken_file_fall_back(self):
        missing = load_macro_cycle(Path(tempfile.gettempdir()) / "no-such-macro-cycle.json")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["regime"], "中性")
        self.assertEqual(missing["risk"], risk_params_for_regime("中性"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "macro_cycle.json"
            path.write_text("{", encoding="utf-8")
            broken = load_macro_cycle(path)
        self.assertFalse(broken["ok"])
        self.assertTrue(any("JSON" in message for message in broken["errors"]))


class RegimeMappingTests(unittest.TestCase):
    def test_bands_cover_minus_16_to_plus_16_once(self):
        covered = []
        for _name, low, high in REGIME_BANDS:
            covered.extend(range(low, high + 1))
        self.assertEqual(sorted(covered), list(range(-18, 19)))
        self.assertEqual([name for name, _low, _high in REGIME_BANDS], ["牛", "偏多", "中性", "偏空", "熊"])
        self.assertEqual(set(REGIME_RISK), {"牛", "偏多", "中性", "偏空", "熊"})

    def test_score_edges_and_risk_defaults(self):
        cases = {
            16: "牛",
            9: "牛",
            8: "偏多",
            4: "偏多",
            3: "中性",
            1: "中性",
            0: "中性",
            -3: "中性",
            -4: "偏空",
            -8: "偏空",
            -9: "熊",
            -16: "熊",
            -18: "熊",
            18: "牛",
        }
        for score, regime in cases.items():
            self.assertEqual(regime_for_score(score), regime)
        self.assertEqual({regime_for_score(score) for score in range(-16, 17)}, set(REGIME_RISK))
        expected = {
            "牛": (2.0, 10.0),
            "偏多": (1.8, 12.0),
            "中性": (1.5, 15.0),
            "偏空": (1.2, 22.0),
            "熊": (1.0, 30.0),
        }
        for regime, (leverage, cash) in expected.items():
            params = risk_params_for_regime(regime)
            self.assertEqual(params["target_leverage"], leverage)
            self.assertEqual(params["min_cash_pct"], cash)
            self.assertEqual(params["regime"], regime)
        with self.assertRaises(ValueError):
            regime_for_score(19)
        with self.assertRaises(TypeError):
            regime_for_score(True)

    def test_ticker_tags_and_quadrants(self):
        self.assertEqual(canonical_ticker(" 2330.tw "), "2330")
        self.assertEqual(canonical_ticker("4979.TWO"), "4979")
        self.assertEqual(canonical_ticker("BRK.B"), "BRK.B")
        view = load_macro_cycle()
        advanced = holding_tag(view, "2330")
        self.assertEqual(advanced["industry"], "先進製程")
        self.assertEqual(advanced["stance"], "加碼")
        self.assertFalse(advanced["example"])
        self.assertEqual(holding_tag(view, "2330.TW")["industry"], "先進製程")
        self.assertEqual(holding_tag(view, "tsm")["stance"], "加碼")
        self.assertEqual(format_holding_tag(holding_tag(view, "NVDA")), "AI 算力 · 中性")
        ic_tag = format_holding_tag(holding_tag(view, "2454"))
        self.assertIn("中性", ic_tag)
        self.assertIn("示例", ic_tag)
        self.assertIsNone(holding_tag(view, "SOXL"))
        self.assertIsNone(holding_tag(view, "TX"))
        self.assertIsNone(holding_tag(view, "00663L"))
        self.assertEqual(format_holding_tag(None), "")

        points = industry_quadrant_points(view["industries"])
        self.assertEqual(len(points), 20)
        self.assertEqual({point["quadrant"] for point in points}, {"復甦", "成長", "趨緩", "衰退"})
        for point in points:
            self.assertTrue(_in_quadrant(point["cycle"], point["x"], point["y"]), point)
            self.assertEqual(point["quadrant"], point["cycle"])


class SheetAndAutoTests(unittest.TestCase):
    def setUp(self):
        clear_live_view()

    def tearDown(self):
        clear_live_view()

    def test_headers_and_allowed_values(self):
        self.assertEqual(
            MACRO_SCORE_HEADERS,
            ("indicator_id", "name", "score", "source", "value", "trend", "note", "updated_at"),
        )
        self.assertEqual(
            INDUSTRY_CYCLE_HEADERS,
            ("industry", "cycle", "direction", "stance", "tickers", "rationale", "prev_cycle", "note", "updated_at"),
        )
        self.assertEqual(HISTORY_HEADERS, ("week", "total", "regime"))
        self.assertEqual(headers_missing(["indicator_id", "name"], MACRO_SCORE_HEADERS)[0], "score")
        self.assertEqual(CYCLES, ("復甦", "成長", "趨緩", "衰退"))
        self.assertEqual(DIRECTIONS, ("上行", "持平", "下行"))
        self.assertEqual(STANCES, ("加碼", "中性", "減碼"))
        self.assertEqual(AUTO_INDICATOR_IDS, {"us_real_yield", "hy_spread", "vix", "equity_ma", "sox_trend"})

    def test_bad_rows_are_skipped_and_missing_indicators_use_json(self):
        rows = [
            _score_row("vix", 2, "auto"),
            _score_row("pmi", 9, "manual"),
            _score_row("not_a_real_id", 1, "manual"),
            _score_row("valuation", 1, "手動"),
        ]
        sheet = {"ok": True, "scores": rows, "industries": [], "score_error": None, "industry_error": "這次不測產業"}
        view = compose_macro_view(load_macro_cycle(), sheet, {}, today="2026-10-29")
        self.assertTrue(view["ok"], view.get("warnings"))
        by_id = {item["id"]: item for item in view["indicators"]}
        self.assertEqual(by_id["vix"]["score"], 2)
        self.assertEqual(by_id["vix"]["origin"], "sheet")
        self.assertEqual(by_id["valuation"]["score"], 1)
        self.assertEqual(by_id["valuation"]["source"], "manual")
        self.assertEqual(by_id["pmi"]["origin"], "json")
        self.assertTrue(any("pmi" in message and "略過" in message for message in view["warnings"]))
        self.assertTrue(any("未知指標" in message for message in view["warnings"]))

    def test_auto_overrides_sheet_and_failure_keeps_sheet(self):
        sheet = {"ok": True, "scores": [_score_row("vix", 2, "manual")], "industries": [], "score_error": None, "industry_error": "略過產業"}
        overridden = compose_macro_view(
            load_macro_cycle(),
            sheet,
            {"vix": {"ok": True, "score": -2, "value": "VIX 33.0", "updated_at": "2026-10-29", "note": "自動"}},
            today="2026-10-29",
        )
        vix = next(item for item in overridden["indicators"] if item["id"] == "vix")
        self.assertEqual(vix["score"], -2)
        self.assertEqual(vix["source_label"], "自動")
        self.assertEqual(vix["updated_at"], "2026-10-29")

        kept = compose_macro_view(load_macro_cycle(), sheet, {"vix": {"ok": False}}, today="2026-10-29")
        vix_kept = next(item for item in kept["indicators"] if item["id"] == "vix")
        self.assertEqual(vix_kept["score"], 2)
        self.assertTrue(any("沿用試算表" in message for message in kept["warnings"]))

    def test_missing_sheet_uses_json_then_neutral(self):
        view = compose_macro_view(load_macro_cycle(), {"ok": False, "error": "讀不到分頁 Macro_Score"}, {}, today="2026-10-29")
        self.assertEqual(view["data_origin"], "json")
        self.assertEqual(view["total"], 1)
        self.assertEqual(view["regime"], "中性")
        neutral = compose_macro_view(
            {"ok": False, "errors": ["壞掉"], "regime": "中性", "indicators": [], "industries": []},
            {"ok": False, "error": "讀不到"},
            {},
        )
        self.assertFalse(neutral["ok"])
        self.assertEqual(neutral["data_origin"], "neutral")
        self.assertEqual(neutral["regime"], "中性")

    def test_industry_bad_row_skipped(self):
        rows = [
            {
                "industry": "半導體設備",
                "cycle": "成長",
                "direction": "上行",
                "stance": "加碼",
                "tickers": "ASML, AMAT",
                "note": "試算表",
                "updated_at": "2026-10-12",
            },
            {
                "industry": "壞列",
                "cycle": "過熱",
                "direction": "上行",
                "stance": "加碼",
                "tickers": "AAA",
                "note": "",
                "updated_at": "2026-10-12",
            },
        ]
        accepted, warnings = parse_industry_rows(rows)
        self.assertEqual(accepted[0]["tickers"], ["ASML", "AMAT"])
        self.assertEqual(len(accepted), 1)
        self.assertTrue(any("略過" in message for message in warnings))

    def test_scoring_thresholds_and_fred_csv(self):
        self.assertEqual(parse_fred_csv("observation_date,BAMLH0A0HYM2\n2024-01-02,3.5\n2024-01-03,.\n2024-01-04,4\n"), [3.5, 4.0])
        self.assertEqual(score_hy_spread(3.5), 2)
        self.assertEqual(score_hy_spread(4.5), 1)
        self.assertEqual(score_hy_spread(5.5), 0)
        self.assertEqual(score_hy_spread(7.5), -1)
        self.assertEqual(score_hy_spread(7.6), -2)
        self.assertEqual(score_vix(14.9), 2)
        self.assertEqual(score_vix(15), 1)
        self.assertEqual(score_vix(20), 0)
        self.assertEqual(score_vix(25), -1)
        self.assertEqual(score_vix(32), -2)
        self.assertEqual(score_ma_gap(0.03), 2)
        self.assertEqual(score_ma_gap(0.01), 1)
        self.assertEqual(score_ma_gap(0.0), 0)
        self.assertEqual(score_ma_gap(-0.02), -1)
        self.assertEqual(score_ma_gap(-0.03), -2)
        series = [2.30] + [2.0] * 62
        score, latest, delta = score_real_yield_change(series)
        self.assertEqual((score, latest, round(delta, 2)), (2, 2.0, -0.3))
        flat = [100.0] * 200
        scored = score_vs_ma200(flat)
        self.assertEqual(scored[0], 0)
        markets = score_auto_from_markets({"hy": [3.2], "vix": 18, "dfii10": series, "spx": flat, "twii": [], "sox": [], "dgs10": [4.2]}, "2026-10-29")
        self.assertTrue(markets["hy_spread"]["ok"])
        self.assertEqual(markets["hy_spread"]["score"], 2)
        self.assertEqual(markets["vix"]["score"], 1)
        self.assertFalse(markets["sox_trend"]["ok"])

    def test_orders_page_never_fetches(self):
        view = macro_view_for_orders()
        self.assertEqual(view["data_origin"], "json")
        self.assertEqual(view["total"], 1)
        remember_live_view({"ok": True, "data_origin": "sheet", "regime": "牛"}, now=1_000)
        self.assertEqual(macro_view_for_orders(now=1_100)["data_origin"], "sheet")
        self.assertEqual(macro_view_for_orders(now=1_000 + 15 * 60 + 1)["data_origin"], "json")

    def test_history_week_change_and_quadrant_shift(self):
        rows, warnings = parse_history_rows(
            [
                {"week": "2026-10-19", "total": 0, "regime": "中性"},
                {"week": "壞掉", "total": 1, "regime": "中性"},
                {"week": "2026-10-26", "total": 1, "regime": "中性"},
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(week_change(1, rows, "2026-10-26"), 1)
        self.assertEqual(quadrant_shift({"cycle": "趨緩", "prev_cycle": "成長"}), "成長→趨緩")
        self.assertEqual(quadrant_shift({"cycle": "成長", "prev_cycle": "成長"}), "")
        self.assertEqual(quadrant_shift({"cycle": "成長"}), "")
        view = load_macro_cycle()
        self.assertEqual(view["week_change"], 1)
        exposure, grand = exposure_by_industry(
            [{"ticker": "2330", "mv": 80}, {"ticker": "2408.TW", "mv": 20}, {"ticker": "SOXL", "mv": 50}],
            view["industries"],
        )
        self.assertEqual(grand, 150)
        self.assertAlmostEqual(exposure["先進製程"]["pct"], 80 / 150 * 100)
        self.assertAlmostEqual(exposure["記憶體"]["pct"], 20 / 150 * 100)
        self.assertNotIn("SOXL", str(exposure))
        notes = underweight_warnings(view["industries"], {"記憶體": {"pct": 13.3}, "消費電子": {"pct": 9.9}})
        self.assertEqual(notes, ["記憶體 是減碼，持股佔 13.3%"])

    def test_reader_does_not_create_missing_tabs(self):
        class Missing:
            def worksheet(self, title):
                raise KeyError(title)

        result = read_macro_tabs(Missing())
        self.assertFalse(result["ok"])
        self.assertIn("Macro_Score", result["error"])
        self.assertIn("Industry_Cycle", result["error"])


def _score_row(indicator_id, score, source):
    return {
        "indicator_id": indicator_id,
        "name": "",
        "score": score,
        "source": source,
        "value": "表",
        "note": "試算表",
        "updated_at": "2026-10-29",
    }


def _load_copy(raw: dict) -> dict:
    copied = copy.deepcopy(raw)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "macro_cycle.json"
        path.write_text(json.dumps(copied, ensure_ascii=False), encoding="utf-8")
        return load_macro_cycle(path)


if __name__ == "__main__":
    unittest.main()
