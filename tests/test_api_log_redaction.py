"""API 載入失敗日誌：例外型別、訊息、簡短堆疊都要記，但祕密一律塗掉，回應不洩漏細節。"""

import io
import logging
import os
import unittest

from fastapi.testclient import TestClient

from api.auth import REDACTED, redact_secrets
from api.main import app, get_book_loader, reset_advice_cache, short_traceback

API_KEY = "test-key-not-a-secret"
PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n"
    "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu\n"
    "-----END PRIVATE KEY-----\n"
)


class RedactSecretsTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_env_values_of_secret_like_names(self):
        os.environ["QCC_TEST_SERVICE_TOKEN"] = "plain-token-value-1"
        os.environ["SOME_DB_PASSWORD"] = "hunter2-hunter2"
        os.environ["MY_API_KEY"] = "abc12345xyz"
        os.environ["GEMINI_API_KEY"] = "gemini-value-to-hide"
        text = redact_secrets("a plain-token-value-1 b hunter2-hunter2 c abc12345xyz d gemini-value-to-hide")
        for secret in ("plain-token-value-1", "hunter2-hunter2", "abc12345xyz", "gemini-value-to-hide"):
            self.assertNotIn(secret, text)
        self.assertEqual(text.count(REDACTED), 4)

    def test_non_secret_env_names_are_kept(self):
        os.environ["QCC_SHEET_NAME"] = "Quant_Database"
        os.environ["PWD"] = "/workspace/qcc"
        text = redact_secrets("open Quant_Database in /workspace/qcc")
        self.assertEqual(text, "open Quant_Database in /workspace/qcc")

    def test_short_env_values_are_ignored(self):
        os.environ["TINY_TOKEN"] = "abc"
        self.assertEqual(redact_secrets("abc def"), "abc def")

    def test_pem_private_key_block(self):
        text = redact_secrets(f"bad creds:\n{PEM}after")
        self.assertNotIn("MIIEvQIBADANBg", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertIn(REDACTED, text)
        self.assertIn("after", text)

    def test_truncated_pem_is_redacted_to_end(self):
        text = redact_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA")
        self.assertNotIn("MIIEow", text)

    def test_private_key_json_field(self):
        escaped = PEM.replace("\n", "\\n")
        blob = '{"type": "service_account", "private_key_id": "0123abcd", "private_key": "' + escaped + '", "client_email": "bot@x.iam.gserviceaccount.com"}'
        text = redact_secrets(blob)
        self.assertNotIn("MIIEvQ", text)
        self.assertNotIn("0123abcd", text)
        self.assertIn("bot@x.iam.gserviceaccount.com", text)

    def test_key_value_pairs(self):
        cases = [
            "api_key=sk_live_value_123",
            "token: abcdefg",
            "access_token=xyz987",
            "client_secret='s3cr3t-value'",
            '"password": "p@ss"',
            "X-API-Key: some-header-value",
            "https://example.com/v1?key=1&apikey=QQQQ1111&x=1",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                out = redact_secrets(raw)
                self.assertIn(REDACTED, out)
        self.assertNotIn("QQQQ1111", redact_secrets(cases[-1]))
        self.assertIn("x=1", redact_secrets(cases[-1]))

    def test_bearer_and_known_token_formats(self):
        samples = {
            "Authorization: Bearer abcDEF123456789xyz": "abcDEF123456789xyz",
            "key AIzaSyA1234567890abcdefghijklmnopqrstuv": "AIzaSyA1234567890",
            "gh ghp_abcdefghijklmnopqrstuvwxyz0123456789": "ghp_abcdef",
            "pat github_pat_11ABCDEFG0123456789_abcdefghijk": "github_pat_11",
            "oauth ya29.a0AfH6SMBx-abcdefghijklmnop": "ya29.a0AfH6",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U": "eyJhbGciOi",
        }
        for raw, secret in samples.items():
            with self.subTest(raw=raw):
                self.assertNotIn(secret, redact_secrets(raw))

    def test_long_base64ish_strings(self):
        blob = "QWxhZGRpbjpvcGVuIHNlc2FtZQ9876543210abcdXYZ+/=="
        out = redact_secrets(f"blob {blob} end")
        self.assertNotIn("QWxhZGRpbjpv", out)
        self.assertTrue(out.startswith("blob ") and out.endswith(" end"))

    def test_ordinary_text_is_kept(self):
        keep = [
            'File "/workspace/qcc-venv/lib/python3.13/site-packages/gspread/client.py", line 12, in open',
            "commit 4de5d17e93a74c0aa1234567890abcdef1234567",
            "KeyError: 'Active_Positions_worksheet_not_found_in_spreadsheet'",
            "gspread.exceptions.WorksheetNotFound: Futures_Ledger",
            "原始保證金 35,050",
        ]
        for raw in keep:
            with self.subTest(raw=raw):
                self.assertEqual(redact_secrets(raw), raw)

    def test_none_and_empty(self):
        self.assertEqual(redact_secrets(None), "")
        self.assertEqual(redact_secrets(""), "")

    def test_short_traceback_is_redacted_and_bounded(self):
        os.environ["QCC_TEST_SECRET"] = "trace-secret-value"

        def deep(n):
            if n == 0:
                raise ValueError("failed with trace-secret-value and token=abc123")
            deep(n - 1)

        try:
            deep(20)
        except ValueError as exc:
            text = short_traceback(exc, frames=3)
        self.assertIn("ValueError", text)
        self.assertNotIn("trace-secret-value", text)
        self.assertNotIn("abc123", text)
        self.assertLessEqual(text.count('File "'), 3)
        try:
            raise RuntimeError("x" * 5000)
        except RuntimeError as exc:
            self.assertLessEqual(len(short_traceback(exc, max_chars=300)), 301)


class PortfolioFailureLoggingTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        os.environ["READONLY_API_KEY"] = API_KEY
        os.environ["GCP_SERVICE_ACCOUNT_JSON"] = '{"private_key": "sa-json-secret-blob-value"}'
        os.environ["AI_ADVICE_MIN_INTERVAL_SECONDS"] = "0"
        reset_advice_cache()
        app.dependency_overrides.clear()
        self._client = TestClient(app)
        self.client = self._client.__enter__()
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        logging.getLogger("api.main").addHandler(self.handler)

    def tearDown(self):
        logging.getLogger("api.main").removeHandler(self.handler)
        app.dependency_overrides.clear()
        reset_advice_cache()
        self._client.__exit__(None, None, None)
        os.environ.clear()
        os.environ.update(self._saved)

    def _explode(self):
        def loader():
            raise PermissionError(
                "sheet denied for key=AIzaSyA1234567890abcdefghijklmnopqrstuv "
                f"{API_KEY} {os.environ['GCP_SERVICE_ACCOUNT_JSON']} {PEM} SENTINEL-ROW-7"
            )

        app.dependency_overrides[get_book_loader] = lambda: loader

    def _assert_logged_and_hidden(self, response):
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "portfolio unavailable")
        self.assertNotIn("SENTINEL-ROW-7", response.text)
        self.assertNotIn("PermissionError", response.text)
        written = self.stream.getvalue()
        self.assertIn("portfolio load failed", written)
        self.assertIn("PermissionError", written)
        self.assertIn("sheet denied", written)
        self.assertIn("SENTINEL-ROW-7", written)
        self.assertIn("Traceback", written)
        self.assertIn("in loader", written)
        for secret in (API_KEY, "AIzaSyA1234567890", "sa-json-secret-blob-value", "MIIEvQIBADANBg"):
            self.assertNotIn(secret, written)
        self.assertIn(REDACTED, written)

    def test_portfolio_failure_logs_type_message_traceback(self):
        self._explode()
        response = self.client.get("/v1/portfolio", headers={"X-API-Key": API_KEY})
        self._assert_logged_and_hidden(response)
        self.assertIn("route=/v1/portfolio", self.stream.getvalue())

    def test_ai_advice_portfolio_failure_logs_type_message_traceback(self):
        self._explode()
        response = self.client.get("/v1/ai-advice", headers={"X-API-Key": API_KEY})
        self._assert_logged_and_hidden(response)
        self.assertIn("route=/v1/ai-advice", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
