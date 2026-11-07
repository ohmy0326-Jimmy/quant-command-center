"""停損分級與「首次觀察日」的說法。第四頁、第六頁、Telegram 共用。

試算表有寫、而且是有效停損時，用試算表。空白、NaN、非數字、或舊的 −100 哨兵，
才依商品給預設：個股 −10%、2 倍 ETF −15%、3 倍 ETF −20%。
期貨、選擇權、可轉債、CBAS、權證不套個股預設。

已觀察天數只在呼叫端拿出持久化的日期時才算。沒有日期就說「首次觸發日期未記錄」，
不把今天補成第一次。
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

UNSET_STOP = -100.0
EQUITY_STOP = -10.0
ETF_2X_STOP = -15.0
ETF_3X_STOP = -20.0
UNRECORDED_BREACH = "首次觸發日期未記錄"

# 台股槓桿 ETF 不在 beta_rules.LEVERAGED_UNDERLYING 裡，倍數寫在這裡。
TW_LEVERAGED_MULTIPLE = {"00631L": 2.0, "00663L": 2.0}


def explicit_stop(value) -> float | None:
    """有效的試算表停損（百分點，負數到 0）。−100、空白、NaN、正數都不是有效值。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("%", "").replace("％", "")
        if text in ("", "未設定", "nan", "NaN", "None", "—", "-", "不適用"):
            return None
        value = text
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number <= UNSET_STOP + 1e-9 or number > 0:
        return None
    return number


def _ticker_key(ticker) -> str:
    text = str(ticker or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def _listed_multiple(ticker) -> float | None:
    from beta_rules import LEVERAGED_UNDERLYING

    key = _ticker_key(ticker)
    if key in LEVERAGED_UNDERLYING:
        return float(LEVERAGED_UNDERLYING[key][1])
    if key in TW_LEVERAGED_MULTIPLE:
        return TW_LEVERAGED_MULTIPLE[key]
    return None


def _sheet_multiple(leverage) -> float:
    try:
        if leverage is None or pd.isna(leverage):
            return 1.0
    except TypeError:
        pass
    try:
        number = float(leverage)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(number) or number <= 0:
        return 1.0
    return number


def _skips_equity_default(kind, ticker, name) -> bool:
    from cb_holdings import is_convertible_row
    from portfolio_risk import is_option_row

    row = {"種類": kind, "名稱": name, "標的": ticker}
    if is_convertible_row(row) or is_option_row(row):
        return True
    text = str(kind or "")
    return ("期" in text) or ("權證" in text)


def default_stop(kind, ticker, leverage, name="") -> tuple[float | None, str]:
    """沒有有效試算表停損時的預設。(停損百分點或 None, 來源說明)。"""
    if _skips_equity_default(kind, ticker, name):
        return None, "未設定"
    multiple = _listed_multiple(ticker)
    if multiple is None:
        multiple = _sheet_multiple(leverage)
    if multiple >= 2.5:
        return ETF_3X_STOP, "預設（3 倍 ETF −20%）"
    if multiple >= 1.5:
        return ETF_2X_STOP, "預設（2 倍 ETF −15%）"
    return EQUITY_STOP, "預設（個股 −10%）"


def apply_stop_defaults(frame: pd.DataFrame) -> pd.DataFrame:
    """寫入生效的「最大虧損比例」與「停損來源」。已有「停損原值」就沿用，避免覆寫後再套一次。"""
    if frame is None or frame.empty:
        return frame
    if "停損原值" not in frame.columns:
        if "最大虧損比例" in frame.columns:
            frame["停損原值"] = frame["最大虧損比例"]
        else:
            frame["停損原值"] = np.nan
    stops: list[float] = []
    sources: list[str] = []
    for _, row in frame.iterrows():
        chosen = explicit_stop(row.get("停損原值"))
        if chosen is not None:
            stops.append(chosen)
            sources.append("試算表")
            continue
        fallback, source = default_stop(row.get("種類"), row.get("標的"), row.get("標的槓桿"), row.get("名稱", ""))
        if fallback is None:
            stops.append(UNSET_STOP)
            sources.append(source)
        else:
            stops.append(fallback)
            sources.append(source)
    frame["最大虧損比例"] = stops
    frame["停損來源"] = sources
    return frame


def breach_age_label(first_seen, today: date) -> str:
    """有持久化日期才算天數。沒有、或日期比今天還晚，都說未記錄。"""
    if first_seen is None:
        return UNRECORDED_BREACH
    if isinstance(first_seen, date):
        seen = first_seen
    else:
        text = str(first_seen).strip()
        if not text:
            return UNRECORDED_BREACH
        try:
            seen = date.fromisoformat(text[:10])
        except ValueError:
            return UNRECORDED_BREACH
    days = (today - seen).days
    if days < 0:
        return UNRECORDED_BREACH
    if days == 0:
        return "今天首次觀察到，尚未跨日"
    return f"已觀察 {days} 天（自 {seen.isoformat()}）"


def stop_alarm_text(name: str, ticker: str, roi: float, limit: float, source: str, first_seen, today: date) -> str:
    src = f"，{source}" if source else ""
    return (
        f"{name} ({ticker})：報酬 {roi:.2f}% 已低於停損 {limit:.0f}%{src}。"
        f"{breach_age_label(first_seen, today)}"
    )
