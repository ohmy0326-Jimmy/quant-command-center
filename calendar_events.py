"""第三頁行事曆。只組事件與部位對照，不寫試算表、不推播、不下單。

台股休市表只收證交所民國 115 年（2026）公布的不交易日。
沒有該年休市表時，不把星期三直接列成結算日。
第三個星期三若落在休市日，不自行決定提前或順延。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from beta_rules import LEVERAGED_UNDERLYING
from cb_holdings import product_kind, underlying_ticker_from_row
from portfolio_risk import is_option_row
from sector_board import ATTACK, DEFENSE
from sector_map import ETF_INDUSTRY_MAP, OTHER_SECTOR
from stock_futures import resolve_stock_future

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")

# 證交所 holidaySchedule queryYear=115 裡「不交易」的日期。開始交易日不放進來。
# 週末另由 weekday 判斷。這張表不是即時期交所改期公告。
TW_NON_TRADING_2026 = frozenset({
    date(2026, 1, 1),
    date(2026, 2, 12),
    date(2026, 2, 13),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 27),
    date(2026, 4, 3),
    date(2026, 4, 4),
    date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 10, 9),
    date(2026, 10, 26),
    date(2026, 12, 25),
})
HOLIDAY_YEARS = frozenset({2026})

# 只收這份程式已經用過的基金代號。沒有在這裡的代號，除非種類或名稱寫了 ETF，否則不當成基金。
FUND_TICKERS = frozenset(
    set(ETF_INDUSTRY_MAP)
    | set(LEVERAGED_UNDERLYING)
    | set(ATTACK)
    | set(DEFENSE)
    | {"0050", "006208", "00631L", "00663L", "KORU", "IGV", "SOXX", "UFO", "SPY", "QQQ", "SMH", "EWY"}
)
INDEX_FUTURES = frozenset({"TX", "MTX", "FMF", "TMF", "MXF"})
MAX_EARNINGS_LOOKUPS = 40

NOT_DONE = (
    {"name": "台積電月營收、合約負債", "why": "使用者後來限定只放財模，不進行事曆。"},
    {"name": "央行理監事會、主計總處 GDP", "why": "沒有固定月規則，也沒有接上官方行程檔，不硬編日期。"},
    {"name": "MSCI 等指數調整", "why": "沒有免費且可核對的官方調整日來源，不加事件型標籤。"},
    {"name": "財報前自動 AI 解析", "why": "要排程與 Gemini，這批不自動呼叫。"},
    {"name": "全球經濟事件全集", "why": "沒有 FMP_API_KEY 時不顯示；有金鑰也只留與利率、美國總經、匯率有關的項目。"},
)

FMP_KEEP = (
    ("利率", ("fomc", "fed interest", "interest rate decision", "federal funds")),
    ("美國總經", ("cpi", "ppi", "pce", "non farm", "nonfarm", "unemployment", "ism ", "retail sales", "jolts", "initial jobless", "jobless claims", "gdp")),
    ("匯率", ("usd/twd", "twd", "dollar index", "dxy")),
)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    if n < 1 or month < 1 or month > 12:
        return None
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if found.month != month:
        return None
    return found


def is_tw_trading_day(day: date, holidays: frozenset[date] | None = None) -> bool:
    table = TW_NON_TRADING_2026 if holidays is None else holidays
    if day.weekday() >= 5:
        return False
    if day.year not in HOLIDAY_YEARS:
        return False
    return day not in table


def normalize_symbol(text: str) -> tuple[str, str]:
    """回傳 (代號, 市場)。台股四碼補 .TW。空字串代表無法辨識。"""
    raw = str(text or "").strip().upper().replace(" ", "")
    if not raw or raw == "NAN":
        return "", ""
    if raw.endswith(".TW") or raw.endswith(".TWO"):
        return raw, "TW"
    base = raw.split(".")[0]
    if re.fullmatch(r"\d{4}", base):
        return base + ".TW", "TW"
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw):
        return raw, "US"
    return "", ""


def is_fund(ticker: str, kind: str = "", name: str = "") -> bool:
    blob = f"{kind} {name}".upper()
    if "ETF" in blob:
        return True
    key = str(ticker or "").upper().split(".")[0]
    if key in FUND_TICKERS:
        return True
    label = str(OTHER_SECTOR.get(key, ""))
    return "ETF" in label or key in {"0050", "006208"}


def is_index_derivative(symbol: str) -> bool:
    text = str(symbol or "").upper().replace(" ", "")
    base = text.split(".")[0]
    if base in INDEX_FUTURES or base.startswith("TXO"):
        return True
    folded = text.replace("臺", "台")
    return any(word in folded for word in ("台指", "小台", "微台", "大台"))


def _clean(value) -> str:
    text = str(value or "").strip()
    if text.lower() == "nan":
        return ""
    return text


def _append(found: list[dict], item: dict):
    found.append(item)


def holdings_from_positions(rows) -> tuple[list[dict], list[str]]:
    """持倉列。ETF、指數期貨仍留下，但 earnings 為假，不當成公司財報。"""
    found: list[dict] = []
    funds: list[str] = []
    indexes: list[str] = []
    other_futures: list[str] = []
    options: list[str] = []
    missing_links: list[str] = []
    unknown: list[str] = []
    if rows is None:
        return found, []
    iterable = rows.to_dict("records") if hasattr(rows, "to_dict") else list(rows)
    for row in iterable:
        if not isinstance(row, dict):
            row = dict(row)
        kind = _clean(row.get("種類"))
        name = _clean(row.get("名稱"))
        symbol = _clean(row.get("標的"))
        if not symbol:
            continue
        display = name or symbol
        if product_kind(kind):
            underlying = underlying_ticker_from_row(row)
            key, market = normalize_symbol(underlying)
            if key and not is_fund(key):
                _append(found, _item(key, market, display, True, f"可轉債／CBAS {symbol} 的標的 {key}", "holding"))
            else:
                market = market or _market_from_kind(kind)
                _append(found, _item("", market, display, False, f"{symbol} 沒有可靠的標的股票", "holding"))
                missing_links.append(symbol)
            continue
        future = resolve_stock_future(symbol) or resolve_stock_future(name)
        if future is not None:
            key, market = normalize_symbol(future.code)
            if key:
                _append(found, _item(key, market, display, True, f"個股期 {symbol} 對到 {key}", "holding"))
            else:
                _append(found, _item("", "TW", display, False, f"{symbol} 對不到四碼股票", "holding"))
                missing_links.append(symbol)
            continue
        if is_index_derivative(symbol) or is_index_derivative(name):
            _append(found, _item("", "TW", display, False, f"{symbol} 是指數期貨或台指選擇權", "holding"))
            indexes.append(symbol)
            continue
        if is_option_row(row):
            _append(found, _item("", _market_from_kind(kind), display, False, f"{symbol} 是選擇權", "holding"))
            options.append(symbol)
            continue
        key, market = normalize_symbol(symbol)
        if not key:
            unknown.append(symbol)
            continue
        if is_fund(key, kind, name):
            _append(found, _item(key, market, display, False, f"持倉 {symbol}", "holding"))
            funds.append(symbol)
            continue
        if "期" in kind:
            market = _market_from_kind(kind) or market
            _append(found, _item("", market, display, False, f"{symbol} 是期貨，沒有對到個股", "holding"))
            other_futures.append(symbol)
            continue
        _append(found, _item(key, market, display, True, f"持倉 {symbol}", "holding"))
    notes = []
    if funds:
        notes.append("這些是 ETF 或指數型基金，不排公司財報：" + "、".join(funds))
    if indexes:
        notes.append("這些是指數期貨或選擇權，沒有單一公司財報：" + "、".join(indexes))
    if other_futures:
        notes.append("這些期貨沒有對到個股，不排公司財報：" + "、".join(other_futures))
    if options:
        notes.append("這些選擇權沒有可靠的單一股票對照：" + "、".join(options))
    if missing_links:
        notes.append("這些可轉債、CBAS 或個股期沒有可靠標的：" + "、".join(missing_links))
    if unknown:
        notes.append("這些代號無法辨識：" + "、".join(unknown))
    return found, notes


def _item(key: str, market: str, display: str, earnings: bool, reason: str, source: str) -> dict:
    return {
        "key": key,
        "market": market,
        "display": display or key or "未命名",
        "earnings": earnings,
        "reasons": [reason] if reason else [],
        "source": source,
    }


def _market_from_kind(kind: str) -> str:
    text = str(kind or "")
    if "美" in text and "台" not in text:
        return "US"
    if "台" in text:
        return "TW"
    return ""


def watch_from_text(text: str) -> tuple[list[dict], list[str]]:
    found = []
    funds: list[str] = []
    indexes: list[str] = []
    unknown: list[str] = []
    raw = str(text or "").replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if is_index_derivative(token):
            indexes.append(token)
            continue
        key, market = normalize_symbol(token)
        if not key:
            unknown.append(token)
            continue
        if is_fund(key):
            found.append(_item(key, market, key, False, "觀察清單", "watch"))
            funds.append(key)
            continue
        found.append(_item(key, market, key, True, "觀察清單", "watch"))
    notes = []
    if funds:
        notes.append("觀察清單裡這些是 ETF，不排公司財報：" + "、".join(funds))
    if indexes:
        notes.append("觀察清單裡這些是指數商品，不排公司財報：" + "、".join(indexes))
    if unknown:
        notes.append("觀察清單裡這些無法辨識：" + "、".join(unknown))
    return found, notes


def earnings_symbols(items: list[dict]) -> list[str]:
    seen = []
    for item in items:
        if item.get("earnings") and item.get("key") and item["key"] not in seen:
            seen.append(item["key"])
    return seen


def session_from_timestamp(moment: datetime) -> dict:
    """美股時點換到台北，並標原交易時段。沒有時區的時間不猜測。"""
    if moment.tzinfo is None:
        return {"taipei_date": None, "taipei_text": "", "session": "時段未知", "note": "來源沒有時區，不把這個時間當成台北或美東。"}
    taipei = moment.astimezone(TAIPEI)
    eastern = moment.astimezone(NEW_YORK)
    minutes = eastern.hour * 60 + eastern.minute
    if minutes < 9 * 60 + 30:
        session = "美股盤前"
    elif minutes >= 16 * 60:
        session = "美股盤後"
    else:
        session = "美股盤中"
    return {
        "taipei_date": taipei.date(),
        "taipei_text": taipei.strftime("%Y-%m-%d %H:%M 台北"),
        "session": session,
        "note": f"美東 {eastern.strftime('%Y-%m-%d %H:%M')}。",
    }


def _blank_number(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in {"", "none", "null", "nan", "n/a", "na", "-"}:
            return None
        value = text
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_yahoo_calendar(symbol: str, calendar, market: str) -> dict | None:
    """Yahoo 日曆只當估計。沒有日期就回 None，不發明。"""
    when = None
    raw = None
    if isinstance(calendar, dict):
        for key, value in calendar.items():
            if "earnings" not in str(key).lower():
                continue
            raw = value[0] if isinstance(value, list) and value else value
            break
    elif hasattr(calendar, "index") and getattr(calendar, "empty", True) is False:
        for label in calendar.index:
            if "earnings" in str(label).lower():
                raw = calendar.loc[label].iloc[0]
                break
    if raw is None:
        return None
    if isinstance(raw, datetime):
        when = raw
    elif isinstance(raw, date):
        when = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            when = parsed
        except ValueError:
            return None
    if isinstance(when, datetime):
        clock = session_from_timestamp(when)
        if clock["taipei_date"] is None:
            event_date = when.date()
            session = "時段未知"
            when_text = event_date.isoformat()
            note = clock["note"]
        else:
            event_date = clock["taipei_date"]
            session = clock["session"]
            when_text = clock["taipei_text"]
            note = clock["note"]
    else:
        event_date = when
        session = "台股（來源未給時點）" if market == "TW" else "時段未知"
        when_text = event_date.isoformat()
        note = "來源只給日期，沒有盤前或盤後，不自行指定時段。"
    return {
        "date": event_date,
        "when_text": when_text,
        "session": session,
        "source": "Yahoo Finance 日曆",
        "certainty": "估計",
        "title": f"財報 {symbol}",
        "symbol": symbol,
        "market": market,
        "estimate": None,
        "previous": None,
        "actual": None,
        "note": note,
        "kind": "earnings",
    }


def monthly_settlement(year: int, month: int) -> dict:
    anchor = nth_weekday(year, month, 2, 3)
    if anchor is None:
        return {"date": None, "status": "未知", "note": "算不出第三個星期三。"}
    if year not in HOLIDAY_YEARS:
        return {
            "date": None,
            "anchor": anchor,
            "status": "未知",
            "note": f"未知。{year} 年沒有內建休市表，不把 {anchor.isoformat()} 直接列成有效結算日。",
        }
    if is_tw_trading_day(anchor):
        return {
            "date": anchor,
            "anchor": anchor,
            "status": "規則推估",
            "note": "第三個星期三，且不在證交所 115 年休市表。尚未逐筆核對期交所當月公告。",
        }
    return {
        "date": None,
        "anchor": anchor,
        "status": "未知",
        "note": f"未知。第三個星期三 {anchor.isoformat()} 落在休市日。提前或順延需期交所公告，這裡不自行指定日期。",
    }


def weekly_settlement_dates(start: date, end: date) -> tuple[list[dict], list[str]]:
    """非休市的星期三才列週選。假日的星期三略過，不改到別天。"""
    found = []
    skipped = []
    missing_years = set()
    day = start
    while day <= end:
        if day.weekday() == 2:
            if day.year not in HOLIDAY_YEARS:
                missing_years.add(day.year)
            elif is_tw_trading_day(day):
                found.append({
                    "date": day,
                    "status": "規則推估",
                    "note": "非休市的星期三。週選遇假日不自動改期。",
                })
            else:
                skipped.append(f"{day.isoformat()} 是休市星期三，不當成週選結算日，也不改到其他日期。")
        day += timedelta(days=1)
    for year in sorted(missing_years):
        skipped.append(f"{year} 年沒有內建休市表，該年的星期三不列入週選。")
    return found, skipped


def business_indicator_date(year: int, month: int) -> dict:
    try:
        anchor = date(year, month, 27)
    except ValueError:
        return {"date": None, "status": "未知", "note": "沒有 27 日。"}
    if year not in HOLIDAY_YEARS:
        return {"date": None, "anchor": anchor, "status": "未知", "note": f"未知。{year} 年沒有內建休市表，不硬編景氣燈號公布日。"}
    if is_tw_trading_day(anchor):
        return {"date": anchor, "anchor": anchor, "status": "慣例推估", "note": "國發會景氣對策信號慣例約每月 27 日，不是當月新聞稿確認日。"}
    return {"date": None, "anchor": anchor, "status": "未知", "note": f"未知。{anchor.isoformat()} 不是交易日，實際公布日可能延後，這裡不另編日期。"}


def institutional_events(start: date, end: date) -> tuple[list[dict], list[str]]:
    events = []
    notes = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        monthly = monthly_settlement(cursor.year, cursor.month)
        if monthly.get("date") and start <= monthly["date"] <= end:
            events.append({
                "date": monthly["date"],
                "when_text": monthly["date"].isoformat(),
                "session": "台股日盤",
                "source": "期交所契約規則＋證交所 115 年休市表",
                "certainty": monthly["status"],
                "title": "台指期、月選與個股期最後交易日（推估）",
                "symbol": "",
                "market": "TW",
                "estimate": None,
                "previous": None,
                "actual": None,
                "note": monthly["note"],
                "kind": "settlement",
            })
        elif monthly["status"] == "未知" and monthly.get("anchor") and start <= monthly["anchor"] <= end:
            notes.append(monthly["note"])
        signal = business_indicator_date(cursor.year, cursor.month)
        if signal.get("date") and start <= signal["date"] <= end:
            events.append({
                "date": signal["date"],
                "when_text": signal["date"].isoformat(),
                "session": "台股",
                "source": "國發會公布慣例",
                "certainty": signal["status"],
                "title": "國發會景氣對策信號（慣例推估）",
                "symbol": "",
                "market": "TW",
                "estimate": None,
                "previous": None,
                "actual": None,
                "note": signal["note"],
                "kind": "tw_macro",
            })
        elif signal["status"] == "未知" and signal.get("anchor") and start <= signal["anchor"] <= end:
            notes.append(signal["note"])
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    weekly, skipped = weekly_settlement_dates(start, end)
    notes.extend(skipped)
    for item in weekly:
        if any(event["kind"] == "settlement" and event["date"] == item["date"] for event in events):
            continue
        events.append({
            "date": item["date"],
            "when_text": item["date"].isoformat(),
            "session": "台股日盤",
            "source": "期交所週選慣例＋證交所 115 年休市表",
            "certainty": item["status"],
            "title": "台指選擇權週選最後交易日（推估）",
            "symbol": "",
            "market": "TW",
            "estimate": None,
            "previous": None,
            "actual": None,
            "note": item["note"],
            "kind": "weekly",
        })
    return events, notes


def parse_fmp_event(item: dict) -> dict | None:
    name = str(item.get("event") or "").strip()
    country = str(item.get("country") or "")
    if country not in {"US", "United States", "Global"}:
        return None
    lowered = name.lower()
    category = ""
    for label, keys in FMP_KEEP:
        if any(key in lowered for key in keys):
            category = label
            break
    if not category:
        return None
    raw_date = str(item.get("date") or "")
    try:
        if "T" in raw_date or " " in raw_date[10:]:
            moment = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                event_date = moment.date()
                when_text = event_date.isoformat()
                session = "時段未知"
                note = "來源時間沒有時區，不換算成台北，也不標盤前或盤後。"
            else:
                clock = session_from_timestamp(moment)
                event_date = clock["taipei_date"] or moment.date()
                when_text = clock["taipei_text"] or event_date.isoformat()
                session = clock["session"]
                note = clock["note"]
        else:
            event_date = date.fromisoformat(raw_date[:10])
            when_text = event_date.isoformat()
            session = "時段未知"
            note = "來源只給日期。"
    except ValueError:
        return None
    return {
        "date": event_date,
        "when_text": when_text,
        "session": session,
        "source": "FMP 經濟行事曆",
        "certainty": "來源提供",
        "title": name,
        "symbol": "",
        "market": "US" if category != "匯率" else "FX",
        "estimate": _blank_number(item.get("estimate")),
        "previous": _blank_number(item.get("previous")),
        "actual": _blank_number(item.get("actual")),
        "note": note,
        "kind": "fx" if category == "匯率" else ("rate" if category == "利率" else "us_macro"),
    }


def _names(items: list[dict], limit: int = 8) -> str:
    shown = [item["display"] for item in items[:limit]]
    text = "、".join(shown)
    if len(items) > limit:
        text += f" 等 {len(items)} 檔"
    return text


def impacts_for(event: dict, items: list[dict]) -> list[str]:
    """只標可能相關的持倉。不寫利多、利空或漲跌。觀察清單不是持倉。"""
    holdings = [item for item in items if item.get("source") == "holding"]
    watches = [item for item in items if item.get("source") == "watch"]
    symbol = event.get("symbol") or ""
    kind = event.get("kind")
    if kind == "earnings" and symbol:
        lines = []
        held = [item for item in holdings if item.get("key") == symbol]
        watched = [item for item in watches if item.get("key") == symbol]
        for item in held:
            side = "台股" if item["market"] == "TW" else "美股"
            lines.append(f"直接標的（持倉，{side}）：{item['display']}。{'；'.join(item['reasons'])}。這不是漲跌預測。")
        for item in watched:
            side = "台股" if item["market"] == "TW" else "美股"
            lines.append(f"觀察清單同一代號（{side}，不是持倉）：{item['display']}。")
        if not lines:
            return ["沒有對到這個直接標的。"]
        return lines
    if kind in {"settlement", "weekly", "tw_macro"}:
        tw = [item for item in holdings if item.get("market") == "TW"]
        if not tw:
            return ["沒有台股持倉。觀察清單與美股部位不併入台股制度事件。"]
        return [f"台股市場或制度相關（推論，不是預測）：{_names(tw)}。美股部位不併入這一列。"]
    if kind in {"rate", "us_macro"}:
        us = [item for item in holdings if item.get("market") == "US"]
        if not us:
            return ["沒有美股持倉。台股部位不併入美國利率或總經事件。"]
        return [f"美股或利率敏感（推論，不是預測）：{_names(us)}。台股部位不併入這一列。"]
    if kind == "fx":
        tw = [item for item in holdings if item.get("market") == "TW"]
        us = [item for item in holdings if item.get("market") == "US"]
        lines = ["匯率敏感（推論，不是預測）。台股與美股分開看，不做跨市場避險。"]
        lines.append("台股：" + (_names(tw) if tw else "沒有"))
        lines.append("美股：" + (_names(us) if us else "沒有"))
        return lines
    return ["沒有自動對到的部位。"]


class CalendarSourceError(RuntimeError):
    """來源失敗。呼叫端要顯示缺漏，不要改用寫死的日期。"""


def fetch_yahoo_calendar(symbol: str, factory=None):
    """抓 Yahoo 日曆。失敗丟 CalendarSourceError，讓快取不要記住這次失敗。"""
    maker = factory or _yahoo_ticker
    try:
        calendar = maker(symbol).calendar
    except Exception as exc:
        raise CalendarSourceError(f"{symbol} 日曆讀取失敗") from exc
    if calendar is None:
        raise CalendarSourceError(f"{symbol} 沒有日曆")
    return calendar


def _yahoo_ticker(symbol: str):
    import yfinance as yf

    return yf.Ticker(symbol)


def fetch_fmp_calendar(api_key: str, start: date, end: date, get=None):
    if not str(api_key or "").strip():
        raise CalendarSourceError("沒有 FMP_API_KEY")
    getter = get or _http_get
    url = (
        "https://financialmodelingprep.com/api/v3/economic_calendar"
        f"?from={start.isoformat()}&to={end.isoformat()}&apikey={api_key}"
    )
    try:
        response = getter(url, timeout=10)
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise CalendarSourceError(f"FMP 回應 {status}")
        payload = response.json()
    except CalendarSourceError:
        raise
    except Exception as exc:
        raise CalendarSourceError("FMP 連線失敗") from exc
    if not isinstance(payload, list):
        raise CalendarSourceError("FMP 回應不是事件清單")
    return payload


def _http_get(url: str, timeout: int = 10):
    import requests

    return requests.get(url, timeout=timeout)


def build_calendar(rows, watchlist: str, earnings_by_symbol: dict, fmp_items, today: date, fetched_at: datetime, horizon_days: int = 90, fmp_error: str = "") -> dict:
    """組第三頁用的事件。earnings_by_symbol 的值可以是日曆，或含 error 的 dict。"""
    end = today + timedelta(days=horizon_days)
    notes: list[str] = []
    gaps: list[str] = []
    if rows is None:
        holdings = []
        notes.append("持倉讀不到。台股制度事件仍列出，但對不到部位。")
    else:
        holdings, holding_notes = holdings_from_positions(rows)
        notes.extend(holding_notes)
    watches, watch_notes = watch_from_text(watchlist)
    notes.extend(watch_notes)
    items = holdings + watches
    symbols = earnings_symbols(items)
    if len(symbols) > MAX_EARNINGS_LOOKUPS:
        skipped = symbols[MAX_EARNINGS_LOOKUPS:]
        symbols = symbols[:MAX_EARNINGS_LOOKUPS]
        notes.append(f"財報查詢上限 {MAX_EARNINGS_LOOKUPS} 檔，略過：{'、'.join(skipped)}。")
    events = []
    institutional, institutional_notes = institutional_events(today, end)
    notes.extend(institutional_notes)
    events.extend(institutional)
    for symbol in symbols:
        payload = (earnings_by_symbol or {}).get(symbol)
        market = next((item["market"] for item in items if item.get("key") == symbol), "")
        if payload is None:
            gaps.append(f"{symbol} 尚未取得財報日。")
            continue
        if isinstance(payload, dict) and payload.get("error"):
            gaps.append(f"{symbol} 財報來源失敗：{payload['error']}")
            continue
        parsed = parse_yahoo_calendar(symbol, payload, market)
        if parsed is None or parsed.get("date") is None:
            gaps.append(f"{symbol} 來源沒有財報日期。")
            continue
        parsed["updated_at"] = fetched_at
        if not within(parsed, today, end):
            gaps.append(f"{symbol} 的日期 {parsed['date'].isoformat()} 不在未來 {horizon_days} 天內，不列成即將發生。")
            continue
        events.append(parsed)
    if fmp_error:
        notes.append(fmp_error)
    elif fmp_items is None:
        notes.append("沒有 FMP_API_KEY，美國利率、總經與匯率事件不可用。不會改用寫死日期。")
    else:
        for raw in fmp_items:
            parsed = parse_fmp_event(raw) if isinstance(raw, dict) else None
            if parsed is None or not within(parsed, today, end):
                continue
            parsed["updated_at"] = fetched_at
            events.append(parsed)
    events.sort(key=lambda item: (item["date"], item["title"]))
    for event in events:
        event["impacts"] = impacts_for(event, items)
        event.setdefault("updated_at", fetched_at)
    return {"events": events, "notes": notes, "gaps": gaps, "symbols": symbols, "holdings": holdings, "not_done": list(NOT_DONE)}


def format_number(value) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def within(event: dict, start: date, end: date) -> bool:
    day = event.get("date")
    return isinstance(day, date) and start <= day <= end
