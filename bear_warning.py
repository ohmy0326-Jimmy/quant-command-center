"""熊市預警燈。獨立於牛熊分數，不改 REGIME_RISK，也不當進場關卡。

缺資料或過舊是未知，不算通過，也不猜。
建議槓桿上限＝min(牛熊分數的上限, 預警調整後的上限)。
"""

from __future__ import annotations

import math
from datetime import date

# 程式預設。畫面上要標出來，請對照自己的用法再改。
THRESHOLDS = {
    "near_high_pct": 2.0,
    "breadth_fall_weeks": 4,
    "defensive_weeks": 2,
    "week_sessions": 5,
    "leader_dma": 60,
    "leader_min_known": 3,
    "high_beta_weeks": 4,
    "high_beta_red_gap": 0.03,
    "index_dma": 200,
    "index_slope_sessions": 20,
    "hy_red_bp": 100.0,
    "hy_yellow_bp": 50.0,
    "hy_weeks": 4,
    "vix_red": 25.0,
    "vix_red_sessions": 10,
    "vix_yellow": 20.0,
    "curve_yellow": 0.25,
}

LEADERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO")
CAP_STEPS = ("牛", "偏多", "中性", "偏空", "熊")
CAPTION = "牛熊分數決定槓桿上限，預警燈決定要不要提早降槓桿，兩者衝突時以較保守的為準。"
US_BREADTH_UNIVERSE = "標普 500 成分股收盤是否站上 200 日線。沒有成分股收盤時是未知。"
TW_BREADTH_UNIVERSE = "台股廣度不是全體加權成分。代理是 0050（台灣50）加 0051（中型100）。沒有這些成分股收盤時是未知。"
NOTE = "門檻是程式預設。領先層的放款意願與高收益利差沿用信用傳導，不另設一套門檻。大型權值是 AAPL、MSFT、NVDA、AMZN、GOOGL、META、AVGO，不是自動抓持倉。"

_STATE_TEXT = {"green": "綠", "yellow": "黃", "red": "紅", "unknown": "未知"}
_PRICE_CACHE: dict = {}


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or not math.isfinite(number):
        return None
    return number


def _indicator(name: str, state: str, *, value=None, value_text: str = "", threshold: str = "", source: str = "") -> dict:
    if state not in _STATE_TEXT:
        state = "unknown"
    return {
        "name": name,
        "state": state,
        "value": value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else None,
        "value_text": value_text or "未知",
        "threshold": threshold,
        "source": source,
    }


def _credit_state(light) -> str:
    text = str(light or "").strip()
    if text in {"green", "yellow", "red"}:
        return text
    return "unknown"


def _layer_state(indicators: list[dict]) -> str:
    states = [item["state"] for item in indicators]
    if not states or all(item == "unknown" for item in states):
        return "unknown"
    if "red" in states:
        return "red"
    if "yellow" in states:
        return "yellow"
    if "green" in states:
        return "green"
    return "unknown"


def _lit(state: str) -> bool:
    return state in {"yellow", "red"}


def regime_cap(regime: str | None) -> float | None:
    from macro_cycle import REGIME_RISK

    name = str(regime or "").strip()
    if name not in REGIME_RISK:
        return None
    return float(REGIME_RISK[name]["target_leverage"])


def _step_down(regime: str, steps: int) -> str | None:
    if regime not in CAP_STEPS:
        return None
    index = min(CAP_STEPS.index(regime) + steps, len(CAP_STEPS) - 1)
    return CAP_STEPS[index]


def week_returns(closes: list[float] | None, weeks: int) -> list[float] | None:
    """由新到舊的週報酬。一週＝5 根收盤，程式預設。"""
    sessions = THRESHOLDS["week_sessions"]
    if not closes or len(closes) < sessions * weeks + 1:
        return None
    found = []
    end = len(closes) - 1
    for week in range(weeks):
        start = closes[end - sessions * (week + 1)]
        finish = closes[end - sessions * week]
        if start is None or finish is None or start <= 0:
            return None
        found.append(finish / start - 1.0)
    return found


def curve_indicator(name: str, series_id: str, value) -> dict:
    number = _finite(value)
    threshold = f"小於 0 為紅，0 到 {THRESHOLDS['curve_yellow']} 為黃。單位是百分點。"
    if number is None:
        return _indicator(name, "unknown", threshold=threshold, source=f"FRED {series_id}")
    if number < 0:
        state = "red"
    elif number < THRESHOLDS["curve_yellow"]:
        state = "yellow"
    else:
        state = "green"
    return _indicator(name, state, value=number, value_text=f"{number:.2f}", threshold=threshold, source=f"FRED {series_id}")


def defensive_indicator(prices: dict | None) -> dict:
    weeks = THRESHOLDS["defensive_weeks"]
    threshold = f"XLV、XLP、XLU 連 {weeks} 週贏過 SPY，且 XLK 連 {weeks} 週輸給 SPY。三檔防禦都成立是紅，兩檔是黃。"
    source = "Yahoo 收盤。一週＝5 根收盤，程式預設。"
    prices = prices or {}
    returns = {}
    for symbol in ("SPY", "XLV", "XLP", "XLU", "XLK"):
        returns[symbol] = week_returns(prices.get(symbol), weeks)
        if returns[symbol] is None:
            return _indicator("防禦股相對強、科技股相對弱", "unknown", threshold=threshold, source=source)
    def beats(symbol: str) -> bool:
        return all(returns[symbol][index] > returns["SPY"][index] for index in range(weeks))
    def lags(symbol: str) -> bool:
        return all(returns[symbol][index] < returns["SPY"][index] for index in range(weeks))
    count = sum(beats(symbol) for symbol in ("XLV", "XLP", "XLU"))
    tech_weak = lags("XLK")
    if count == 3 and tech_weak:
        state = "red"
    elif count >= 2 and tech_weak:
        state = "yellow"
    else:
        state = "green"
    return _indicator("防禦股相對強、科技股相對弱", state, value=count, value_text=f"{count} 檔防禦連{weeks}週領先", threshold=threshold, source=source)


def breadth_percent(closes_by_symbol: dict | None, window: int | None = None) -> tuple[float | None, int]:
    """站上均線的比例。歷史不夠的代號不計入，也不當成站上。一檔都算不出來是 (None, 0)。"""
    window = window or THRESHOLDS["index_dma"]
    counted = 0
    above = 0
    for series in (closes_by_symbol or {}).values():
        if not series or len(series) < window:
            continue
        window_values = [item for item in series[-window:] if _finite(item) is not None]
        last = _finite(series[-1])
        if len(window_values) < window or last is None:
            continue
        counted += 1
        if last > sum(window_values) / window:
            above += 1
    if counted == 0:
        return None, 0
    return above / counted * 100.0, counted


def breadth_indicator(name: str, universe: str, current, prior, index_last, index_high) -> dict:
    weeks = THRESHOLDS["breadth_fall_weeks"]
    near = THRESHOLDS["near_high_pct"]
    threshold = f"廣度較 {weeks} 週前下降是黃。指數距 52 週高點 {near:.0f}% 以內且廣度下降是紅。"
    now = _finite(current)
    then = _finite(prior)
    last = _finite(index_last)
    high = _finite(index_high)
    if None in {now, then, last, high} or high <= 0:
        return _indicator(name, "unknown", threshold=threshold, source=universe)
    falling = now < then
    near_high = last >= high * (1.0 - near / 100.0)
    if near_high and falling:
        state = "red"
    elif falling:
        state = "yellow"
    else:
        state = "green"
    return _indicator(
        name,
        state,
        value=now,
        value_text=f"{now:.1f}%（{weeks} 週前 {then:.1f}%）",
        threshold=threshold,
        source=universe,
    )


def leaders_indicator(below_flags: list | None) -> dict:
    window = THRESHOLDS["leader_dma"]
    names = "、".join(LEADERS)
    threshold = f"已知的權值股裡，半數以上收盤低於 {window} 日線是紅，至少 2 檔是黃。已知檔數少於 {THRESHOLDS['leader_min_known']} 是未知。"
    source = f"程式預設的大型權值：{names}。不是持倉自動名單。"
    known = [flag for flag in (below_flags or []) if isinstance(flag, bool)]
    if len(known) < THRESHOLDS["leader_min_known"]:
        return _indicator("大型權值跌破 60 日線", "unknown", threshold=threshold, source=source)
    count = sum(1 for flag in known if flag)
    if count * 2 >= len(known):
        state = "red"
    elif count >= 2:
        state = "yellow"
    else:
        state = "green"
    return _indicator("大型權值跌破 60 日線", state, value=count, value_text=f"{count}/{len(known)} 檔在均線下", threshold=threshold, source=source)


def high_beta_indicator(high_beta_return, low_vol_return) -> dict:
    weeks = THRESHOLDS["high_beta_weeks"]
    gap_red = THRESHOLDS["high_beta_red_gap"]
    threshold = f"SPHB 近 {weeks} 週報酬小於 SPLV 是黃，落後超過 {gap_red:.0%} 是紅。"
    source = "Yahoo SPHB 與 SPLV。一週＝5 根收盤，程式預設。"
    high = _finite(high_beta_return)
    low = _finite(low_vol_return)
    if high is None or low is None:
        return _indicator("高貝塔落後低波", "unknown", threshold=threshold, source=source)
    gap = high - low
    if gap <= -gap_red:
        state = "red"
    elif gap < 0:
        state = "yellow"
    else:
        state = "green"
    return _indicator("高貝塔落後低波", state, value=gap, value_text=f"SPHB－SPLV {gap:+.1%}", threshold=threshold, source=source)


def index_trend_indicator(name: str, closes: list[float] | None, source: str) -> dict:
    window = THRESHOLDS["index_dma"]
    slope = THRESHOLDS["index_slope_sessions"]
    threshold = f"收盤低於 {window} 日線是黃。同時 {window} 日線比 {slope} 根之前低是紅。"
    if not closes or len(closes) < window + slope:
        return _indicator(name, "unknown", threshold=threshold, source=source)
    if any(_finite(item) is None for item in closes[-(window + slope):]):
        return _indicator(name, "unknown", threshold=threshold, source=source)
    sma = sum(closes[-window:]) / window
    previous = sum(closes[-(window + slope):-slope]) / window
    last = closes[-1]
    if last < sma and sma < previous:
        state = "red"
    elif last < sma:
        state = "yellow"
    else:
        state = "green"
    return _indicator(name, state, value=last, value_text=f"收盤 {last:.2f}，200 日線 {sma:.2f}", threshold=threshold, source=source)


def hy_widening_indicator(change_bp) -> dict:
    red = THRESHOLDS["hy_red_bp"]
    yellow = THRESHOLDS["hy_yellow_bp"]
    weeks = THRESHOLDS["hy_weeks"]
    threshold = f"{weeks} 週約 20 個交易日，利差擴大 {red:.0f} bp 是紅，{yellow:.0f} bp 是黃。沒有區間變化是未知。"
    change = _finite(change_bp)
    if change is None:
        return _indicator("高收益利差急擴", "unknown", threshold=threshold, source="沿用信用傳導的高收益 OAS 變化")
    if change >= red:
        state = "red"
    elif change >= yellow:
        state = "yellow"
    else:
        state = "green"
    return _indicator("高收益利差急擴", state, value=change, value_text=f"{change:+.0f} bp", threshold=threshold, source="沿用信用傳導的高收益 OAS 變化")


def vix_indicator(closes: list[float] | None) -> dict:
    level = THRESHOLDS["vix_red"]
    sessions = THRESHOLDS["vix_red_sessions"]
    yellow = THRESHOLDS["vix_yellow"]
    threshold = f"連續 {sessions} 根收盤高於 {level:.0f} 是紅。最新一根高於 {level:.0f} 或 {yellow:.0f} 是黃。"
    source = "Yahoo ^VIX"
    if not closes or len(closes) < sessions or any(_finite(item) is None for item in closes[-sessions:]):
        return _indicator("VIX 持續偏高", "unknown", threshold=threshold, source=source)
    recent = closes[-sessions:]
    last = recent[-1]
    if all(item > level for item in recent):
        state = "red"
    elif last > yellow:
        state = "yellow"
    else:
        state = "green"
    return _indicator("VIX 持續偏高", state, value=last, value_text=f"{last:.1f}", threshold=threshold, source=source)


def _credit_indicator(name: str, light, source: str) -> dict:
    state = _credit_state(light)
    return _indicator(name, state, value_text=_STATE_TEXT[state], threshold="沿用信用傳導門檻。缺資料或過舊是未知，不算通過。", source=source)


def suggestion(regime: str | None, layers: list[dict]) -> dict:
    current = regime_cap(regime)
    states = [layer["state"] for layer in layers]
    leading, internal, confirm = (states + ["unknown", "unknown", "unknown"])[:3]
    if current is None:
        return {
            "regime": None,
            "regime_cap": None,
            "warning_cap": None,
            "suggested_cap": None,
            "suggested_action": "牛熊狀態未知，不猜測槓桿上限。",
        }
    name = str(regime)
    if confirm == "red":
        warning_name = "熊"
        action = "第三層確認，只留核心與避險。"
    elif _lit(leading) and _lit(internal):
        warning_name = _step_down(name, 2)
        action = "領先與內部轉弱都亮，槓桿上限再降一檔，並加避險。"
    elif _lit(leading):
        warning_name = _step_down(name, 1)
        action = "第一層亮，槓桿上限降一檔。"
    elif _lit(internal):
        warning_name = _step_down(name, 1)
        action = "第二層亮，槓桿上限降一檔。"
    else:
        warning_name = name
        action = "預警未亮，沿用牛熊分數的槓桿上限。"
    warning = regime_cap(warning_name)
    suggested = None if warning is None else min(current, warning)
    return {
        "regime": name,
        "regime_cap": current,
        "warning_cap": warning,
        "suggested_cap": suggested,
        "suggested_action": action,
    }


def _breadth_block(snapshot: dict) -> dict:
    us = breadth_indicator(
        "美股廣度",
        US_BREADTH_UNIVERSE,
        snapshot.get("breadth_us"),
        snapshot.get("breadth_us_prior"),
        snapshot.get("spx_last"),
        snapshot.get("spx_high"),
    )
    tw = breadth_indicator(
        "台股廣度",
        TW_BREADTH_UNIVERSE,
        snapshot.get("breadth_tw"),
        snapshot.get("breadth_tw_prior"),
        snapshot.get("twii_last"),
        snapshot.get("twii_high"),
    )
    return {"us": us, "tw": tw}


def assess(snapshot: dict | None = None) -> dict:
    """由已備好的數字組預警。沒傳的項目是未知。"""
    snapshot = snapshot or {}
    credit = snapshot.get("credit") or {}
    leading = [
        _credit_indicator("銀行放款意願 DRTSCILM", credit.get("drtscilm_light"), "信用傳導"),
        _credit_indicator("高收益債利差", credit.get("hy_light"), "信用傳導"),
        curve_indicator("十年減兩年利差", "T10Y2Y", snapshot.get("t10y2y")),
        curve_indicator("十年減三個月利差", "T10Y3M", snapshot.get("t10y3m")),
        defensive_indicator(snapshot.get("prices")),
    ]
    breadth = _breadth_block(snapshot)
    internal = [
        breadth["us"],
        breadth["tw"],
        leaders_indicator(snapshot.get("leader_below")),
        high_beta_indicator(snapshot.get("sphb_return"), snapshot.get("splv_return")),
    ]
    confirm = [
        index_trend_indicator("標普 500 跌破向下的 200 日線", snapshot.get("spx_closes"), "Yahoo ^GSPC"),
        index_trend_indicator("加權指數跌破向下的 200 日線", snapshot.get("twii_closes"), "Yahoo ^TWII"),
        hy_widening_indicator(credit.get("hy_change_bp")),
        vix_indicator(snapshot.get("vix_closes")),
    ]
    layers = [
        {"id": "leading", "name": "第一層 領先", "indicators": leading, "state": _layer_state(leading)},
        {"id": "internal", "name": "第二層 內部轉弱", "indicators": internal, "state": _layer_state(internal)},
        {"id": "confirm", "name": "第三層 確認", "indicators": confirm, "state": _layer_state(confirm)},
    ]
    plan = suggestion(snapshot.get("regime"), layers)
    return {
        "caption": CAPTION,
        "note": NOTE,
        "thresholds": dict(THRESHOLDS),
        "leaders": list(LEADERS),
        "breadth": {
            "us_universe": US_BREADTH_UNIVERSE,
            "tw_universe": TW_BREADTH_UNIVERSE,
            "us": breadth["us"],
            "tw": breadth["tw"],
        },
        "layers": layers,
        **plan,
    }


def _row_light(board: dict | None, row_id: str) -> str | None:
    for row in (board or {}).get("rows") or []:
        if row.get("id") == row_id:
            return row.get("light")
    return None


def _row_change(board: dict | None, row_id: str):
    for row in (board or {}).get("rows") or []:
        if row.get("id") == row_id:
            return row.get("change")
    return None


def _latest(observations) -> float | None:
    if not observations:
        return None
    return _finite(observations[-1][1])


def cached_price_bundle(today: date, fetch_prices=None) -> dict:
    """同一天、同一個程序只抓一次行情。抓不到回空，呼叫端當成未知。"""
    key = today.isoformat()
    if _PRICE_CACHE.get("day") == key:
        return _PRICE_CACHE.get("prices") or {}
    prices = {}
    if fetch_prices is not None:
        try:
            prices = fetch_prices() or {}
        except Exception:
            prices = {}
    _PRICE_CACHE["day"] = key
    _PRICE_CACHE["prices"] = prices
    return prices


def _return_over(closes: list[float] | None, weeks: int) -> float | None:
    series = week_returns(closes, weeks)
    if not series:
        return None
    total = 1.0
    for item in series:
        total *= 1.0 + item
    return total - 1.0


def load_bear_warning(today: date, regime: str | None = None, credit_board: dict | None = None, fetch_fred=None, fetch_prices=None) -> dict:
    """信用傳導沿用呼叫端已經抓好的 board，不再抓一次。利差與行情失敗就留未知。"""
    if fetch_fred is None:
        from page1_lights import fetch_fred_observations

        fetch_fred = fetch_fred_observations
    prices = cached_price_bundle(today, fetch_prices)
    if _PRICE_CACHE.get("curve_day") == today.isoformat():
        curves = dict(_PRICE_CACHE.get("curves") or {})
    else:
        curves = {}
        for series_id in ("T10Y2Y", "T10Y3M"):
            try:
                curves[series_id] = _latest(fetch_fred(series_id))
            except Exception:
                curves[series_id] = None
        _PRICE_CACHE["curve_day"] = today.isoformat()
        _PRICE_CACHE["curves"] = curves
    hy_change = _row_change(credit_board, "hy_oas")
    snapshot = {
        "regime": regime,
        "credit": {
            "drtscilm_light": _row_light(credit_board, "drtscilm"),
            "hy_light": _row_light(credit_board, "hy_oas"),
            "hy_change_bp": hy_change,
        },
        "t10y2y": curves.get("T10Y2Y"),
        "t10y3m": curves.get("T10Y3M"),
        "prices": {symbol: prices.get(symbol) for symbol in ("SPY", "XLV", "XLP", "XLU", "XLK")},
        "leader_below": prices.get("leader_below"),
        "sphb_return": _return_over(prices.get("SPHB"), THRESHOLDS["high_beta_weeks"]),
        "splv_return": _return_over(prices.get("SPLV"), THRESHOLDS["high_beta_weeks"]),
        "spx_closes": prices.get("^GSPC"),
        "twii_closes": prices.get("^TWII"),
        "vix_closes": prices.get("^VIX"),
        "breadth_us": prices.get("breadth_us"),
        "breadth_us_prior": prices.get("breadth_us_prior"),
        "breadth_tw": prices.get("breadth_tw"),
        "breadth_tw_prior": prices.get("breadth_tw_prior"),
        "spx_last": prices.get("spx_last"),
        "spx_high": prices.get("spx_high"),
        "twii_last": prices.get("twii_last"),
        "twii_high": prices.get("twii_high"),
    }
    return assess(snapshot)


def render_bear_panel(st, report: dict) -> None:
    """第一頁信用傳導旁邊。只顯示。"""
    report = report or assess()
    st.subheader("熊市預警燈")
    st.caption(CAPTION)
    cap = report.get("suggested_cap")
    regime_cap_value = report.get("regime_cap")
    warning_cap = report.get("warning_cap")
    cap_text = "未知" if cap is None else f"{cap:.1f} 倍"
    regime_text = "未知" if regime_cap_value is None else f"{regime_cap_value:.1f} 倍"
    warning_text = "未知" if warning_cap is None else f"{warning_cap:.1f} 倍"
    st.caption(f"建議槓桿上限 {cap_text}（牛熊上限 {regime_text}，預警調整 {warning_text}，取較低者）。{report.get('suggested_action') or ''}")
    st.caption(report.get("note") or NOTE)
    for layer in report.get("layers") or []:
        st.markdown(f"**{layer['name']}：{_STATE_TEXT.get(layer['state'], '未知')}**")
        lit = [item["name"] for item in layer["indicators"] if item["state"] in {"yellow", "red"}]
        unknown = [item["name"] for item in layer["indicators"] if item["state"] == "unknown"]
        st.caption("亮：" + ("、".join(lit) if lit else "無") + "。未知：" + ("、".join(unknown) if unknown else "無") + "。")
    st.caption(US_BREADTH_UNIVERSE)
    st.caption(TW_BREADTH_UNIVERSE)
