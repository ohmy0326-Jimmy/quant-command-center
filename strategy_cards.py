"""策略劇情卡。文案、硬規則與卡片列都來自 data/strategy_cards.json。

此模組不連網、不讀金鑰。燈號只吃呼叫端傳入的持倉數字、回撤、VIX 序列與結算日。
頁面上的風控滑桿不能改硬規則。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "strategy_cards.json"

CATEGORIES = frozenset({"defense", "settlement", "rebound", "event", "swing"})
STATUSES = frozenset({"draft", "live", "retired"})
LIGHTS = frozenset({"red", "yellow", "green", "off"})

_TEXT_KEYS = (
    "title",
    "trigger_rules",
    "trigger_zh",
    "actions",
    "max_loss_rule",
    "data_sources",
    "notes",
    "notes_url",
)


class StrategyCardError(ValueError):
    """JSON 缺欄、型別不對，或卡片列無法採用。"""


def text_of(value) -> str:
    """字串、或以後改成陣列的欄位，都收成一段文字。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def load_strategy_cards(path: Path | str | None = None) -> dict:
    """讀取策略卡。多出來的欄位保留，方便以後加列或改接試算表。"""
    source = Path(path) if path else DATA_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyCardError(str(exc)) from exc
    if not isinstance(raw, dict):
        raise StrategyCardError("strategy cards root must be an object")
    rules = _hard_rules(raw.get("hard_rules"))
    errors: list[str] = []
    cards: list[dict] = []
    seen: set[str] = set()
    rows = raw.get("cards")
    if not isinstance(rows, list):
        raise StrategyCardError("cards must be a list")
    for index, item in enumerate(rows):
        try:
            card = _card(item)
        except StrategyCardError as exc:
            errors.append(f"{index}: {exc}")
            continue
        if card["card_id"] in seen:
            errors.append(f"{index}: duplicate {card['card_id']}")
            continue
        seen.add(card["card_id"])
        cards.append(card)
    ui = raw.get("ui") if isinstance(raw.get("ui"), dict) else {}
    return {
        "version": raw.get("version"),
        "updated": raw.get("updated"),
        "hard_rules": rules,
        "ui": dict(ui),
        "cards": cards,
        "errors": errors,
        "path": str(source),
    }


def locked_hard_rules(deck: dict, overrides: dict | None = None) -> dict:
    """硬規則只採用檔案裡的數字。overrides 是頁面或等級預設，一律不採用。"""
    del overrides
    rules = deck.get("hard_rules") or {}
    return {
        "max_leverage_bullish": float(rules["max_leverage_bullish"]),
        "min_cash_pct": float(rules["min_cash_pct"]),
        "drawdown_brakes": [float(item) for item in rules["drawdown_brakes"]],
    }


def hard_rule_banner(deck: dict, overrides: dict | None = None) -> str:
    """橫幅句子來自 JSON，數字來自鎖定的硬規則。"""
    rules = locked_hard_rules(deck, overrides)
    ui = deck.get("ui") or {}
    brakes = format_brakes(rules["drawdown_brakes"], str(ui.get("brake_sep") or "/"))
    payload = {
        "max_leverage_bullish": _num(rules["max_leverage_bullish"]),
        "min_cash_pct": _num(rules["min_cash_pct"]),
        "brakes": brakes,
    }
    template = str(ui.get("hard_rule_banner") or "").strip()
    if not template:
        template = "max_leverage_bullish {max_leverage_bullish} min_cash_pct {min_cash_pct} drawdown_brakes {brakes}"
    return _fill(template, payload)


def format_brakes(levels: list[float], sep: str) -> str:
    parts = []
    for level in levels:
        value = float(level)
        number = _num(abs(value))
        parts.append(f"−{number}" if value < 0 else number)
    return sep.join(parts)


def block_text_for(deck: dict, card_id: str) -> str:
    ui = deck.get("ui") or {}
    template = str(ui.get("s0_block") or "").strip()
    if not template:
        return card_id
    return _fill(template, {"card_id": card_id})


@dataclass
class RadarContext:
    """自動燈號的輸入。沒帶到的來源維持 None，該卡就改走手動。"""

    metrics: dict | None = None
    drawdown_pct: float | None = None
    panic_urge: bool = False
    settlement_dates: list[date] | None = None
    today: date | None = None
    vix_series: list[list[float]] | None = None
    manual_marks: dict[str, bool] = field(default_factory=dict)
    manual_lights: dict[str, str] = field(default_factory=dict)


def bullish_leverage(metrics: dict | None) -> float | None:
    """偏多槓桿。淨部位偏空時不當成偏多；數字沿用第四頁的合併槓桿。"""
    if not metrics:
        return None
    beta = metrics.get("account_beta", metrics.get("beta"))
    leverage = metrics.get("leverage")
    if leverage is None:
        leverage = metrics.get("total_leverage")
    if leverage is None and metrics.get("nav"):
        exposure = metrics.get("notional_exposure", metrics.get("exposure", metrics.get("position_exposure")))
        nav = float(metrics["nav"])
        if exposure is not None and nav > 0:
            leverage = float(exposure) / nav
    if leverage is None:
        return None
    if beta is not None and float(beta) < 0:
        return 0.0
    return float(leverage)


def evaluate_radar(deck: dict, ctx: RadarContext | None = None) -> list[dict]:
    """每張卡一列。生存卡亮紅時排最前，其餘卡帶上擋下句子。"""
    ctx = ctx or RadarContext()
    rules = locked_hard_rules(deck)
    rows = []
    for card in deck.get("cards") or []:
        light, reasons, mode = _evaluate_card(card, ctx, rules, deck.get("ui") or {})
        rows.append({"card": card, "light": light, "reasons": reasons, "mode": mode, "block_text": ""})
    survival_id = _survival_id(deck.get("cards") or [])
    survival = next((row for row in rows if row["card"]["card_id"] == survival_id), None)
    active = bool(survival and survival["light"] == "red")
    phrase = block_text_for(deck, survival_id) if active and survival_id else ""
    if phrase:
        for row in rows:
            if row["card"]["card_id"] != survival_id:
                row["block_text"] = phrase

    def sort_key(row: dict):
        card_id = row["card"]["card_id"]
        priority = int(row["card"].get("priority") or 0)
        if active and card_id == survival_id:
            return (0, 0, card_id)
        return (1, priority, card_id)

    rows.sort(key=sort_key)
    return rows


def portfolio_drawdown_pct(nav_frame, today_nav: float) -> float | None:
    """沿用風險卡的回撤：扣除出入金，歷史不足就不判斷。"""
    if nav_frame is None:
        return None
    from risk_performance import drawdown_stats, flow_adjusted_index
    from sheet_dates import nav_flow_map, nav_growth_series

    series = nav_growth_series(nav_frame, float(today_nav))
    stats = drawdown_stats(flow_adjusted_index(series, nav_flow_map(nav_frame)))
    if not stats.get("available"):
        return None
    return stats.get("current_dd_pct")


def builtin_settlement_dates(today: date | None = None) -> list[date]:
    """月選第三個星期三，加上「今天若是星期三」的週選日。不連網。"""
    today = today or date.today()
    found: list[date] = []
    year, month = today.year, today.month
    # 含上個月，避免月初仍落在月選暖身窗。
    month -= 1
    if month == 0:
        month = 12
        year -= 1
    for _ in range(5):
        settled = _nth_weekday(year, month, 2, 3)
        if settled is not None:
            found.append(settled)
        month += 1
        if month == 13:
            month = 1
            year += 1
    if today.weekday() == 2:
        found.append(today)
    return sorted(set(found))


def _evaluate_card(card: dict, ctx: RadarContext, rules: dict, ui: dict) -> tuple[str, list[str], str]:
    rules_text = card.get("trigger_rules") or ""
    if _is_survival(card):
        return (*_survival_light(ctx, rules), "auto")
    if "national_security_fund_announced" in rules_text:
        marked = bool((ctx.manual_marks or {}).get(card["card_id"]))
        if marked:
            return "red", ["national_security_fund_announced"], "mark"
        return "off", [], "mark"
    if "VIX_peak" in rules_text or "drawdown_from_peak" in rules_text:
        if ctx.vix_series is not None:
            light, reasons = _vix_light(card, ctx.vix_series)
            if light is not None:
                return light, reasons, "auto"
        return _manual_light(card, ctx)
    if "option_settlement_day" in rules_text:
        if ctx.settlement_dates is not None:
            warmup = int(ui.get("settlement_warmup_days") or 0)
            today = ctx.today or date.today()
            light = _settlement_light(today, ctx.settlement_dates, warmup)
            reason = ["option_settlement_day"] if light == "yellow" else []
            return light, reason, "auto"
        return _manual_light(card, ctx)
    return _manual_light(card, ctx)


def _survival_light(ctx: RadarContext, rules: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    metrics = ctx.metrics or {}
    leverage = bullish_leverage(metrics)
    cash = _optional_float(metrics.get("cash_pct")) if metrics else None
    cap = float(rules["max_leverage_bullish"])
    floor = float(rules["min_cash_pct"])
    if leverage is not None and leverage > cap + 1e-9:
        reasons.append("lev>regime_cap")
    if cash is not None and cash + 1e-9 < floor:
        reasons.append("cash<min")
    if ctx.panic_urge:
        reasons.append("panic_urge")
    brakes = [float(item) for item in rules["drawdown_brakes"]]
    if ctx.drawdown_pct is not None and brakes and float(ctx.drawdown_pct) <= max(brakes) + 1e-9:
        reasons.append("drawdown_brakes")
    if reasons:
        return "red", reasons
    if leverage is None and cash is None and ctx.drawdown_pct is None and not ctx.panic_urge:
        return "off", []
    return "green", []


def _vix_light(card: dict, series_list: list[list[float]]) -> tuple[str | None, list[str]]:
    peak_min, pullback, window = _vix_params(card)
    if peak_min is None or pullback is None:
        return None, []
    saw = False
    warmed = False
    for series in series_list:
        values = [float(item) for item in series if item is not None]
        if not values:
            continue
        saw = True
        windowed = values[-window:] if window else values
        peak = max(windowed)
        last = windowed[-1]
        if peak <= peak_min:
            continue
        if peak > 0 and (peak - last) / peak * 100.0 + 1e-9 >= pullback:
            return "red", ["VIX_peak", "drawdown_from_peak"]
        warmed = True
    if not saw:
        return None, []
    if warmed:
        return "yellow", ["VIX_peak"]
    return "green", []


def _vix_params(card: dict) -> tuple[float | None, float | None, int]:
    rules = card.get("trigger_rules") or ""
    prose = card.get("trigger_zh") or ""
    peak = _first_float(r"VIX_peak\s*>\s*(\d+(?:\.\d+)?)", rules)
    pullback = _first_float(r"drawdown_from_peak\s*>=\s*(\d+(?:\.\d+)?)", rules)
    window = _first_float(r"近\s*(\d+)\s*日", prose)
    return peak, pullback, int(window or 10)


def _settlement_light(today: date, dates: list[date], warmup_days: int) -> str:
    if today in set(dates):
        return "yellow"
    monthly = [day for day in dates if day > today and _is_third_wednesday(day)]
    if monthly and warmup_days > 0:
        gap = (min(monthly) - today).days
        if 0 < gap <= warmup_days:
            return "yellow"
    return "green"


def _manual_light(card: dict, ctx: RadarContext) -> tuple[str, list[str], str]:
    choice = (ctx.manual_lights or {}).get(card["card_id"])
    if choice in LIGHTS:
        return choice, ["manual"], "manual"
    return "off", [], "manual"


def _is_survival(card: dict) -> bool:
    return "lev>regime_cap" in (card.get("trigger_rules") or "")


def _survival_id(cards: list[dict]) -> str | None:
    for card in cards:
        if _is_survival(card):
            return card["card_id"]
    return None


def _card(raw) -> dict:
    if not isinstance(raw, dict):
        raise StrategyCardError("card must be an object")
    card = dict(raw)
    card_id = str(card.get("card_id") or "").strip()
    if not card_id:
        raise StrategyCardError("missing card_id")
    card["card_id"] = card_id
    category = str(card.get("category") or "").strip()
    if category not in CATEGORIES:
        raise StrategyCardError(f"bad category {category}")
    status = str(card.get("status") or "").strip()
    if status not in STATUSES:
        raise StrategyCardError(f"bad status {status}")
    card["category"] = category
    card["status"] = status
    try:
        card["priority"] = int(card.get("priority"))
    except (TypeError, ValueError) as exc:
        raise StrategyCardError("bad priority") from exc
    for key in _TEXT_KEYS:
        if key in card:
            card[key] = text_of(card[key])
    return card


def _hard_rules(raw) -> dict:
    if not isinstance(raw, dict):
        raise StrategyCardError("hard_rules must be an object")
    try:
        brakes = [float(item) for item in raw["drawdown_brakes"]]
        return {
            "max_leverage_bullish": float(raw["max_leverage_bullish"]),
            "min_cash_pct": float(raw["min_cash_pct"]),
            "drawdown_brakes": brakes,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyCardError("hard_rules fields") from exc


def _nth_weekday(year: int, month: int, target_weekday: int, n: int) -> date | None:
    if n < 1:
        return None
    first = date(year, month, 1)
    offset = (target_weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if found.month != month:
        return None
    return found


def _is_third_wednesday(day: date) -> bool:
    return day.weekday() == 2 and 15 <= day.day <= 21


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text or "")
    if not match:
        return None
    return float(match.group(1))


def _num(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _fill(template: str, mapping: dict) -> str:
    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return str(template).format_map(_Safe(mapping))
