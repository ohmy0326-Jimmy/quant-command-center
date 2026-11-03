"""臺指選擇權波動率指數（台指 VIX）。

即時與近一年日收盤都來自期交所公開檔，不使用美股 VIX。
抓不到就回無報價，讓畫面標來源，不拿別的指數充數。
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from sheet_dates import parse_sheet_date, taipei_today

DAILY_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{yyyymm}new.txt"
INTRADAY_URL = "https://www.taifex.com.tw/cht/7/getVixData?filesname={yyyymmdd}"
SOURCE_LIVE = "期交所即時（臺指選擇權波動率指數）"
SOURCE_DAILY = "期交所日收盤（臺指選擇權波動率指數）"
SOURCE_MISSING = "無報價（期交所臺指選擇權波動率指數抓取失敗）"
_SUCCESS_TTL = 6 * 3600
_FAILURE_TTL = 120
_CACHE: dict[str, tuple[float, dict]] = {}


def month_stamps(today: date, months: int = 12) -> list[str]:
    """含當月在內、往回數的 YYYYMM。"""
    stamps = []
    year, month = today.year, today.month
    for _ in range(max(int(months), 1)):
        stamps.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(stamps))


def decode_vix_bytes(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    for encoding in ("big5", "cp950", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_vix_table(text: str) -> list[tuple[date, str, float]]:
    """解析期交所 VIX 文字檔。回傳 (日期, 時間欄, 指數)。"""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-") or stripped.startswith("交易") or set(stripped) <= {"-"}:
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) < 3:
            continue
        if parts[1].lower().startswith("last"):
            continue
        day = parse_sheet_date(parts[0])
        if day is None:
            continue
        try:
            value = float(parts[2].replace(",", ""))
        except ValueError:
            continue
        rows.append((day, parts[1], value))
    return rows


def _fetch_month(getter, stamp: str) -> list[tuple[date, str, float]]:
    try:
        body = getter(DAILY_URL.format(yyyymm=stamp))
    except Exception:
        return []
    if not body:
        return []
    return [(day, clock, value) for day, clock, value in parse_vix_table(decode_vix_bytes(body))]


def format_vix_clock(token: str) -> str:
    digits = "".join(ch for ch in str(token) if ch.isdigit())
    if not digits:
        return ""
    digits = digits.zfill(8)
    return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"


def fetch_taiex_vix(today: date | None = None, *, get=None, use_cache: bool | None = None) -> dict:
    """近一年日收盤，加上能抓到的最新一筆。

    get(url) 回傳 bytes 或 str；缺檔或失敗請丟例外或回空字串。
    測試注入 get 時不走記憶體快取。
    """
    today = taipei_today(today)
    caching = get is None if use_cache is None else use_cache
    if caching:
        cached = _read_cache()
        if cached is not None:
            return cached
    getter = get or _http_get
    history: list[tuple[date, float]] = []
    stamps = month_stamps(today, 12)
    workers = max(1, min(6, len(stamps)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = pool.map(lambda stamp: _fetch_month(getter, stamp), stamps)
        for batch in batches:
            for day, _clock, value in batch:
                history.append((day, value))
    history = [(day, value) for day, value in history if day <= today]
    history.sort(key=lambda item: item[0])
    dedup: dict[date, float] = {}
    for day, value in history:
        dedup[day] = value
    ordered = sorted(dedup.items())
    # 只留近一年（含今天往回 366 天）。
    cutoff = date.fromordinal(today.toordinal() - 366)
    ordered = [(day, value) for day, value in ordered if day >= cutoff]

    live_value = None
    live_as_of = ""
    try:
        intraday = getter(INTRADAY_URL.format(yyyymmdd=today.strftime("%Y%m%d")))
    except Exception:
        intraday = ""
    if intraday:
        prints = parse_vix_table(decode_vix_bytes(intraday))
        if prints:
            day, clock, value = prints[-1]
            live_value = value
            live_as_of = f"{day.isoformat()} {format_vix_clock(clock)}".strip()

    if live_value is not None:
        result = _result(True, live_value, live_as_of, SOURCE_LIVE, ordered)
    elif ordered:
        day, value = ordered[-1]
        result = _result(True, value, day.isoformat(), SOURCE_DAILY, ordered)
    else:
        result = _result(False, None, "", SOURCE_MISSING, [])
    if caching:
        _CACHE["quote"] = (time.monotonic(), result)
    return result


def _result(ok: bool, value: float | None, as_of: str, source: str, ordered: list[tuple[date, float]]) -> dict:
    return {
        "ok": ok,
        "value": value,
        "as_of": as_of,
        "source": source,
        "history": [value for _day, value in ordered],
        "history_days": len(ordered),
        "history_label": f"近一年台指 VIX 日收盤（{len(ordered)} 個交易日）" if ordered else "近一年台指 VIX 日收盤（沒有資料）",
    }


def _read_cache() -> dict | None:
    cached = _CACHE.get("quote")
    if not cached:
        return None
    stamped, payload = cached
    ttl = _SUCCESS_TTL if payload.get("ok") else _FAILURE_TTL
    if time.monotonic() - stamped > ttl:
        return None
    return payload


def clear_cache() -> None:
    _CACHE.clear()


def _http_get(url: str) -> bytes:
    response = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.taifex.com.tw/cht/7/vixDaily3MNew",
        },
        timeout=8,
    )
    if getattr(response, "status_code", 200) != 200:
        return b""
    return response.content or b""
