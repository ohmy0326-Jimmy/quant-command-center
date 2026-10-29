"""唯讀 API 測試。不連網、不讀金鑰、不呼叫 Gemini。"""

import io
import json
import logging
import os
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi.testclient import TestClient

from ai_advice import RISK_MANAGER_MODEL, build_risk_manager_prompt, page4_advice_figures
from api.advice_cache import AdviceCache, AdviceTimeout, RateLimited, call_with_timeout
from api.auth import RedactSecretsFilter, api_key_matches, install_redact_filter
from api.main import AdviceNotConfigured, app, get_book_loader, get_prompt_runner, reset_advice_cache
from api.schemas import export_json_schema
from portfolio_briefing import SCHEMA_VERSION, format_as_of, portfolio_payload, position_records
from portfolio_risk import YUANTA_FUTURES_BASE_EQUITY, finalize_valuation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "docs", "readonly_api.schema.json")
API_KEY = "test-key-not-a-secret"
AS_OF = datetime(2026, 10, 29, 22, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _stock(account, kind, ticker, qty, cost, price, lev=1.0, beta=1.0, sector="其他", max_loss=-100):
    return {
        "帳戶": account,
        "種類": kind,
        "標的": ticker,
        "名稱": ticker,
        "持有數量": qty,
        "平均成本": cost,
        "投入總金額": 0.0 if "期" in kind else cost * qty,
        "最大虧損比例": max_loss,
        "標的槓桿": lev,
        "即時報價": price,
        "Beta值": beta,
        "產業別": sector,
    }


def _valued_book():
    positions = pd.DataFrame(
        [
            _stock("股票戶", "台股", "2330", 1000, 100, 110, lev=1, beta=1.2, sector="晶圓代工"),
            _stock("股票戶", "美股", "SOXL", 10, 20, 25, lev=3, beta=3, sector="半導體槓桿ETF"),
            _stock("元大期貨", "期貨", "TX", 1, 20000, 21000, lev=1, beta=1, sector="臺指期貨"),
        ]
    )
    accounts = pd.DataFrame(
        [
            {"帳戶名稱": "股票戶", "現金餘額": 50000, "融資/質押借款": 10000},
            {"帳戶名稱": "元大期貨", "現金餘額": 0, "融資/質押借款": 0},
        ]
    )
    valued, _accounts, metrics = finalize_valuation(positions, accounts, usdtwd=30)
    payload = portfolio_payload(valued, metrics, usdtwd=30, as_of=AS_OF)
    return valued, metrics, payload


class Page4SnapshotTests(unittest.TestCase):
    def test_payload_uses_page4_numbers(self):
        _valued, metrics, payload = _valued_book()
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["as_of"], "2026-10-29T22:00:00+08:00")
        self.assertEqual(payload["currency"], "TWD")
        self.assertAlmostEqual(payload["net_value"], 527922.0, places=6)
        self.assertAlmostEqual(payload["exposure"], 1_033_500.0, places=6)
        self.assertAlmostEqual(payload["leverage"], 1_033_500 / 527922, places=8)
        self.assertAlmostEqual(payload["cash_pct"], 50000 / 527922 * 100, places=8)
        self.assertAlmostEqual(payload["beta"], 1_100_500 / 1_033_500, places=8)
        self.assertAlmostEqual(payload["net_value"], metrics["nav"], places=8)
        self.assertAlmostEqual(payload["leverage"], metrics["leverage"], places=8)

        by_symbol = {row["symbol"]: row for row in payload["positions"]}
        self.assertEqual(list(by_symbol), ["2330", "SOXL", "TX"])
        self.assertAlmostEqual(by_symbol["2330"]["qty"], 1000, places=6)
        self.assertAlmostEqual(by_symbol["2330"]["price"], 110, places=6)
        self.assertAlmostEqual(by_symbol["2330"]["market_value"], 110000, places=6)
        self.assertAlmostEqual(by_symbol["2330"]["pnl"], 9670, places=6)
        self.assertAlmostEqual(by_symbol["SOXL"]["market_value"], 7500, places=6)
        self.assertAlmostEqual(by_symbol["SOXL"]["pnl"], 49.25 * 30, places=6)
        self.assertAlmostEqual(by_symbol["TX"]["pnl"], 200000, places=6)
        self.assertEqual(by_symbol["2330"]["name"], "2330")

        yuanta = payload["yuanta_futures"]
        self.assertAlmostEqual(yuanta["equity"], YUANTA_FUTURES_BASE_EQUITY + 200000, places=6)
        self.assertAlmostEqual(yuanta["maintenance_margin"], 538000, places=6)
        self.assertAlmostEqual(yuanta["margin_ratio_pct"], 370422 / 538000 * 100, places=6)
        self.assertTrue(yuanta["margin_call"])
        self.assertAlmostEqual(yuanta["excess_margin"], 0.0, places=6)

    def test_empty_book_has_no_positions(self):
        payload = portfolio_payload(pd.DataFrame(), {"nav": 0, "exposure": 0, "leverage": 0, "cash_pct": 0, "beta": 0}, usdtwd=32, as_of=AS_OF)
        self.assertEqual(payload["positions"], [])
        self.assertEqual(position_records(pd.DataFrame()), [])
        self.assertIsNone(payload["yuanta_futures"]["margin_ratio_pct"])
        self.assertFalse(payload["yuanta_futures"]["margin_call"])


class AdvicePromptTests(unittest.TestCase):
    def test_prompt_matches_page4_wording(self):
        valued, metrics, _payload = _valued_book()
        nav, leverage, beta, cash_pct = page4_advice_figures(metrics)
        self.assertEqual(leverage, round(float(metrics["leverage"]), 2))
        self.assertEqual(cash_pct, round(float(metrics["cash_pct"]), 1))
        text = build_risk_manager_prompt(valued, nav, leverage, beta, cash_pct)
        columns = ["標的", "名稱", "產業別", "Beta值", "真實風險權重 (%)", "標的槓桿", "報酬率 (%)"]
        expected = (
            f"總帳戶淨值: NT$ {nav:,.0f}\n"
            f"合併總槓桿率: {leverage}x\n"
            f"投資組合加權Beta: {beta:.2f}\n"
            f"總現金水位: {cash_pct}%\n"
            f"持倉明細:\n{valued[columns].to_string(index=False)}\n"
            f"請給予 3 點風控建議。"
        )
        self.assertEqual(text, expected)
        self.assertIn("合併總槓桿率: 1.96x", text)
        self.assertIn("總現金水位: 9.5%", text)
        self.assertEqual(RISK_MANAGER_MODEL, "gemini-3.8-flash")

    def test_empty_positions_prompt(self):
        text = build_risk_manager_prompt(pd.DataFrame(), 0, 0.0, 0.0, 0.0)
        self.assertIn("無持倉", text)
        self.assertTrue(text.endswith("請給予 3 點風控建議。"))

    def test_dashboard_calls_shared_advice(self):
        with open(os.path.join(ROOT, "Dashboard.py"), encoding="utf-8") as handle:
            dashboard = handle.read()
        with open(os.path.join(ROOT, "ai_advice.py"), encoding="utf-8") as handle:
            advice = handle.read()
        self.assertIn("generate_risk_manager_advice", dashboard)
        self.assertNotIn("請給予 3 點風控建議。", dashboard)
        self.assertIn("請給予 3 點風控建議。", advice)
        self.assertEqual(dashboard.count("gemini-3.8-flash"), 3)


class AuthUnitTests(unittest.TestCase):
    def test_constant_time_compare(self):
        self.assertTrue(api_key_matches(API_KEY, API_KEY))
        self.assertTrue(api_key_matches(API_KEY, f"  {API_KEY}\n"))
        self.assertFalse(api_key_matches("wrong-key", API_KEY))
        self.assertFalse(api_key_matches("x", "yy"))
        self.assertFalse(api_key_matches("", ""))
        self.assertFalse(api_key_matches(API_KEY, ""))
        self.assertFalse(api_key_matches(None, API_KEY))

    def test_log_filter_redacts_key(self):
        install_redact_filter()
        os.environ["READONLY_API_KEY"] = "this-is-a-test-key-value"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("uvicorn.access")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info("GET /v1/portfolio X-API-Key: this-is-a-test-key-value")
        finally:
            logger.removeHandler(handler)
            os.environ.pop("READONLY_API_KEY", None)
        written = stream.getvalue()
        self.assertNotIn("this-is-a-test-key-value", written)
        self.assertIn("[redacted]", written)
        self.assertIsInstance(RedactSecretsFilter(), logging.Filter)


class CacheUnitTests(unittest.TestCase):
    def test_cache_hit_and_rate_limit(self):
        clock = {"now": 0.0}
        cache = AdviceCache(ttl_seconds=10, min_interval_seconds=30, clock=lambda: clock["now"])
        moment = datetime(2026, 10, 29, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        first = cache.generate(lambda: "三點建議", moment)
        self.assertFalse(first.cached)
        clock["now"] = 5
        second = cache.lookup()
        self.assertTrue(second.cached)
        self.assertEqual(second.text, "三點建議")

        clock["now"] = 11
        stale = cache.lookup()
        self.assertTrue(stale.cached)

        failed = AdviceCache(ttl_seconds=10, min_interval_seconds=30, clock=lambda: clock["now"])
        clock["now"] = 0

        def boom():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            failed.generate(boom, moment)
        clock["now"] = 5
        with self.assertRaises(RateLimited) as caught:
            failed.lookup()
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_call_timeout(self):
        started = time.monotonic()
        with self.assertRaises(AdviceTimeout):
            call_with_timeout(lambda: time.sleep(1.0), 0.1)
        self.assertLess(time.monotonic() - started, 0.7)


class ApiHttpTests(unittest.TestCase):
    def setUp(self):
        os.environ["READONLY_API_KEY"] = API_KEY
        os.environ.pop("AI_ADVICE_TIMEOUT_SECONDS", None)
        os.environ.pop("AI_ADVICE_CACHE_TTL_SECONDS", None)
        os.environ.pop("AI_ADVICE_MIN_INTERVAL_SECONDS", None)
        reset_advice_cache()
        app.dependency_overrides.clear()
        self._client = TestClient(app)
        self.client = self._client.__enter__()
        self.valued, self.metrics, self.payload = _valued_book()
        self.loads = {"n": 0}
        self.prompts = []

        def loader():
            self.loads["n"] += 1

            class Book:
                payload = self.payload
                positions = self.valued
                metrics = self.metrics

            return Book()

        def runner(prompt: str) -> str:
            self.prompts.append(prompt)
            return "建議一：降低槓桿。"

        self.runner = runner
        app.dependency_overrides[get_book_loader] = lambda: loader
        app.dependency_overrides[get_prompt_runner] = lambda: runner

    def tearDown(self):
        app.dependency_overrides.clear()
        reset_advice_cache()
        self._client.__exit__(None, None, None)
        os.environ.pop("READONLY_API_KEY", None)
        os.environ.pop("AI_ADVICE_TIMEOUT_SECONDS", None)
        os.environ.pop("AI_ADVICE_MIN_INTERVAL_SECONDS", None)

    def test_health_without_key(self):
        response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"schema_version", "as_of", "status"})
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["schema_version"], SCHEMA_VERSION)
        self.assertIn("+08:00", body["as_of"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.loads["n"], 0)

    def test_auth_rejected_and_accepted(self):
        missing = self.client.get("/v1/portfolio")
        wrong = self.client.get("/v1/portfolio", headers={"X-API-Key": "super-secret-wrong-value"})
        query = self.client.get("/v1/portfolio", params={"X-API-Key": API_KEY})
        for response in (missing, wrong, query):
            self.assertEqual(response.status_code, 401)
            body = response.json()
            self.assertEqual(set(body), {"schema_version", "as_of", "detail"})
            self.assertEqual(body["detail"], "unauthorized")
            self.assertEqual(body["schema_version"], SCHEMA_VERSION)
            self.assertNotIn(API_KEY, response.text)
            self.assertNotIn("super-secret-wrong-value", response.text)
        self.assertEqual(missing.json()["detail"], wrong.json()["detail"])
        self.assertEqual(self.loads["n"], 0)

        os.environ["READONLY_API_KEY"] = ""
        unset = self.client.get("/v1/portfolio", headers={"X-API-Key": ""})
        self.assertEqual(unset.status_code, 401)
        os.environ["READONLY_API_KEY"] = API_KEY

        accepted = self.client.get("/v1/portfolio", headers={"X-API-Key": API_KEY})
        self.assertEqual(accepted.status_code, 200)
        self.assertNotIn(API_KEY, accepted.text)
        body = accepted.json()
        self.assertEqual(
            set(body),
            {
                "schema_version",
                "as_of",
                "currency",
                "usdtwd",
                "net_value",
                "exposure",
                "leverage",
                "cash_pct",
                "beta",
                "positions",
                "yuanta_futures",
            },
        )
        self.assertEqual(set(body["positions"][0]), {"symbol", "name", "qty", "price", "market_value", "pnl"})
        self.assertEqual(
            set(body["yuanta_futures"]),
            {"equity", "maintenance_margin", "margin_ratio_pct", "excess_margin", "margin_call"},
        )
        self.assertAlmostEqual(body["net_value"], 527922.0, places=4)
        self.assertEqual(body["as_of"], "2026-10-29T22:00:00+08:00")
        self.assertEqual(body["positions"][0]["symbol"], "2330")
        self.assertEqual(accepted.headers["cache-control"], "no-store")
        self.assertEqual(self.loads["n"], 1)

        lowered = self.client.get("/v1/portfolio", headers={"x-api-key": API_KEY})
        self.assertEqual(lowered.status_code, 200)

    def test_post_is_rejected_and_routes_are_read_only(self):
        response = self.client.post("/v1/portfolio", headers={"X-API-Key": API_KEY})
        self.assertEqual(response.status_code, 405)
        methods = set()
        for route in app.routes:
            methods.update(getattr(route, "methods", None) or set())
        self.assertTrue(methods <= {"GET", "HEAD"})

    def test_loader_error_hides_detail(self):
        def explode():
            raise RuntimeError("SENTINEL-SHEET-ROW")

        app.dependency_overrides[get_book_loader] = lambda: explode
        response = self.client.get("/v1/portfolio", headers={"X-API-Key": API_KEY})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "portfolio unavailable")
        self.assertNotIn("SENTINEL-SHEET-ROW", response.text)
        self.assertNotIn(API_KEY, response.text)

    def test_ai_advice_schema_cache_and_timeout(self):
        headers = {"X-API-Key": API_KEY}
        first = self.client.get("/v1/ai-advice", headers=headers)
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(
            set(body),
            {"schema_version", "as_of", "text", "model", "cached", "generated_at"},
        )
        self.assertEqual(body["text"], "建議一：降低槓桿。")
        self.assertEqual(body["model"], "gemini-3.8-flash")
        self.assertFalse(body["cached"])
        self.assertIn("+08:00", body["generated_at"])
        self.assertEqual(body["schema_version"], SCHEMA_VERSION)
        self.assertIn("請給予 3 點風控建議。", self.prompts[0])
        self.assertIn("NT$ 527,922", self.prompts[0])
        self.assertNotIn(API_KEY, first.text)

        second = self.client.get("/v1/ai-advice", headers=headers)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["cached"])
        self.assertEqual(second.json()["text"], body["text"])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.loads["n"], 1)

        missing = self.client.get("/v1/ai-advice")
        self.assertEqual(missing.status_code, 401)

    def test_ai_advice_timeout_and_unconfigured(self):
        os.environ["AI_ADVICE_TIMEOUT_SECONDS"] = "0.1"
        os.environ["AI_ADVICE_MIN_INTERVAL_SECONDS"] = "0"
        reset_advice_cache()

        def slow(_prompt: str) -> str:
            time.sleep(0.5)
            return "too late"

        app.dependency_overrides[get_prompt_runner] = lambda: slow
        timed = self.client.get("/v1/ai-advice", headers={"X-API-Key": API_KEY})
        self.assertEqual(timed.status_code, 504)
        self.assertEqual(timed.json()["detail"], "ai advice timed out")
        self.assertEqual(set(timed.json()), {"schema_version", "as_of", "detail"})

        reset_advice_cache()

        def unconfigured(_prompt: str) -> str:
            raise AdviceNotConfigured()

        app.dependency_overrides[get_prompt_runner] = lambda: unconfigured
        bare = self.client.get("/v1/ai-advice", headers={"X-API-Key": API_KEY})
        self.assertEqual(bare.status_code, 503)
        self.assertEqual(bare.json()["detail"], "ai advice is not configured")

    def test_committed_schema_matches_models(self):
        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            committed = json.load(handle)
        self.assertEqual(committed, export_json_schema())
        self.assertEqual(committed["header"], "X-API-Key")
        self.assertIn("net_value", committed["models"]["portfolio"]["properties"])
        self.assertIn("positions", committed["models"]["portfolio"]["properties"])

    def test_as_of_is_taipei(self):
        self.assertEqual(format_as_of(AS_OF), "2026-10-29T22:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
