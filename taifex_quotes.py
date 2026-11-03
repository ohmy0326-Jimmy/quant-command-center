"""TAIFEX Public MIS Quote API Module.

Data Source Verified Live:
https://mis.taifex.com.tw/futures/api/getQuoteList
Header: Content-Type: application/json, User-Agent: Mozilla/5.0
POST body format:
{
    "MarketType": "0" (day session) or "1" (night/after-hours session),
    "SymbolType": "F" (futures) or "O" (options),
    "KindID": "1" (index) or "4"/"8" (stock futures),
    "CID": "TMF" / "TXF" / "MXF" / "CDF" / "TXO",
    "ExpireMonth": "",
    "RowSize": "全部",
    "PageNo": "",
    "SortColumn": "",
    "AscDesc": "A"
}
Response: {"RtCode":"0","RtMsg":"","RtData":{"QuoteList":[{...}, ...]}}.
Each item contains string fields: SymbolID, DispCName, DispEName, CLastPrice,
CRefPrice, SettlementPrice, CBidPrice1, CAskPrice1, CDate ("20261102"), CTime ("134459"),
CTotalVolume, OpenInterest.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
import json
import math
import re
import threading
import time as time_module
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

QUOTE_CACHE_TTL_SECONDS = 45.0
FAILURE_CACHE_TTL_SECONDS = 15.0
SYMBOL_CACHE_TTL_SECONDS = 43200.0  # 12 hours

MULTIPLIERS = {
    "TX": 200,
    "TXF": 200,
    "MTX": 50,
    "MXF": 50,
    "TMF": 10,
    "FMF": 10,
    "TXO": 50,
}
STOCK_FUTURES_MULTIPLIER = 2000
SMALL_STOCK_FUTURES_MULTIPLIER = 100

INDEX_FUTURES_CID = {
    "TX": "TXF",
    "TXF": "TXF",
    "MTX": "MXF",
    "MXF": "MXF",
    "TMF": "TMF",
    "FMF": "TMF",
}

_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()

_http_post: Callable[..., Any] = requests.post


def clear_cache() -> None:
    """Clear in-process TTL cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _post(
    endpoint: str,
    body: dict[str, Any],
    timeout: float = 6.0,
    ttl: float = QUOTE_CACHE_TTL_SECONDS,
) -> dict[str, Any] | None:
    cache_key = (endpoint, json.dumps(body, sort_keys=True))
    now_ts = time_module.monotonic()
    with _CACHE_LOCK:
        if cache_key in _CACHE:
            ts, cached_val = _CACHE[cache_key]
            limit = ttl if cached_val is not None else min(ttl, FAILURE_CACHE_TTL_SECONDS)
            if now_ts - ts < limit:
                return cached_val

    url = (
        endpoint
        if endpoint.startswith("http")
        else f"https://mis.taifex.com.tw/futures/api/{endpoint.lstrip('/')}"
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    rt_data = None
    try:
        resp = _http_post(url, json=body, headers=headers, timeout=timeout)
        status = getattr(resp, "status_code", 200)
        if status == 200:
            data = resp.json() if callable(getattr(resp, "json", None)) else resp
            if isinstance(data, dict) and data.get("RtCode") == "0" and isinstance(data.get("RtData"), dict):
                rt_data = data["RtData"]
    except Exception:
        rt_data = None
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now_ts, rt_data)
    return rt_data


@dataclass(frozen=True)
class Quote:
    price: float | None
    source: str
    label: str
    symbol_id: str
    contract_month: str
    session: str
    price_type: str
    as_of: datetime | None
    is_fallback: bool
    underlying_price: float | None
    multiplier: float


def _parse_taifex_dt(cdate: str, ctime: str) -> datetime | None:
    if not cdate or len(cdate) != 8 or not cdate.isdigit():
        return None
    year, month, day = int(cdate[:4]), int(cdate[4:6]), int(cdate[6:8])
    hour, minute, second = 0, 0, 0
    if ctime and ctime.isdigit():
        if len(ctime) >= 6:
            hour, minute, second = int(ctime[:2]), int(ctime[2:4]), int(ctime[4:6])
        elif len(ctime) >= 4:
            hour, minute = int(ctime[:2]), int(ctime[2:4])
    try:
        return datetime(
            year, month, day, hour, minute, second, tzinfo=ZoneInfo("Asia/Taipei")
        )
    except ValueError:
        return None


def _parse_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s in ("0", "0.00", "0.000"):
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _resolve_contract_year(y_digit: int, current_year: int) -> int:
    candidates = [current_year - 1 + i for i in range(12)]
    matching = [y for y in candidates if y % 10 == y_digit]
    matching.sort(key=lambda y: (abs(y - current_year), y < current_year))
    return matching[0] if matching else current_year


def _parse_futures_symbol(symbol_id: str, current_year: int) -> dict[str, Any] | None:
    m = re.match(r"^([A-Z0-9]+)([A-L])(\d)-(F|M)$", symbol_id.upper().strip())
    if not m:
        return None
    cid, month_letter, year_digit, session_code = m.groups()
    month_num = ord(month_letter) - ord("A") + 1
    year = _resolve_contract_year(int(year_digit), current_year)
    contract_month = f"{year:04d}{month_num:02d}"
    session = "day" if session_code == "F" else "night"
    return {
        "cid": cid,
        "month_num": month_num,
        "year": year,
        "contract_month": contract_month,
        "session": session,
    }


def _parse_option_symbol(symbol_id: str, current_year: int) -> dict[str, Any] | None:
    m = re.match(r"^([A-Z0-9]{3})(\d+)([A-X])(\d)-([O|N])$", symbol_id.upper().strip())
    if not m:
        return None
    prefix, strike_str, month_letter, year_digit, session_code = m.groups()
    strike = float(strike_str)
    letter_ord = ord(month_letter)
    if ord("A") <= letter_ord <= ord("L"):
        cp = "C"
        month_num = letter_ord - ord("A") + 1
    elif ord("M") <= letter_ord <= ord("X"):
        cp = "P"
        month_num = letter_ord - ord("M") + 1
    else:
        return None
    year = _resolve_contract_year(int(year_digit), current_year)
    contract_month = f"{year:04d}{month_num:02d}"
    session = "day" if session_code == "O" else "night"
    return {
        "prefix": prefix,
        "strike": strike,
        "call_put": cp,
        "month_num": month_num,
        "year": year,
        "contract_month": contract_month,
        "session": session,
    }


def futures_cid_for(
    symbol: str, underlying: str | None = None, small: bool = False
) -> tuple[str, str, float] | None:
    """Find CID, KindID, and multiplier for index or stock futures.

    Returns (cid, kindid, multiplier) or None.
    """
    if not symbol:
        return None
    sym_upper = symbol.upper().strip()
    clean_sym = sym_upper.split(".")[0]

    if clean_sym in INDEX_FUTURES_CID:
        cid = INDEX_FUTURES_CID[clean_sym]
        mult = float(MULTIPLIERS.get(clean_sym, MULTIPLIERS.get(cid, 200)))
        return (cid, "1", mult)

    target_code = (underlying or symbol).upper().strip().split(".")[0]
    target_code_base = target_code[:-1] if target_code.endswith("F") else target_code

    def check_kind(kind_id: str) -> tuple[str, str, float] | None:
        body = {"SymbolType": "F", "KindID": kind_id, "CID": ""}
        rt = _post("getCmdyDDLItemByKind", body, ttl=SYMBOL_CACHE_TTL_SECONDS)
        if not rt or "Items" not in rt:
            return None
        mult = (
            float(SMALL_STOCK_FUTURES_MULTIPLIER)
            if kind_id == "8"
            else float(STOCK_FUTURES_MULTIPLIER)
        )
        for item in rt.get("Items", []):
            spot_id = str(item.get("SpotID", "")).upper()
            cid_val = str(item.get("CID", "")).upper()
            ename_val = str(item.get("DispEName", "")).upper()
            if (
                target_code in (spot_id, cid_val, ename_val)
                or target_code_base in (spot_id, cid_val, ename_val)
                or sym_upper in (cid_val, ename_val)
            ):
                return (cid_val, kind_id, mult)
        return None

    kinds = ["8", "4"] if small else ["4", "8"]
    for k in kinds:
        res = check_kind(k)
        if res:
            return res

    return None


def get_futures_quote(
    symbol: str,
    delivery_month: str | None = None,
    *,
    underlying: str | None = None,
    small: bool = False,
    fallback_price: float | None = None,
    fallback_label: str = "加權指數現貨",
    now: datetime | None = None,
) -> Quote:
    now_dt = now or datetime.now(ZoneInfo("Asia/Taipei"))
    cid_info = futures_cid_for(symbol, underlying=underlying, small=small)
    _sym_key = str(symbol).upper().strip()
    _fallback_mult = float(
        cid_info[2] if cid_info else MULTIPLIERS.get(_sym_key, STOCK_FUTURES_MULTIPLIER)
    )

    def _fallback_quote(msg: str = "") -> Quote:
        if fallback_price is not None:
            p_str = (
                f"{int(fallback_price):,}"
                if fallback_price.is_integer()
                else f"{fallback_price:,.2f}"
            )
            lbl = (
                f"期貨報價失敗，改用{fallback_label} {p_str}"
                if msg
                else f"{fallback_label} {p_str}"
            )
            return Quote(
                price=fallback_price,
                source="TAIEX現貨(備援)" if fallback_label == "加權指數現貨" else f"{fallback_label}(備援)",
                label=lbl,
                symbol_id=symbol,
                contract_month="",
                session="",
                price_type="fallback",
                as_of=now_dt,
                is_fallback=True,
                underlying_price=None,
                multiplier=_fallback_mult,
            )
        return Quote(
            price=None,
            source="無報價",
            label=f"期貨報價失敗 ({symbol}) 無報價",
            symbol_id=symbol,
            contract_month="",
            session="",
            price_type="none",
            as_of=now_dt,
            is_fallback=False,
            underlying_price=None,
            multiplier=_fallback_mult,
        )

    if not cid_info:
        return _fallback_quote("unknown symbol")

    cid, kind_id, multiplier = cid_info
    body_day = {
        "MarketType": "0",
        "SymbolType": "F",
        "KindID": kind_id,
        "CID": cid,
        "ExpireMonth": "",
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    body_night = {
        "MarketType": "1",
        "SymbolType": "F",
        "KindID": kind_id,
        "CID": cid,
        "ExpireMonth": "",
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }

    rt_day = _post("getQuoteList", body_day)
    rt_night = _post("getQuoteList", body_night)

    if not rt_day and not rt_night:
        return _fallback_quote("api failure")

    underlying_price = None
    all_items = []
    for rt in (rt_day, rt_night):
        if not rt:
            continue
        for row in rt.get("QuoteList", []):
            sym_id = row.get("SymbolID", "")
            if sym_id.endswith("-S") or sym_id.endswith("-P"):
                p = _parse_float(row.get("CLastPrice"))
                if p:
                    underlying_price = p
            else:
                all_items.append(row)

    contracts_by_month: dict[str, dict[str, dict[str, Any]]] = {}
    for item in all_items:
        parsed = _parse_futures_symbol(item.get("SymbolID", ""), now_dt.year)
        if not parsed:
            continue
        c_month = parsed["contract_month"]
        sess = parsed["session"]
        contracts_by_month.setdefault(c_month, {})[sess] = item

    if not contracts_by_month:
        return _fallback_quote("no contracts")

    if delivery_month:
        target_month = delivery_month
    else:
        now_month_str = now_dt.strftime("%Y%m")
        future_months = sorted(
            [m for m in contracts_by_month.keys() if m >= now_month_str]
        )
        candidate_months = future_months or sorted(list(contracts_by_month.keys()))
        target_month = candidate_months[0]
        for m in candidate_months:
            sess_map = contracts_by_month[m]
            has_price = False
            for s_item in sess_map.values():
                if (
                    _parse_float(s_item.get("CLastPrice"))
                    or (
                        _parse_float(s_item.get("CBidPrice1"))
                        and _parse_float(s_item.get("CAskPrice1"))
                    )
                    or _parse_float(s_item.get("SettlementPrice"))
                    or _parse_float(s_item.get("CRefPrice"))
                ):
                    has_price = True
                    break
            if has_price:
                target_month = m
                break

    target_sessions = contracts_by_month.get(target_month, {})
    if not target_sessions:
        return _fallback_quote("month not found")

    day_item = target_sessions.get("day")
    night_item = target_sessions.get("night")

    def eval_item(item: dict[str, Any] | None) -> dict[str, Any]:
        if not item:
            return {"price": None, "type": "none", "as_of": None, "item": None}
        dt = _parse_taifex_dt(item.get("CDate", ""), item.get("CTime", ""))
        last_p = _parse_float(item.get("CLastPrice"))
        if last_p:
            return {"price": last_p, "type": "last", "as_of": dt, "item": item}
        bid = _parse_float(item.get("CBidPrice1"))
        ask = _parse_float(item.get("CAskPrice1"))
        if bid and ask:
            return {
                "price": (bid + ask) / 2.0,
                "type": "mid",
                "as_of": dt,
                "item": item,
            }
        settle = _parse_float(item.get("SettlementPrice"))
        if settle:
            return {
                "price": settle,
                "type": "settlement",
                "as_of": dt,
                "item": item,
            }
        ref = _parse_float(item.get("CRefPrice"))
        if ref:
            return {"price": ref, "type": "reference", "as_of": dt, "item": item}
        return {"price": None, "type": "none", "as_of": dt, "item": item}

    d_eval = eval_item(day_item)
    n_eval = eval_item(night_item)

    chosen = None
    if d_eval["type"] in ("last", "mid") and n_eval["type"] in ("last", "mid"):
        t_d = d_eval["as_of"] or datetime.min.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        t_n = n_eval["as_of"] or datetime.min.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        chosen = n_eval if t_n >= t_d else d_eval
    elif n_eval["type"] in ("last", "mid"):
        chosen = n_eval
    elif d_eval["type"] in ("last", "mid"):
        chosen = d_eval
    elif n_eval["type"] in ("settlement", "reference"):
        chosen = n_eval
    elif d_eval["type"] in ("settlement", "reference"):
        chosen = d_eval
    else:
        return _fallback_quote("no price")

    item = chosen["item"]
    price = chosen["price"]
    price_type = chosen["type"]
    as_of = chosen["as_of"]
    symbol_id = item.get("SymbolID", "")
    sess_str = (
        "night"
        if symbol_id.endswith("-M")
        else ("day" if symbol_id.endswith("-F") else "")
    )
    is_fallback = price_type in ("settlement", "reference")
    source = "結算價(備援)" if is_fallback else "TAIFEX"

    disp_name_map = {"TXF": "大臺指期", "MXF": "小臺指期", "TMF": "微臺指期"}
    disp_name = disp_name_map.get(
        cid, item.get("DispCName", cid).rstrip("0123456789")
    )
    p_formatted = f"{int(price):,}" if price.is_integer() else f"{price:,.2f}"

    if is_fallback:
        label = f"TAIFEX {disp_name} {symbol_id} 結算價(備援) {p_formatted}"
    else:
        sess_cn = "夜盤" if sess_str == "night" else "日盤"
        time_cn = as_of.strftime("%H:%M") if as_of else ""
        pt_cn = "成交價" if price_type == "last" else "買賣中價"
        parts = [
            "TAIFEX",
            disp_name,
            symbol_id,
            sess_cn,
            time_cn,
            pt_cn,
            p_formatted,
        ]
        label = " ".join([p for p in parts if p])

    return Quote(
        price=price,
        source=source,
        label=label,
        symbol_id=symbol_id,
        contract_month=target_month,
        session=sess_str,
        price_type=price_type,
        as_of=as_of,
        is_fallback=is_fallback,
        underlying_price=underlying_price,
        multiplier=multiplier,
    )


def list_option_expiries(cid: str = "TXO") -> list[str]:
    body = {"SymbolType": "O", "KindID": "1", "CID": cid, "MarketType": "0"}
    rt = _post("getCmdyMonthDDLItemByKind", body, ttl=SYMBOL_CACHE_TTL_SECONDS)
    if not rt:
        return []
    items = rt.get("Items", [])
    expiries: list[tuple[str, int, str]] = []
    for idx, item in enumerate(items):
        val = str(item.get("item", "")).strip()
        if not val or val == "現貨":
            continue
        # dispName looks like "202611W1(2026/11/04)"; sort by that settlement date,
        # because a plain string sort would put "202611" before "202611W1".
        m = re.search(r"(\d{4})/(\d{2})/(\d{2})", str(item.get("dispName", "")))
        key = f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else "99999999"
        expiries.append((key, idx, val))
    expiries.sort()
    return [val for _key, _idx, val in expiries]


def get_option_quote(
    strike: float,
    call_put: str,
    expiry: str | None = None,
    *,
    cid: str = "TXO",
    now: datetime | None = None,
) -> Quote:
    now_dt = now or datetime.now(ZoneInfo("Asia/Taipei"))
    cp_upper = call_put.upper().strip()
    cp_target = "C" if cp_upper in ("C", "CALL", "買權") else "P"
    multiplier = float(MULTIPLIERS.get(cid, 50))

    if not expiry:
        expiries = list_option_expiries(cid)
        expiry = expiries[0] if expiries else now_dt.strftime("%Y%m")

    body_day = {
        "MarketType": "0",
        "SymbolType": "O",
        "KindID": "1",
        "CID": cid,
        "ExpireMonth": expiry,
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    body_night = {
        "MarketType": "1",
        "SymbolType": "O",
        "KindID": "1",
        "CID": cid,
        "ExpireMonth": expiry,
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }

    rt_day = _post("getQuoteList", body_day)
    rt_night = _post("getQuoteList", body_night)

    def find_matching_row(
        rt: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not rt:
            return None, None
        for row in rt.get("QuoteList", []):
            parsed = _parse_option_symbol(row.get("SymbolID", ""), now_dt.year)
            if (
                parsed
                and abs(parsed["strike"] - float(strike)) < 1e-4
                and parsed["call_put"] == cp_target
            ):
                return row, parsed
        return None, None

    day_row, day_parsed = find_matching_row(rt_day)
    night_row, night_parsed = find_matching_row(rt_night)

    def eval_opt(
        row: dict[str, Any] | None, parsed: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not row:
            return {"price": None, "type": "none", "as_of": None, "row": None}
        dt = _parse_taifex_dt(row.get("CDate", ""), row.get("CTime", ""))
        last_p = _parse_float(row.get("CLastPrice"))
        if last_p:
            return {"price": last_p, "type": "last", "as_of": dt, "row": row}
        bid = _parse_float(row.get("CBidPrice1"))
        ask = _parse_float(row.get("CAskPrice1"))
        if bid and ask:
            return {
                "price": (bid + ask) / 2.0,
                "type": "mid",
                "as_of": dt,
                "row": row,
            }
        settle = _parse_float(row.get("SettlementPrice"))
        if settle:
            return {
                "price": settle,
                "type": "settlement",
                "as_of": dt,
                "row": row,
            }
        ref = _parse_float(row.get("CRefPrice"))
        if ref:
            return {"price": ref, "type": "reference", "as_of": dt, "row": row}
        return {"price": None, "type": "none", "as_of": dt, "row": row}

    d_eval = eval_opt(day_row, day_parsed)
    n_eval = eval_opt(night_row, night_parsed)

    chosen = None
    if d_eval["type"] in ("last", "mid") and n_eval["type"] in ("last", "mid"):
        t_d = d_eval["as_of"] or datetime.min.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        t_n = n_eval["as_of"] or datetime.min.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        chosen = n_eval if t_n >= t_d else d_eval
    elif n_eval["type"] in ("last", "mid"):
        chosen = n_eval
    elif d_eval["type"] in ("last", "mid"):
        chosen = d_eval
    elif n_eval["type"] in ("settlement", "reference"):
        chosen = n_eval
    elif d_eval["type"] in ("settlement", "reference"):
        chosen = d_eval

    strike_fmt = f"{strike:.0f}" if strike.is_integer() else f"{strike}"
    if not chosen or not chosen["price"]:
        return Quote(
            price=None,
            source="無報價",
            label=f"TAIFEX {cid} {expiry} {strike_fmt}{cp_target} 無報價",
            symbol_id="",
            contract_month=expiry,
            session="",
            price_type="none",
            as_of=now_dt,
            is_fallback=False,
            underlying_price=None,
            multiplier=multiplier,
        )

    row = chosen["row"]
    price = chosen["price"]
    price_type = chosen["type"]
    as_of = chosen["as_of"]
    symbol_id = row.get("SymbolID", "")
    sess_str = (
        "night"
        if symbol_id.endswith("-N")
        else ("day" if symbol_id.endswith("-O") else "")
    )
    is_fallback = price_type in ("settlement", "reference")
    source = "結算價(備援)" if is_fallback else "TAIFEX"
    p_formatted = f"{int(price):,}" if price.is_integer() else f"{price:,.2f}"

    if is_fallback:
        label = f"TAIFEX {cid} {expiry} {strike_fmt}{cp_target} 結算價(備援) {p_formatted}"
    else:
        sess_cn = "夜盤" if sess_str == "night" else "日盤"
        time_cn = as_of.strftime("%H:%M") if as_of else ""
        pt_cn = "成交價" if price_type == "last" else "買賣中價"
        parts = [
            "TAIFEX",
            cid,
            expiry,
            f"{strike_fmt}{cp_target}",
            sess_cn,
            time_cn,
            pt_cn,
            p_formatted,
        ]
        label = " ".join([p for p in parts if p])

    return Quote(
        price=price,
        source=source,
        label=label,
        symbol_id=symbol_id,
        contract_month=expiry,
        session=sess_str,
        price_type=price_type,
        as_of=as_of,
        is_fallback=is_fallback,
        underlying_price=None,
        multiplier=multiplier,
    )


def parse_txo_symbol(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    s = text.upper().strip()
    s = s.replace("CALL", "C").replace("PUT", "P").replace("買權", "C").replace("賣權", "P")
    m = re.search(
        r"(?:TXO|TX[1-5U-Z])?[-_ ]*(\d{6}(?:[WF]\d)?)[-_ ]*([CP])[-_ ]*(\d+(?:\.\d+)?)",
        s,
    )
    if not m:
        return None
    exp, cp, strike_str = m.groups()
    return {"expiry": exp, "call_put": cp, "strike": float(strike_str)}


def black76_delta(
    F: float, K: float, T_years: float, vol: float, call_put: str
) -> float | None:
    if F <= 0 or K <= 0 or T_years <= 0 or vol <= 0:
        return None
    cp = call_put.upper().strip()
    is_call = cp in ("C", "CALL", "買權")
    is_put = cp in ("P", "PUT", "賣權")
    if not (is_call or is_put):
        return None

    d1 = (math.log(F / K) + 0.5 * vol * vol * T_years) / (vol * math.sqrt(T_years))
    n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return n_d1 if is_call else n_d1 - 1.0


def implied_vol_black76(
    price: float, F: float, K: float, T_years: float, call_put: str
) -> float | None:
    if F <= 0 or K <= 0 or T_years <= 0 or price <= 0:
        return None
    cp = call_put.upper().strip()
    is_call = cp in ("C", "CALL", "買權")
    is_put = cp in ("P", "PUT", "賣權")
    if not (is_call or is_put):
        return None

    min_p = max(0.0, F - K) if is_call else max(0.0, K - F)
    max_p = F if is_call else K
    if price <= min_p or price >= max_p:
        return None

    low, high = 0.001, 5.0

    def calc_price(v: float) -> float:
        d1 = (math.log(F / K) + 0.5 * v * v * T_years) / (v * math.sqrt(T_years))
        d2 = d1 - v * math.sqrt(T_years)
        n1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        n2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return (F * n1 - K * n2) if is_call else (K * (1.0 - n2) - F * (1.0 - n1))

    for _ in range(60):
        mid = (low + high) / 2.0
        p_mid = calc_price(mid)
        if abs(p_mid - price) < 1e-5:
            return mid
        if p_mid > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def option_delta_from_quote(
    option_price: float,
    futures_price: float,
    strike: float,
    expiry_date: date | datetime | str,
    call_put: str,
    now: datetime | None = None,
) -> float | None:
    now_dt = now or datetime.now(ZoneInfo("Asia/Taipei"))
    if isinstance(expiry_date, str):
        exp_d = datetime.strptime(expiry_date, "%Y%m%d").date()
    elif isinstance(expiry_date, datetime):
        exp_d = expiry_date.date()
    else:
        exp_d = expiry_date

    exp_dt = datetime.combine(exp_d, time(13, 30), tzinfo=ZoneInfo("Asia/Taipei"))
    seconds = (exp_dt - now_dt).total_seconds()
    t_years = max(seconds, 3600.0) / (365.0 * 86400.0)

    vol = implied_vol_black76(option_price, futures_price, strike, t_years, call_put)
    if vol is None:
        return None
    return black76_delta(futures_price, strike, t_years, vol, call_put)
