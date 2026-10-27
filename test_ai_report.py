"""沒有 Gemini API key 時，用假的客戶端覆蓋逾時、錯誤、重試與快取。"""

import os
import threading
import time
import unittest
from unittest.mock import patch

import streamlit as st

import ai_report
from ai_report import (
    AiEmptyResponseError,
    AiReportError,
    AiTimeoutError,
    bind_streamlit_cache,
    format_ai_error,
    format_position_lines,
    format_sector_lines,
    gemini_model_name,
    generate_ai_text,
    run_ai_report,
    trim_prompt,
)


class Chunk:
    def __init__(self, text="", finish=None, block=None):
        self.text = text or None
        self.candidates = []
        self.prompt_feedback = None
        if finish:
            self.candidates = [type("Cand", (), {"finish_reason": finish, "content": None})()]
        if block:
            self.prompt_feedback = type("Feedback", (), {"block_reason": block})()


class Hang:
    release = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.release.wait(8)
        raise StopIteration


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content_stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = FakeClient.behavior(model, contents, config)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    behavior = None
    instances = []

    def __init__(self, *, api_key=None, http_options=None, **kwargs):
        self.api_key = api_key
        self.http_options = http_options
        self.models = FakeModels()
        FakeClient.instances.append(self)


class EnvGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: os.environ.get(name)
            for name in (
                "GEMINI_MODEL",
                "GEMINI_TIMEOUT_SEC",
                "GEMINI_CACHE_TTL_SEC",
                "GEMINI_MAX_OUTPUT_TOKENS",
                "GEMINI_MAX_PROMPT_CHARS",
            )
        }
        for name in self._saved:
            os.environ.pop(name, None)
        FakeClient.instances = []
        FakeClient.behavior = None
        Hang.release.clear()

    def tearDown(self):
        Hang.release.set()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class GenerateTests(EnvGuard):
    def test_default_model_and_streaming_with_http_timeout(self):
        def behavior(model, contents, config):
            return [Chunk("宏"), Chunk("觀")]

        FakeClient.behavior = behavior
        seen = []
        with patch("google.genai.Client", FakeClient):
            text = generate_ai_text("test-key", "  請分析  ", timeout_sec=30, on_chunk=seen.append)
        self.assertEqual(text, "宏觀")
        self.assertEqual(seen, ["宏", "宏觀"])
        self.assertEqual(gemini_model_name(), "gemini-2.5-flash-lite")
        client = FakeClient.instances[0]
        self.assertEqual(client.api_key, "test-key")
        self.assertEqual(client.http_options.timeout, 30000)
        self.assertEqual(client.http_options.retry_options.attempts, 1)
        call = client.models.calls[0]
        self.assertEqual(call["model"], "gemini-2.5-flash-lite")
        self.assertEqual(call["config"].max_output_tokens, 1024)
        self.assertEqual(call["config"].thinking_config.thinking_budget, 0)

    def test_env_model_overrides_default(self):
        os.environ["GEMINI_MODEL"] = "gemini-2.5-flash"
        FakeClient.behavior = lambda model, contents, config: [Chunk("好")]
        with patch("google.genai.Client", FakeClient):
            text = generate_ai_text("k", "問題", timeout_sec=10)
        self.assertEqual(text, "好")
        self.assertEqual(FakeClient.instances[0].models.calls[0]["model"], "gemini-2.5-flash")
        self.assertEqual(FakeClient.instances[0].models.calls[0]["config"].thinking_config.thinking_budget, 0)

    def test_hard_timeout_does_not_retry_and_message_is_chinese(self):
        calls = {"n": 0}

        def behavior(model, contents, config):
            calls["n"] += 1
            return Hang()

        FakeClient.behavior = behavior
        started = time.monotonic()
        with patch("google.genai.Client", FakeClient):
            with self.assertRaises(AiTimeoutError) as caught:
                generate_ai_text("k", "慢", timeout_sec=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)
        self.assertEqual(calls["n"], 1)
        message = format_ai_error(caught.exception, timeout_sec=1)
        self.assertIn("AI 分析逾時", message)
        self.assertIn("超過 1 秒仍未完成", message)
        self.assertIn("沒有寫入報告", message)
        self.assertIn("原因：", message)
        self.assertIn("gemini-2.5-flash-lite", message)

    def test_transient_error_retries_once(self):
        calls = {"n": 0}

        class Overloaded(Exception):
            def __init__(self):
                super().__init__("503 UNAVAILABLE. overloaded")
                self.code = 503

        def behavior(model, contents, config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Overloaded()
            return [Chunk("重試成功")]

        FakeClient.behavior = behavior
        seen = []
        with patch("google.genai.Client", FakeClient):
            text = generate_ai_text("k", "問題", timeout_sec=10, on_chunk=seen.append)
        self.assertEqual(text, "重試成功")
        self.assertEqual(calls["n"], 2)
        self.assertIn("", seen)

    def test_auth_error_is_not_retried_and_reason_is_shown(self):
        calls = {"n": 0}

        class Denied(Exception):
            def __init__(self):
                super().__init__("401 PERMISSION_DENIED. API key not valid")
                self.code = 401

        def behavior(model, contents, config):
            calls["n"] += 1
            raise Denied()

        FakeClient.behavior = behavior
        with patch("google.genai.Client", FakeClient):
            with self.assertRaises(Denied) as caught:
                generate_ai_text("k", "問題", timeout_sec=10)
        self.assertEqual(calls["n"], 1)
        message = format_ai_error(caught.exception)
        self.assertIn("AI 分析失敗，沒有產生報告", message)
        self.assertIn("API key not valid", message)

    def test_missing_api_key_does_not_call_client(self):
        FakeClient.behavior = lambda model, contents, config: [Chunk("不該出現")]
        with patch("google.genai.Client", FakeClient):
            with self.assertRaises(AiReportError) as caught:
                generate_ai_text("", "問題", timeout_sec=5)
        self.assertEqual(FakeClient.instances, [])
        self.assertIn("GEMINI_API_KEY", format_ai_error(caught.exception))

    def test_safety_block_is_not_retried(self):
        calls = {"n": 0}

        def behavior(model, contents, config):
            calls["n"] += 1
            return [Chunk("", finish="SAFETY")]

        FakeClient.behavior = behavior
        with patch("google.genai.Client", FakeClient):
            with self.assertRaises(AiEmptyResponseError) as caught:
                generate_ai_text("k", "問題", timeout_sec=5)
        self.assertEqual(calls["n"], 1)
        self.assertIn("SAFETY", str(caught.exception))
        self.assertIn("沒有產生報告", format_ai_error(caught.exception))

    def test_empty_response_retries_once(self):
        calls = {"n": 0}

        def behavior(model, contents, config):
            calls["n"] += 1
            if calls["n"] == 1:
                return [Chunk("", finish="STOP")]
            return [Chunk("補上了")]

        FakeClient.behavior = behavior
        with patch("google.genai.Client", FakeClient):
            text = generate_ai_text("k", "問題", timeout_sec=5)
        self.assertEqual(text, "補上了")
        self.assertEqual(calls["n"], 2)

    def test_thinking_config_rejection_falls_back_without_using_the_transient_retry(self):
        calls = {"n": 0}

        def behavior(model, contents, config):
            calls["n"] += 1
            thinking = getattr(config, "thinking_config", None)
            if thinking is not None and getattr(thinking, "thinking_budget", None) == 0:
                raise RuntimeError("400 INVALID_ARGUMENT thinking_budget is not supported")
            return [Chunk("改為不限制思考後完成")]

        FakeClient.behavior = behavior
        with patch("google.genai.Client", FakeClient):
            text = generate_ai_text("k", "問題", timeout_sec=5)
        self.assertEqual(text, "改為不限制思考後完成")
        self.assertEqual(calls["n"], 2)
        self.assertIsNone(FakeClient.instances[1].models.calls[0]["config"].thinking_config)

    def test_non_streaming_client_still_returns_text(self):
        class Models:
            def generate_content(self, *, model, contents, config):
                return Chunk("整段")

        class Client:
            def __init__(self, *, api_key=None, http_options=None, **kwargs):
                self.models = Models()

        with patch("google.genai.Client", Client):
            self.assertEqual(generate_ai_text("k", "問題", timeout_sec=5), "整段")


class PromptAndCacheTests(EnvGuard):
    def test_trim_prompt_drops_indent_and_caps_length(self):
        indented = "\n".join("            " + line for line in ["你是一位經理人。", "請用繁體中文。"])
        trimmed = trim_prompt(indented)
        self.assertEqual(trimmed, "你是一位經理人。\n請用繁體中文。")
        capped = trim_prompt("甲" * 100, max_chars=30)
        self.assertLessEqual(len(capped), 30)
        self.assertIn("提示過長，已截斷", capped)
        self.assertEqual(trim_prompt(capped, max_chars=30), capped)

    def test_sector_and_position_prompts_are_compact(self):
        sector = format_sector_lines([{
            "ticker": "XLK",
            "name": "科技",
            "daily": 1.2,
            "monthly": -0.5,
            "holdings": "NVDA " * 80,
        }])
        self.assertIn("XLK 科技 日+1.20% 月-0.50%", sector)
        self.assertIn("…", sector)
        self.assertLess(len(sector), 250)
        rows = [
            {"ticker": f"T{i:02d}", "name": "很長的標的名稱" * 6, "sector": "半導體", "weight": i, "beta": 1.239, "ret": -1.234}
            for i in range(1, 31)
        ]
        positions = format_position_lines(rows, limit=25)
        self.assertIn("其餘 5 檔風險權重較低，已省略", positions)
        self.assertIn("T30", positions)
        self.assertNotIn("T01", positions)
        self.assertIn("Beta 1.24", positions)
        self.assertIn("報酬 -1.23%", positions)
        self.assertLess(len(positions.splitlines()[0]), 160)

    def test_cache_hit_does_not_call_the_model_again(self):
        calls = []

        def fake_generate(api_key, prompt, *, model=None, timeout_sec=None, on_chunk=None):
            calls.append(prompt)
            if on_chunk:
                on_chunk("甲")
                on_chunk("甲乙")
            return "甲乙"

        with patch.object(ai_report, "generate_ai_text", fake_generate):
            cached = bind_streamlit_cache(st.cache_data)
            cached.clear()
            first_seen = []
            text = run_ai_report("k", "  相同提示  ", on_chunk=first_seen.append, cached_func=cached)
            second_seen = []
            again = run_ai_report("k", "相同提示", on_chunk=second_seen.append, cached_func=cached)
            other = run_ai_report("k", "另一個提示", on_chunk=lambda _text: None, cached_func=cached)
        self.assertEqual(text, "甲乙")
        self.assertEqual(again, "甲乙")
        self.assertEqual(other, "甲乙")
        self.assertEqual(calls, ["相同提示", "另一個提示"])
        self.assertEqual(first_seen, ["甲", "甲乙"])
        self.assertEqual(second_seen, ["甲乙"])

    def test_failed_call_is_not_cached(self):
        state = {"n": 0}

        def flaky(api_key, prompt, *, model=None, timeout_sec=None, on_chunk=None):
            state["n"] += 1
            if state["n"] == 1:
                raise AiReportError("400 bad request")
            return "第二次成功"

        with patch.object(ai_report, "generate_ai_text", flaky):
            cached = bind_streamlit_cache(st.cache_data)
            cached.clear()
            with self.assertRaises(AiReportError):
                run_ai_report("k", "會失敗一次", cached_func=cached)
            text = run_ai_report("k", "會失敗一次", cached_func=cached)
            cached_text = run_ai_report("k", "會失敗一次", cached_func=cached)
        self.assertEqual(text, "第二次成功")
        self.assertEqual(cached_text, "第二次成功")
        self.assertEqual(state["n"], 2)

    def test_dashboard_uses_cache_and_not_the_blocking_model_call(self):
        with open("Dashboard.py", encoding="utf-8") as handle:
            source = handle.read()
        with open("ai_report.py", encoding="utf-8") as handle:
            helper = handle.read()
        self.assertIn("@cache_data(ttl=ttl, show_spinner=False)", helper)
        self.assertIn("bind_streamlit_cache(st.cache_data)", source)
        self.assertNotIn("generate_content", source)
        self.assertNotIn("gemini-2.5-flash'", source)
        self.assertIn('os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")', source)


if __name__ == "__main__":
    unittest.main()
