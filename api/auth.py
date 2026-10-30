"""X-API-Key 比對。失敗時不把金鑰寫進日誌或回應。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

_FILTER_INSTALLED = False
_MIN_REDACT_LENGTH = 8


def expected_api_key() -> str:
    return os.environ.get("READONLY_API_KEY", "").strip()


def api_key_matches(provided: str | None, expected: str | None) -> bool:
    """以 SHA-256 摘要做常數時間比較。空的設定一律拒絕。"""
    provided_digest = hashlib.sha256((provided or "").strip().encode("utf-8")).digest()
    expected_text = (expected or "").strip()
    if not expected_text:
        hmac.compare_digest(provided_digest, hashlib.sha256(b"readonly-api-key-unset").digest())
        return False
    expected_digest = hashlib.sha256(expected_text.encode("utf-8")).digest()
    return hmac.compare_digest(provided_digest, expected_digest)


class RedactSecretsFilter(logging.Filter):
    """日誌訊息若含 API 金鑰或 Gemini 金鑰，先塗掉再送出。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = _redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                redacted_exc = _redact(record.exc_text)
                if redacted_exc != record.exc_text:
                    record.exc_text = redacted_exc
        except Exception:
            return True
        return True


def _redact(message: str) -> str:
    redacted = message
    for name in ("READONLY_API_KEY", "GEMINI_API_KEY"):
        secret = os.environ.get(name, "").strip()
        if len(secret) >= _MIN_REDACT_LENGTH and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def install_redact_filter() -> None:
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return
    filt = RedactSecretsFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "api", "api.main"):
        logging.getLogger(name).addFilter(filt)
    _FILTER_INSTALLED = True
