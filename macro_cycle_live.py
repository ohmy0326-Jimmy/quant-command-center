"""讀 Quant_Database 與自動行情。只在第五頁呼叫；第六頁只用記憶體裡的結果。

不會建立或寫入試算表分頁。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from macro_cycle import load_macro_cycle
from macro_cycle_sheet import (
    AUTO_CACHE_SECONDS,
    HISTORY_HEADERS,
    HISTORY_TAB,
    INDUSTRY_CYCLE_HEADERS,
    INDUSTRY_CYCLE_TAB,
    MACRO_SCORE_HEADERS,
    MACRO_SCORE_TAB,
    QUANT_SPREADSHEET_ID,
    SHEET_CACHE_SECONDS,
    compose_macro_view,
    headers_missing,
    parse_fred_csv,
    score_auto_from_markets,
)

_MEM = {"view": None, "at": 0.0}
_FETCH_TIMEOUT = 8


def remember_live_view(view: dict, now: float | None = None) -> None:
    _MEM["view"] = view
    _MEM["at"] = time.time() if now is None else float(now)


def clear_live_view() -> None:
    _MEM["view"] = None
    _MEM["at"] = 0.0


def peek_live_view(max_age: float = SHEET_CACHE_SECONDS, now: float | None = None):
    if not _MEM["view"]:
        return None
    current = time.time() if now is None else float(now)
    if current - _MEM["at"] > max_age:
        return None
    return _MEM["view"]


def macro_view_for_orders(now: float | None = None) -> dict:
    """第六頁用。有 15 分鐘內的結果就用，否則只讀倉庫 JSON，不打網路。"""
    found = peek_live_view(now=now)
    if found is not None:
        return found
    return load_macro_cycle()


def read_macro_tabs(spreadsheet) -> dict:
    """讀兩個分頁。分頁不存在或表頭不符時，該側帶錯誤，不建立分頁。"""
    scores, score_error = _read_tab(spreadsheet, MACRO_SCORE_TAB, MACRO_SCORE_HEADERS)
    industries, industry_error = _read_tab(spreadsheet, INDUSTRY_CYCLE_TAB, INDUSTRY_CYCLE_HEADERS)
    history, history_error = _read_tab(spreadsheet, HISTORY_TAB, HISTORY_HEADERS)
    if score_error and industry_error:
        return {
            "ok": False,
            "error": f"{score_error}；{industry_error}",
            "scores": [],
            "industries": [],
            "history": [],
            "score_error": score_error,
            "industry_error": industry_error,
            "history_error": history_error,
        }
    return {
        "ok": True,
        "error": "",
        "scores": scores,
        "industries": industries,
        "history": history,
        "score_error": score_error,
        "industry_error": industry_error,
        "history_error": history_error,
    }


def read_quant_macro_tabs(key_data: dict, spreadsheet_id: str = QUANT_SPREADSHEET_ID) -> dict:
    import socket

    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_FETCH_TIMEOUT)
    try:
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_id)
        return read_macro_tabs(spreadsheet)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"試算表讀不到（{exc.__class__.__name__}）",
            "scores": [],
            "industries": [],
            "history": [],
            "score_error": "",
            "industry_error": "",
            "history_error": "",
        }
    finally:
        socket.setdefaulttimeout(previous)


def fetch_auto_results(today: str | None = None, fetchers: dict | None = None) -> dict:
    """平行抓 FRED 與 Yahoo。單項逾時就當作這項失敗。"""
    from datetime import date

    today = today or date.today().isoformat()
    jobs = fetchers or _default_fetchers()
    markets = {key: None for key in jobs}
    pool = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
    future_map = {pool.submit(fn): key for key, fn in jobs.items()}
    try:
        for future in as_completed(future_map, timeout=_FETCH_TIMEOUT + 2):
            key = future_map[future]
            try:
                markets[key] = future.result()
            except Exception:
                markets[key] = None
    except TimeoutError:
        for future, key in future_map.items():
            if not future.done():
                future.cancel()
                markets[key] = None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    cleaned = {
        "dfii10": markets.get("dfii10") or [],
        "dgs10": markets.get("dgs10") or [],
        "hy": markets.get("hy") or [],
        "vix": markets.get("vix"),
        "spx": markets.get("spx") or [],
        "twii": markets.get("twii") or [],
        "sox": markets.get("sox") or [],
    }
    return score_auto_from_markets(cleaned, today)


def load_service_key():
    from app_secrets import read_secret
    from portfolio_risk import load_service_account_info

    def fallback():
        data = read_secret(None, "google_key")
        if not isinstance(data, dict) and not hasattr(data, "items"):
            raise RuntimeError("找不到 Google 服務帳戶金鑰")
        return dict(data)

    return load_service_account_info(fallback)


def _read_tab(spreadsheet, title: str, required: tuple[str, ...]):
    try:
        worksheet = spreadsheet.worksheet(title)
        headers = [str(item).strip() for item in worksheet.row_values(1)]
    except Exception:
        return [], f"讀不到分頁 {title}"
    missing = headers_missing(headers, required)
    if missing:
        return [], f"{title} 缺少欄位：" + ", ".join(missing)
    try:
        return worksheet.get_all_records(), None
    except Exception:
        return [], f"{title} 內容讀不到"


def _default_fetchers() -> dict:
    return {
        "dfii10": lambda: _fred("DFII10"),
        "dgs10": lambda: _fred("DGS10"),
        "hy": lambda: _fred("BAMLH0A0HYM2"),
        "vix": lambda: _last_close("^VIX"),
        "spx": lambda: _closes("^GSPC"),
        "twii": lambda: _closes("^TWII"),
        "sox": lambda: _closes("^SOX"),
    }


def _fred(series_id: str) -> list[float]:
    import requests

    response = requests.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series_id},
        headers={"User-Agent": "quant-command-center/1.0"},
        timeout=_FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return parse_fred_csv(response.text)


def _closes(symbol: str) -> list[float]:
    import yfinance as yf

    frame = yf.Ticker(symbol).history(period="2y", auto_adjust=True)
    if frame is None or getattr(frame, "empty", True) or "Close" not in getattr(frame, "columns", []):
        return []
    return [float(item) for item in frame["Close"].dropna().tolist()]


def _last_close(symbol: str):
    closes = _closes(symbol)
    return closes[-1] if closes else None


# 給文件與測試對照快取秒數，避免魔術數字散落。
__all__ = [
    "AUTO_CACHE_SECONDS",
    "SHEET_CACHE_SECONDS",
    "clear_live_view",
    "fetch_auto_results",
    "macro_view_for_orders",
    "peek_live_view",
    "read_macro_tabs",
    "read_quant_macro_tabs",
    "remember_live_view",
]
