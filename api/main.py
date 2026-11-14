"""FastAPI。GET 全部唯讀；寫入只有 POST /v1/nav-snapshot（只寫 NAV_Snapshots）與
POST /v1/alert-check（只寫 Alert_State 並推播 Telegram 即時警報），各用自己的金鑰。"""

from __future__ import annotations

import logging
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from ai_advice import RISK_MANAGER_MODEL, build_risk_manager_prompt, complete_risk_manager_prompt, page4_advice_figures
from api.advice_cache import AdviceCache, AdviceTimeout, RateLimited, call_with_timeout
from api.auth import api_key_matches, expected_api_key, expected_push_key, expected_snapshot_key, install_redact_filter, redact_secrets
from api.schemas import AdviceResponse, BearBackfillResponse, ErrorResponse, HealthResponse, NavSnapshotResponse, PortfolioResponse
from portfolio_briefing import SCHEMA_VERSION, format_as_of, load_live_book, taipei_now

logger = logging.getLogger(__name__)
install_redact_filter()
if not any(getattr(handler, "quant_api", False) for handler in logger.handlers):
    _handler = logging.StreamHandler()
    _handler.quant_api = True
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_snapshot_key_header = APIKeyHeader(name="X-Snapshot-Key", auto_error=False, scheme_name="SnapshotKey")
_push_key_header = APIKeyHeader(name="X-Push-Key", auto_error=False, scheme_name="PushKey")
_cache: AdviceCache | None = None


def get_book_loader():
    return load_live_book


def get_snapshot_writer():
    from nav_snapshot import take_snapshot

    return take_snapshot


def get_warning_backfill():
    from bear_warning import backfill_warning_history
    from portfolio_briefing import taipei_now
    from portfolio_risk import load_service_account_info, open_quant_spreadsheet

    def run():
        moment = taipei_now()
        book = open_quant_spreadsheet(load_service_account_info())
        return backfill_warning_history(moment.date(), book, written_at=moment.strftime("%Y-%m-%d %H:%M:%S"))

    return run


def get_alert_runner():
    from alert_inputs import run_alert_check

    return run_alert_check


def get_prompt_runner():
    return _default_runner


def get_advice_cache() -> AdviceCache:
    global _cache
    if _cache is None:
        _cache = AdviceCache(
            ttl_seconds=float(os.environ.get("AI_ADVICE_CACHE_TTL_SECONDS", "1800")),
            min_interval_seconds=float(os.environ.get("AI_ADVICE_MIN_INTERVAL_SECONDS", "60")),
        )
    return _cache


def reset_advice_cache() -> None:
    global _cache
    _cache = None


def advice_timeout_seconds() -> float:
    return float(os.environ.get("AI_ADVICE_TIMEOUT_SECONDS", "25"))


def _default_runner(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise AdviceNotConfigured()
    return complete_risk_manager_prompt(prompt, api_key=api_key, timeout_seconds=advice_timeout_seconds())


class AdviceNotConfigured(Exception):
    pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    install_redact_filter()
    yield


app = FastAPI(
    title="Quant Command Center Read-only API",
    version=SCHEMA_VERSION,
    lifespan=_lifespan,
    docs_url="/docs",
    redoc_url=None,
)


def require_api_key(x_api_key: str | None = Security(_api_key_header)) -> None:
    if not api_key_matches(x_api_key, expected_api_key()):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_snapshot_key(x_snapshot_key: str | None = Security(_snapshot_key_header)) -> None:
    """寫入金鑰（QCC_SNAPSHOT_KEY）。沒設定 → 503 停用；不對 → 401。唯讀金鑰不能拿來寫。"""
    expected = expected_snapshot_key()
    if not expected:
        raise HTTPException(status_code=503, detail="nav snapshot disabled")
    if not api_key_matches(x_snapshot_key, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_push_key(x_push_key: str | None = Security(_push_key_header)) -> None:
    """推播金鑰（QCC_PUSH_KEY）。沒設定 → 503；不對 → 401。其他金鑰都不能用。"""
    expected = expected_push_key()
    if not expected:
        raise HTTPException(status_code=503, detail="alert check disabled")
    if not api_key_matches(x_push_key, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _error_body(detail: str) -> dict:
    return ErrorResponse(schema_version=SCHEMA_VERSION, as_of=format_as_of(taipei_now()), detail=detail).model_dump()


@app.exception_handler(HTTPException)
async def _http_error(_request, exc: HTTPException):
    headers = {"Cache-Control": "no-store"}
    if exc.headers:
        headers.update(exc.headers)
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content=_error_body(detail), headers=headers)


TRACEBACK_FRAMES = 5
TRACEBACK_MAX_CHARS = 2000
MESSAGE_MAX_CHARS = 500


def short_traceback(exc: BaseException, frames: int = TRACEBACK_FRAMES, max_chars: int = TRACEBACK_MAX_CHARS) -> str:
    """最後幾層堆疊（含 raise from 的原因），已塗掉祕密並截斷長度。"""
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=-abs(int(frames)))
    text = redact_secrets("".join(lines).rstrip())
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text


def _log_load_failure(route: str, exc: BaseException) -> None:
    """記下例外型別、訊息與簡短堆疊，祕密先塗掉。回應本身仍只說 portfolio unavailable。"""
    message = redact_secrets(str(exc))
    if len(message) > MESSAGE_MAX_CHARS:
        message = message[:MESSAGE_MAX_CHARS] + "…"
    logger.warning(
        "portfolio load failed route=%s type=%s message=%s\n%s",
        route,
        type(exc).__name__,
        message or "(empty)",
        short_traceback(exc),
    )


def _no_store(response_model, payload):
    body = response_model.model_validate(payload).model_dump()
    return JSONResponse(status_code=200, content=body, headers={"Cache-Control": "no-store"})


@app.get("/v1/health", response_model=HealthResponse)
def health():
    body = HealthResponse(schema_version=SCHEMA_VERSION, as_of=format_as_of(taipei_now()), status="ok")
    return JSONResponse(status_code=200, content=body.model_dump(), headers={"Cache-Control": "no-store"})


@app.get("/v1/portfolio", response_model=PortfolioResponse)
def portfolio(_: None = Depends(require_api_key), loader=Depends(get_book_loader)):
    try:
        book = loader()
    except Exception as exc:
        _log_load_failure("/v1/portfolio", exc)
        raise HTTPException(status_code=502, detail="portfolio unavailable") from exc
    return _no_store(PortfolioResponse, book.payload)


@app.get("/v1/ai-advice", response_model=AdviceResponse)
def ai_advice(
    _: None = Depends(require_api_key),
    loader=Depends(get_book_loader),
    runner=Depends(get_prompt_runner),
    cache: AdviceCache = Depends(get_advice_cache),
):
    try:
        cached = cache.lookup()
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail="rate limited", headers={"Retry-After": str(exc.retry_after)}) from exc
    if cached is not None:
        return _advice_response(cached.text, cached.generated_at, cached.cached)

    try:
        book = loader()
    except Exception as exc:
        _log_load_failure("/v1/ai-advice", exc)
        raise HTTPException(status_code=502, detail="portfolio unavailable") from exc

    nav, leverage, beta, cash_pct = page4_advice_figures(book.metrics)
    prompt = build_risk_manager_prompt(book.positions, nav, leverage, beta, cash_pct)

    def produce() -> str:
        return call_with_timeout(lambda: runner(prompt), advice_timeout_seconds())

    try:
        result = cache.generate(produce, taipei_now())
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail="rate limited", headers={"Retry-After": str(exc.retry_after)}) from exc
    except AdviceTimeout as exc:
        raise HTTPException(status_code=504, detail="ai advice timed out") from exc
    except AdviceNotConfigured as exc:
        raise HTTPException(status_code=503, detail="ai advice is not configured") from exc
    except Exception as exc:
        logger.exception("ai advice failed")
        raise HTTPException(status_code=502, detail="ai advice unavailable") from exc
    return _advice_response(result.text, result.generated_at, result.cached)


def _advice_response(text: str, generated_at, cached: bool):
    body = AdviceResponse(
        schema_version=SCHEMA_VERSION,
        as_of=format_as_of(taipei_now()),
        text=text,
        model=RISK_MANAGER_MODEL,
        cached=cached,
        generated_at=format_as_of(generated_at),
    )
    return _no_store(AdviceResponse, body.model_dump())


@app.post("/v1/nav-snapshot", response_model=NavSnapshotResponse)
def nav_snapshot(_: None = Depends(require_snapshot_key), writer=Depends(get_snapshot_writer)):
    """用第四頁同一套估價算出今天（台北）的淨值，upsert 到 NAV_Snapshots。同一天重複呼叫只覆蓋那一列。"""
    try:
        result = writer()
    except Exception as exc:
        _log_load_failure("/v1/nav-snapshot", exc)
        raise HTTPException(status_code=502, detail="snapshot failed") from exc
    payload = dict(result)
    payload.update(schema_version=SCHEMA_VERSION, as_of=format_as_of(taipei_now()), status="written")
    return _no_store(NavSnapshotResponse, payload)


@app.post("/v1/bear-warning-backfill", response_model=BearBackfillResponse)
def bear_warning_backfill(_: None = Depends(require_snapshot_key), runner=Depends(get_warning_backfill)):
    """補 Bear_Warning_Daily 約 60 個交易日。同一天重跑只填空白欄。不寫 NAV_Snapshots 或 Daily_NAV。"""
    try:
        result = runner()
    except Exception as exc:
        _log_load_failure("/v1/bear-warning-backfill", exc)
        raise HTTPException(status_code=502, detail="bear warning backfill failed") from exc
    status = str((result or {}).get("status") or "skipped")
    if status not in {"written", "kept", "skipped", "failed"}:
        status = "written"
    body = BearBackfillResponse(
        schema_version=SCHEMA_VERSION,
        as_of=format_as_of(taipei_now()),
        status=status,
        sheet=str((result or {}).get("sheet") or "Bear_Warning_Daily"),
        rows=int((result or {}).get("rows") or 0),
        detail=str((result or {}).get("detail") or ""),
    )
    return _no_store(BearBackfillResponse, body.model_dump())


@app.post("/v1/alert-check")
def alert_check(_: None = Depends(require_push_key), runner=Depends(get_alert_runner)):
    """Cloud Scheduler 每 15 分鐘呼叫：只在狀態改變時推播 Telegram，狀態存 Alert_State。"""
    from alert_inputs import TelegramNotConfigured

    try:
        summary = runner()
    except TelegramNotConfigured as exc:
        raise HTTPException(status_code=503, detail="telegram not configured") from exc
    except Exception as exc:
        _log_load_failure("/v1/alert-check", exc)
        raise HTTPException(status_code=502, detail="alert check failed") from exc
    body = dict(summary)
    body.update(schema_version=SCHEMA_VERSION, as_of=format_as_of(taipei_now()), status="checked")
    return JSONResponse(status_code=200, content=body, headers={"Cache-Control": "no-store"})
