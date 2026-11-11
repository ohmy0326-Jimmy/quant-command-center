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
    score_auto_from_markets,
)

_MEM = {"view": None, "at": 0.0}
_FETCH_TIMEOUT = 8
_POOL_TIMEOUT = 18


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
    jobs = fetchers or {**_default_fetchers(), **_proxy_fetchers()}
    markets = {key: None for key in jobs}
    pool = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
    future_map = {pool.submit(fn): key for key, fn in jobs.items()}
    try:
        for future in as_completed(future_map, timeout=_POOL_TIMEOUT):
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
    dfii_raw = markets.get("dfii10") or []
    dfii_values = [item[1] for item in dfii_raw] if dfii_raw and isinstance(dfii_raw[0], tuple) else list(dfii_raw or [])
    cleaned = {
        "dfii10": dfii_values,
        "dgs10": markets.get("dgs10") or [],
        "hy": markets.get("hy") or [],
        "vix": markets.get("vix"),
        "spx": markets.get("spx") or [],
        "twii": markets.get("twii") or [],
        "sox": markets.get("sox") or [],
    }
    from macro_proxies import proxies_from_payloads

    found = score_auto_from_markets(cleaned, today)
    found.update(
        proxies_from_payloads(
            {
                "pmi": markets.get("pmi_bundle") or {},
                "ndc_scores": markets.get("ndc_scores") or [],
                "export": markets.get("export_yoy") or {},
                "pe": markets.get("spx_pe") or {},
                "dfii10": dfii_raw,
                "closes": markets.get("sector_closes") or {},
            },
            today,
        )
    )
    return found


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
        "dfii10": lambda: _fred_points("DFII10"),
        "dgs10": lambda: _fred("DGS10"),
        "hy": lambda: _fred("BAMLH0A0HYM2"),
        "vix": lambda: _last_close("^VIX"),
        "spx": lambda: _closes("^GSPC"),
        "twii": lambda: _closes("^TWII"),
        "sox": lambda: _closes("^SOX"),
    }


def _fred(series_id: str) -> list[float]:
    return [value for _day, value in _fred_points(series_id)]


def _fred_points(series_id: str) -> list[tuple[str, float]]:
    import requests

    from macro_proxies import parse_fred_points

    response = requests.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series_id},
        headers={"User-Agent": "quant-command-center/1.0"},
        timeout=_FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return parse_fred_points(response.text)


def _proxy_fetchers() -> dict:
    return {
        "pmi_bundle": _fetch_pmi_bundle,
        "ndc_scores": _fetch_ndc_scores,
        "export_yoy": _fetch_export_yoy,
        "spx_pe": _fetch_spx_pe,
        "sector_closes": _fetch_sector_closes,
    }


def _fetch_pmi_bundle() -> dict:
    from macro_proxies import (
        EMPIRE_HEADLINE,
        EMPIRE_INVENTORIES,
        EMPIRE_NEW_ORDERS,
        PHILLY_HEADLINE,
        PHILLY_INVENTORIES,
        PHILLY_NEW_ORDERS,
    )

    ids = {
        "philly": PHILLY_HEADLINE,
        "empire": EMPIRE_HEADLINE,
        "philly_orders": PHILLY_NEW_ORDERS,
        "philly_inventories": PHILLY_INVENTORIES,
        "empire_orders": EMPIRE_NEW_ORDERS,
        "empire_inventories": EMPIRE_INVENTORIES,
    }
    found = {key: [] for key in ids}
    pool = ThreadPoolExecutor(max_workers=len(ids))
    future_map = {pool.submit(_fred_points, series_id): key for key, series_id in ids.items()}
    try:
        for future in as_completed(future_map, timeout=_FETCH_TIMEOUT):
            key = future_map[future]
            try:
                found[key] = future.result()
            except Exception:
                found[key] = []
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return found


def _fetch_ndc_scores() -> list[dict]:
    import io
    import zipfile

    import requests

    from macro_proxies import NDC_DATASET_URL, NDC_SCORE_FILE, parse_ndc_scores

    response = requests.get(NDC_DATASET_URL, headers={"User-Agent": "quant-command-center/1.0"}, timeout=_FETCH_TIMEOUT)
    response.raise_for_status()
    url = response.json()["result"]["distribution"][0]["resourceDownloadUrl"]
    blob = requests.get(url, headers={"User-Agent": "quant-command-center/1.0"}, timeout=_FETCH_TIMEOUT)
    blob.raise_for_status()
    archive = zipfile.ZipFile(io.BytesIO(blob.content))
    return parse_ndc_scores(archive.read(NDC_SCORE_FILE).decode("utf-8-sig"))


def _fetch_export_yoy() -> dict:
    import requests

    from macro_proxies import CUSTOMS_EXPORT_URL, parse_export_yoy

    response = requests.get(CUSTOMS_EXPORT_URL, headers={"User-Agent": "quant-command-center/1.0"}, timeout=_FETCH_TIMEOUT)
    response.raise_for_status()
    return parse_export_yoy(response.content.decode("utf-8-sig"))


def _fetch_spx_pe() -> dict:
    import yfinance as yf

    info = yf.Ticker("^GSPC").info or {}
    return {"forward": info.get("forwardPE"), "trailing": info.get("trailingPE")}


def _fetch_sector_closes() -> dict:
    from macro_proxies import INDUSTRY_ETFS

    symbols = sorted({symbol for pair in INDUSTRY_ETFS.values() for symbol in pair})
    found = {}
    for symbol in symbols:
        try:
            found[symbol] = _closes(symbol)
        except Exception:
            found[symbol] = []
    return found


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
