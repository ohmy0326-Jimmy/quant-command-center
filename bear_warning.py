"""熊市預警燈。獨立於牛熊分數，不改 REGIME_RISK，也不當進場關卡。

缺資料或過舊是未知，不算通過，也不猜。
建議槓桿上限＝min(牛熊分數的上限, 預警調整後的上限)。
"""

from __future__ import annotations

import math
from datetime import date

# 依 2026 覆盤設定，含後見之明。只改這張表，不改 REGIME_RISK。
POSTMORTEM_LABEL = "依 2026 覆盤設定，含後見之明。"
FRED_LAG_NOTE = "公布落差假設：高收益與殖利率約 1 日、NFCI 約 5 日、STLFSI4 約 6 日、H.8 約 9 日、SLOOS 約 35 日。只用已經公布的最新一筆，不往前補。"
THRESHOLDS = {
    "hy_lead": 0.30,
    "hy_confirm": 0.75,
    "hy_window": 60,
    "nfci_rise": 0.03,
    "nfci_weeks": 4,
    "foreign_10d": -1500 * 1e8,
    "foreign_days": 10,
    "high_window": 252,
    "high_recent": 20,
    "sox_below_high": 0.05,
    "rsp_spy_drop": -0.02,
    "relative_sessions": 20,
    "tw_breadth_weak": 40.0,
    "tw_breadth_drop": 10.0,
    "leader_dma": 60,
    "leader_need": 2,
    "leader_min_known": 3,
    "sphb_splv_drop": -0.05,
    "high_beta_weeks": 4,
    "high_beta_red_gap": 0.03,
    "dma20": 20,
    "layer_on_days": 3,
    "layer_off_days": 10,
    "vix_watch": 25.0,
    "vix_red": 25.0,
    "vix_red_sessions": 10,
    "vix_yellow": 20.0,
    "hy_red_bp": 100.0,
    "hy_yellow_bp": 50.0,
    "hy_weeks": 4,
    "breadth_fall_weeks": 4,
    "near_high_pct": 2.0,
    "curve_yellow": 0.25,
    "index_dma": 200,
    "index_slope_sessions": 20,
    "defensive_weeks": 2,
    "week_sessions": 5,
}
LEADERS = ("NVDA", "2330.TW", "MU", "AVGO")
LEADER_FALLBACK = {"2330.TW": "TSM"}
CAP_STEPS = ("牛", "偏多", "中性", "偏空", "熊")
CAPTION = "牛熊分數決定槓桿上限，預警燈決定要不要提早降槓桿，兩者衝突時以較保守的為準。"
US_BREADTH_UNIVERSE = "寬度看 RSP／SPY 二十日報酬，不重抓標普 500 成分股。"
TW_BREADTH_UNIVERSE = "台股寬度用證交所 MI_INDEX 上市股票上漲家數 ÷（上漲＋下跌）。沒有家數時是未知。"
NOTE = (
    POSTMORTEM_LABEL + " 每一層只讓主訊號點燈，其餘是觀察，不點亮該層。"
    "亮燈要連續 3 個交易日，熄燈要連續 10 個交易日。歷史不夠就寫遲滯資料不足，不猜。"
    "核心部位跟著預警上限。收破 20 日線減 1/3 只套在槓桿／戰術曝險，不下單。"
    + FRED_LAG_NOTE
)
TACTICAL_UNDERLYING = {
    "00663L": ("^TWII", "加權指數"),
    "00631L": ("0050.TW", "台灣50"),
    "SOXL": ("SOXX", "費半 ETF"),
    "MUU": ("MU", "美光"),
    "SNXX": ("SNDK", "SNDK"),
    "LITX": ("LITE", "Lumentum"),
    "AMUU": ("AMD", "超微"),
}
PRICE_TICKERS = (
    "SPY", "RSP", "XLV", "XLP", "XLU", "XLK", "SPHB", "SPLV",
    "^GSPC", "^TWII", "^SOX", "^VIX",
    "NVDA", "2330.TW", "TSM", "MU", "AVGO",
    "0050.TW", "SOXX", "SNDK", "LITE", "AMD",
)
WARNING_TAB = "Bear_Warning_Daily"
WARNING_HEADER = ["日期", "領先條件", "內部條件", "確認條件", "外資買賣超", "上漲家數", "下跌家數", "寫入時間"]
BREADTH_TAB = "Bear_Breadth"
BREADTH_HEADER = ["日期", "美股廣度", "美股四周前", "台股廣度", "台股四周前", "美股檔數", "台股檔數", "寫入時間"]
BREADTH_MAX_AGE_DAYS = 5
SP500_LIST_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
_PRICE_FAIL_SECONDS = 600
_REGIME_CACHE = {"at": 0.0, "regime": None, "ready": False}
_BREADTH_CACHE = {"at": 0.0, "value": None}
_HISTORY_CACHE = {"at": 0.0, "value": None}

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


def _indicator(name: str, state: str, *, value=None, value_text: str = "", threshold: str = "", source: str = "", role: str = "main") -> dict:
    if state not in _STATE_TEXT:
        state = "unknown"
    return {
        "name": name,
        "state": state,
        "value": value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) else None,
        "value_text": value_text or "未知",
        "threshold": threshold,
        "source": source,
        "role": "watch" if role == "watch" else "main",
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


def _series(closes) -> list[float]:
    return [item for item in (closes or []) if _finite(item) is not None]


def _return_over_sessions(closes, sessions: int) -> float | None:
    series = _series(closes)
    if len(series) < sessions + 1 or series[-1 - sessions] <= 0:
        return None
    return series[-1] / series[-1 - sessions] - 1.0


def ratio_return(left, right, sessions: int | None = None) -> float | None:
    """左／右的區間報酬。缺一邊是 None，不是 0。"""
    sessions = THRESHOLDS["relative_sessions"] if sessions is None else sessions
    left_series = _series(left)
    right_series = _series(right)
    if len(left_series) < sessions + 1 or len(right_series) < sessions + 1:
        return None
    if min(left_series[-1], right_series[-1], left_series[-1 - sessions], right_series[-1 - sessions]) <= 0:
        return None
    now = left_series[-1] / right_series[-1]
    then = left_series[-1 - sessions] / right_series[-1 - sessions]
    if then <= 0:
        return None
    return now / then - 1.0


def hy_gap(levels, window: int | None = None) -> float | None:
    """最新值減最近 window 筆的最低值。筆數不夠是 None。單位沿用 FRED 的百分點。"""
    window = THRESHOLDS["hy_window"] if window is None else window
    series = _series(levels)
    if len(series) < window:
        return None
    recent = series[-window:]
    return recent[-1] - min(recent)


def nfci_rise(observations, weeks: int | None = None) -> float | None:
    """最新減約四週前的那一筆。沒有落在四週以前的觀測是 None。"""
    from datetime import timedelta

    weeks = THRESHOLDS["nfci_weeks"] if weeks is None else weeks
    points = []
    for item in observations or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            day, value = item[0], _finite(item[1])
        else:
            continue
        if value is None or day is None:
            continue
        points.append((day, value))
    if len(points) < 2:
        return None
    points.sort(key=lambda pair: pair[0])
    latest_day, latest = points[-1]
    cutoff = latest_day - timedelta(days=7 * weeks)
    prior = [value for day, value in points if day <= cutoff]
    if not prior:
        return None
    return latest - prior[-1]


def foreign_sum(nets, days: int | None = None) -> float | None:
    days = THRESHOLDS["foreign_days"] if days is None else days
    series = _series(nets)
    if len(series) < days:
        return None
    return sum(series[-days:])


def made_recent_high(closes, window: int | None = None, recent: int | None = None) -> bool | None:
    window = THRESHOLDS["high_window"] if window is None else window
    recent = THRESHOLDS["high_recent"] if recent is None else recent
    series = _series(closes)
    if len(series) < window:
        return None
    span = series[-window:]
    return max(span[-recent:]) >= max(span)


def pct_below_high(closes, gap: float | None = None, window: int | None = None) -> bool | None:
    window = THRESHOLDS["high_window"] if window is None else window
    gap = THRESHOLDS["sox_below_high"] if gap is None else gap
    series = _series(closes)
    if len(series) < window or max(series[-window:]) <= 0:
        return None
    high = max(series[-window:])
    return series[-1] <= high * (1.0 - gap)


def below_dma(closes, window: int) -> bool | None:
    series = _series(closes)
    if len(series) < window:
        return None
    average = sum(series[-window:]) / window
    return series[-1] < average


def _or_flag(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return None


def _and_flag(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def raw_layer(flags: list, need: int) -> bool | None:
    """已知成立數已經夠就成立。已知不成立多到湊不齊就未成立。其餘未知。"""
    ons = sum(1 for flag in flags if flag is True)
    unknowns = sum(1 for flag in flags if flag is None)
    if ons >= need:
        return True
    if ons + unknowns < need:
        return False
    return None


def apply_hysteresis(prior: list | None, today: bool | None, on_days: int | None = None, off_days: int | None = None) -> dict:
    """不假設歷史開始之前是熄燈。沒看過連續亮燈就不宣告亮或熄。"""
    on_days = THRESHOLDS["layer_on_days"] if on_days is None else on_days
    off_days = THRESHOLDS["layer_off_days"] if off_days is None else off_days
    state = None
    on_streak = 0
    off_streak = 0
    for value in list(prior or []) + [today]:
        if value is None:
            on_streak = 0
            off_streak = 0
            continue
        if value is True:
            on_streak += 1
            off_streak = 0
            if on_streak >= on_days:
                state = True
        else:
            off_streak += 1
            on_streak = 0
            if state is True and off_streak >= off_days:
                state = False
    if today is True:
        raw_text = "今日條件成立"
    elif today is False:
        raw_text = "今日條件未成立"
    else:
        raw_text = "今日條件未知"
    sufficient = state is not None
    provisional = (not sufficient) and today is True
    if provisional:
        note = "疑似亮燈（遲滯資料不足）"
        light = "yellow"
    elif not sufficient:
        note = "遲滯資料不足"
        light = "unknown"
    else:
        note = ""
        light = "red" if state is True else "green"
    return {
        "state": light,
        "on": state is True,
        "provisional": provisional,
        "raw_on": today,
        "raw_text": raw_text,
        "on_streak": on_streak,
        "off_streak": off_streak,
        "on_days": on_days,
        "off_days": off_days,
        "sufficient": sufficient,
        "note": note,
    }


def _flag_indicator(name: str, flag: bool | None, *, value=None, value_text: str = "", threshold: str = "", source: str = "", role: str = "main") -> dict:
    if flag is True:
        state = "red"
    elif flag is False:
        state = "green"
    else:
        state = "unknown"
    text = value_text or {True: "成立", False: "未成立"}.get(flag, "未知")
    return _indicator(name, state, value=value, value_text=text, threshold=POSTMORTEM_LABEL + threshold, source=source, role=role)


def _leader_flags(prices: dict) -> list:
    flags = []
    for symbol in LEADERS:
        series = prices.get(symbol) or []
        if not series and LEADER_FALLBACK.get(symbol):
            series = prices.get(LEADER_FALLBACK[symbol]) or []
        flags.append(below_dma(series, THRESHOLDS["leader_dma"]))
    return flags


def _leader_on(flags: list) -> bool | None:
    ons = sum(1 for flag in flags if flag is True)
    unknowns = sum(1 for flag in flags if flag is None)
    if ons >= THRESHOLDS["leader_need"]:
        return True
    if ons + unknowns < THRESHOLDS["leader_need"]:
        return False
    return None


def tw_breadth_on(latest, prior) -> bool | None:
    now = _finite(latest)
    then = _finite(prior)
    if now is None:
        return None
    if now < THRESHOLDS["tw_breadth_weak"]:
        return True
    if then is None:
        return None
    if then - now >= THRESHOLDS["tw_breadth_drop"]:
        return True
    return False


BACKFILL_TRADING_DAYS = 60
HY_LAG_DAYS = 1
NFCI_LAG_DAYS = 5


def _through(dated, day: date, lag_days: int = 0):
    from datetime import timedelta

    cutoff = day - timedelta(days=lag_days)
    return [(item_day, value) for item_day, value in dated if item_day <= cutoff]


def build_backfill_rows(today: date, bundle: dict, trading_days: int = BACKFILL_TRADING_DAYS) -> list[dict]:
    """用每個交易日當時已公布的數字重算三層條件。週末與沒有加權收盤的日子不寫。"""
    from datetime import timedelta

    prices = bundle.get("prices") or {}
    twii = list(prices.get("^TWII") or [])
    days = [item_day for item_day, _value in twii if item_day <= today and item_day.weekday() < 5]
    days = days[-trading_days:]
    foreign = list(bundle.get("foreign") or [])
    breadth = list(bundle.get("breadth") or [])
    hy = list(bundle.get("hy") or [])
    nfci = list(bundle.get("nfci") or [])
    foreign_by_day = {item_day: value for item_day, value in foreign}
    breadth_by_day = {item_day: (up, down) for item_day, up, down in breadth}
    rows = []
    for day in days:
        hy_values = [value for _day, value in _through(hy, day, HY_LAG_DAYS)]
        nfci_points = _through(nfci, day, NFCI_LAG_DAYS)
        nets = [value for item_day, value in foreign if item_day <= day]
        breadth_days = [(item_day, breadth_percent_from_counts(up, down)) for item_day, up, down in breadth if item_day <= day]
        breadth_days = [(item_day, percent) for item_day, percent in breadth_days if percent is not None]
        sliced = {symbol: [value for item_day, value in series if item_day <= day] for symbol, series in prices.items()}
        snapshot = {
            "hy": hy_values,
            "nfci": nfci_points,
            "foreign_nets": nets[-THRESHOLDS["foreign_days"]:] if len(nets) >= THRESHOLDS["foreign_days"] else None,
            "tw_breadth": breadth_days[-1][1] if breadth_days else None,
            "tw_breadth_prior": breadth_days[-21][1] if len(breadth_days) >= 21 else None,
            "prices": sliced,
        }
        leading = raw_layer([_credit_main(snapshot)[0], _foreign_main(snapshot)[0], _divergence_main(snapshot)[0]], 2)
        internal = raw_layer([_breadth_main(snapshot)[0], _leaders_main(snapshot)[0], _high_beta_main(snapshot)[0]], 2)
        confirm = raw_layer([_confirm_credit(snapshot)[0], _confirm_tape(snapshot)[0]], 1)
        row = {"date": day.isoformat()}
        if leading is not None:
            row["leading"] = leading
        if internal is not None:
            row["internal"] = internal
        if confirm is not None:
            row["confirm"] = confirm
        if day in foreign_by_day:
            row["foreign_net"] = foreign_by_day[day]
        if day in breadth_by_day:
            row["up"], row["down"] = breadth_by_day[day]
        rows.append(row)
    return rows


BATCH_DAYS = 8
BATCH_BUDGET_SECONDS = 60
SNAPSHOT_BATCH_DAYS = 1
SNAPSHOT_BUDGET_SECONDS = 8
REQUEST_TIMEOUT_SECONDS = 8


def _progress(target: list[date], filled: set[date], wrote: int, failures: list[str], status: str) -> dict:
    missing = [day for day in target if day not in filled]
    return {
        "status": status,
        "sheet": WARNING_TAB,
        "rows": wrote,
        "done": not missing and bool(target),
        "filled_days": len(target) - len(missing),
        "remaining_days": len(missing),
        "next_date": missing[0].isoformat() if missing else None,
        "failures": failures,
        "detail": "；".join(failures[:8]),
    }


def stored_foreign_points(spreadsheet, today: date) -> list[tuple[date, float]]:
    from sheet_dates import parse_sheet_date

    points = []
    for day, row in _warning_rows(spreadsheet, today):
        number = _finite(str(row.get("外資買賣超") or "").replace(",", ""))
        if number is not None and day <= today:
            points.append((day, number))
    return points


def stored_breadth_points(spreadsheet, today: date) -> list[tuple]:
    points = []
    for day, row in _warning_rows(spreadsheet, today):
        if str(row.get("上漲家數") or "").strip() == "" or str(row.get("下跌家數") or "").strip() == "":
            continue
        points.append((day, row.get("上漲家數"), row.get("下跌家數")))
    return points


def _warning_rows(spreadsheet, today: date) -> list[tuple]:
    from sheet_dates import parse_sheet_date

    try:
        worksheet = spreadsheet.worksheet(WARNING_TAB)
    except Exception:
        return []
    try:
        from sheet_numbers import read_worksheet_records

        rows = read_worksheet_records(worksheet)
    except Exception:
        try:
            rows = worksheet.get_all_records()
        except Exception:
            return []
    parsed = []
    for row in rows or []:
        day = parse_sheet_date(row.get("日期"))
        if day is None or day > today:
            continue
        parsed.append((day, row))
    parsed.sort(key=lambda item: item[0])
    return parsed


def _flags_for_day(day: date, prices: dict, hy, nfci, foreign_points, breadth_points) -> dict:
    """只使用這一天當時已公布的數字。高收益晚一天，NFCI 晚五天。"""
    hy_values = [value for _day, value in _through(hy or [], day, HY_LAG_DAYS)]
    nfci_points = _through(nfci or [], day, NFCI_LAG_DAYS)
    nets = [value for item_day, value in foreign_points if item_day <= day]
    breadth_days = []
    for item_day, up, down in breadth_points:
        if item_day > day:
            continue
        percent = breadth_percent_from_counts(up, down)
        if percent is not None:
            breadth_days.append(percent)
    sliced = {symbol: [value for item_day, value in series if item_day <= day] for symbol, series in (prices or {}).items()}
    snapshot = {
        "hy": hy_values,
        "nfci": nfci_points,
        "foreign_nets": nets[-THRESHOLDS["foreign_days"]:] if len(nets) >= THRESHOLDS["foreign_days"] else None,
        "tw_breadth": breadth_days[-1] if breadth_days else None,
        "tw_breadth_prior": breadth_days[-21] if len(breadth_days) >= 21 else None,
        "prices": sliced,
    }
    leading = raw_layer([_credit_main(snapshot)[0], _foreign_main(snapshot)[0], _divergence_main(snapshot)[0]], 2)
    internal = raw_layer([_breadth_main(snapshot)[0], _leaders_main(snapshot)[0], _high_beta_main(snapshot)[0]], 2)
    confirm = raw_layer([_confirm_credit(snapshot)[0], _confirm_tape(snapshot)[0]], 1)
    row = {}
    if leading is not None:
        row["leading"] = leading
    if internal is not None:
        row["internal"] = internal
    if confirm is not None:
        row["confirm"] = confirm
    return row


def filled_warning_dates(spreadsheet, today: date) -> set[date]:
    """已寫過外資、漲跌家數或任一層條件的日期。空白列不算。"""
    from sheet_dates import parse_sheet_date

    try:
        worksheet = spreadsheet.worksheet(WARNING_TAB)
    except Exception:
        return set()
    try:
        from sheet_numbers import read_worksheet_records

        rows = read_worksheet_records(worksheet)
    except Exception:
        try:
            rows = worksheet.get_all_records()
        except Exception:
            return set()
    found = set()
    for row in rows or []:
        day = parse_sheet_date(row.get("日期"))
        if day is None or day > today:
            continue
        has_foreign = str(row.get("外資買賣超") or "").strip() != ""
        has_breadth = str(row.get("上漲家數") or "").strip() != "" or str(row.get("下跌家數") or "").strip() != ""
        has_flag = any(_cell_flag(row.get(name)) is not None for name in ("領先條件", "內部條件", "確認條件"))
        if has_foreign or has_breadth or has_flag:
            found.add(day)
    return found


def fetch_warning_day(day: date, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict:
    """抓一天的 BFI82U 與 MI_INDEX。單日失敗回錯誤字串，不丟例外。"""
    import requests

    from macro_risk import TWSE_BFI82U_URL, parse_bfi82u

    result = {"foreign_net": None, "up": None, "down": None, "errors": []}
    headers = {"User-Agent": "quant-command-center/1.0"}
    try:
        response = requests.get(
            TWSE_BFI82U_URL,
            params={"type": "day", "dayDate": day.strftime("%Y%m%d"), "response": "json"},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        result["foreign_net"] = parse_bfi82u(response.json())
        if result["foreign_net"] is None:
            result["errors"].append(f"{day.isoformat()} BFI82U 沒有外資列")
    except Exception as exc:
        result["errors"].append(f"{day.isoformat()} BFI82U {exc.__class__.__name__}")
    try:
        response = requests.get(
            "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
            params={"date": day.strftime("%Y%m%d"), "type": "ALL", "response": "json"},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        parsed = parse_mi_index_breadth(response.json())
        if parsed:
            result["up"] = parsed["up"]
            result["down"] = parsed["down"]
        else:
            result["errors"].append(f"{day.isoformat()} MI_INDEX 沒有漲跌家數")
    except Exception as exc:
        result["errors"].append(f"{day.isoformat()} MI_INDEX {exc.__class__.__name__}")
    return result


def backfill_warning_chunk(
    today: date,
    spreadsheet,
    *,
    max_days: int = BATCH_DAYS,
    budget_seconds: float = BATCH_BUDGET_SECONDS,
    pause: float = 0.25,
    written_at: str = "",
    clock=None,
    prices: dict | None = None,
    hy=None,
    nfci=None,
    fetch_day=None,
    load_market=None,
) -> dict:
    """一次只補有限幾個交易日，每補完一天就寫入。時間到就停，留下剩下的日期。"""
    import time

    now = clock or time.monotonic
    started = now()
    failures: list[str] = []
    if load_market is None:
        def load_market():
            return _fetch_dated_prices(), _fetch_fred_history()
    if prices is None or hy is None or nfci is None:
        try:
            loaded_prices, loaded_fred = load_market()
            prices = prices if prices is not None else loaded_prices
            if hy is None or nfci is None:
                hy = loaded_fred[0] if hy is None else hy
                nfci = loaded_fred[1] if nfci is None else nfci
        except Exception as exc:
            failures.append(f"行情或 FRED {exc.__class__.__name__}")
            prices = prices or {}
            hy = hy or []
            nfci = nfci or []
    target = [item_day for item_day, _value in (prices.get("^TWII") or []) if item_day <= today and item_day.weekday() < 5]
    target = target[-BACKFILL_TRADING_DAYS:]
    filled = filled_warning_dates(spreadsheet, today)
    if not target:
        progress = _progress([], filled, 0, failures or ["沒有加權收盤，無法決定要補哪些交易日。"], "skipped")
        return progress
    missing = [day for day in target if day not in filled]
    if not missing:
        return _progress(target, filled, 0, failures, "kept")
    fetch = fetch_day or fetch_warning_day
    known_foreign = stored_foreign_points(spreadsheet, today)
    known_breadth = stored_breadth_points(spreadsheet, today)
    wrote = 0
    for index, day in enumerate(missing):
        if index >= max_days or now() - started >= budget_seconds - 1:
            break
        try:
            got = fetch(day) or {}
        except Exception as exc:
            failures.append(f"{day.isoformat()} {exc.__class__.__name__}")
            continue
        failures.extend(str(item) for item in (got.get("errors") or []))
        if got.get("foreign_net") is None and got.get("up") is None and got.get("down") is None:
            continue
        if got.get("foreign_net") is not None:
            known_foreign.append((day, got["foreign_net"]))
        if got.get("up") is not None and got.get("down") is not None:
            known_breadth.append((day, got["up"], got["down"]))
        row = {"date": day.isoformat()}
        row.update(_flags_for_day(day, prices, hy, nfci, known_foreign, known_breadth))
        if got.get("foreign_net") is not None:
            row["foreign_net"] = got["foreign_net"]
        if got.get("up") is not None:
            row["up"] = got["up"]
        if got.get("down") is not None:
            row["down"] = got["down"]
        record_warning_rows(spreadsheet, [row], written_at)
        filled.add(day)
        wrote += 1
        if pause and index + 1 < len(missing) and index + 1 < max_days and now() - started < budget_seconds:
            time.sleep(min(pause, max(0.0, budget_seconds - (now() - started))))
    status = "written" if wrote else "skipped"
    return _progress(target, filled, wrote, failures, status)


def _fetch_foreign_days(today: date, trading_days: int, pause: float) -> list[tuple]:
    from macro_risk import fetch_foreign_flows

    flows = fetch_foreign_flows(today, trading_days=trading_days, max_calendar_days=trading_days * 2, pause=pause)
    return [(day, net) for day, net in (flows or {}).get("days") or [] if net is not None]


def _fetch_dated_prices() -> dict:
    from order_insights import fetch_close_history

    history = fetch_close_history(list(PRICE_TICKERS), period="2y")
    found = {}
    for symbol, series in (history or {}).items():
        points = []
        index = getattr(series, "index", [])
        values = getattr(series, "tolist", lambda: [])()
        for stamp, value in zip(index, values):
            number = _finite(value)
            if number is None:
                continue
            day = stamp.date() if hasattr(stamp, "date") else stamp
            points.append((day, number))
        if points:
            found[str(symbol)] = points
    return found


def _fetch_fred_history() -> tuple[list, list]:
    from page1_lights import fetch_fred_observations

    def points(series_id):
        rows = []
        for item in fetch_fred_observations(series_id) or []:
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            number = _finite(item[1])
            if number is None or item[0] is None:
                continue
            rows.append((item[0], number))
        return rows

    return points("BAMLH0A0HYM2"), points("NFCI")


def flag_history_length(history: dict | None) -> int:
    flags = (history or {}).get("flags") or {}
    return len(flags.get("leading") or [])


def ensure_warning_history(spreadsheet, today: date, written_at: str = "") -> dict:
    """淨值快照裡只補一天、最多 8 秒。補不完就停，不能拖住已經寫好的淨值。"""
    return backfill_warning_chunk(
        today,
        spreadsheet,
        max_days=SNAPSHOT_BATCH_DAYS,
        budget_seconds=SNAPSHOT_BUDGET_SECONDS,
        written_at=written_at,
    )


def collect_warning_rows(today: date, fetch_foreign=None, fetch_breadth=None) -> list[dict]:
    """給每日快照補外資與上市股票漲跌家數。失敗的那一側留空，不寫 0。"""
    from datetime import timedelta

    rows: dict[str, dict] = {}

    def slot(day) -> dict:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        found = rows.get(key)
        if found is None:
            found = {"date": key}
            rows[key] = found
        return found

    try:
        if fetch_foreign is None:
            from macro_risk import fetch_foreign_flows

            flows = fetch_foreign_flows(today, trading_days=12, max_calendar_days=24, pause=0.15)
        else:
            flows = fetch_foreign(today)
        for day, net in (flows or {}).get("days") or []:
            if net is None:
                continue
            slot(day)["foreign_net"] = net
    except Exception:
        pass
    try:
        breadth_days = fetch_breadth(today) if fetch_breadth is not None else fetch_listed_breadth(today)
        for item in breadth_days or []:
            target = slot(item.get("date"))
            if item.get("up") is not None:
                target["up"] = item.get("up")
            if item.get("down") is not None:
                target["down"] = item.get("down")
    except Exception:
        pass
    return [rows[key] for key in sorted(rows)]


def fetch_listed_breadth(today: date, trading_days: int = 21, pause: float = 0.2) -> list[dict]:
    """證交所 MI_INDEX 上市股票漲跌家數。連不上就停，不把缺日補 0。"""
    import time

    import requests
    from datetime import timedelta

    found = []
    for offset in range(trading_days * 2):
        if len(found) >= trading_days:
            break
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            response = requests.get(
                "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                params={"date": day.strftime("%Y%m%d"), "type": "ALL", "response": "json"},
                headers={"User-Agent": "quant-command-center/1.0"},
                timeout=8,
            )
            response.raise_for_status()
            parsed = parse_mi_index_breadth(response.json())
        except Exception:
            break
        if not parsed:
            continue
        found.append({"date": day, "up": parsed["up"], "down": parsed["down"]})
        if pause:
            time.sleep(pause)
    return found


def parse_mi_index_breadth(payload) -> dict | None:
    """MI_INDEX「漲跌證券數合計」的股票欄。上漲、下跌拆不出來是 None。"""
    if not isinstance(payload, dict):
        return None
    table = None
    for item in payload.get("tables") or []:
        if "漲跌" in str(item.get("title") or ""):
            table = item
            break
    if not table:
        return None
    fields = [str(name) for name in (table.get("fields") or [])]
    try:
        column = fields.index("股票")
    except ValueError:
        return None
    found = {}
    for row in table.get("data") or []:
        if not row or len(row) <= column:
            continue
        label = str(row[0])
        raw = str(row[column])
        number = _finite(raw.split("(")[0].replace(",", ""))
        if number is None:
            continue
        if label.startswith("上漲"):
            found["up"] = number
        elif label.startswith("下跌"):
            found["down"] = number
    if "up" not in found or "down" not in found:
        return None
    return found


def breadth_percent_from_counts(up, down) -> float | None:
    up_n = _finite(up)
    down_n = _finite(down)
    if up_n is None or down_n is None or up_n + down_n <= 0:
        return None
    return up_n / (up_n + down_n) * 100.0


def tactical_dma_advice(held: list | None, prices: dict | None) -> list[dict]:
    """只對持有的槓桿／戰術部位。用對應指數或股票的 20 日線，不下單。"""
    prices = prices or {}
    rows = []
    seen = set()
    for item in held or []:
        if isinstance(item, str):
            symbol = _ticker_key_local(item)
        else:
            symbol = _ticker_key_local((item or {}).get("symbol") or (item or {}).get("標的"))
        if not symbol or symbol in seen or symbol not in TACTICAL_UNDERLYING:
            continue
        seen.add(symbol)
        underlying, label = TACTICAL_UNDERLYING[symbol]
        series = _series(prices.get(underlying))
        window = THRESHOLDS["dma20"]
        row = {
            "symbol": symbol,
            "underlying": underlying,
            "underlying_label": label,
            "last": series[-1] if series else None,
            "dma20": None,
            "below": None,
            "advice": "未知",
        }
        if len(series) < window:
            rows.append(row)
            continue
        average = sum(series[-window:]) / window
        below = series[-1] < average
        row["dma20"] = average
        row["below"] = below
        row["advice"] = "收破 20 日線減 1/3" if below else "未破 20 日線，不減"
        rows.append(row)
    return rows


def _ticker_key_local(symbol) -> str:
    text = str(symbol or "").strip().upper()
    for suffix in (".TW", ".TWO"):
        if text.endswith(suffix) and text[:-len(suffix)].isdigit():
            text = text[:-len(suffix)]
    return text


def _active(state: str) -> bool:
    """已確認的紅燈，或歷史不夠但今日條件成立的黃燈，都先當成亮。"""
    return state in {"red", "yellow"}


def suggestion(regime: str | None, layers: list[dict]) -> dict:
    current = regime_cap(regime)
    states = [layer["state"] for layer in layers]
    leading, internal, confirm = (states + ["unknown", "unknown", "unknown"])[:3]
    provisional = any(layer.get("provisional") for layer in layers)
    prefix = "暫行：遲滯資料不足，今日條件先當成亮燈。" if provisional else ""
    if current is None:
        return {
            "regime": None,
            "regime_cap": None,
            "warning_cap": None,
            "suggested_cap": None,
            "suggested_action": "牛熊狀態未知，不猜測槓桿上限。",
            "provisional": False,
        }
    name = str(regime)
    if _active(confirm):
        warning_name = "熊"
        action = prefix + "第三層確認，只留核心與避險。核心跟著預警上限。"
    elif _active(leading) and _active(internal):
        warning_name = _step_down(name, 2)
        action = prefix + "領先與內部都亮，槓桿上限再降一檔，並加避險。核心跟著預警上限。"
    elif _active(leading):
        warning_name = _step_down(name, 1)
        action = prefix + "第一層亮，槓桿上限降一檔。核心跟著預警上限。"
    elif _active(internal):
        warning_name = _step_down(name, 1)
        action = prefix + "第二層亮，槓桿上限降一檔，並加避險。核心跟著預警上限。"
    else:
        warning_name = name
        action = "預警未亮，沿用牛熊分數的槓桿上限。核心跟著這個上限。"
    warning = regime_cap(warning_name)
    suggested = None if warning is None else min(current, warning)
    return {
        "regime": name,
        "regime_cap": current,
        "warning_cap": warning,
        "suggested_cap": suggested,
        "suggested_action": action,
        "provisional": provisional,
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


def _credit_main(snapshot: dict) -> tuple[bool | None, dict]:
    gap = hy_gap(snapshot.get("hy"))
    rise = nfci_rise(snapshot.get("nfci"))
    hy_on = None if gap is None else gap >= THRESHOLDS["hy_lead"]
    nfci_on = None if rise is None else rise >= THRESHOLDS["nfci_rise"]
    flag = _or_flag(hy_on, nfci_on)
    parts = []
    if gap is None:
        parts.append("高收益距 60 日低點未知")
    else:
        parts.append(f"高收益距 60 日低點 {gap:.2f}")
    if rise is None:
        parts.append("NFCI 四週變化未知")
    else:
        parts.append(f"NFCI 四週 {rise:+.3f}")
    indicator = _flag_indicator(
        "信用合成",
        flag,
        value=gap if gap is not None else rise,
        value_text="；".join(parts),
        threshold=f"高收益 OAS 減 60 日低點 ≥ {THRESHOLDS['hy_lead']}，或 NFCI 四週上升 ≥ {THRESHOLDS['nfci_rise']}。",
        source="FRED BAMLH0A0HYM2、NFCI。" + FRED_LAG_NOTE,
    )
    return flag, indicator


def _foreign_main(snapshot: dict) -> tuple[bool | None, dict]:
    total = foreign_sum(snapshot.get("foreign_nets"))
    flag = None if total is None else total < THRESHOLDS["foreign_10d"]
    if total is None:
        text = "不滿 10 個交易日"
    else:
        text = f"10 日累計 {total / 1e8:,.0f} 億"
    indicator = _flag_indicator(
        "外資 10 日累計買賣超",
        flag,
        value=total,
        value_text=text,
        threshold="外資及陸資（不含外資自營商）10 日累計 < −1,500 億元。",
        source="TWSE BFI82U。每日快照留下歷史，請求不重抓十日。",
    )
    return flag, indicator


def _divergence_main(snapshot: dict) -> tuple[bool | None, dict]:
    prices = snapshot.get("prices") or {}
    twii = made_recent_high(prices.get("^TWII") or snapshot.get("twii"))
    sox = pct_below_high(prices.get("^SOX") or snapshot.get("sox"))
    flag = _and_flag(twii, sox)
    indicator = _flag_indicator(
        "加權新高而費半背離",
        flag,
        value_text=f"加權近 20 日創 252 日高：{twii}；費半低於 252 日高 5%：{sox}",
        threshold="加權近 20 根曾創 252 日新高，且 ^SOX 收盤 ≤ 自己 252 日高點的 95%。",
        source="Yahoo ^TWII、^SOX。",
    )
    return flag, indicator


def _breadth_main(snapshot: dict) -> tuple[bool | None, dict]:
    prices = snapshot.get("prices") or {}
    relative = ratio_return(prices.get("RSP") or snapshot.get("rsp"), prices.get("SPY") or snapshot.get("spy"))
    rsp_on = None if relative is None else relative <= THRESHOLDS["rsp_spy_drop"]
    tw_on = tw_breadth_on(snapshot.get("tw_breadth"), snapshot.get("tw_breadth_prior"))
    flag = _or_flag(rsp_on, tw_on)
    bits = []
    bits.append("RSP／SPY 未知" if relative is None else f"RSP／SPY {relative:+.1%}")
    tw = _finite(snapshot.get("tw_breadth"))
    bits.append("台股寬度未知" if tw is None else f"台股上漲比例 {tw:.1f}%")
    indicator = _flag_indicator(
        "寬度合成",
        flag,
        value=relative if relative is not None else tw,
        value_text="；".join(bits),
        threshold="RSP／SPY 二十日報酬 ≤ −2%，或台股上漲比例 < 40% 或較 20 個交易日前下降 ≥ 10 個百分點。",
        source="Yahoo RSP、SPY。台股用證交所 MI_INDEX 上市股票漲跌家數。",
    )
    return flag, indicator


def _leaders_main(snapshot: dict) -> tuple[bool | None, dict]:
    prices = snapshot.get("prices") or {}
    flags = snapshot.get("leader_flags")
    if flags is None:
        flags = _leader_flags(prices)
    flag = _leader_on(list(flags))
    known = sum(1 for item in flags if isinstance(item, bool))
    below = sum(1 for item in flags if item is True)
    indicator = _flag_indicator(
        "領導股跌破 60 日線",
        flag,
        value=below,
        value_text=f"{below} 檔在均線下，已知 {known} 檔",
        threshold="NVDA、2330（沒有收盤才改看 TSM）、MU、AVGO。至少 2 檔收盤低於 60 日線。湊不齊就未知。",
        source="Yahoo。",
    )
    return flag, indicator


def _high_beta_main(snapshot: dict) -> tuple[bool | None, dict]:
    prices = snapshot.get("prices") or {}
    relative = ratio_return(prices.get("SPHB") or snapshot.get("sphb"), prices.get("SPLV") or snapshot.get("splv"))
    flag = None if relative is None else relative <= THRESHOLDS["sphb_splv_drop"]
    text = "未知" if relative is None else f"SPHB／SPLV {relative:+.1%}"
    indicator = _flag_indicator(
        "高貝塔相對低波",
        flag,
        value=relative,
        value_text=text,
        threshold="SPHB／SPLV 二十日報酬 ≤ −5%。",
        source="Yahoo SPHB、SPLV。",
    )
    return flag, indicator


def _confirm_credit(snapshot: dict) -> tuple[bool | None, dict]:
    gap = hy_gap(snapshot.get("hy"))
    flag = None if gap is None else gap >= THRESHOLDS["hy_confirm"]
    text = "未知" if gap is None else f"距 60 日低點 {gap:.2f}"
    indicator = _flag_indicator(
        "高收益距低點擴大",
        flag,
        value=gap,
        value_text=text,
        threshold=f"高收益 OAS 減 60 日低點 ≥ {THRESHOLDS['hy_confirm']}。",
        source="FRED BAMLH0A0HYM2。" + FRED_LAG_NOTE,
    )
    return flag, indicator


def _confirm_tape(snapshot: dict) -> tuple[bool | None, dict]:
    prices = snapshot.get("prices") or {}
    below = below_dma(prices.get("^TWII") or snapshot.get("twii"), THRESHOLDS["leader_dma"])
    foreign_flag, _item = _foreign_main(snapshot)
    flag = _and_flag(below, foreign_flag)
    indicator = _flag_indicator(
        "加權跌破 60 日線且外資賣超",
        flag,
        value_text=f"加權在 60 日線下：{below}；外資 10 日賣超條件：{foreign_flag}",
        threshold="加權收盤低於 60 日線，且外資 10 日累計 < −1,500 億。",
        source="Yahoo ^TWII、TWSE BFI82U。",
    )
    return flag, indicator


def _layer(layer_id: str, name: str, pairs: list[tuple], prior: list | None, need: int) -> dict:
    flags = [flag for flag, _item in pairs]
    today = raw_layer(flags, need)
    hysteresis = apply_hysteresis(prior, today)
    return {
        "id": layer_id,
        "name": name,
        "state": hysteresis["state"],
        "raw_on": today,
        "provisional": bool(hysteresis.get("provisional")),
        "need": need,
        "hysteresis": hysteresis,
        "indicators": [item for _flag, item in pairs],
    }


def _watch_block(snapshot: dict) -> list[dict]:
    prices = snapshot.get("prices") or {}
    credit = snapshot.get("credit") or {}
    vix = _series(prices.get("^VIX") or snapshot.get("vix"))
    if not vix:
        vix_item = _indicator("VIX 高於 25", "unknown", threshold="收盤 > 25 僅供觀察。", source="Yahoo ^VIX", role="watch")
    elif vix[-1] > THRESHOLDS["vix_watch"]:
        vix_item = _indicator("VIX 高於 25", "red", value=vix[-1], value_text=f"{vix[-1]:.1f}", threshold="收盤 > 25 僅供觀察。", source="Yahoo ^VIX", role="watch")
    else:
        vix_item = _indicator("VIX 高於 25", "green", value=vix[-1], value_text=f"{vix[-1]:.1f}", threshold="收盤 > 25 僅供觀察。", source="Yahoo ^VIX", role="watch")
    watches = [
        vix_item,
        curve_indicator("十年減兩年利差", "T10Y2Y", snapshot.get("t10y2y")),
        curve_indicator("十年減三個月利差", "T10Y3M", snapshot.get("t10y3m")),
        defensive_indicator({"SPY": prices.get("SPY"), "XLV": prices.get("XLV"), "XLP": prices.get("XLP"), "XLU": prices.get("XLU"), "XLK": prices.get("XLK")}),
        _credit_indicator("金融壓力 STLFSI4", credit.get("stlfsi4_light"), "觀察。FRED STLFSI4。" + FRED_LAG_NOTE),
        _credit_indicator("銀行放款意願 DRTSCILM", credit.get("drtscilm_light"), "觀察。信用傳導。" + FRED_LAG_NOTE),
        _credit_indicator("銀行放款量 TOTLL", credit.get("totll_light"), "觀察。H.8。" + FRED_LAG_NOTE),
        _credit_indicator("工商放款 TOTCI", credit.get("totci_light"), "觀察。H.8。" + FRED_LAG_NOTE),
        index_trend_indicator("標普 500 跌破向下的 200 日線", prices.get("^GSPC") or snapshot.get("spx"), "觀察。Yahoo ^GSPC"),
        index_trend_indicator("加權指數跌破向下的 200 日線", prices.get("^TWII") or snapshot.get("twii"), "觀察。Yahoo ^TWII"),
    ]
    for item in watches:
        item["role"] = "watch"
    return watches


def assess(snapshot: dict | None = None) -> dict:
    """由已備好的數字組預警。沒傳的項目是未知。觀察項不點亮層級。"""
    snapshot = snapshot or {}
    history = snapshot.get("history") or {}
    leading_flag, leading_item = _credit_main(snapshot)
    foreign_flag, foreign_item = _foreign_main(snapshot)
    divergence_flag, divergence_item = _divergence_main(snapshot)
    breadth_flag, breadth_item = _breadth_main(snapshot)
    leader_flag, leader_item = _leaders_main(snapshot)
    beta_flag, beta_item = _high_beta_main(snapshot)
    confirm_credit_flag, confirm_credit_item = _confirm_credit(snapshot)
    confirm_tape_flag, confirm_tape_item = _confirm_tape(snapshot)
    layers = [
        _layer("leading", "第一層 領先", [(leading_flag, leading_item), (foreign_flag, foreign_item), (divergence_flag, divergence_item)], history.get("leading"), 2),
        _layer("internal", "第二層 內部", [(breadth_flag, breadth_item), (leader_flag, leader_item), (beta_flag, beta_item)], history.get("internal"), 2),
        _layer("confirm", "第三層 確認", [(confirm_credit_flag, confirm_credit_item), (confirm_tape_flag, confirm_tape_item)], history.get("confirm"), 1),
    ]
    prices = snapshot.get("prices") or {}
    advice = tactical_dma_advice(snapshot.get("held"), prices)
    plan = suggestion(snapshot.get("regime"), layers)
    return {
        "caption": CAPTION,
        "note": NOTE,
        "overall": overall_state(layers),
        "thresholds": dict(THRESHOLDS),
        "leaders": list(LEADERS),
        "breadth": {"us_universe": US_BREADTH_UNIVERSE, "tw_universe": TW_BREADTH_UNIVERSE},
        "layers": layers,
        "watches": _watch_block(snapshot),
        "tactical_advice": advice,
        "core_note": "核心部位跟著預警上限，不套用收破 20 日線減 1/3。",
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
    _HISTORY_CACHE["at"] = 0.0
    _HISTORY_CACHE["value"] = None


def _bundle_has_closes(prices: dict | None) -> bool:
    for value in (prices or {}).values():
        if isinstance(value, list) and any(_finite(item) is not None for item in value):
            return True
    return False


def closes_to_bundle(closes: dict | None) -> dict:
    """收盤序列（由舊到新）收成預警要用的行情。不夠長的項目留空，不當 0。"""
    closes = closes or {}
    bundle: dict = {}
    for symbol in PRICE_TICKERS:
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


def _fred_values(fetch_fred, series_id: str) -> list[float]:
    try:
        observations = fetch_fred(series_id) or []
    except Exception:
        return []
    values = []
    for item in observations:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            number = _finite(item[1])
        else:
            number = _finite(item)
        if number is not None:
            values.append(number)
    return values


def _fred_points(fetch_fred, series_id: str) -> list:
    try:
        observations = fetch_fred(series_id) or []
    except Exception:
        return []
    return list(observations)


def _cached_curves(today: date, fetch_fred) -> dict:
    if _PRICE_CACHE.get("curve_day") == today.isoformat():
        return dict(_PRICE_CACHE.get("curves") or {})
    curves = {}
    for series_id in ("T10Y2Y", "T10Y3M"):
        try:
            curves[series_id] = _latest(fetch_fred(series_id))
        except Exception:
            curves[series_id] = None
    _PRICE_CACHE["curve_day"] = today.isoformat()
    _PRICE_CACHE["curves"] = curves
    return curves


def cached_warning_history(today: date, reader=None) -> dict:
    """十五分鐘內不重讀試算表。沒有存檔就是空，請求不去補抓十日外資。"""
    import time

    current = time.time()
    cached = _HISTORY_CACHE.get("value")
    if isinstance(cached, dict) and current - float(_HISTORY_CACHE.get("at") or 0) < 15 * 60:
        return dict(cached)
    if reader is None:
        reader = read_warning_history
    try:
        found = reader(today) or {}
    except Exception:
        found = {}
    if not isinstance(found, dict):
        found = {}
    _HISTORY_CACHE["at"] = current
    _HISTORY_CACHE["value"] = found
    return dict(found)


def _flag_cell(value) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return ""


def _cell_flag(value) -> bool | None:
    text = str(value or "").strip()
    if text in {"1", "True", "true", "成立"}:
        return True
    if text in {"0", "False", "false", "未成立"}:
        return False
    return None


def record_warning_rows(spreadsheet, rows: list[dict], written_at: str = "") -> dict:
    """依日期補上外資與漲跌家數。條件欄空白不覆蓋已經寫過的 0／1。"""
    from sheet_dates import parse_sheet_date

    usable = [row for row in rows or [] if row.get("date")]
    if not usable:
        return {"status": "skipped", "sheet": WARNING_TAB, "detail": "沒有可寫的列，沒有把缺值寫成 0。"}
    from nav_snapshot import _ensure_worksheet

    worksheet, created, _migrated = _ensure_worksheet(spreadsheet, WARNING_TAB, list(WARNING_HEADER))
    existing = worksheet.col_values(1)
    index_by_day = {}
    for index, raw in enumerate(existing[1:], start=2):
        day = parse_sheet_date(raw)
        if day is not None:
            index_by_day[day.isoformat()] = index
    for row in usable:
        day = parse_sheet_date(row.get("date"))
        if day is None:
            continue
        values = [
            day.isoformat(),
            _flag_cell(row.get("leading")) if "leading" in row else "",
            _flag_cell(row.get("internal")) if "internal" in row else "",
            _flag_cell(row.get("confirm")) if "confirm" in row else "",
            "" if row.get("foreign_net") is None else row.get("foreign_net"),
            "" if row.get("up") is None else row.get("up"),
            "" if row.get("down") is None else row.get("down"),
            written_at,
        ]
        target = index_by_day.get(day.isoformat())
        if target is None:
            target = max(len(existing), 1) + 1
            existing.append(day.isoformat())
            index_by_day[day.isoformat()] = target
            worksheet.update(range_name=f"A{target}:H{target}", values=[values], value_input_option="USER_ENTERED")
            continue
        current = worksheet.row_values(target)
        while len(current) < 8:
            current.append("")
        for offset, key in ((1, "leading"), (2, "internal"), (3, "confirm")):
            if key in row and current[offset] in {"", None}:
                current[offset] = values[offset]
        if "foreign_net" in row and row.get("foreign_net") is not None and str(current[4]).strip() == "":
            current[4] = values[4]
        if row.get("up") is not None and str(current[5]).strip() == "":
            current[5] = values[5]
        if row.get("down") is not None and str(current[6]).strip() == "":
            current[6] = values[6]
        current[7] = written_at or current[7]
        worksheet.update(range_name=f"A{target}:H{target}", values=[current[:8]], value_input_option="USER_ENTERED")
    return {"status": "written", "sheet": WARNING_TAB, "rows": len(usable), "sheet_created": created, "detail": ""}


def read_warning_history(today: date) -> dict:
    from portfolio_risk import load_service_account_info, open_quant_spreadsheet

    return warning_history_from_sheet(open_quant_spreadsheet(load_service_account_info()), today)


def warning_history_from_sheet(spreadsheet, today: date) -> dict:
    """讀每日列。外資要滿 10 筆才回傳。寬度用最新一列與往前 20 列。條件不含今天，今天由請求重算。"""
    from sheet_dates import parse_sheet_date

    try:
        worksheet = spreadsheet.worksheet(WARNING_TAB)
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
    parsed = []
    for row in rows or []:
        day = parse_sheet_date(row.get("日期"))
        if day is None or day > today:
            continue
        parsed.append((day, row))
    parsed.sort(key=lambda item: item[0])
    foreign = []
    for _day, row in parsed:
        number = _finite(str(row.get("外資買賣超") or "").replace(",", ""))
        if number is not None:
            foreign.append(number)
    breadth_rows = []
    for day, row in parsed:
        percent = breadth_percent_from_counts(row.get("上漲家數"), row.get("下跌家數"))
        if percent is not None:
            breadth_rows.append((day, percent))
    flags = {"leading": [], "internal": [], "confirm": []}
    for day, row in parsed:
        if day >= today:
            continue
        leading = _cell_flag(row.get("領先條件"))
        internal = _cell_flag(row.get("內部條件"))
        confirm = _cell_flag(row.get("確認條件"))
        if leading is None and internal is None and confirm is None:
            continue
        flags["leading"].append(leading)
        flags["internal"].append(internal)
        flags["confirm"].append(confirm)
    latest = breadth_rows[-1][1] if breadth_rows else None
    prior = breadth_rows[-21][1] if len(breadth_rows) >= 21 else None
    return {
        "foreign_nets": foreign[-THRESHOLDS["foreign_days"]:] if len(foreign) >= THRESHOLDS["foreign_days"] else None,
        "tw_breadth": latest,
        "tw_breadth_prior": prior,
        "flags": flags,
    }


def _return_over(closes: list[float] | None, weeks: int) -> float | None:
    series = week_returns(closes, weeks)
    if not series:
        return None
    total = 1.0
    for item in series:
        total *= 1.0 + item
    return total - 1.0


def load_bear_warning(today: date, regime: str | None = None, credit_board: dict | None = None, fetch_fred=None, fetch_prices=None, regime_reader=None, breadth_reader=None, history_reader=None, held_symbols=None) -> dict:
    """信用傳導沿用呼叫端已經抓好的 board，不再抓一次。行情沒傳函式就抓 Yahoo。廣度只讀每日快照。"""
    if regime not in CAP_STEPS:
        regime = cached_sheet_regime(reader=regime_reader)
    if fetch_fred is None:
        from page1_lights import fetch_fred_observations

        fetch_fred = fetch_fred_observations
    prices = cached_price_bundle(today, fetch_prices)
    history = cached_warning_history(today, reader=history_reader)
    curves = _cached_curves(today, fetch_fred)
    hy_levels = _fred_values(fetch_fred, "BAMLH0A0HYM2")
    nfci_points = _fred_points(fetch_fred, "NFCI")
    snapshot = {
        "regime": regime,
        "hy": hy_levels,
        "nfci": nfci_points,
        "foreign_nets": history.get("foreign_nets"),
        "tw_breadth": history.get("tw_breadth"),
        "tw_breadth_prior": history.get("tw_breadth_prior"),
        "history": history.get("flags") or {},
        "credit": {
            "drtscilm_light": _row_light(credit_board, "drtscilm"),
            "stlfsi4_light": _row_light(credit_board, "stlfsi4"),
            "totll_light": _row_light(credit_board, "totll"),
            "totci_light": _row_light(credit_board, "totci"),
        },
        "t10y2y": curves.get("T10Y2Y"),
        "t10y3m": curves.get("T10Y3M"),
        "prices": prices,
        "held": held_symbols,
    }
    return assess(snapshot)


LAYER_ACTION = {
    "leading": "上限降一級",
    "internal": "再降一級＋避險",
    "confirm": "只留核心與避險",
}
_DOT = {"red": "🔴", "yellow": "🟡", "green": "🟢", "unknown": "⚪"}


def layer_light_counts(layer: dict | None) -> dict:
    """已亮只數主訊號裡的紅燈。未知不算亮。"""
    indicators = [
        item for item in (layer or {}).get("indicators") or []
        if item.get("role", "main") == "main"
    ]
    return {
        "lit": sum(1 for item in indicators if item.get("state") == "red"),
        "unknown": sum(1 for item in indicators if item.get("state") not in {"red", "yellow", "green"}),
        "total": len(indicators),
    }


def layer_title(layer: dict | None) -> str:
    """層級標題。已亮 N 是紅燈數，分母是這一層的主訊號數。"""
    layer = layer or {}
    counts = layer_light_counts(layer)
    need = layer.get("need") or 1
    name = layer.get("name") or "這一層"
    title = f"{name}｜需≥{need} 亮｜已亮 {counts['lit']}/{counts['total']}"
    action = LAYER_ACTION.get(str(layer.get("id") or ""))
    if action:
        title += f"｜動作：{action}"
    if counts["unknown"]:
        title += f"｜未知 {counts['unknown']}"
    return title


def hysteresis_status_line(hysteresis: dict | None) -> str:
    """遲滯一句話。已確認亮燈時補上熄燈要幾天。"""
    hyst = hysteresis or {}
    raw = hyst.get("raw_on")
    if raw is True:
        today = "今日成立"
    elif raw is False:
        today = "今日未成立"
    else:
        today = "今日未知"
    on_streak = int(hyst.get("on_streak") or 0)
    on_days = int(hyst.get("on_days") or 3)
    line = f"{today}｜已連續 {on_streak} 日（需 {on_days} 日生效）"
    if hyst.get("state") == "red":
        off_days = int(hyst.get("off_days") or 10)
        off_streak = int(hyst.get("off_streak") or 0)
        line += f"｜熄燈需連續 {off_days} 日不成立，目前 {off_streak} 日"
    return line


def cap_headline(report: dict | None) -> str:
    """建議槓桿上限。數字沿用 payload 的 suggested_cap，這裡不再重算。"""
    cap = (report or {}).get("suggested_cap")
    if cap is None:
        return "建議槓桿上限 未知"
    return f"建議槓桿上限 {float(cap):.1f} 倍"


def indicator_line(item: dict | None) -> str:
    item = item or {}
    dot = _DOT.get(item.get("state"), "⚪")
    return f"{dot} {item.get('name') or '子指標'}　{item.get('value_text') or '未知'}"


def _cap_source_line(report: dict) -> str:
    def _times(value) -> str:
        return "未知" if value is None else f"{float(value):.1f} 倍"

    return (
        f"牛熊上限 {_times(report.get('regime_cap'))}（牛熊分數）。"
        f"預警上限 {_times(report.get('warning_cap'))}（預警燈）。取較低者。"
    )


def _overall_banner(state: str, headline: str) -> str:
    colors = {
        "red": ("#b42318", "#fff"),
        "yellow": ("#b54708", "#fff"),
        "green": ("#067647", "#fff"),
        "unknown": ("#6b7280", "#fff"),
    }
    background, color = colors.get(state, colors["unknown"])
    label = _STATE_TEXT.get(state, "未知")
    return (
        f'<div style="background:{background};color:{color};border-radius:12px;'
        'padding:1rem 1.1rem;margin:0.4rem 0 0.6rem 0;">'
        f'<div style="font-size:1.35rem;font-weight:800;">{label}燈</div>'
        f'<div style="font-size:1.7rem;font-weight:800;line-height:1.3;">{headline}</div>'
        "</div>"
    )


def render_bear_panel(st, report: dict) -> None:
    """第一頁信用傳導旁邊。只改層級呈現，不重算燈號。"""
    report = report or {}
    if not report.get("layers"):
        report = assess({"regime": report.get("regime")})
    st.subheader("熊市預警燈")
    st.caption(CAPTION)
    overall = report.get("overall") or overall_state(report.get("layers"))
    st.markdown(_overall_banner(overall, cap_headline(report)), unsafe_allow_html=True)
    st.caption(_cap_source_line(report))
    if report.get("suggested_action"):
        st.caption(report["suggested_action"])
    for layer in report.get("layers") or []:
        with st.container(border=True):
            state = layer.get("state") or "unknown"
            st.markdown(f"### {_DOT.get(state, '⚪')} {layer_title(layer)}")
            if layer.get("provisional") or (layer.get("hysteresis") or {}).get("provisional"):
                st.warning("疑似亮燈（遲滯資料不足）")
            st.caption(hysteresis_status_line(layer.get("hysteresis")))
            for item in layer.get("indicators") or []:
                st.markdown(
                    f'<div style="font-size:0.92rem;margin-left:0.6rem;">{indicator_line(item)}</div>',
                    unsafe_allow_html=True,
                )
                detail = "。".join(part for part in (item.get("threshold"), item.get("source")) if part)
                if detail:
                    st.caption(detail)
    with st.expander("觀察指標（不升級）", expanded=False):
        for item in report.get("watches") or []:
            word = _STATE_TEXT.get(item.get("state"), "未知")
            st.markdown(
                (
                    '<div style="color:#6b7280;font-size:0.9rem;">'
                    f"● {item.get('name') or '觀察'}　{item.get('value_text') or '未知'}　{word}"
                    "</div>"
                ),
                unsafe_allow_html=True,
            )
    st.caption(report.get("core_note") or "核心部位跟著預警上限。")
    advice = report.get("tactical_advice") or []
    if advice:
        st.markdown("**戰術曝險：收破 20 日線減 1/3**")
        for item in advice:
            st.caption(f"{item.get('symbol')} 對照 {item.get('underlying_label')}（{item.get('underlying')}）：{item.get('advice')}")
    else:
        st.caption("戰術曝險若收破對應標的的 20 日線，減三分之一。沒有持倉或沒有收盤時不列。不下單。")
    st.caption(US_BREADTH_UNIVERSE)
    st.caption(TW_BREADTH_UNIVERSE)
