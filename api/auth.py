"""X-API-Key 比對。失敗時不把金鑰寫進日誌或回應。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

_FILTER_INSTALLED = False
_MIN_REDACT_LENGTH = 8


def expected_api_key() -> str:
    return os.environ.get("READONLY_API_KEY", "").strip()


SNAPSHOT_KEY_ENV = "QCC_SNAPSHOT_KEY"


def expected_snapshot_key() -> str:
    """寫入用金鑰，與唯讀金鑰分開。沒設定時快照端點停用（503）。"""
    return os.environ.get(SNAPSHOT_KEY_ENV, "").strip()


PUSH_KEY_ENV = "QCC_PUSH_KEY"


def expected_push_key() -> str:
    """/v1/alert-check 用（Cloud Scheduler 帶 X-Push-Key）。沒設定時端點停用（503）。"""
    return os.environ.get(PUSH_KEY_ENV, "").strip()


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
    """日誌訊息若含金鑰、權杖、私鑰或其他祕密，先塗掉再送出（規則見 redact_secrets）。"""

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


REDACTED = "[redacted]"
# 名稱看起來像祕密的環境變數，其值一律在日誌中塗掉。
_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|APIKEY|KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWD|PASS|CREDENTIAL|CREDENTIALS|"
    r"PRIVATE|AUTH|SERVICE_ACCOUNT|SA_JSON|COOKIE|DSN|WEBHOOK)(?:_|$)",
    re.IGNORECASE,
)
_ALWAYS_SECRET_ENV = (
    "READONLY_API_KEY",
    "GEMINI_API_KEY",
    "QCC_SNAPSHOT_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_CONFIG",
)
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)
_PRIVATE_KEY_FIELD = re.compile(r"""(["']?private_key(?:_id)?["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2).)*\2""", re.IGNORECASE | re.DOTALL)
_KEY_VALUE = re.compile(
    r"""(?P<name>\b[\w.-]*(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|passwd|client[_-]?secret|authorization|x-api-key|credential)s?["']?\s*[:=]\s*["']?)(?P<value>(?:bearer\s+|basic\s+)?[^\s"',;&)}\]]+)""",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_KNOWN_TOKENS = re.compile(
    r"(AIza[0-9A-Za-z_-]{30,}"
    r"|gh[pousr]_[0-9A-Za-z]{20,}"
    r"|github_pat_[0-9A-Za-z_]{20,}"
    r"|sk-[0-9A-Za-z_-]{16,}"
    r"|ya29\.[0-9A-Za-z_.-]{16,}"
    r"|xox[abprs]-[0-9A-Za-z-]{10,}"
    r"|eyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,})"
)
_BASE64ISH = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/=_-])")


def _secret_env_values() -> list[str]:
    values = set()
    for name, value in os.environ.items():
        if name in _ALWAYS_SECRET_ENV or _SECRET_ENV_NAME.search(name):
            text = (value or "").strip()
            if len(text) >= _MIN_REDACT_LENGTH:
                values.add(text)
    try:
        from alert_inputs import telegram_secret_values

        for extra in telegram_secret_values():
            if len(extra) >= _MIN_REDACT_LENGTH:
                values.add(extra)
    except Exception:
        # 遮罩失敗不能讓日誌本身炸掉，也不要在這裡再寫日誌（會重入）。
        pass
    # 長的先換，避免短值先換掉一部分而漏掉整段。
    return sorted(values, key=len, reverse=True)


def _looks_like_path(candidate: str) -> bool:
    parts = [part for part in candidate.split("/") if part]
    if len(parts) < 2:
        return False
    wordy = sum(1 for part in parts if re.fullmatch(r"[A-Za-z_-]+", part) or part.islower())
    return wordy * 2 >= len(parts)


def _is_base64ish(candidate: str) -> bool:
    core = candidate.rstrip("=")
    if len(core) < 32:
        return False
    if "/" in core and _looks_like_path(core):
        return False
    digits = sum(ch.isdigit() for ch in core)
    upper = sum(ch.isupper() for ch in core)
    lower = sum(ch.islower() for ch in core)
    # 隨機字串會混合大小寫與數字；一般識別字（snake_case、路徑、十六進位雜湊）不會。
    return digits >= 2 and upper >= 2 and lower >= 2


def redact_secrets(message) -> str:
    """把日誌字串裡的祕密塗成 [redacted]。

    * 名稱像祕密的環境變數（*_KEY、*_TOKEN、*SECRET*、*PASSWORD*…）的值
    * TELEGRAM_CONFIG／TELEGRAM_CONFIG_FILE 裡的 bot_token、chat_id
    * PEM 私鑰區塊與 JSON 的 private_key 欄位
    * key=value／key: value 形式的 api_key、token、secret、password、authorization
    * Bearer／Basic 權杖、常見金鑰格式（AIza…、ghp_…、sk-…、ya29.…、JWT）
    * 32 字以上、混合大小寫與數字的 base64 類長字串
    """
    text = "" if message is None else str(message)
    if not text:
        return text
    for secret in _secret_env_values():
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _PEM_BLOCK.sub(REDACTED, text)
    text = _PRIVATE_KEY_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _KNOWN_TOKENS.sub(REDACTED, text)

    def _kv(match: re.Match) -> str:
        value = match.group("value")
        if value.startswith("[redacted") or re.match(r"(?:bearer|basic)\s+\[redacted", value, re.IGNORECASE):
            return match.group(0)
        return f"{match.group('name')}{REDACTED}"

    text = _KEY_VALUE.sub(_kv, text)
    text = _BASE64ISH.sub(lambda m: REDACTED if _is_base64ish(m.group(0)) else m.group(0), text)
    return text


def _redact(message: str) -> str:
    return redact_secrets(message)


def install_redact_filter() -> None:
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return
    filt = RedactSecretsFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "api", "api.main"):
        logging.getLogger(name).addFilter(filt)
    _FILTER_INSTALLED = True
