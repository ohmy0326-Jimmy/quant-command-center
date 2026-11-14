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
NOTE = (
    "門檻是程式預設。領先層的放款意願與高收益利差沿用信用傳導，不另設一套門檻。"
    "大型權值是 AAPL、MSFT、NVDA、AMZN、GOOGL、META、AVGO，不是自動抓持倉。"
    "總燈取已知各層裡最差的一層。某一層全部未知是灰燈，不算通過。"
    "美股廣度用標普 500 成分股，只在每日淨值快照算一次，這一頁的請求不重抓那 500 檔。"
    "台股廣度要 0050 與 0051 的全部成分。沒有完整清單時是未知，不用較短的代理名單充數。"
)
PRICE_TICKERS = ("SPY", "XLV", "XLP", "XLU", "XLK", "SPHB", "SPLV", "^GSPC", "^TWII", "^VIX", *LEADERS)
BREADTH_TAB = "Bear_Breadth"
BREADTH_HEADER = ["日期", "美股廣度", "美股四周前", "台股廣度", "台股四周前", "美股檔數", "台股檔數", "寫入時間"]
BREADTH_MAX_AGE_DAYS = 5
SP500_LIST_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
_PRICE_FAIL_SECONDS = 600
_REGIME_CACHE = {"at": 0.0, "regime": None, "ready": False}
_BREADTH_CACHE = {"at": 0.0, "value": None}

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


def overall_state(layers: list[dict] | None) -> str:
    """已知各層裡最差的一盞。未知的層不把別層拉成未知，也不算通過。全未知是 unknown。"""
    known = [str((layer or {}).get("state") or "") for layer in (layers or [])]
    known = [state for state in known if state in {"green", "yellow", "red"}]
    if not known:
        return "unknown"
    if "red" in known:
        return "red"
    if "yellow" in known:
        return "yellow"
    return "green"


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
        "overall": overall_state(layers),
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


def clear_bear_cache() -> None:
    """重新整理時清掉同一天的行情與廣度。不改牛熊分數。"""
    _PRICE_CACHE.clear()
    _REGIME_CACHE["at"] = 0.0
    _REGIME_CACHE["regime"] = None
    _REGIME_CACHE["ready"] = False
    _BREADTH_CACHE["at"] = 0.0
    _BREADTH_CACHE["value"] = None


def _bundle_has_closes(prices: dict | None) -> bool:
    for value in (prices or {}).values():
        if isinstance(value, list) and any(_finite(item) is not None for item in value):
            return True
    return False


def closes_to_bundle(closes: dict | None) -> dict:
    """收盤序列（由舊到新）收成預警要用的行情。不夠長的項目留空，不當 0。"""
    closes = closes or {}
    bundle: dict = {}
    for symbol in ("SPY", "XLV", "XLP", "XLU", "XLK", "SPHB", "SPLV", "^GSPC", "^TWII", "^VIX", *LEADERS):
        series = [_finite(item) for item in (closes.get(symbol) or [])]
        series = [item for item in series if item is not None]
        bundle[symbol] = series or None
    window = THRESHOLDS["leader_dma"]
    flags = []
    for symbol in LEADERS:
        series = bundle.get(symbol) or []
        if len(series) < window:
            flags.append(None)
        else:
            average = sum(series[-window:]) / window
            flags.append(bool(series[-1] < average))
    bundle["leader_below"] = flags
    for key, symbol in (("spx", "^GSPC"), ("twii", "^TWII")):
        series = bundle.get(symbol) or []
        bundle[f"{key}_last"] = series[-1] if series else None
        bundle[f"{key}_high"] = max(series[-252:]) if len(series) >= 252 else None
    return bundle


def fetch_core_prices() -> dict:
    """約十幾個代號的一年以上收盤。不含標普 500 成分股，避免每次請求打五百檔。"""
    from order_insights import fetch_close_history

    history = fetch_close_history(list(PRICE_TICKERS), period="2y")
    closes = {}
    for symbol, series in (history or {}).items():
        values = []
        for item in getattr(series, "tolist", lambda: [])():
            number = _finite(item)
            if number is not None:
                values.append(number)
        if values:
            closes[str(symbol)] = values
    if not closes:
        return {}
    return closes_to_bundle(closes)


def cached_price_bundle(today: date, fetch_prices=None) -> dict:
    """同一天、同一個程序只抓一次行情。沒傳抓取函式就用 Yahoo。整包失敗十分鐘內不連打，成功才留到當天。"""
    import time

    key = today.isoformat()
    if _PRICE_CACHE.get("day") == key and _PRICE_CACHE.get("ok"):
        return dict(_PRICE_CACHE.get("prices") or {})
    if fetch_prices is None:
        fetch_prices = fetch_core_prices
    failed_at = _PRICE_CACHE.get("fail_at")
    if _PRICE_CACHE.get("fail_day") == key and failed_at and time.time() - float(failed_at) < _PRICE_FAIL_SECONDS:
        return dict(_PRICE_CACHE.get("prices") or {})
    try:
        prices = fetch_prices() or {}
    except Exception:
        prices = {}
    if _bundle_has_closes(prices):
        _PRICE_CACHE["day"] = key
        _PRICE_CACHE["ok"] = True
        _PRICE_CACHE["prices"] = prices
        _PRICE_CACHE.pop("fail_at", None)
    else:
        _PRICE_CACHE["ok"] = False
        _PRICE_CACHE["fail_day"] = key
        _PRICE_CACHE["fail_at"] = time.time()
        _PRICE_CACHE["prices"] = prices
    return dict(prices)


def read_sheet_regime() -> str | None:
    """試算表上的牛熊狀態。倉庫 JSON 示例不算。"""
    from macro_cycle import load_macro_cycle
    from macro_cycle_live import read_quant_macro_tabs, remember_live_view
    from macro_cycle_sheet import compose_macro_view
    from portfolio_greeks import trusted_regime
    from portfolio_risk import load_service_account_info

    key = load_service_account_info()
    sheet = read_quant_macro_tabs(key)
    view = compose_macro_view(load_macro_cycle(), sheet, {})
    remember_live_view(view)
    return trusted_regime(view)


def cached_sheet_regime(reader=None, now: float | None = None) -> str | None:
    """十五分鐘內用同一份。第五頁剛讀過的試算表結果優先。"""
    import time

    from macro_cycle_live import peek_live_view
    from portfolio_greeks import trusted_regime

    current = time.time() if now is None else float(now)
    live = trusted_regime(peek_live_view(now=current))
    if live:
        return live
    if _REGIME_CACHE["ready"] and current - float(_REGIME_CACHE["at"]) < 15 * 60:
        return _REGIME_CACHE["regime"]
    if reader is None:
        reader = read_sheet_regime
    try:
        regime = reader()
    except Exception:
        regime = None
    if regime not in CAP_STEPS:
        regime = None
    _REGIME_CACHE["at"] = current
    _REGIME_CACHE["regime"] = regime
    _REGIME_CACHE["ready"] = True
    return regime


def parse_symbol_column(text: str) -> list[str]:
    """CSV 第一列或名為 Symbol 的欄。BRK.B 改成 Yahoo 的 BRK-B。"""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return []
    header = [part.strip().strip('"').lower() for part in lines[0].split(",")]
    index = header.index("symbol") if "symbol" in header else 0
    found = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) <= index:
            continue
        raw = parts[index].strip().strip('"').upper().replace(".", "-")
        if raw and raw not in found:
            found.append(raw)
    return found


def fetch_sp500_symbols(opener=None) -> list[str]:
    import urllib.request

    open_url = opener or urllib.request.urlopen
    with open_url(SP500_LIST_URL, timeout=20) as response:
        payload = response.read()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    symbols = parse_symbol_column(payload)
    if len(symbols) < 400:
        return []
    return symbols


def breadth_from_closes(closes_by_symbol: dict | None) -> tuple[float | None, float | None, int]:
    """目前站上 200 日線的比例，以及 4 週（20 根）前的比例。算不出來是 None，不是 0。"""
    sessions = THRESHOLDS["week_sessions"] * THRESHOLDS["breadth_fall_weeks"]
    current, counted = breadth_percent(closes_by_symbol)
    prior_map = {}
    for symbol, series in (closes_by_symbol or {}).items():
        if series and len(series) > sessions:
            prior_map[symbol] = series[:-sessions]
    prior, _prior_counted = breadth_percent(prior_map)
    return current, prior, counted


def compute_breadth(fetch_symbols, fetch_closes) -> dict:
    """成分股由呼叫端提供。名單或收盤不夠就留 None。"""
    result = {
        "breadth_us": None,
        "breadth_us_prior": None,
        "breadth_us_count": 0,
        "breadth_tw": None,
        "breadth_tw_prior": None,
        "breadth_tw_count": 0,
    }
    for market, prefix in (("us", "breadth_us"), ("tw", "breadth_tw")):
        try:
            symbols = list(fetch_symbols(market) or [])
        except Exception:
            symbols = []
        if not symbols:
            continue
        try:
            closes = fetch_closes(symbols) or {}
        except Exception:
            closes = {}
        current, prior, counted = breadth_from_closes(closes)
        result[prefix] = current
        result[f"{prefix}_prior"] = prior
        result[f"{prefix}_count"] = counted
    return result


def fetch_close_lists(symbols: list[str], period: str = "2y", chunk: int = 40) -> dict:
    """分批抓收盤。單批失敗留空，不把缺的代號當成 0。"""
    from order_insights import fetch_close_history

    unique = []
    for symbol in symbols:
        text = str(symbol or "").strip()
        if text and text not in unique:
            unique.append(text)
    found = {}
    for start in range(0, len(unique), chunk):
        history = fetch_close_history(unique[start:start + chunk], period=period)
        for symbol, series in (history or {}).items():
            values = []
            for item in getattr(series, "tolist", lambda: [])():
                number = _finite(item)
                if number is not None:
                    values.append(number)
            if values:
                found[str(symbol)] = values
    return found


def compute_stored_breadth() -> dict:
    """每日快照用。美股用標普 500 公開名單。台股沒有完整的 0050+0051 檔案時不拿別的名單充數。"""
    def symbols(market: str):
        if market == "us":
            return fetch_sp500_symbols()
        return []

    return compute_breadth(symbols, fetch_close_lists)


def _blank_number(value):
    if value is None or str(value).strip() == "":
        return None
    return _finite(value)


def _sheet_cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return round(value, 2)
    return value


def record_breadth(spreadsheet, today: str, result: dict, written_at: str = "") -> dict:
    """寫進 Bear_Breadth。沒有任何一側算得出來就不寫，也不寫 0。"""
    result = result or {}
    usable = any(result.get(key) is not None for key in ("breadth_us", "breadth_tw"))
    summary = {
        "status": "skipped",
        "sheet": BREADTH_TAB,
        "as_of": today,
        "breadth_us": result.get("breadth_us"),
        "breadth_tw": result.get("breadth_tw"),
        "detail": "成分股收盤不足，沒有把缺的廣度寫成 0。",
    }
    if not usable:
        return summary
    from nav_snapshot import _ensure_worksheet

    worksheet, created, _migrated = _ensure_worksheet(spreadsheet, BREADTH_TAB, list(BREADTH_HEADER))
    values = [
        today,
        _sheet_cell(result.get("breadth_us")),
        _sheet_cell(result.get("breadth_us_prior")),
        _sheet_cell(result.get("breadth_tw")),
        _sheet_cell(result.get("breadth_tw_prior")),
        _sheet_cell(result.get("breadth_us_count")),
        _sheet_cell(result.get("breadth_tw_count")),
        written_at,
    ]
    existing = worksheet.col_values(1)
    target = None
    from sheet_dates import parse_sheet_date

    for index, raw in enumerate(existing[1:], start=2):
        if parse_sheet_date(raw) == parse_sheet_date(today):
            target = index
            break
    if target is None:
        target = max(len(existing), 1) + 1
    worksheet.update(range_name=f"A{target}:H{target}", values=[values], value_input_option="USER_ENTERED")
    summary["status"] = "written"
    summary["sheet_created"] = created
    summary["detail"] = ""
    return summary


def read_stored_breadth(spreadsheet, today: date) -> dict:
    """讀最近一列。超過 5 天，或分頁不存在，回空。空白不是 0。"""
    try:
        worksheet = spreadsheet.worksheet(BREADTH_TAB)
    except Exception:
        return {}
    try:
        from sheet_numbers import read_worksheet_records

        rows = read_worksheet_records(worksheet)
    except Exception:
        try:
            rows = worksheet.get_all_records()
        except Exception:
            return {}
    from sheet_dates import parse_sheet_date

    best = None
    for row in rows or []:
        day = parse_sheet_date(row.get("日期"))
        if day is None:
            continue
        if best is None or day > best[0]:
            best = (day, row)
    if best is None:
        return {}
    age = (today - best[0]).days
    if age < 0 or age > BREADTH_MAX_AGE_DAYS:
        return {}
    row = best[1]
    return {
        "as_of": best[0].isoformat(),
        "breadth_us": _blank_number(row.get("美股廣度")),
        "breadth_us_prior": _blank_number(row.get("美股四周前")),
        "breadth_tw": _blank_number(row.get("台股廣度")),
        "breadth_tw_prior": _blank_number(row.get("台股四周前")),
    }


def read_sheet_breadth(today: date) -> dict:
    from portfolio_risk import load_service_account_info, open_quant_spreadsheet

    book = open_quant_spreadsheet(load_service_account_info())
    return read_stored_breadth(book, today)


def cached_breadth(today: date, reader=None) -> dict:
    """十五分鐘內不重讀試算表。沒有存檔就是空，請求本身不去抓 500 檔。"""
    import time

    current = time.time()
    cached = _BREADTH_CACHE.get("value")
    if isinstance(cached, dict) and current - float(_BREADTH_CACHE.get("at") or 0) < 15 * 60:
        return dict(cached)
    if reader is None:
        reader = read_sheet_breadth
    try:
        found = reader(today) or {}
    except Exception:
        found = {}
    if not isinstance(found, dict):
        found = {}
    _BREADTH_CACHE["at"] = current
    _BREADTH_CACHE["value"] = found
    return dict(found)


def _return_over(closes: list[float] | None, weeks: int) -> float | None:
    series = week_returns(closes, weeks)
    if not series:
        return None
    total = 1.0
    for item in series:
        total *= 1.0 + item
    return total - 1.0


def load_bear_warning(today: date, regime: str | None = None, credit_board: dict | None = None, fetch_fred=None, fetch_prices=None, regime_reader=None, breadth_reader=None) -> dict:
    """信用傳導沿用呼叫端已經抓好的 board，不再抓一次。行情沒傳函式就抓 Yahoo。廣度只讀每日快照。"""
    if regime not in CAP_STEPS:
        regime = cached_sheet_regime(reader=regime_reader)
    if fetch_fred is None:
        from page1_lights import fetch_fred_observations

        fetch_fred = fetch_fred_observations
    prices = cached_price_bundle(today, fetch_prices)
    stored = cached_breadth(today, reader=breadth_reader)
    for key in ("breadth_us", "breadth_us_prior", "breadth_tw", "breadth_tw_prior"):
        if prices.get(key) is None and stored.get(key) is not None:
            prices[key] = stored[key]
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


def _paint_light(st, state: str, text: str) -> None:
    """綠黃紅用 Streamlit 色塊。未知用灰底，不是藍底提示，也不會藏起來。"""
    label = _STATE_TEXT.get(state, "未知")
    line = f"{label}　{text}"
    if state == "red":
        st.error(line)
    elif state == "yellow":
        st.warning(line)
    elif state == "green":
        st.success(line)
    else:
        st.markdown(
            (
                '<div style="background:#e5e7eb;color:#374151;border:1px solid #9ca3af;'
                'border-radius:8px;padding:0.6rem 0.8rem;font-weight:700;margin:0.25rem 0;">'
                f"未知　{text}</div>"
            ),
            unsafe_allow_html=True,
        )


def render_bear_panel(st, report: dict) -> None:
    """第一頁信用傳導旁邊。每一層與總燈都畫出來，未知是灰燈。"""
    report = report or {}
    if not report.get("layers"):
        report = assess({"regime": report.get("regime")})
    st.subheader("熊市預警燈")
    st.caption(CAPTION)
    overall = report.get("overall") or overall_state(report.get("layers"))
    _paint_light(st, overall, "總燈（已知各層裡最差的一層）")
    cap = report.get("suggested_cap")
    regime_cap_value = report.get("regime_cap")
    warning_cap = report.get("warning_cap")
    cap_text = "未知" if cap is None else f"{cap:.1f} 倍"
    regime_text = "未知" if regime_cap_value is None else f"{regime_cap_value:.1f} 倍"
    warning_text = "未知" if warning_cap is None else f"{warning_cap:.1f} 倍"
    st.caption(f"建議槓桿上限 {cap_text}（牛熊上限 {regime_text}，預警調整 {warning_text}，取較低者）。{report.get('suggested_action') or ''}")
    st.caption(report.get("note") or NOTE)
    for layer in report.get("layers") or []:
        _paint_light(st, layer.get("state") or "unknown", layer.get("name") or "這一層")
        for item in layer.get("indicators") or []:
            _paint_light(st, item.get("state") or "unknown", f"{item.get('name') or '子指標'}　{item.get('value_text') or '未知'}")
    st.caption(US_BREADTH_UNIVERSE)
    st.caption(TW_BREADTH_UNIVERSE)
