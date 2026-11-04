"""個股期貨保證金：期交所「股票期貨保證金一覽表」（依級距的適用比例）。

每口原始保證金 ＝ 原始保證金適用比例 × 參考價 × 合約乘數。
來源優先序：
1. Futures_Ledger 設定列「原始保證金」（每口金額，與 PR #18 的 TX/MTX/FMF 相同機制）。
2. 期交所保證金一覽表（https://www.taifex.com.tw/cht/5/stockMargining），快取 12 小時。
3. 抓不到時用預設級距 3 比例（原始 20.25%／維持 15.53%），偏保守。
注意：期交所以前一日結算價計算，與用最新現股價估算會有差異。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

TAIFEX_STOCK_MARGIN_URL = "https://www.taifex.com.tw/cht/5/stockMargining"
CACHE_TTL_SECONDS = 12 * 3600
FAILURE_CACHE_TTL_SECONDS = 600
DEFAULT_INITIAL_RATIO = 0.2025
DEFAULT_MAINT_RATIO = 0.1553
DEFAULT_TIER = "級距3（預設）"


@dataclass(frozen=True)
class MarginRatio:
    code: str
    tier: str
    clearing: float
    maintenance: float
    initial: float


_http_get: Callable[..., Any] = requests.get
_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "table": None}

_ROW_RE = re.compile(
    r"\|(\d{4,6})\|[^|]*\|[^|]*\|(級距\s*\d+)\|([\d.]+)%\|([\d.]+)%\|([\d.]+)%"
)


def clear_cache() -> None:
    with _lock:
        _cache.update(ts=0.0, table=None)


def parse_margin_table(html: str) -> dict[str, MarginRatio]:
    """把期交所頁面解析成 {證券代號: MarginRatio}。同一代號（一般／小型）比例相同，取第一筆。"""
    text = re.sub(r"<[^>]+>", "|", html or "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"\|+", "|", text)
    out: dict[str, MarginRatio] = {}
    for m in _ROW_RE.finditer(text):
        code = m.group(1)
        if code in out:
            continue
        out[code] = MarginRatio(code, m.group(2), float(m.group(3)) / 100, float(m.group(4)) / 100, float(m.group(5)) / 100)
    return out


def fetch_margin_table(force: bool = False) -> dict[str, MarginRatio] | None:
    now = time.time()
    with _lock:
        table, ts = _cache["table"], _cache["ts"]
        limit = CACHE_TTL_SECONDS if table else FAILURE_CACHE_TTL_SECONDS
        if not force and ts and now - ts < limit:
            return table or None
    table = None
    try:
        resp = _http_get(TAIFEX_STOCK_MARGIN_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if getattr(resp, "status_code", 200) == 200:
            resp.encoding = resp.encoding or "utf-8"
            parsed = parse_margin_table(resp.text)
            table = parsed or None
    except Exception:
        table = None
    with _lock:
        _cache.update(ts=now, table=table or {})
    return table


def margin_ratio(code: str) -> tuple[MarginRatio, str]:
    table = fetch_margin_table()
    if table and code in table:
        r = table[code]
        return r, f"期交所股票期貨保證金一覽表 {r.tier} 原始 {r.initial:.2%}"
    r = MarginRatio(code, DEFAULT_TIER, 0.0, DEFAULT_MAINT_RATIO, DEFAULT_INITIAL_RATIO)
    return r, f"預設比例 原始 {DEFAULT_INITIAL_RATIO:.2%}（期交所表抓不到）"


def stock_future_margin(code: str, notional_abs: float, qty_abs: float, override_per_lot: float | None = None) -> tuple[float, float, str]:
    """回傳 (原始保證金, 維持保證金, 來源說明)。notional_abs＝參考價 × 乘數 × 口數。"""
    ratio, label = margin_ratio(code)
    if override_per_lot is not None and override_per_lot > 0:
        initial = float(override_per_lot) * qty_abs
        maint_ratio = ratio.maintenance / ratio.initial if ratio.initial else 0.767
        return initial, initial * maint_ratio, f"設定列每口原始保證金 {override_per_lot:,.0f}"
    return notional_abs * ratio.initial, notional_abs * ratio.maintenance, label
