"""個股期貨代號解析：全系統（第四頁、唯讀 API、元大期貨帳）共用。

「小國巨期」／「小型國巨期」／S2327 → (2327, 小型, 乘數 100)
「國巨期」／2327F                     → (2327, 一般, 乘數 2000)
小型個股期以現股價對標（不抓期交所期貨報價）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STOCK_FUTURES_MULTIPLIER = 2000.0
SMALL_STOCK_FUTURES_MULTIPLIER = 100.0

# 中文簡稱 → 證券代號。新增個股期時在這裡補。
STOCK_NAME_ALIASES = {
    "國巨": "2327",
    "台積電": "2330",
    "臺積電": "2330",
    "聯發科": "2454",
    "鴻海": "2317",
    "廣達": "2382",
    "緯創": "3231",
    "緯穎": "6669",
    "技嘉": "2376",
    "華碩": "2357",
    "日月光投控": "3711",
    "聯電": "2303",
    "長榮": "2603",
    "陽明": "2609",
    "萬海": "2615",
    "台達電": "2308",
    "臺達電": "2308",
    "奇鋐": "3017",
    "華邦電": "2344",
    "南亞科": "2408",
    "金居": "8358",
    "群創": "3481",
    "友達": "2409",
    "富邦金": "2881",
    "國泰金": "2882",
    "中信金": "2891",
}

# 指數期貨名稱，避免「小台」「小型臺指」被誤判成個股期。
_INDEX_WORDS = ("台指", "臺指", "小台", "微台", "大台", "TX", "MTX", "TMF", "FMF", "MXF", "TXO")


@dataclass(frozen=True)
class StockFuture:
    code: str          # 證券代號，例 "2327"
    small: bool
    multiplier: float

    @property
    def yahoo_ticker(self) -> str:
        return f"{self.code}.TW"


def _make(code: str, small: bool) -> StockFuture:
    return StockFuture(code, small, SMALL_STOCK_FUTURES_MULTIPLIER if small else STOCK_FUTURES_MULTIPLIER)


def resolve_stock_future(symbol) -> StockFuture | None:
    """解析個股期代號或中文名稱；不是個股期回傳 None。"""
    raw = str(symbol or "").strip().replace(" ", "").replace("　", "")
    if not raw:
        return None
    up = raw.upper()
    if any(w in up for w in _INDEX_WORDS):
        return None
    m = re.fullmatch(r"S(\d{4,6})", up)
    if m:
        return _make(m.group(1), True)
    m = re.fullmatch(r"(\d{4,6})(F|期|期貨)", up)
    if m:
        return _make(m.group(1), False)
    m = re.fullmatch(r"小(?:型)?(\d{4,6})(?:期|期貨)?", raw)
    if m:
        return _make(m.group(1), True)

    small = False
    name = raw
    for pre in ("小型", "小"):
        if name.startswith(pre):
            small, name = True, name[len(pre):]
            break
    if "小型" in name:  # 「國巨小型期貨」
        small, name = True, name.replace("小型", "")
    had_suffix = False
    for suf in ("期貨", "期"):
        if name.endswith(suf):
            name, had_suffix = name[: -len(suf)], True
            break
    if not had_suffix:
        return None
    code = STOCK_NAME_ALIASES.get(name)
    if code is None and re.fullmatch(r"\d{4,6}", name):
        code = name
    if code is None and name:
        code = lookup_code_by_name(name, small)
    return _make(code, small) if code else None


def stock_futures_multiplier(symbol) -> float | None:
    sf = resolve_stock_future(symbol)
    return sf.multiplier if sf else None


def lookup_code_by_name(name: str, small: bool = False) -> str | None:
    """別名表沒有時，查期交所個股期清單（DispCName 含中文簡稱 → SpotID）。12 小時快取，失敗回 None。"""
    try:
        import taifex_quotes
    except Exception:
        return None
    kinds = ["8", "4"] if small else ["4", "8"]
    for kind_id in kinds:
        try:
            rt = taifex_quotes._post(
                "getCmdyDDLItemByKind",
                {"SymbolType": "F", "KindID": kind_id, "CID": ""},
                ttl=taifex_quotes.SYMBOL_CACHE_TTL_SECONDS,
            )
        except Exception:
            rt = None
        for item in (rt or {}).get("Items", []) if isinstance(rt, dict) else []:
            cname = str(item.get("DispCName", ""))
            spot = str(item.get("SpotID", "")).strip()
            if name and name in cname and re.fullmatch(r"\d{4,6}", spot):
                return spot
    return None
