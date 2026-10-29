"""總經週期檔的 schema、載入與多空狀態對應。不連網。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema.validators import Draft202012Validator

from macro_cycle import (
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
        self.assertEqual(score["indicators"]["minItems"], 8)
        self.assertEqual(score["indicators"]["maxItems"], 8)
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
        self.assertEqual(len(view["indicators"]), 8)
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
        self.assertEqual(sorted(covered), list(range(-16, 17)))
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
            regime_for_score(17)
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


def _load_copy(raw: dict) -> dict:
    copied = copy.deepcopy(raw)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "macro_cycle.json"
        path.write_text(json.dumps(copied, ensure_ascii=False), encoding="utf-8")
        return load_macro_cycle(path)


if __name__ == "__main__":
    unittest.main()
