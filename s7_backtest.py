"""S7：近 10 日 VIX 高點 > 35，且當天從該高點回落至少 15%。

只根據傳入的收盤序列列事件。沒有序列就不產生日期。
之後 5／20／60 日如果不夠長，報酬和最大回撤是 None，不當 0。
"""

from __future__ import annotations

from datetime import date

PEAK_MIN = 35.0
DROP = 0.15
LOOKBACK = 10
HORIZONS = (5, 20, 60)


def s7_events(vix_rows: list[dict], price_rows: list[dict], *, market: str) -> dict:
    """vix_rows / price_rows 是 {date, close}。日期要能對上，否則該市場不完整。"""
    vix = _series(vix_rows)
    price = _series(price_rows)
    if not vix or not price:
        return {
            "market": market,
            "ok": False,
            "note": "資料不足，沒有列出事件。",
            "events": [],
        }
    days = [day for day in vix if day in price]
    if len(days) < LOOKBACK:
        return {
            "market": market,
            "ok": False,
            "note": f"資料不足：對得上的交易日只有 {len(days)} 天，少於 {LOOKBACK} 天。",
            "events": [],
        }
    events = []
    for index in range(LOOKBACK - 1, len(days)):
        window = days[index - LOOKBACK + 1 : index + 1]
        peak = max(vix[day] for day in window)
        today = days[index]
        level = vix[today]
        if peak <= PEAK_MIN or peak <= 0:
            continue
        if (peak - level) / peak < DROP:
            continue
        horizons = {}
        for horizon in HORIZONS:
            horizons[str(horizon)] = _forward(price, days, index, horizon)
        events.append({
            "date": today.isoformat(),
            "market": market,
            "vix": level,
            "peak": peak,
            "drop": (peak - level) / peak,
            "horizons": horizons,
        })
    return {
        "market": market,
        "ok": True,
        "note": "" if events else "序列夠長，但沒有符合觸發條件的日子。這不是補出來的事件。",
        "events": events,
    }


def _series(rows: list[dict] | None) -> dict[date, float]:
    found: dict[date, float] = {}
    for row in rows or []:
        day = row.get("date")
        close = row.get("close")
        if not isinstance(day, date) or close is None:
            continue
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value != value:
            continue
        found[day] = value
    return found


def _forward(price: dict[date, float], days: list[date], index: int, horizon: int) -> dict:
    if index + horizon >= len(days):
        return {"return": None, "max_drawdown": None, "note": "資料不足"}
    start = price[days[index]]
    if start == 0:
        return {"return": None, "max_drawdown": None, "note": "資料不足"}
    path = [price[days[index + step]] for step in range(horizon + 1)]
    end = path[-1]
    peak = path[0]
    worst = 0.0
    for value in path:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)
    return {"return": end / start - 1.0, "max_drawdown": worst, "note": ""}
