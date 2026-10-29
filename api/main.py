"""唯讀 FastAPI。只有 GET，不寫試算表、不推播。"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from ai_advice import RISK_MANAGER_MODEL, build_risk_manager_prompt, complete_risk_manager_prompt, page4_advice_figures
from api.advice_cache import AdviceCache, AdviceTimeout, RateLimited, call_with_timeout
from api.auth import api_key_matches, expected_api_key, install_redact_filter
from api.schemas import AdviceResponse, ErrorResponse, HealthResponse, PortfolioResponse
from portfolio_briefing import SCHEMA_VERSION, format_as_of, load_live_book, taipei_now

logger = logging.getLogger(__name__)
install_redact_filter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_cache: AdviceCache | None = None


def get_book_loader():
    return load_live_book


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


def _error_body(detail: str) -> dict:
    return ErrorResponse(schema_version=SCHEMA_VERSION, as_of=format_as_of(taipei_now()), detail=detail).model_dump()


@app.exception_handler(HTTPException)
async def _http_error(_request, exc: HTTPException):
    headers = {"Cache-Control": "no-store"}
    if exc.headers:
        headers.update(exc.headers)
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content=_error_body(detail), headers=headers)


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
        logger.warning("portfolio load failed: %s", type(exc).__name__)
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
        logger.warning("portfolio load failed: %s", type(exc).__name__)
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
        logger.warning("ai advice failed: %s", type(exc).__name__)
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
