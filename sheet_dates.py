"""Google 試算表日期。

Sheets 會把日期存成序號（1899-12-30 起算的天數）。
`pandas.to_datetime` 直接吃這個數字會變成 1970 年，歷史淨值圖就會錯。
讀取時轉成日期；寫入時用 yyyy-mm-dd，並把欄位設成日期格式。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

TAIPEI = ZoneInfo("Asia/Taipei")
SHEETS_EPOCH = date(1899, 12, 30)
# 約 1954–2119。八位數 YYYYMMDD 遠大於這個區間，不會被當成序號。
_SERIAL_MIN = 20000
_SERIAL_MAX = 80000
NAV_DATE_FORMAT = {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}


def taipei_today(today: date | None = None) -> date:
    if today is not None:
        return today
    return datetime.now(TAIPEI).date()


def parse_sheet_date(value) -> date | None:
    """把儲存格轉成日期。序號、YYYYMMDD、一般日期字串都可以。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.date()
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass

    number = _as_number(value)
    if number is not None:
        if _SERIAL_MIN <= number <= _SERIAL_MAX:
            return SHEETS_EPOCH + timedelta(days=int(math.floor(number)))
        if 19900101 <= number <= 21001231 and abs(number - round(number)) < 1e-6:
            text = f"{int(round(number)):08d}"
            try:
                return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return None

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat", "-"}:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def format_sheet_date(value) -> str:
    """寫回試算表用的日期字串。Sheets 以 USER_ENTERED 吃 yyyy-mm-dd 會存成日期。"""
    parsed = parse_sheet_date(value)
    if parsed is None:
        raise ValueError(f"無法轉成日期：{value!r}")
    return parsed.isoformat()


def _as_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def normalize_nav_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily_NAV 的日期欄改成 yyyy-mm-dd 字串。其他欄不動。"""
    if frame is None or frame.empty or "日期" not in frame.columns:
        return frame
    out = frame.copy()
    out["日期"] = [(_iso(parse_sheet_date(value)) or value) for value in out["日期"]]
    return out


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def nav_growth_series(nav_data: pd.DataFrame, today_nav: float, today: date | None = None) -> pd.Series:
    """歷史淨值曲線。序號會先轉成日期，今天的點用呼叫端算好的淨值蓋上。

    回傳的索引是 yyyy-mm-dd，由舊到新。只改記憶體，不寫回試算表。
    """
    today = taipei_today(today)
    rows: list[tuple[date, float]] = []
    if nav_data is not None and not nav_data.empty and "日期" in nav_data.columns and "總淨值" in nav_data.columns:
        values = pd.to_numeric(nav_data["總淨值"], errors="coerce")
        for raw_day, raw_nav in zip(nav_data["日期"], values):
            day = parse_sheet_date(raw_day)
            if day is None or pd.isna(raw_nav):
                continue
            rows.append((day, float(raw_nav)))
    rows = [(day, nav) for day, nav in rows if day != today]
    rows.append((today, float(today_nav)))
    rows.sort(key=lambda item: item[0])
    # 同一天留最後一筆（試算表若有重複列）。
    dedup: dict[date, float] = {}
    for day, nav in rows:
        dedup[day] = nav
    ordered = sorted(dedup.items())
    index = pd.Index([day.isoformat() for day, _nav in ordered], name="日期")
    return pd.Series([nav for _day, nav in ordered], index=index, name="總淨值")


def write_daily_nav(worksheet, when, total_nav) -> dict:
    """寫入或覆蓋 Daily_NAV 的一天。

    日期以 USER_ENTERED 的 yyyy-mm-dd 寫入，並把 A 欄設成日期格式，
    避免再存成序號。已有同一天（含序號）就蓋掉該列。
    """
    day = format_sheet_date(when)
    existing = worksheet.get_all_values() or []
    if not existing:
        worksheet.update("A1:B1", [["日期", "總淨值"]], value_input_option="USER_ENTERED")
        existing = [["日期", "總淨值"]]
    target = None
    for offset, row in enumerate(existing[1:], start=2):
        cell = row[0] if row else ""
        parsed = parse_sheet_date(cell)
        if parsed is not None and parsed.isoformat() == day:
            target = offset
            break
    if target is None:
        target = len(existing) + 1
    written = [day, float(total_nav)]
    worksheet.update(f"A{target}:B{target}", [written], value_input_option="USER_ENTERED")
    if hasattr(worksheet, "format"):
        worksheet.format("A2:A5000", NAV_DATE_FORMAT)
    return {"row": target, "values": written, "value_input_option": "USER_ENTERED"}


def rewrite_daily_nav_dates(worksheet) -> int:
    """把 Daily_NAV 日期欄裡的序號改寫成 yyyy-mm-dd，並套上日期格式。

    給維護試算表用，戰情室讀取時不會自動呼叫，避免每次開頁面就改表。
    """
    existing = worksheet.get_all_values() or []
    changed = 0
    for offset, row in enumerate(existing[1:], start=2):
        if not row:
            continue
        parsed = parse_sheet_date(row[0])
        if parsed is None:
            continue
        text = parsed.isoformat()
        if str(row[0]).strip() == text:
            continue
        worksheet.update(f"A{offset}", [[text]], value_input_option="USER_ENTERED")
        changed += 1
    if hasattr(worksheet, "format"):
        worksheet.format("A2:A5000", NAV_DATE_FORMAT)
    return changed
