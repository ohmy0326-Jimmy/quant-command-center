"""信用傳導。只觀察 Fed 政策有沒有變成信用壓力。

不計入牛熊總分，不進 Macro_Score，不推播 Telegram。
高收益 OAS 沿用 page1_lights.fetch_fred_observations，不另算第五頁利差分。
門檻只改 THRESHOLDS。
"""

from __future__ import annotations

import time
from datetime import date

# 燈號門檻。畫面註解與 API 都讀這張表。
THRESHOLDS = {
    "drtscilm": {
        "series_id": "DRTSCILM",
        "green_below": 0.0,
        "yellow_high": 20.0,
        "jump_red": 15.0,
        "stale_days": 150,
        "frequency": "quarter",
    },
    "lending": {
        "yoy_green_above": 3.0,
        "yoy_red_below": 0.0,
        "deceleration_gap": 3.0,
        "week_yoy_steps": 52,
        "week_fast_steps": 13,
        "week_stale_days": 21,
        "month_yoy_steps": 12,
        "month_fast_steps": 3,
        "month_stale_days": 45,
        "totll_series_id": "TOTLL",
        "totci_series_id": "TOTCI",
        "totci_fallback_series_id": "BUSLOANS",
    },
    "hy_oas": {
        "series_id": "BAMLH0A0HYM2",
        "to_bp": 100.0,
        "green_below_bp": 350.0,
        "yellow_high_bp": 400.0,
        "jump_red_bp": 100.0,
        "jump_sessions": 21,
        "stale_days": 10,
    },
    "ig_oas": {
        "series_id": "BAMLC0A0CM",
        "to_bp": 100.0,
        "green_below_bp": 120.0,
        "yellow_high_bp": 150.0,
        "jump_sessions": 21,
        "stale_days": 10,
    },
    "nfci": {
        "series_id": "NFCI",
        "red_above": 0.0,
        "yellow_low": -0.2,
        "stale_days": 21,
    },
    "stlfsi4": {
        "series_id": "STLFSI4",
        "red_above": 0.0,
        "stale_days": 21,
    },
}

SERIES = (
    {"id": "drtscilm", "name": "銀行放款意願（SLOOS 大中型 C&I）", "kind": "standards"},
    {"id": "totll", "name": "銀行放款量 TOTLL", "kind": "lending", "series_id": "TOTLL"},
    {"id": "totci", "name": "工商放款（TOTCI）", "kind": "lending", "series_id": "TOTCI"},
    {"id": "hy_oas", "name": "高收益債 OAS", "kind": "price"},
    {"id": "ig_oas", "name": "投資級債 OAS", "kind": "price"},
    {"id": "nfci", "name": "金融條件 NFCI", "kind": "price"},
    {"id": "stlfsi4", "name": "金融壓力 STLFSI4", "kind": "price_optional"},
)

CDS_TEXT = "CDS：無免費可靠來源，以利差代替"
DISCLAIMER = "只觀察。不計入牛熊總分，不進 Macro_Score，不推播 Telegram。總經風險要放款量與信用價格同時惡化才算傳導確認。"
RULE_NOTE = (
    "放款意願：綠 <0、黃 0–20、紅 >20，或單季跳升 ≥15 個百分點也是紅。超過 150 天算過舊。"
    "放款量：綠為年增 >3%；黃為年增 0–3%，或快速減速（13 週年化比年增率低 3 個百分點以上）；紅為年增 <0%。"
    "TOTLL 與工商放款 TOTCI 都是 H.8 週資料，年增看 52 週，過舊門檻 21 天。"
    "TOTCI 抓不到時才用 BUSLOANS，那一列標明月資料，年增看 12 期、快速減速看 3 個月年化，超過 45 天算過舊。"
    "兩條放款量各自給燈，整體取較差者；其中一條過舊或缺失時，不把另一條綠燈當成放款量整體還健康。"
    "高收益 OAS 換成基點：綠 <350、黃 350–400、紅 >400，或約 21 個交易日上升 ≥100bp 也是紅。"
    "投資級 OAS：綠 <120、黃 120–150、紅 >150。日資料超過 10 天算過舊。"
    "NFCI 是週資料：紅 >0、黃 -0.2–0、其餘綠，超過 21 天算過舊。"
    "STLFSI4 是週資料：紅 >0、其餘綠；缺了不擋其他價格指標，有紅黃仍算價格惡化。"
    "高收益 OAS 與第五頁 hy_spread 共用同一條抓取，這裡不另算 Macro_Score。"
)

STAGE_EQUITY = "只有股市修正（信用穩）"
STAGE_PRICE = "信用開始收緊（價格先動）"
STAGE_STANDARDS = "信用開始收緊（銀行放款意願轉緊，價格與放款量仍穩）"
STAGE_BOTH = "價格與放款意願同步轉緊"
STAGE_CONFIRMED = "傳導確認（放款量＋信用價格同步惡化）"
STAGE_VOLUME = "放款量轉弱，信用價格尚未同步惡化"
STAGE_UNKNOWN = "資料不足，無法判定傳導"

LIGHT_TEXT = {
    "green": "綠",
    "yellow": "黃",
    "red": "紅",
    "missing": "資料缺失",
    "stale": "資料過舊",
    "unavailable": "—",
}
_RANK = {"green": 0, "yellow": 1, "red": 2}
_CACHE_TTL = 3600
_cache: dict[str, tuple[float, list]] = {}


def memoize_fetch(fetch, ttl: float = _CACHE_TTL, clock=time.time):
    """成功結果留 ttl 秒。例外不留，下次會再抓。"""
    store: dict[str, tuple[float, list]] = {}

    def wrapped(series_id: str):
        now = clock()
        hit = store.get(series_id)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        data = fetch(series_id)
        store[series_id] = (now, data)
        return data

    return wrapped


def clear_fetch_cache() -> None:
    _cache.clear()


def default_fetch(series_id: str):
    """有 FRED_API_KEY 先走官方 API，否則公開 CSV。失敗不進快取。"""
    now = time.time()
    hit = _cache.get(series_id)
    if hit is not None and now - hit[0] < _CACHE_TTL:
        return hit[1]
    from page1_lights import fetch_fred_observations

    data = fetch_fred_observations(series_id)
    _cache[series_id] = (now, data)
    return data


def public_reason(exc: BaseException) -> str:
    from page1_lights import public_fetch_reason

    return public_fetch_reason(exc)


def _age_days(day: date, today: date) -> int:
    return (today - day).days


def _at(observations: list[tuple[date, float]], steps_back: int) -> tuple[date, float] | None:
    index = len(observations) - 1 - steps_back
    if index < 0:
        return None
    return observations[index]


def infer_frequency(observations: list[tuple[date, float]]) -> str | None:
    """用最近幾期間隔判斷週或月。無法判斷就不要套錯年增窗口。"""
    if len(observations) < 4:
        return None
    gaps = []
    for offset in range(1, min(8, len(observations))):
        gaps.append((observations[-offset][0] - observations[-1 - offset][0]).days)
    gaps.sort()
    median = gaps[len(gaps) // 2]
    if 5 <= median <= 10:
        return "week"
    if 25 <= median <= 40:
        return "month"
    if 80 <= median <= 110:
        return "quarter"
    return None


def _blank_row(spec: dict, light: str, reason: str, *, value=None, day: date | None = None, change=None, change_label: str = "", value_text: str = "—", change_text: str = "—") -> dict:
    return {
        "id": spec["id"],
        "name": spec["name"],
        "series_id": spec.get("series_id") or THRESHOLDS.get(spec["id"], {}).get("series_id", ""),
        "value": value,
        "unit": spec.get("unit", ""),
        "date": day.isoformat() if day else None,
        "change": change,
        "change_label": change_label,
        "light": light,
        "light_text": LIGHT_TEXT[light],
        "reason": reason,
        "value_text": value_text,
        "change_text": change_text,
    }


def _stale_or_missing(spec, observations, today, stale_days: int, reason: str) -> dict | None:
    if not observations:
        return _blank_row(spec, "missing", reason or "沒有有效觀測")
    day, value = observations[-1]
    if _age_days(day, today) > stale_days:
        return _blank_row(
            spec,
            "stale",
            f"最新資料 {day.isoformat()}，超過 {stale_days} 天",
            value=value,
            day=day,
            value_text=f"{value:g}",
        )
    return None


def score_drtscilm(observations, today: date, reason: str = "") -> dict:
    spec = {"id": "drtscilm", "name": SERIES[0]["name"], "series_id": "DRTSCILM", "unit": "淨收緊百分點"}
    rule = THRESHOLDS["drtscilm"]
    blocked = _stale_or_missing(spec, observations, today, rule["stale_days"], reason)
    if blocked and blocked["light"] == "missing":
        return blocked
    if not observations:
        return blocked
    day, value = observations[-1]
    previous = _at(observations, 1)
    jump = None if previous is None else value - previous[1]
    change_text = "—" if jump is None else f"較前一季 {jump:+.1f}pt"
    if blocked and blocked["light"] == "stale":
        blocked.update(value=value, change=jump, change_label="較前一季", value_text=f"{value:.1f}", change_text=change_text, unit=spec["unit"])
        return blocked
    if jump is not None and jump >= rule["jump_red"]:
        light = "red"
    elif value < rule["green_below"]:
        light = "green"
    elif value <= rule["yellow_high"]:
        light = "yellow"
    else:
        light = "red"
    return _blank_row(
        spec, light, "", value=value, day=day, change=jump, change_label="較前一季",
        value_text=f"{value:.1f}", change_text=change_text,
    )


def _growth(latest: float, past: float, power: float) -> float | None:
    if past <= 0 or latest <= 0:
        return None
    return (latest / past) ** power - 1.0


def score_lending(spec: dict, observations, today: date, reason: str = "", frequency: str | None = None) -> dict:
    rule = THRESHOLDS["lending"]
    freq = frequency or infer_frequency(observations or [])
    if freq == "month":
        yoy_steps = rule["month_yoy_steps"]
        fast_steps = rule["month_fast_steps"]
        power = rule["month_yoy_steps"] / rule["month_fast_steps"]
        stale_days = rule["month_stale_days"]
        fast_name = "3個月年化"
        change_label = "較前一月"
        freq_note = "FRED 這條是月資料，年增看 12 期，快速減速看 3 個月年化。"
    elif freq == "week":
        yoy_steps = rule["week_yoy_steps"]
        fast_steps = rule["week_fast_steps"]
        power = rule["week_yoy_steps"] / rule["week_fast_steps"]
        stale_days = rule["week_stale_days"]
        fast_name = "13週年化"
        change_label = "較前一週"
        freq_note = ""
    else:
        row = _blank_row({**spec, "unit": "年增率"}, "missing", reason or "無法判斷這條是週資料還是月資料")
        return row
    item = {**spec, "unit": "年增率（百分點）"}
    blocked = _stale_or_missing(item, observations, today, stale_days, reason)
    if blocked and blocked["light"] == "missing":
        return blocked
    latest_day, latest = observations[-1]
    yoy_point = _at(observations, yoy_steps)
    fast_point = _at(observations, fast_steps)
    prev = _at(observations, 1)
    yoy = None if yoy_point is None else _growth(latest, yoy_point[1], 1.0)
    ann = None if fast_point is None else _growth(latest, fast_point[1], power)
    yoy_pct = None if yoy is None else round(yoy * 100.0, 6)
    ann_pct = None if ann is None else round(ann * 100.0, 6)
    level_change = None
    if prev is not None and prev[1] > 0:
        level_change = latest / prev[1] - 1.0
    prior_yoy = None
    if prev is not None and yoy_point is not None:
        older = _at(observations, yoy_steps + 1)
        if older is not None:
            prior = _growth(prev[1], older[1], 1.0)
            if prior is not None and yoy is not None:
                prior_yoy = (yoy - prior) * 100.0
    bits = []
    if prior_yoy is not None:
        bits.append(f"年增{change_label} {prior_yoy:+.2f}pt")
    if level_change is not None:
        bits.append(f"餘額{change_label} {level_change * 100:+.2f}%")
    change_text = "；".join(bits) if bits else "—"
    value_text = "—"
    if yoy_pct is not None and ann_pct is not None:
        value_text = f"年增 {yoy_pct:.2f}%；{fast_name} {ann_pct:.2f}%"
    elif yoy_pct is not None:
        value_text = f"年增 {yoy_pct:.2f}%"
    if freq_note:
        change_text = f"{change_text}。{freq_note}" if change_text != "—" else freq_note
    if blocked and blocked["light"] == "stale":
        blocked.update(
            value=yoy_pct, change=prior_yoy, change_label=change_label,
            value_text=value_text, change_text=change_text, unit=item["unit"],
        )
        return blocked
    if yoy_pct is None or ann_pct is None:
        row = _blank_row(item, "missing", "年增或快速減速算不出來", day=latest_day, value_text=value_text, change_text=change_text)
        return row
    if yoy_pct < rule["yoy_red_below"]:
        light = "red"
    elif yoy_pct <= rule["yoy_green_above"] or ann_pct <= yoy_pct - rule["deceleration_gap"]:
        light = "yellow"
    else:
        light = "green"
    return _blank_row(
        item, light, freq_note, value=yoy_pct, day=latest_day, change=prior_yoy,
        change_label=change_label, value_text=value_text, change_text=change_text,
    )


def score_oas(kind: str, observations, today: date, reason: str = "") -> dict:
    rule = THRESHOLDS[kind]
    spec = next(item for item in SERIES if item["id"] == kind)
    item = {**spec, "series_id": rule["series_id"], "unit": "bp"}
    blocked = _stale_or_missing(item, observations, today, rule["stale_days"], reason)
    if blocked and blocked["light"] == "missing":
        return blocked
    day, raw = observations[-1]
    bp = raw * rule["to_bp"]
    sessions = rule.get("jump_sessions")
    jump = None
    if sessions:
        past = _at(observations, sessions)
        if past is not None:
            jump = bp - past[1] * rule["to_bp"]
    change_text = "—" if jump is None else f"較約 {sessions} 個交易日前 {jump:+.0f} bp"
    value_text = f"{bp:.0f} bp"
    if blocked and blocked["light"] == "stale":
        blocked.update(value=bp, change=jump, change_label=change_text and "較約 21 個交易日前", value_text=value_text, change_text=change_text, unit="bp")
        return blocked
    if jump is not None and "jump_red_bp" in rule and jump >= rule["jump_red_bp"]:
        light = "red"
    elif bp < rule["green_below_bp"]:
        light = "green"
    elif bp <= rule["yellow_high_bp"]:
        light = "yellow"
    else:
        light = "red"
    return _blank_row(item, light, "", value=bp, day=day, change=jump, change_label="較約 21 個交易日前" if sessions else "", value_text=value_text, change_text=change_text)


def score_index(kind: str, observations, today: date, reason: str = "") -> dict:
    rule = THRESHOLDS[kind]
    spec = next(item for item in SERIES if item["id"] == kind)
    item = {**spec, "series_id": rule["series_id"], "unit": "指數"}
    blocked = _stale_or_missing(item, observations, today, rule["stale_days"], reason)
    if blocked and blocked["light"] == "missing":
        return blocked
    day, value = observations[-1]
    previous = _at(observations, 1)
    change = None if previous is None else value - previous[1]
    change_text = "—" if change is None else f"較前一週 {change:+.3f}"
    value_text = f"{value:.3f}"
    if blocked and blocked["light"] == "stale":
        blocked.update(value=value, change=change, change_label="較前一週", value_text=value_text, change_text=change_text, unit="指數")
        return blocked
    if "yellow_low" in rule and rule["yellow_low"] <= value <= rule["red_above"]:
        light = "yellow"
    elif value > rule["red_above"]:
        light = "red"
    else:
        light = "green"
    return _blank_row(item, light, "", value=value, day=day, change=change, change_label="較前一週", value_text=value_text, change_text=change_text)


def _usable(light: str | None) -> str | None:
    if light in _RANK:
        return light
    return None


def worst_light(lights: list[str | None], *, require_all: bool) -> str | None:
    """有紅或黃就取較差者。要整體綠燈時，require_all 的每一條都得是綠。"""
    colors = [_usable(item) for item in lights]
    known = [item for item in colors if item is not None]
    if any(item == "red" for item in known):
        return "red"
    if any(item == "yellow" for item in known):
        return "yellow"
    if require_all and len(known) == len(lights) and known and all(item == "green" for item in known):
        return "green"
    if not require_all and known and all(item == "green" for item in known):
        return "green"
    return None


def judge_stage(volume: str | None, price: str | None, standards: str | None) -> tuple[str, str, str | None]:
    """放款量、信用價格、放款意願的燈號 → (階段句, stage_code, stage_trigger)。

    volume / price 是 green、yellow、red 或 None（無法判斷）。
    standards 黃或紅算收緊；缺失、過舊、綠都不算收緊。
    stage_code 在價格或放款意願收緊、放款量未惡化時都是 price_first。
    stage_trigger 只在這一級有值：price、standards 或 both。
    """
    if volume is None or price is None:
        return STAGE_UNKNOWN, "insufficient", None
    volume_bad = volume in {"yellow", "red"}
    price_bad = price in {"yellow", "red"}
    standards_bad = standards in {"yellow", "red"}
    if volume_bad and price_bad:
        return STAGE_CONFIRMED, "confirmed", None
    if (price_bad or standards_bad) and not volume_bad:
        if price_bad and standards_bad:
            return STAGE_BOTH, "price_first", "both"
        if standards_bad:
            return STAGE_STANDARDS, "price_first", "standards"
        return STAGE_PRICE, "price_first", "price"
    if volume_bad and not price_bad:
        return STAGE_VOLUME, "volume_only", None
    return STAGE_EQUITY, "equity_only", None


def _cds_row() -> dict:
    return {
        "id": "cds",
        "name": "CDS",
        "series_id": "",
        "value": None,
        "unit": "",
        "date": None,
        "change": None,
        "change_label": "",
        "light": "unavailable",
        "light_text": "—",
        "reason": CDS_TEXT,
        "value_text": "—",
        "change_text": "—",
    }


def _totci_row(observations, today: date, reason: str, fallback: str | None) -> dict:
    spec = next(item for item in SERIES if item["id"] == "totci")
    if fallback == "BUSLOANS":
        spec = {
            **spec,
            "name": "工商放款（BUSLOANS，月資料備援）",
            "series_id": THRESHOLDS["lending"]["totci_fallback_series_id"],
        }
        return score_lending(spec, observations, today, reason, frequency="month")
    return score_lending(spec, observations, today, reason)


def resolve_totci(fetch) -> tuple[list | None, str, str | None]:
    """先抓週資料 TOTCI。失敗才改抓月資料 BUSLOANS。回傳 (觀測, 原因, 備援代號)。"""
    rule = THRESHOLDS["lending"]
    try:
        data = fetch(rule["totci_series_id"])
    except Exception as exc:
        data = None
        totci_reason = public_reason(exc)
    else:
        if data:
            return data, "", None
        totci_reason = "TOTCI 沒有有效觀測"
    try:
        backup = fetch(rule["totci_fallback_series_id"])
    except Exception as exc:
        return None, f"TOTCI：{totci_reason}。月資料備援 BUSLOANS：{public_reason(exc)}", None
    if not backup:
        return None, f"TOTCI：{totci_reason}。月資料備援 BUSLOANS：沒有有效觀測", None
    return backup, "", "BUSLOANS"


def build_board(series: dict | None, reasons: dict | None, today: date, fallbacks: dict | None = None) -> dict:
    """series[id] 是由舊到新的 (日期, 數值)，或 None。reasons 是抓取失敗的原因，不含金鑰。"""
    series = series or {}
    reasons = reasons or {}
    fallbacks = fallbacks or {}
    rows = [
        score_drtscilm(series.get("drtscilm"), today, reasons.get("drtscilm", "")),
        score_lending(SERIES[1], series.get("totll"), today, reasons.get("totll", "")),
        _totci_row(series.get("totci"), today, reasons.get("totci", ""), (fallbacks or {}).get("totci")),
        score_oas("hy_oas", series.get("hy_oas"), today, reasons.get("hy_oas", "")),
        score_oas("ig_oas", series.get("ig_oas"), today, reasons.get("ig_oas", "")),
        score_index("nfci", series.get("nfci"), today, reasons.get("nfci", "")),
        score_index("stlfsi4", series.get("stlfsi4"), today, reasons.get("stlfsi4", "")),
        _cds_row(),
    ]
    by_id = {row["id"]: row for row in rows}
    standards = _usable(by_id["drtscilm"]["light"])
    volume = worst_light([by_id["totll"]["light"], by_id["totci"]["light"]], require_all=True)
    price_required = [by_id["hy_oas"]["light"], by_id["ig_oas"]["light"], by_id["nfci"]["light"]]
    price = worst_light(price_required, require_all=True)
    optional = _usable(by_id["stlfsi4"]["light"])
    if optional in {"yellow", "red"}:
        price = worst_light([price, optional], require_all=False) if price else optional
    stage, code, trigger = judge_stage(volume, price, standards)
    table = []
    for row in rows:
        light = row["light_text"]
        if row["reason"] and row["light"] in {"missing", "stale"}:
            light = f"{light}：{row['reason']}"
        elif row["id"] == "cds":
            light = row["reason"]
        table.append({
            "指標": row["name"],
            "最新值": row["value_text"],
            "資料日期": row["date"] or "—",
            "較前期變化": row["change_text"],
            "燈號": light,
        })
    return {
        "stage": stage,
        "stage_code": code,
        "stage_trigger": trigger,
        "lending_light": volume,
        "price_light": price,
        "standards_light": standards,
        "disclaimer": DISCLAIMER,
        "rule_note": RULE_NOTE,
        "cds": CDS_TEXT,
        "thresholds": THRESHOLDS,
        "rows": [{key: row[key] for key in ("id", "name", "series_id", "value", "unit", "date", "change", "change_label", "light", "light_text", "reason")} for row in rows],
        "table_rows": table,
    }


def api_view(board: dict) -> dict:
    """給 /v1/portfolio。不含畫面用的 table_rows。"""
    keys = (
        "stage", "stage_code", "stage_trigger", "lending_light", "price_light", "standards_light",
        "disclaimer", "rule_note", "cds", "thresholds", "rows",
    )
    return {key: board[key] for key in keys}


def absent_board() -> dict:
    """測試或尚未附上觀測時用。沒有數字，不當成綠燈。"""
    return build_board({}, {item["id"]: "這次回應沒有附上信用傳導" for item in SERIES if item["id"] != "cds"}, date(1970, 1, 1))


def load_board(today: date, fetch=None) -> dict:
    """逐條抓。單條失敗記原因，不把別條的舊值填過來。TOTCI 失敗才改用 BUSLOANS。"""
    fetch = fetch or default_fetch
    series = {}
    reasons = {}
    for spec in SERIES:
        if spec["id"] == "totci":
            continue
        series_id = spec.get("series_id") or THRESHOLDS[spec["id"]]["series_id"]
        try:
            series[spec["id"]] = fetch(series_id)
        except Exception as exc:
            series[spec["id"]] = None
            reasons[spec["id"]] = public_reason(exc)
    totci, totci_reason, fallback = resolve_totci(fetch)
    series["totci"] = totci
    fallbacks = {}
    if fallback:
        fallbacks["totci"] = fallback
    elif totci_reason:
        reasons["totci"] = totci_reason
    return build_board(series, reasons, today, fallbacks)


def render_credit_panel(st, board: dict) -> None:
    """第一頁 FRED 區塊下方。st 是 Streamlit。"""
    st.subheader("信用傳導")
    stage = board.get("stage") or STAGE_UNKNOWN
    code = board.get("stage_code")
    if code == "confirmed":
        st.error(stage)
    elif code in {"price_first", "volume_only"}:
        st.warning(stage)
    elif code == "equity_only":
        st.success(stage)
    else:
        st.info(stage)
    st.caption(board.get("disclaimer") or DISCLAIMER)
    st.caption(board.get("cds") or CDS_TEXT)
    st.caption(f"放款量整體：{LIGHT_TEXT.get(board.get('lending_light'), '資料不足')}。信用價格整體：{LIGHT_TEXT.get(board.get('price_light'), '資料不足')}。")
    st.table(board.get("table_rows") or [], hide_index=True)
    st.caption(board.get("rule_note") or RULE_NOTE)
