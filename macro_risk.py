"""第一頁「宏觀市場風險計分板」的抓取與計分。

每個指標回傳 value、date、status（ok / missing / stale）、points 等欄位。
抓不到、空表、全是 NaN 一律標「資料缺漏」，不當成 0 分的「沒觸發」。
計分函式不連網，方便測試；抓取函式在抓不到時丟例外，讓 st.cache_data 不快取失敗結果。
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ---- 總分與燈號 ----
# 原本兩項指標滿分 6：0–1 綠、2–3 黃、4–6 紅。
# 新增三項台灣指標後滿分 11。依原本界線的中點等比例換算：
#   綠/黃界線 1.5/6 × 11 ≈ 2.75 → 0–2 綠；黃/紅界線 3.5/6 × 11 ≈ 6.42 → 7 分以上紅。
MAX_SCORE = 11
GREEN_MAX = 2
YELLOW_MAX = 6

# 最後一筆資料距今超過這麼多個工作日，視為過期。
STALE_BUSINESS_DAYS = 3

# Yahoo ^TNX 有兩種報價：舊制是殖利率 × 10（例如 49.25），新制直接是百分比（4.925）。
# 現代 10 年債殖利率沒站上過 20%，原始報價 > 20 才當舊制除以 10。
TNX_INDEX_QUOTE_THRESHOLD = 20.0

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_STALE = "stale"
STATUS_LABELS = {STATUS_OK: "正常", STATUS_MISSING: "資料缺漏", STATUS_STALE: "資料過期"}

NY = ZoneInfo("America/New_York")
TPE = ZoneInfo("Asia/Taipei")

FOREIGN_ROW_NAME = "外資及陸資(不含外資自營商)"
FOREIGN_SELL_THRESHOLD = 50e9  # 單日賣超新台幣 500 億
FOREIGN_STREAK_DAYS = 3
TWSE_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"

INDICATOR_SPECS = {
    "vix": {
        "name": "VIX 恐慌指數",
        "max_points": 4,
        "threshold": "> 20：+2；> 25：+4",
        "source": "Yahoo ^VIX",
    },
    "tnx": {
        "name": "美國 10 年公債殖利率",
        "max_points": 2,
        "threshold": "> 4.5%：+2",
        "source": "Yahoo ^TNX",
    },
    "foreign": {
        "name": "外資買賣超（上市）",
        "max_points": 2,
        "threshold": "單日賣超 > 500 億，或連續賣超 ≥ 3 天：+2",
        "source": "TWSE BFI82U 三大法人",
    },
    "usdtwd": {
        "name": "美元/台幣",
        "max_points": 1,
        "threshold": "5 日升 > 1%，或高於 20 日均線 > 1%：+1",
        "source": "Yahoo USDTWD=X",
    },
    "taiex": {
        "name": "台灣加權指數",
        "max_points": 2,
        "threshold": "盤中低點較昨收跌 ≥ 3%、5 日跌 ≥ 5%，或低於 20 日均線 > 5%：+2",
        "source": "Yahoo ^TWII",
    },
}
INDICATOR_ORDER = ["vix", "tnx", "foreign", "usdtwd", "taiex"]
# 這些指標缺漏時，綠燈改顯示灰色「資料不足」。目前全部五項都算核心。
CORE_KEYS = frozenset(INDICATOR_ORDER)

assert sum(INDICATOR_SPECS[k]["max_points"] for k in INDICATOR_ORDER) == MAX_SCORE


# ---------------------------------------------------------------- 基本工具
def tnx_quote_to_yield_percent(raw_close) -> float:
    """單筆 Yahoo ^TNX 報價轉成百分比殖利率，相容舊制（× 10）與新制（百分比）。"""
    value = float(raw_close)
    if value > TNX_INDEX_QUOTE_THRESHOLD:
        return value / 10.0
    return value


def _finite(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def clean_frame(frame, columns=("Close",)) -> pd.DataFrame:
    """留下指定欄位都是有效數字的列。沒有欄位或沒有資料時回空表。"""
    if frame is None or getattr(frame, "empty", True):
        return pd.DataFrame()
    if any(col not in frame.columns for col in columns):
        return pd.DataFrame()
    out = frame.loc[:, list(columns)].apply(pd.to_numeric, errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    return out


def bar_date(ts) -> date:
    """K 線的交易日（依交易所時區的日期）。"""
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return pd.Timestamp(ts).date()


def business_days_between(earlier: date, later: date) -> int:
    if later <= earlier:
        return 0
    return int(np.busday_count(earlier, later))


def freshness(data_date: date, today: date) -> str:
    if business_days_between(data_date, today) > STALE_BUSINESS_DAYS:
        return STATUS_STALE
    return STATUS_OK


def _base(key: str) -> dict:
    spec = INDICATOR_SPECS[key]
    return {
        "key": key,
        "name": spec["name"],
        "threshold": spec["threshold"],
        "source": spec["source"],
        "max_points": spec["max_points"],
        "core": key in CORE_KEYS,
        "value": None,
        "value_text": "—",
        "date": None,
        "points": 0,
        "status": STATUS_MISSING,
        "triggered": [],
        "note": "",
    }


def missing(key: str, note: str = "") -> dict:
    item = _base(key)
    item["note"] = note or "抓不到資料"
    return item


def _finish(item: dict, data_date: date, today: date, points: int) -> dict:
    item["date"] = data_date.isoformat()
    item["status"] = freshness(data_date, today)
    if item["status"] == STATUS_OK:
        item["points"] = int(points)
    else:
        item["points"] = 0
        item["note"] = (item["note"] + "；" if item["note"] else "") + f"最新資料 {item['date']}，超過 {STALE_BUSINESS_DAYS} 個工作日，不計分"
    return item


# ---------------------------------------------------------------- 各指標計分
def us_cash_session_open(now: datetime) -> bool:
    """美股現貨盤（含 VIX 收盤 16:15）是否進行中。不處理美國假日。"""
    ny = now.astimezone(NY)
    if ny.weekday() >= 5:
        return False
    minutes = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= minutes < 16 * 60 + 15


def score_vix(frame, now: datetime) -> dict:
    item = _base("vix")
    data = clean_frame(frame)
    if data.empty:
        return missing("vix", "^VIX 沒有有效收盤價")
    closes = data["Close"]
    latest = float(closes.iloc[-1])
    latest_date = bar_date(closes.index[-1])
    ny_now = now.astimezone(NY)
    value, used_date = latest, latest_date
    if not us_cash_session_open(now) and len(closes) >= 2:
        pre_open_today = latest_date == ny_now.date() and (ny_now.hour * 60 + ny_now.minute) < 9 * 60 + 30
        if pre_open_today:
            prev = float(closes.iloc[-2])
            if prev >= latest:
                value, used_date = prev, bar_date(closes.index[-2])
            item["note"] = f"美股未開盤：取前一交易日收盤 {prev:.2f} 與最新報價 {latest:.2f} 較大者"
    elif us_cash_session_open(now):
        item["note"] = "美股盤中即時值"
    item["value"] = value
    item["value_text"] = f"{value:.2f}"
    points = 0
    if value > 25:
        points = 4
        item["triggered"].append(f"🚨 VIX 市場極度恐慌（{value:.2f} > 25）")
    elif value > 20:
        points = 2
        item["triggered"].append(f"⚠️ VIX 恐慌指數偏高（{value:.2f} > 20）")
    return _finish(item, used_date, ny_now.date(), points)


def score_tnx(frame, today: date) -> dict:
    item = _base("tnx")
    data = clean_frame(frame)
    if data.empty:
        return missing("tnx", "^TNX 沒有有效收盤價")
    raw = float(data["Close"].iloc[-1])
    value = tnx_quote_to_yield_percent(raw)
    item["value"] = value
    item["value_text"] = f"{value:.2f}%"
    points = 0
    if value > 4.5:
        points = 2
        item["triggered"].append(f"⚠️ 10 年債殖利率處於高檔（{value:.2f}% > 4.5%）")
    return _finish(item, bar_date(data.index[-1]), today, points)


def score_usdtwd(frame, today: date) -> dict:
    item = _base("usdtwd")
    data = clean_frame(frame)
    if len(data) < 21:
        return missing("usdtwd", f"USDTWD=X 有效資料只有 {len(data)} 筆，算不出 20 日均線")
    closes = data["Close"]
    last = float(closes.iloc[-1])
    rise_5d = (last / float(closes.iloc[-6]) - 1) * 100
    ma20 = float(closes.tail(20).mean())
    above_ma = (last / ma20 - 1) * 100
    item["value"] = last
    item["value_text"] = f"{last:.3f}（5 日 {rise_5d:+.2f}%，距 20MA {above_ma:+.2f}%）"
    points = 0
    reasons = []
    if rise_5d > 1:
        reasons.append(f"5 日升 {rise_5d:.2f}%")
    if above_ma > 1:
        reasons.append(f"高於 20 日均線 {above_ma:.2f}%")
    if reasons:
        points = 1
        item["triggered"].append("⚠️ 台幣急貶（" + "、".join(reasons) + "）")
    return _finish(item, bar_date(closes.index[-1]), today, points)


def score_taiex(frame, today: date) -> dict:
    item = _base("taiex")
    data = clean_frame(frame, ("Low", "Close"))
    if len(data) < 21:
        return missing("taiex", f"^TWII 有效資料只有 {len(data)} 筆，算不出 20 日均線")
    closes = data["Close"]
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    low = float(data["Low"].iloc[-1])
    intraday = (low / prev - 1) * 100
    drop_5d = (last / float(closes.iloc[-6]) - 1) * 100
    ma20 = float(closes.tail(20).mean())
    vs_ma = (last / ma20 - 1) * 100
    item["value"] = last
    item["value_text"] = f"{last:,.0f}（盤中低 {intraday:+.2f}%，5 日 {drop_5d:+.2f}%，距 20MA {vs_ma:+.2f}%）"
    reasons = []
    if intraday <= -3:
        reasons.append(f"盤中較昨收跌 {abs(low - prev):,.0f} 點（{intraday:.2f}%）")
    if drop_5d <= -5:
        reasons.append(f"5 日跌 {abs(drop_5d):.2f}%")
    if vs_ma < -5:
        reasons.append(f"低於 20 日均線 {abs(vs_ma):.2f}%")
    points = 0
    if reasons:
        points = 2
        item["triggered"].append("🚨 台股重挫（" + "、".join(reasons) + "）")
    return _finish(item, bar_date(closes.index[-1]), today, points)


def parse_bfi82u(payload) -> float | None:
    """回傳外資及陸資（不含外資自營商）買賣差額（元）。沒有資料回 None。"""
    if not isinstance(payload, dict) or payload.get("stat") != "OK":
        return None
    for row in payload.get("data") or []:
        if row and str(row[0]).strip() == FOREIGN_ROW_NAME and len(row) >= 4:
            try:
                value = float(str(row[3]).replace(",", "").strip())
            except ValueError:
                return None
            return value if math.isfinite(value) else None
    return None


def score_foreign(flows: dict, today: date) -> dict:
    """flows = {"days": [(date, 淨買賣元), ...] 新到舊, "complete": bool}。"""
    item = _base("foreign")
    days = [(d, float(v)) for d, v in (flows or {}).get("days") or [] if _finite(v)]
    if not days:
        return missing("foreign", "TWSE 沒有可用的三大法人資料")
    days.sort(key=lambda pair: pair[0], reverse=True)
    latest_date, latest_net = days[0]
    streak = 0
    for _, net in days:
        if net < 0:
            streak += 1
        else:
            break
    capped = streak == len(days) and len(days) > 0
    streak_text = f"{streak}{'+' if capped else ''}"
    item["value"] = latest_net
    side = "賣超" if latest_net < 0 else "買超"
    item["value_text"] = f"{side} {abs(latest_net) / 1e8:,.1f} 億（連續賣超 {streak_text} 天）"
    if not (flows or {}).get("complete", True):
        item["note"] = "部分日期抓取失敗，連續天數可能低估"
    reasons = []
    if latest_net < -FOREIGN_SELL_THRESHOLD:
        reasons.append(f"單日賣超 {abs(latest_net) / 1e8:,.1f} 億")
    if streak >= FOREIGN_STREAK_DAYS:
        reasons.append(f"連續賣超 {streak_text} 天")
    points = 0
    if reasons:
        points = 2
        item["triggered"].append("🚨 外資大舉撤出（" + "、".join(reasons) + "）")
    return _finish(item, latest_date, today, points)


# ---------------------------------------------------------------- 彙總
def summarize(indicators: list[dict]) -> dict:
    score = sum(int(i["points"]) for i in indicators if i["status"] == STATUS_OK)
    usable_max = sum(int(i["max_points"]) for i in indicators if i["status"] == STATUS_OK)
    unusable = [i for i in indicators if i["status"] != STATUS_OK]
    core_gap = any(i["core"] for i in unusable)
    if score > YELLOW_MAX:
        level, label = "red", "🔴 市場氣氛：【高風險警戒 (Risk-Off)】"
    elif score > GREEN_MAX:
        level, label = "yellow", "🟡 市場氣氛：【中性觀望 / 局部震盪】"
    elif core_gap:
        level, label = "grey", "⚪ 市場氣氛：【資料不足】"
    else:
        level, label = "green", "🟢 市場氣氛：【風險偏好 (Risk-On)】"
    text = f"{score} / {usable_max} 分"
    if unusable:
        text += f"（{len(unusable)} 項資料缺漏）"
    return {
        "score": score,
        "usable_max": usable_max,
        "max_score": MAX_SCORE,
        "missing_count": len(unusable),
        "missing_names": [i["name"] for i in unusable],
        "level": level,
        "label": label,
        "score_text": text,
        "triggered": [t for i in indicators if i["status"] == STATUS_OK for t in i["triggered"]],
    }


def evaluate(loaders: dict, now: datetime | None = None) -> tuple[list[dict], dict]:
    """loaders：key → 無參數函式，回傳該指標原始資料。任何例外都記成資料缺漏。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    scorers = {
        "vix": lambda raw: score_vix(raw, now),
        "tnx": lambda raw: score_tnx(raw, now.astimezone(NY).date()),
        "foreign": lambda raw: score_foreign(raw, now.astimezone(TPE).date()),
        "usdtwd": lambda raw: score_usdtwd(raw, now.astimezone(TPE).date()),
        "taiex": lambda raw: score_taiex(raw, now.astimezone(TPE).date()),
    }
    indicators = []
    for key in INDICATOR_ORDER:
        loader = loaders.get(key)
        if loader is None:
            indicators.append(missing(key, "沒有設定資料來源"))
            continue
        try:
            raw = loader()
        except Exception as exc:  # 抓取失敗 → 資料缺漏，不當 0 分
            indicators.append(missing(key, f"抓取失敗：{type(exc).__name__}"))
            continue
        try:
            indicators.append(scorers[key](raw))
        except Exception as exc:
            indicators.append(missing(key, f"資料格式無法計分：{type(exc).__name__}"))
    return indicators, summarize(indicators)


def table_rows(indicators: list[dict]) -> list[dict]:
    return [
        {
            "指標": i["name"],
            "數值": i["value_text"],
            "資料日期": i["date"] or "—",
            "門檻": i["threshold"],
            "得分": f"{i['points']} / {i['max_points']}" if i["status"] == STATUS_OK else f"— / {i['max_points']}",
            "來源": i["source"],
            "狀態": STATUS_LABELS[i["status"]] + (f"：{i['note']}" if i["note"] else ""),
        }
        for i in indicators
    ]


# ---------------------------------------------------------------- 連網抓取（失敗丟例外）
def fetch_history(ticker: str, period: str) -> pd.DataFrame:
    import yfinance as yf

    frame = yf.Ticker(ticker).history(period=period)
    if clean_frame(frame).empty:
        raise ValueError(f"{ticker} 沒有有效資料")
    return frame


def fetch_foreign_flows(today: date | None = None, trading_days: int = 6, max_calendar_days: int = 14,
                        session=None, pause: float = 0.3) -> dict:
    """從今天往回抓 TWSE BFI82U，收集最多 trading_days 個交易日的外資買賣差額。

    假日回「沒有符合條件的資料」→ 跳過。連線錯誤 → 停止往回抓，標記 complete=False。
    一天都沒抓到就丟例外（不讓失敗結果被快取）。
    """
    import requests

    http = session or requests.Session()
    today = today or datetime.now(TPE).date()
    days: list[tuple[date, float]] = []
    complete = True
    for offset in range(max_calendar_days):
        if len(days) >= trading_days:
            break
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            response = http.get(
                TWSE_BFI82U_URL,
                params={"type": "day", "dayDate": day.strftime("%Y%m%d"), "response": "json"},
                headers={"User-Agent": "quant-command-center/1.0"},
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            complete = False
            break
        net = parse_bfi82u(payload)
        if net is not None:
            days.append((day, net))
        if pause:
            time.sleep(pause)
    if not days:
        raise ValueError("TWSE BFI82U 沒有抓到任何交易日資料")
    return {"days": days, "complete": complete}
