"""Google Sheet 的金額與數量。

試算表把儲存格設成貨幣格式時，gspread 預設的 FORMATTED_VALUE 會回
'NT$305,430'、'$1,234.5' 這類字串。pandas.to_numeric 讀不懂就變成 NaN，
再被填成 0，借款就從淨值裡消失。

空白（沒填、破折號）和解析失敗要分開。失敗不可當成 0。
"""

from __future__ import annotations

import math
import unicodedata

import pandas as pd

# 影響帳戶淨值的欄。解析失敗時呼叫端要警告，不可填 0。
NAV_AMOUNT_COLUMNS = ("現金餘額", "融資/質押借款")

_BLANK_WORDS = {"", "nan", "none", "null", "n/a", "#n/a", "na", "-"}
_DASHES = set("-—–－―~～")
_CURRENCY = ("NT$", "US$", "TWD", "NTD", "USD")


def classify_sheet_number(value) -> tuple[str, float | None]:
    """回傳 (狀態, 數字)。狀態是 ok、blank、invalid。

    ok 才有數字。blank 是空儲存格或破折號。invalid 是有內容但讀不成數字。
    """
    if isinstance(value, bool):
        return "invalid", None
    if value is None:
        return "blank", None
    try:
        if pd.isna(value):
            return "blank", None
    except TypeError:
        pass
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return "blank", None
        return "ok", number

    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u3000", " ").strip()
    if not text or text.lower() in _BLANK_WORDS or all(ch in _DASHES or ch.isspace() for ch in text):
        return "blank", None

    negative = False
    if len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        negative = True
        text = text[1:-1].strip()
    for token in _CURRENCY:
        text = text.replace(token, "")
    text = (
        text.replace("$", "")
        .replace(",", "")
        .replace("，", "")
        .replace(" ", "")
        .replace("\u00a0", "")
    )
    if not text or all(ch in _DASHES for ch in text):
        return "blank", None
    try:
        number = float(text)
    except ValueError:
        return "invalid", None
    if math.isnan(number) or math.isinf(number):
        return "invalid", None
    if negative:
        number = -abs(number)
    return "ok", number


def parse_sheet_number(value) -> float | None:
    """讀得成數字才回傳。空白或失敗都是 None。"""
    status, number = classify_sheet_number(value)
    return number if status == "ok" else None


def sheet_number_snippet(value) -> str:
    text = unicodedata.normalize("NFKC", str(value)).replace("\n", " ").strip()
    text = " ".join(text.split())
    if len(text) > 40:
        return text[:40] + "…"
    return text or "空白"


def read_worksheet_records(worksheet) -> list[dict]:
    """用未格式化的儲存格值。貨幣格式才會是數字，而不是 'NT$305,430'。

    舊版 gspread 不吃 value_render_option 時退回原本的讀法，字串交給 parse_sheet_number。
    """
    getter = getattr(worksheet, "get_all_records", None)
    if getter is None:
        return []
    try:
        rows = getter(value_render_option="UNFORMATTED_VALUE")
    except TypeError:
        rows = getter()
    return list(rows or [])


def _already_numeric(series: pd.Series) -> bool:
    return bool(pd.api.types.is_numeric_dtype(series))


def coerce_amount_columns(frame: pd.DataFrame, blank_defaults: dict[str, float | None]) -> list[str]:
    """把金額、數量欄轉成 float。

    blank_defaults 的值是空白要填的數字；None 表示空白留 NaN。
    已經是數字欄就不動（第二次呼叫不會把「讀失敗的 NaN」填回預設值）。
    有內容但讀不成數字：留 NaN，並回警告。不填 0。
    """
    if frame is None or frame.empty:
        return []
    warnings: list[str] = []
    invalid = frame.attrs.get("sheet_number_invalid_cells")
    if not isinstance(invalid, set):
        invalid = set()
        frame.attrs["sheet_number_invalid_cells"] = invalid
    for column, blank_value in blank_defaults.items():
        if column not in frame.columns:
            continue
        series = frame[column]
        if _already_numeric(series):
            # 整數欄之後會寫入報價、損益這些小數。先改成 float，避免寫不進去。
            # 空白的 NaN 填回預設；上一輪已判定讀失敗的格子維持 NaN，不能在這裡變回 0。
            numbers = pd.to_numeric(series, errors="coerce").astype("float64")
            if blank_value is not None:
                failed = [idx for idx in numbers.index if (column, idx) in invalid and pd.isna(numbers.at[idx])]
                numbers = numbers.fillna(float(blank_value))
                for idx in failed:
                    numbers.at[idx] = float("nan")
            frame[column] = numbers
            continue
        parsed: list[float] = []
        for idx, value in series.items():
            status, number = classify_sheet_number(value)
            if status == "ok":
                parsed.append(float(number))
                continue
            if status == "blank":
                parsed.append(float("nan") if blank_value is None else float(blank_value))
                continue
            parsed.append(float("nan"))
            invalid.add((column, idx))
            where = _row_where(frame, idx)
            warnings.append(f"{where}的「{column}」無法讀成數字（{sheet_number_snippet(value)}）。沒有把它當成 0。")
        frame[column] = pd.Series(parsed, index=series.index, dtype="float64")
    return warnings


def coerce_nav_amounts(frame: pd.DataFrame) -> list[str]:
    """現金與借款。空白維持 0（這格沒填）。讀失敗留 NaN，並說明這戶淨值未計入。"""
    warnings = coerce_amount_columns(frame, {column: 0.0 for column in NAV_AMOUNT_COLUMNS})
    return [item.replace("沒有把它當成 0。", "這戶淨值未計入，沒有把這欄當成 0。") for item in warnings]


def _row_where(frame: pd.DataFrame, idx) -> str:
    parts: list[str] = []
    for column in ("帳戶名稱", "帳戶", "標的", "商品代號", "名稱"):
        if column not in frame.columns:
            continue
        text = str(frame.at[idx, column]).strip()
        if text and text.lower() != "nan":
            parts.append(text)
    if parts:
        return " ".join(dict.fromkeys(parts))
    return f"第 {idx} 列"
