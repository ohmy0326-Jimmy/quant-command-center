"""Gemini 報告呼叫：串流、硬逾時、暫時性錯誤重試一次，並把成功結果交給 st.cache_data。"""

import contextvars
import logging
import os
import queue
import textwrap
import threading
import time

logger = logging.getLogger("trading_dashboard.ai")

DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_CACHE_TTL_SEC = 1800
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_MAX_PROMPT_CHARS = 4500
DEFAULT_HOLDINGS_CHARS = 180
DEFAULT_POSITION_ROWS = 25

_TRANSIENT_CODES = {408, 409, 429, 500, 502, 503, 504}
_PRECOMPUTED = contextvars.ContextVar("ai_report_precomputed", default=None)
_AI_API_KEY = contextvars.ContextVar("ai_report_api_key", default=None)


class AiReportError(Exception):
    """可顯示給使用者的 AI 呼叫錯誤。"""


class AiTimeoutError(AiReportError):
    """硬逾時。不再自動重試，避免把等待時間再拉長一倍。"""


class AiEmptyResponseError(AiReportError):
    """模型有回應但沒有可顯示的文字。"""

    def __init__(self, detail, retryable=True):
        super().__init__(detail)
        self.retryable = retryable


def _env_int(name, default, minimum, maximum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("環境變數 %s=%r 不是整數，改用 %s", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def gemini_model_name():
    """預設用較快、預設不思考的 Flash-Lite。可用 GEMINI_MODEL 換回其他模型。"""
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def gemini_timeout_sec():
    return _env_int("GEMINI_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC, 5, 180)


def gemini_cache_ttl_sec():
    return _env_int("GEMINI_CACHE_TTL_SEC", DEFAULT_CACHE_TTL_SEC, 60, 86400)


def gemini_max_output_tokens():
    return _env_int("GEMINI_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, 128, 4096)


def gemini_max_prompt_chars():
    return _env_int("GEMINI_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS, 500, 20000)


def trim_prompt(text, max_chars=None):
    """去掉縮排空白，並在超過上限時截斷。重複呼叫結果不變。"""
    limit = gemini_max_prompt_chars() if max_chars is None else int(max_chars)
    cleaned = textwrap.dedent(str(text or "")).strip()
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    note = "\n…（提示過長，已截斷）"
    keep = max(0, limit - len(note))
    return cleaned[:keep].rstrip() + note


def format_sector_lines(rows, max_holdings_chars=DEFAULT_HOLDINGS_CHARS):
    """一檔一行，避免 pandas.to_string 依最寬的持股欄位補空白。"""
    lines = []
    for row in rows or []:
        holdings = " ".join(str(row.get("holdings") or "").split())
        if len(holdings) > max_holdings_chars:
            holdings = holdings[:max_holdings_chars].rstrip() + "…"
        daily = float(row.get("daily") or 0)
        monthly = float(row.get("monthly") or 0)
        lines.append(
            f"{row.get('ticker', '')} {row.get('name', '')} "
            f"日{daily:+.2f}% 月{monthly:+.2f}% 持股:{holdings}"
        )
    return "\n".join(lines)


def format_position_lines(rows, limit=DEFAULT_POSITION_ROWS):
    """只留風險權重絕對值最高的幾檔，數字四捨五入，縮短名稱。"""
    prepared = list(rows or [])
    if not prepared:
        return "無持倉"

    def weight_of(row):
        try:
            return abs(float(row.get("weight") or 0))
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(prepared, key=weight_of, reverse=True)
    shown = ranked[: max(1, int(limit))]
    omitted = len(ranked) - len(shown)
    lines = []
    for row in shown:
        name = str(row.get("name") or "")
        if len(name) > 24:
            name = name[:24] + "…"
        bits = [str(row.get("ticker") or ""), name, str(row.get("sector") or "")]
        beta = _optional_float(row.get("beta"))
        weight = _optional_float(row.get("weight"))
        notional = _optional_float(row.get("notional"))
        pnl = _optional_float(row.get("pnl"))
        leverage = _optional_float(row.get("leverage"))
        ret = _optional_float(row.get("ret"))
        if beta is not None:
            bits.append(f"Beta {beta:.2f}")
        if weight is not None:
            bits.append(f"權重 {weight:.2f}%")
        if notional is not None:
            bits.append(f"名目 {notional:,.0f}")
        if pnl is not None:
            bits.append(f"損益 {pnl:,.0f}")
        if leverage is not None:
            bits.append(f"槓桿 {leverage:.1f}x")
        if ret is not None:
            bits.append(f"報酬 {ret:+.2f}%")
        lines.append(" | ".join(bit for bit in bits if bit))
    if omitted > 0:
        lines.append(f"（其餘 {omitted} 檔風險權重較低，已省略）")
    return "\n".join(lines)


def _optional_float(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def is_transient_error(exc):
    """429、5xx、連線中斷可重試。硬逾時與金鑰／參數錯誤不重試。"""
    if isinstance(exc, AiTimeoutError):
        return False
    if isinstance(exc, AiEmptyResponseError):
        return bool(getattr(exc, "retryable", False))
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, AiReportError):
        return True
    for attr in ("code", "status_code"):
        code = getattr(exc, attr, None)
        if code in _TRANSIENT_CODES:
            return True
    text = f"{type(exc).__name__} {exc}".lower()
    if any(token in text for token in ("401", "403", "api key", "permission", "invalid api")):
        return False
    if "400" in text or "invalid_argument" in text:
        return False
    if any(str(code) in text for code in _TRANSIENT_CODES):
        return True
    markers = (
        "unavailable",
        "overloaded",
        "resource_exhausted",
        "resource exhausted",
        "temporarily",
        "try again",
        "timeout",
        "timed out",
        "connection",
        "reset",
        "broken pipe",
    )
    return any(marker in text for marker in markers)


def _is_thinking_config_error(exc):
    text = f"{type(exc).__name__} {exc}".lower()
    if "thinking" not in text:
        return False
    return any(token in text for token in ("budget", "level", "invalid", "unsupported", "not supported", "400"))


def _is_blocked_reason(detail):
    text = str(detail or "").upper()
    return any(token in text for token in ("SAFETY", "BLOCK", "PROHIBIT", "RECITATION", "SPII"))


def format_ai_error(exc, timeout_sec=None):
    """繁體中文錯誤，並附上實際例外內容。"""
    seconds = gemini_timeout_sec() if timeout_sec is None else int(timeout_sec)
    reason = str(exc).strip() or type(exc).__name__
    lowered = reason.lower()
    timed_out = isinstance(exc, AiTimeoutError) or type(exc).__name__ in {
        "TimeoutError",
        "APITimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }
    if timed_out or "timeout" in lowered or "timed out" in lowered or "逾時" in reason:
        return f"AI 分析逾時（超過 {seconds} 秒仍未完成），已停止等待，沒有寫入報告。原因：{reason}"
    return f"AI 分析失敗，沒有產生報告。原因：{reason}"


def _thinking_config(model, types_module):
    name = (model or "").lower()
    if any(tag in name for tag in ("gemini-3", "gemini-4", "gemini-5")):
        return types_module.ThinkingConfig(thinking_level=types_module.ThinkingLevel.MINIMAL)
    if "2.5" in name and "flash" in name:
        return types_module.ThinkingConfig(thinking_budget=0)
    return None


def _generation_config(model, limit_thinking):
    from google.genai import types

    kwargs = {
        "temperature": 0.3,
        "max_output_tokens": gemini_max_output_tokens(),
    }
    if limit_thinking:
        thinking = _thinking_config(model, types)
        if thinking is not None:
            kwargs["thinking_config"] = thinking
    return types.GenerateContentConfig(**kwargs)


def _make_client(api_key, timeout_sec):
    from google import genai
    from google.genai import types

    timeout_ms = max(1000, int(float(timeout_sec) * 1000))
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _chunk_text(chunk):
    try:
        value = chunk.text
    except Exception:
        value = None
    if isinstance(value, str) and value:
        return value
    return ""


def _finish_detail(chunk):
    if chunk is None:
        return ""
    try:
        bits = []
        candidates = getattr(chunk, "candidates", None) or []
        if candidates:
            reason = getattr(candidates[0], "finish_reason", None)
            if reason:
                bits.append(str(reason))
        feedback = getattr(chunk, "prompt_feedback", None)
        if feedback is not None:
            block = getattr(feedback, "block_reason", None)
            bits.append(str(block or feedback))
        return " ".join(bit for bit in bits if bit and bit not in {"None", "FINISH_REASON_UNSPECIFIED"})
    except Exception as exc:
        return str(exc)


def _generate_once(api_key, model, prompt, timeout_sec, on_chunk, limit_thinking):
    client = _make_client(api_key, timeout_sec)
    config = _generation_config(model, limit_thinking)
    events = queue.Queue()

    def worker():
        try:
            stream_fn = getattr(client.models, "generate_content_stream", None)
            if not callable(stream_fn):
                response = client.models.generate_content(model=model, contents=prompt, config=config)
                piece = _chunk_text(response)
                if piece:
                    events.put(("chunk", piece))
                events.put(("done", _finish_detail(response)))
                return
            last_detail = ""
            for chunk in stream_fn(model=model, contents=prompt, config=config):
                detail = _finish_detail(chunk)
                if detail:
                    last_detail = detail
                piece = _chunk_text(chunk)
                if piece:
                    events.put(("chunk", piece))
            events.put(("done", last_detail))
        except Exception as exc:
            events.put(("error", exc))

    threading.Thread(target=worker, name="gemini-report", daemon=True).start()
    deadline = time.monotonic() + float(timeout_sec)
    parts = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AiTimeoutError(f"模型 {model} 在 {max(1, int(timeout_sec))} 秒內沒有完成")
        try:
            kind, payload = events.get(timeout=min(0.25, remaining))
        except queue.Empty:
            continue
        if kind == "chunk":
            parts.append(payload)
            if on_chunk:
                on_chunk("".join(parts))
        elif kind == "done":
            text = "".join(parts).strip()
            if not text:
                detail = str(payload or "").strip()
                message = "模型沒有回傳可顯示的文字"
                if detail:
                    message += f"（{detail}）"
                raise AiEmptyResponseError(message, retryable=not _is_blocked_reason(detail))
            return text
        else:
            raise payload


def generate_ai_text(api_key, prompt, *, model=None, timeout_sec=None, on_chunk=None):
    """串流收集文字。暫時性錯誤在剩餘時間內重試一次。硬逾時直接失敗。"""
    if not str(api_key or "").strip():
        raise AiReportError("未設定 GEMINI_API_KEY（環境變數或 Streamlit secrets）")
    prompt = trim_prompt(prompt)
    if not prompt:
        raise AiReportError("分析提示是空的")
    model = (model or gemini_model_name()).strip() or DEFAULT_MODEL
    budget = float(gemini_timeout_sec() if timeout_sec is None else timeout_sec)
    deadline = time.monotonic() + budget
    last_error = None
    for attempt in (1, 2):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (attempt > 1 and remaining < 1):
            break
        # 第一次用完整預算，避免函式進入時剩下 0.999 秒就被當成來不及呼叫。
        attempt_timeout = budget if attempt == 1 else remaining
        try:
            return _generate_once(api_key, model, prompt, attempt_timeout, on_chunk, True)
        except AiTimeoutError as exc:
            last_error = exc
            break
        except Exception as exc:
            if _is_thinking_config_error(exc):
                if on_chunk:
                    try:
                        on_chunk("")
                    except Exception:
                        logger.exception("清除已串流的片段失敗")
                try:
                    fallback_timeout = max(1.0, deadline - time.monotonic())
                    return _generate_once(api_key, model, prompt, fallback_timeout, on_chunk, False)
                except AiTimeoutError as timeout_exc:
                    last_error = timeout_exc
                    break
                except Exception as fallback_exc:
                    exc = fallback_exc
            last_error = exc
            if attempt == 1 and is_transient_error(exc) and (deadline - time.monotonic()) >= 1:
                logger.warning("AI 呼叫暫時失敗，重試一次：%s", exc)
                if on_chunk:
                    try:
                        on_chunk("")
                    except Exception:
                        logger.exception("清除已串流的片段失敗")
                continue
            break
    if last_error is None:
        last_error = AiTimeoutError(f"模型 {model} 在 {max(1, int(budget))} 秒內沒有完成")
    raise last_error


def lookup_cached_ai_report(cached_func, model, prompt, timeout_sec):
    """讀取 st.cache_data 既有結果。沒有、過期或介面不同時回傳 None，不觸發重新計算。"""
    try:
        from streamlit.runtime.caching.cache_errors import CacheKeyNotFoundError
        from streamlit.runtime.caching.cache_utils import _make_value_key
    except Exception:
        return None
    try:
        info = cached_func._info
        cache = info.get_function_cache(cached_func._function_key)
        value_key = _make_value_key(
            cache_type=info.cache_type,
            func=info.func,
            func_args=(model, prompt, int(timeout_sec)),
            func_kwargs={},
            hash_funcs=info.hash_funcs,
        )
        entry = cache.read_result(value_key)
    except CacheKeyNotFoundError:
        return None
    except Exception:
        logger.exception("讀取 AI 報告快取失敗，改為重新產生")
        return None
    value = getattr(entry, "value", entry)
    if isinstance(value, str) and value.strip():
        return value
    return None


def bind_streamlit_cache(cache_data):
    """用呼叫端傳入的 st.cache_data 快取成功報告。鍵是模型、提示與逾時秒數。"""
    ttl = gemini_cache_ttl_sec()

    @cache_data(ttl=ttl, show_spinner=False)
    def cached_ai_report(model: str, prompt: str, timeout_sec: int) -> str:
        precomputed = _PRECOMPUTED.get()
        if precomputed is not None and precomputed[0] == (model, prompt, int(timeout_sec)):
            return precomputed[1]
        return generate_ai_text(
            _AI_API_KEY.get() or "",
            prompt,
            model=model,
            timeout_sec=int(timeout_sec),
        )

    return cached_ai_report


def run_ai_report(api_key, prompt, *, on_chunk=None, cached_func=None):
    """先查快取；沒有才串流產生，成功後寫入 st.cache_data。失敗不寫入快取。"""
    model = gemini_model_name()
    timeout_sec = int(gemini_timeout_sec())
    prompt = trim_prompt(prompt)
    if cached_func is not None:
        cached = lookup_cached_ai_report(cached_func, model, prompt, timeout_sec)
        if cached:
            if on_chunk:
                on_chunk(cached)
            return cached
    text = generate_ai_text(api_key, prompt, model=model, timeout_sec=timeout_sec, on_chunk=on_chunk)
    if cached_func is None:
        return text
    pre_token = _PRECOMPUTED.set(((model, prompt, timeout_sec), text))
    key_token = _AI_API_KEY.set(api_key)
    try:
        stored = cached_func(model, prompt, timeout_sec)
    except Exception:
        logger.exception("AI 報告已產生，但寫入快取失敗")
        return text
    finally:
        _PRECOMPUTED.reset(pre_token)
        _AI_API_KEY.reset(key_token)
    if isinstance(stored, str) and stored.strip():
        if stored != text and on_chunk:
            on_chunk(stored)
        return stored
    return text
