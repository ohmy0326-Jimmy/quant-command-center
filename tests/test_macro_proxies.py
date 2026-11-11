"""代理分數、45 日覆寫、覆蓋率，以及抓取失敗不當 0。"""

import unittest
from unittest.mock import patch

from macro_cycle import REGIME_RISK, load_macro_cycle
from macro_cycle_live import fetch_auto_results
from macro_cycle_sheet import compose_macro_view, parse_score_row, parse_sheet_date
from macro_proxies import (
    EMPIRE_HEADLINE,
    EMPIRE_IDS_NOT_ON_FRED,
    NDC_RED_MIN,
    PRICE_CYCLE_LABEL,
    aggregate_scores,
    cycle_from_relative,
    manual_override_fresh,
    parse_export_yoy,
    parse_ndc_scores,
    pmi_confirmation,
    proxies_from_payloads,
    score_erp,
    score_pmi_level,
    score_pmi_series,
    score_tw_activity,
    score_valuation,
)


def _scored(indicator_id, score):
    return {"id": indicator_id, "score": score, "scored": True, "example": False}


class PmiRuleTests(unittest.TestCase):
    def test_level_bands_and_unclear_zero(self):
        self.assertEqual(score_pmi_level(12, "rising"), 2)
        self.assertEqual(score_pmi_level(12, "falling"), 1)
        self.assertEqual(score_pmi_level(12, "unclear"), 1)
        self.assertEqual(score_pmi_level(5, "rising"), 1)
        self.assertEqual(score_pmi_level(3, "unclear"), 0)
        self.assertEqual(score_pmi_level(0, "rising"), 0)
        self.assertEqual(score_pmi_level(-3, "falling"), -1)
        self.assertEqual(score_pmi_level(-12, "falling"), -2)
        self.assertEqual(score_pmi_level(-4, "unclear"), 0)

    def test_confirmation_clamps(self):
        self.assertEqual(pmi_confirmation(1, 0.4, 0), 2)
        self.assertEqual(pmi_confirmation(2, 0.4, -0.2), 2)
        self.assertEqual(pmi_confirmation(-2, -0.4, 0.2), -2)
        self.assertEqual(pmi_confirmation(1, 0.4, 0.2), 1)
        self.assertEqual(pmi_confirmation(1, None, -0.2), 1)

    def test_series_needs_both_surveys(self):
        months = [(f"2026-{month:02d}", 12.0) for month in range(1, 7)]
        missing = score_pmi_series(months, [])
        self.assertFalse(missing["ok"])
        self.assertIsNone(missing["score"])
        both = score_pmi_series(months, [(day, value - 2) for day, value in months])
        self.assertTrue(both["ok"])
        self.assertEqual(both["score"], 1)

    def test_empire_id_on_the_request_does_not_exist(self):
        self.assertIn("GACDINA066MSFRBNY", EMPIRE_IDS_NOT_ON_FRED)
        self.assertEqual(EMPIRE_HEADLINE, "GACDISA066MSFRBNY")


class TaiwanAndValuationTests(unittest.TestCase):
    def test_activity_bands_and_red_cap(self):
        self.assertEqual(score_tw_activity(40, 20, [40, 40])["score"], 2)
        capped = score_tw_activity(39, 20, [40, 39])
        self.assertEqual(capped["score"], 1)
        self.assertIn("上限", capped["note"])
        self.assertEqual(score_tw_activity(40, 20, [38, 40])["score"], 2)
        self.assertEqual(score_tw_activity(12, 20, [12])["score"], -2)
        self.assertEqual(score_tw_activity(20, 5, [20])["score"], -1)
        self.assertEqual(score_tw_activity(28, 5, [28])["score"], 1)
        self.assertEqual(score_tw_activity(28, -1, [28])["score"], -1)
        uncovered = score_tw_activity(40, 10, [40])
        self.assertFalse(uncovered["ok"])
        self.assertIsNone(uncovered["score"])
        missing = score_tw_activity(28, None, [28])
        self.assertFalse(missing["ok"])
        self.assertIsNone(missing["score"])
        self.assertEqual(NDC_RED_MIN, 38)

    def test_ndc_and_export_parsers(self):
        ndc = parse_ndc_scores(
            '"Date","景氣對策信號綜合分數","景氣對策信號"\n'
            "202608,40,紅\n202609,-,紅\n202610,41,紅\n"
        )
        self.assertEqual([item["score"] for item in ndc], [40.0, 41.0])
        export = parse_export_yoy(
            '"年度","月份","出口總值(新臺幣千元)"\n'
            '"114","8","100"\n'
            '"115","8","120"\n'
        )
        self.assertTrue(export["ok"])
        self.assertEqual(export["month"], "2026-08")
        self.assertAlmostEqual(export["yoy"], 20.0)

    def test_erp_thresholds_and_forward_shift(self):
        self.assertEqual(score_erp(4.01, False), 2)
        self.assertEqual(score_erp(4, False), 1)
        self.assertEqual(score_erp(3, False), 1)
        self.assertEqual(score_erp(2, False), 0)
        self.assertEqual(score_erp(1, False), -1)
        self.assertEqual(score_erp(0.9, False), -2)
        self.assertEqual(score_erp(3.51, True), 2)
        self.assertEqual(score_erp(3.5, True), 1)
        self.assertEqual(score_erp(2.5, True), 1)
        self.assertEqual(score_erp(1.5, True), 0)
        self.assertEqual(score_erp(0.5, True), -1)
        self.assertEqual(score_erp(0.4, True), -2)
        forward = score_valuation(4.0, 1.0, True)
        trailing = score_valuation(4.0, 1.0, False)
        self.assertEqual(forward["score"], score_erp(3.0, True))
        self.assertEqual(trailing["yield_kind"], "追溯")
        self.assertIn("遠期", forward["value"])
        self.assertIsNone(score_valuation(None, 1.0, True)["score"])


class CycleAndCoverageTests(unittest.TestCase):
    def test_relative_strength_quadrants(self):
        self.assertEqual(cycle_from_relative(0.08, 0.02), ("成長", "上行"))
        self.assertEqual(cycle_from_relative(0.02, 0.08), ("成長", "下行"))
        self.assertEqual(cycle_from_relative(0.04, -0.02), ("復甦", "上行"))
        self.assertEqual(cycle_from_relative(-0.03, 0.05), ("趨緩", "下行"))
        self.assertEqual(cycle_from_relative(-0.04, -0.01), ("衰退", "下行"))
        self.assertEqual(cycle_from_relative(0, -0.02), (None, None))

    def test_price_label_constant(self):
        self.assertEqual(PRICE_CYCLE_LABEL, "價格推估，非基本面")

    def test_neutral_regime_matches_the_existing_table(self):
        self.assertEqual(REGIME_RISK["中性"]["target_leverage"], 1.5)
        self.assertEqual(REGIME_RISK["中性"]["min_cash_pct"], 15.0)

    def test_coverage_scales_and_caps(self):
        mostly = [_scored(item_id, 1) for item_id in (
            "us_real_yield", "hy_spread", "vix", "equity_ma", "sox_trend", "pmi", "earnings_revisions", "tw_activity"
        )]
        scaled = aggregate_scores(mostly)
        self.assertEqual(scaled["total"], 9)
        self.assertNotEqual(scaled["total"], 8)
        self.assertEqual(scaled["regime"], "牛")
        self.assertEqual(scaled["coverage_status"], "完整")

        partial = [_scored(item_id, 2) for item_id in ("us_real_yield", "hy_spread", "vix", "equity_ma", "sox_trend")]
        partial.append(_scored("valuation", 2))
        capped = aggregate_scores(partial)
        self.assertEqual(capped["regime"], "牛")
        self.assertEqual(capped["coverage_status"], "不完整")
        self.assertEqual(capped["risk"]["target_leverage"], 1.5)
        self.assertEqual(capped["risk"]["min_cash_pct"], 15.0)

        thin = [_scored(item_id, 2) for item_id in ("us_real_yield", "hy_spread", "vix", "equity_ma")]
        forced = aggregate_scores(thin)
        self.assertEqual(forced["regime"], "中性")
        self.assertEqual(forced["risk"]["target_leverage"], 1.5)
        self.assertEqual(forced["risk"]["min_cash_pct"], 15.0)
        self.assertEqual(forced["raw_regime"], "牛")

    def test_missing_is_not_zero_but_a_real_zero_counts(self):
        real_zero = aggregate_scores([_scored("vix", 0), _scored("hy_spread", 2)])
        omitted = aggregate_scores([_scored("hy_spread", 2)])
        self.assertNotEqual(real_zero["total"], omitted["total"])
        failed = aggregate_scores([{"id": "pmi", "score": None, "scored": False, "example": False}, _scored("hy_spread", 2)])
        self.assertEqual(failed["total"], omitted["total"])

    def test_example_scores_are_not_totalled(self):
        view = load_macro_cycle()
        self.assertEqual(sum(item["score"] for item in view["indicators"]), 1)
        self.assertIsNone(view["total"])
        self.assertTrue(all(item["example"] and not item["scored"] for item in view["indicators"]))


class OverrideAndFetchTests(unittest.TestCase):
    def test_override_expires_at_45_days(self):
        today = "2026-11-11"
        self.assertTrue(manual_override_fresh("手動", "2026-09-27", today))
        self.assertTrue(manual_override_fresh("manual", "2026-11-11", today))
        self.assertFalse(manual_override_fresh("手動", "2026-09-26", today))
        self.assertFalse(manual_override_fresh("auto", "2026-11-11", today))
        self.assertFalse(manual_override_fresh("手動", "2026-11-12", today))

        def row(updated):
            return {
                "indicator_id": "pmi",
                "name": "",
                "score": 2,
                "source": "手動",
                "value": "手",
                "note": "",
                "updated_at": updated,
            }

        sheet = {"ok": True, "scores": [row("2026-09-27")], "industries": [], "score_error": None, "industry_error": "略過"}
        proxy = {"pmi": {"ok": True, "score": -2, "value": "代理", "data_date": "2026-10-01"}}
        fresh = compose_macro_view(load_macro_cycle(), sheet, proxy, today=today)
        pmi = next(item for item in fresh["indicators"] if item["id"] == "pmi")
        self.assertEqual(pmi["score"], 2)
        self.assertEqual(pmi["layer"], "手動")

        sheet["scores"] = [row("2026-09-26")]
        stale = compose_macro_view(load_macro_cycle(), sheet, proxy, today=today)
        pmi = next(item for item in stale["indicators"] if item["id"] == "pmi")
        self.assertEqual(pmi["score"], -2)
        self.assertEqual(pmi["layer"], "代理")

        failed = compose_macro_view(load_macro_cycle(), sheet, {"pmi": {"ok": False, "score": None, "reason": "逾時"}}, today=today)
        pmi = next(item for item in failed["indicators"] if item["id"] == "pmi")
        self.assertIsNone(pmi["score"])
        self.assertFalse(pmi["scored"])
        self.assertNotEqual(pmi["score"], 0)

    def test_fetch_failure_does_not_become_zero(self):
        def boom():
            raise RuntimeError("network down")

        keys = (
            "dfii10", "dgs10", "hy", "vix", "spx", "twii", "sox",
            "pmi_bundle", "ndc_scores", "export_yoy", "spx_pe", "sector_closes",
        )
        with patch("macro_cycle_live._default_fetchers", return_value={}), patch(
            "macro_cycle_live._proxy_fetchers", return_value={}
        ):
            result = fetch_auto_results("2026-11-11", fetchers={key: boom for key in keys})
        self.assertFalse(result["pmi"]["ok"])
        self.assertIsNone(result["pmi"]["score"])
        self.assertFalse(result["earnings_revisions"]["ok"])
        self.assertTrue(result["industry_rs"])
        self.assertTrue(all(not item["ok"] for item in result["industry_rs"].values()))
        empty = proxies_from_payloads({}, "2026-11-11")
        self.assertIsNone(empty["valuation"]["score"])
        self.assertIsNone(empty["tw_activity"]["score"])

    def test_manual_industry_override_and_price_label(self):
        industries = [{
            "industry": "金融",
            "cycle": "衰退",
            "direction": "下行",
            "stance": "中性",
            "tickers": "2881",
            "source": "手動",
            "updated_at": "2026-11-01",
        }]
        sheet = {"ok": True, "scores": [], "industries": industries, "score_error": "略過分數", "industry_error": None}
        auto = {"industry_rs": {"金融": {"ok": True, "cycle": "成長", "direction": "上行"}}}
        kept = compose_macro_view(load_macro_cycle(), sheet, auto, today="2026-11-11")
        row = next(item for item in kept["industries"] if item["name"] == "金融")
        self.assertEqual(row["cycle"], "衰退")
        self.assertEqual(row["cycle_layer"], "手動")

        industries[0]["updated_at"] = "2026-01-01"
        replaced = compose_macro_view(load_macro_cycle(), sheet, auto, today="2026-11-11")
        row = next(item for item in replaced["industries"] if item["name"] == "金融")
        self.assertEqual(row["cycle"], "成長")
        self.assertEqual(row["cycle_note"], "價格推估，非基本面")

    def test_sheet_dates_are_not_dropped(self):
        # 2026-11-07 的試算表序號，以及沒有補零的 2026/11/7。
        self.assertEqual(parse_sheet_date(46333), "2026-11-07")
        self.assertEqual(parse_sheet_date("2026/11/7"), "2026-11-07")
        self.assertEqual(parse_sheet_date("2026-11-07"), "2026-11-07")
        self.assertEqual(parse_sheet_date(20261107), "2026-11-07")

    def test_live_macro_score_rows_override_and_autos_stay_live(self):
        today = "2026-11-11"
        rows = []
        for item_id, source, score in (
            ("us_real_yield", "manual", 2),
            ("hy_spread", "auto", 2),
            ("vix", "manual", 2),
            ("equity_ma", "auto", 2),
            ("sox_trend", "manual", 2),
            ("pmi", "manual", 1),
            ("earnings_revisions", "手動", 0),
            ("valuation", "manual", -1),
            ("tw_activity", "manual", 1),
        ):
            rows.append(
                {
                    "indicator_id": item_id,
                    "name": "",
                    "score": score,
                    "source": source,
                    "value": "表",
                    "trend": "",
                    "note": "",
                    "updated_at": "2026-11-07",
                }
            )
        parsed, error = parse_score_row(rows[-1])
        self.assertIsNone(error)
        self.assertEqual(parsed["source"], "manual")
        self.assertEqual(parsed["updated_at"], "2026-11-07")
        sheet = {"ok": True, "scores": rows, "industries": [], "score_error": None, "industry_error": "略過產業"}
        live = {
            "us_real_yield": {"ok": True, "score": -2, "value": "即時", "updated_at": today},
            "hy_spread": {"ok": True, "score": -2, "value": "即時", "updated_at": today},
            "vix": {"ok": True, "score": -2, "value": "即時", "updated_at": today},
            "equity_ma": {"ok": True, "score": -2, "value": "即時", "updated_at": today},
            "sox_trend": {"ok": True, "score": -2, "value": "即時", "updated_at": today},
            "pmi": {"ok": True, "score": -2, "value": "代理", "data_date": "2026-10-01"},
        }
        view = compose_macro_view(load_macro_cycle(), sheet, live, today=today)
        by_id = {item["id"]: item for item in view["indicators"]}
        for item_id in ("us_real_yield", "hy_spread", "vix", "equity_ma", "sox_trend"):
            self.assertEqual(by_id[item_id]["score"], -2, item_id)
            self.assertEqual(by_id[item_id]["layer"], "自動", item_id)
        self.assertEqual(by_id["pmi"]["score"], 1)
        self.assertEqual(by_id["pmi"]["layer"], "手動")
        self.assertEqual(by_id["earnings_revisions"]["score"], 0)
        self.assertEqual(by_id["earnings_revisions"]["layer"], "手動")
        self.assertEqual(by_id["valuation"]["layer"], "手動")
        self.assertEqual(by_id["tw_activity"]["layer"], "手動")
        self.assertNotEqual(view["data_origin"], "json")
        self.assertTrue(all(not item["example"] for item in view["indicators"]))

    def test_industry_row_without_source_is_override_while_fresh(self):
        industries = [{
            "industry": "金融",
            "cycle": "復甦",
            "direction": "上行",
            "stance": "中性",
            "tickers": "2881",
            "updated_at": "2026-10-29",
        }]
        sheet = {"ok": True, "scores": [], "industries": industries, "score_error": "略過分數", "industry_error": None}
        auto = {"industry_rs": {"金融": {"ok": True, "cycle": "衰退", "direction": "下行"}}}
        view = compose_macro_view(load_macro_cycle(), sheet, auto, today="2026-11-11")
        row = next(item for item in view["industries"] if item["name"] == "金融")
        self.assertEqual(row["cycle"], "復甦")
        self.assertEqual(row["cycle_layer"], "手動")
        self.assertNotIn("source", industries[0])


if __name__ == "__main__":
    unittest.main()
