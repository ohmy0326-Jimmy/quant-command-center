"""Telegram 即時警報：狀態判斷、遲滯、靜音時段、每日上限。純函式，不連網。

只在「狀態改變」時推播（追繳中每小時再提醒一次）。讀不到某項資料時，該項維持上一次的
狀態、不推播，不會因為資料缺漏而誤報「解除」。

狀態存在 Quant_Database 的 Alert_State 分頁（見 ``SheetAlertStore``），每列一個 key。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import macro_risk
from risk_performance import BRAKE_CUT_PCT, BRAKE_PAUSE_PCT, CASH_HEALTHY_PCT, LEVERAGE_WARN
from telegram_html import escape

TAIPEI = ZoneInfo("Asia/Taipei")

ALERT_STATE_SHEET = "Alert_State"
ALERT_STATE_HEADERS = ["key", "state", "value", "last_sent_at", "updated_at"]
DAILY_KEY = "_daily"

QUIET_START_HOUR = 0
QUIET_END_HOUR = 7
DAILY_CAP = 12
MARGIN_NEAR_PCT = 5.0          # 台指再跌 5% 內會追繳 → 警報
MARGIN_CLEAR_PCT = 6.0         # 距離拉開到 6% 以上才解除
MARGIN_REMIND = timedelta(hours=1)
DRAWDOWN_HYST_PCT = 1.0        # 回撤燈降級要先回升 1 個百分點
MACRO_HYST_POINTS = 1          # 宏觀風險燈降級要先少 1 分
LEVERAGE_CLEAR = LEVERAGE_WARN - 0.05
CASH_CLEAR_PCT = CASH_HEALTHY_PCT + 2.0
STOP_CLEAR_PCT = 2.0           # 停損部位報酬回升到停損線 +2pp 以上才移出名單

CRITICAL = "critical"
NORMAL = "normal"

KEY_LABELS = {
    "margin": "元大追繳",
    "drawdown": "回撤煞車",
    "macro": "宏觀風險燈",
    "hedge": "避險燈",
    "stop_loss": "停損",
    "leverage": "槓桿",
    "cash": "現金水位",
    "regime": "多空狀態",
    "quadrant": "產業象限",
}


@dataclass
class Alert:
    key: str
    severity: str
    title: str
    body: str = ""

    def html(self, now: datetime | None = None) -> str:
        text = f"<b>{escape(self.title)}</b>"
        if self.body:
            text += "\n" + escape(self.body)
        if now is not None:
            text += f"\n<i>{escape(now.astimezone(TAIPEI).strftime('%Y-%m-%d %H:%M'))}（台北）</i>"
        return text


@dataclass
class Record:
    state: str = ""
    value: str = ""
    last_sent_at: str = ""
    updated_at: str = ""


@dataclass
class CheckResult:
    sent: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    states: dict = field(default_factory=dict)
    sent_today: int = 0

    def summary(self) -> dict:
        return {
            "sent": [a.key for a in self.sent],
            "suppressed": [a.key for a in self.suppressed],
            "failed": [a.key for a in self.failed],
            "skipped": sorted(self.skipped),
            "states": {k: v.state for k, v in self.states.items() if k != DAILY_KEY},
            "sent_today": self.sent_today,
        }


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=TAIPEI)


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(TAIPEI).hour
    return QUIET_START_HOUR <= hour < QUIET_END_HOUR


# ---------------------------------------------------------------- 各項規則
# 每個規則：(reading, prev Record | None, now) → (新狀態, 新數值字串, Alert | None)


def rule_margin(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "ok"
    move = r.get("move_pct")
    if r.get("already"):
        state = "call"
    elif move is not None and abs(float(move)) <= MARGIN_NEAR_PCT:
        state = "near"
    elif prev_state in ("near", "call") and move is not None and abs(float(move)) < MARGIN_CLEAR_PCT:
        state = "near"
    else:
        state = "ok"
    value = "" if move is None else f"{float(move):.2f}"
    level = r.get("index_level")
    level_text = f"，約 {float(level):,.0f} 點" if level else ""
    alert = None
    if state == "call":
        last = _parse_time(prev.last_sent_at) if prev else None
        if prev_state != "call" or last is None or now - last >= MARGIN_REMIND:
            again = "（每小時提醒）" if prev_state == "call" else ""
            alert = Alert("margin", CRITICAL, f"🚨 元大期貨已追繳{again}", "期貨權益低於維持保證金，請補錢或減碼。")
    elif state == "near" and prev_state != "near":
        if prev_state == "call":
            alert = Alert("margin", NORMAL, "🟡 元大追繳已解除，但仍接近追繳線", f"台指再 {float(move):+.1f}%{level_text} 會追繳。")
        else:
            alert = Alert("margin", CRITICAL, "⚠️ 元大追繳距離 5% 內", f"台指再 {float(move):+.1f}%{level_text} 會追繳。")
    elif state == "ok" and prev_state in ("near", "call"):
        alert = Alert("margin", NORMAL, "✅ 元大追繳風險解除", f"追繳距離 {r.get('direction') or value}。")
    return state, value, alert


def _drawdown_level(dd: float, prev_state: str) -> str:
    if dd <= BRAKE_CUT_PCT + 1e-9:
        return "red"
    if prev_state == "red" and dd <= BRAKE_CUT_PCT + DRAWDOWN_HYST_PCT:
        return "red"
    if dd <= BRAKE_PAUSE_PCT + 1e-9:
        return "yellow"
    if prev_state in ("yellow", "red") and dd <= BRAKE_PAUSE_PCT + DRAWDOWN_HYST_PCT:
        return "yellow"
    return "green"


def rule_drawdown(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "green"
    dd = float(r["dd_pct"])
    state = _drawdown_level(dd, prev_state)
    alert = None
    if state != prev_state:
        if state == "red":
            alert = Alert("drawdown", CRITICAL, f"🔴 回撤煞車：降到 1x（回撤 {dd:.1f}%）", f"淨值距高點 {dd:.1f}%，已達 {BRAKE_CUT_PCT:.0f}% 線。")
        elif state == "yellow":
            alert = Alert("drawdown", NORMAL, f"🟡 回撤煞車：停新倉（回撤 {dd:.1f}%）", f"淨值距高點 {dd:.1f}%，停新倉線 {BRAKE_PAUSE_PCT:.0f}%。")
        else:
            alert = Alert("drawdown", NORMAL, f"🟢 回撤煞車解除（回撤 {dd:.1f}%）")
    return state, f"{dd:.2f}", alert


def _macro_level(score: int, prev_state: str) -> str:
    red_at, yellow_at = macro_risk.YELLOW_MAX + 1, macro_risk.GREEN_MAX + 1
    if score >= red_at or (prev_state == "red" and score >= red_at - MACRO_HYST_POINTS):
        return "red"
    if score >= yellow_at or (prev_state in ("yellow", "red") and score >= yellow_at - MACRO_HYST_POINTS):
        return "yellow"
    return "green"


def rule_macro(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "green"
    score = int(r["score"])
    state = _macro_level(score, prev_state)
    alert = None
    if state != prev_state:
        triggered = "；".join(r.get("triggered") or [])
        body = f"綜合風險 {r.get('score_text') or score}" + (f"\n{triggered}" if triggered else "")
        title = {"red": "🔴 宏觀風險燈轉紅（Risk-Off）", "yellow": "🟡 宏觀風險燈：中性觀望", "green": "🟢 宏觀風險燈轉綠（Risk-On）"}[state]
        alert = Alert("macro", CRITICAL if state == "red" else NORMAL, title, body)
    return state, str(score), alert


def rule_hedge(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "off"
    if r.get("alert"):
        state = "on"
    elif prev_state == "on" and r.get("release") is not True:
        state = "on"  # 觸發後要收盤站回 20 日線才解除，避免來回跳
    else:
        state = "off"
    lights = r.get("lights") or {}
    names = {"ma60": "跌破 60 日線", "drawdown": "一年高點回撤", "score": "多空分數下滑"}
    lit = "、".join(names.get(k, k) for k, v in lights.items() if v is True)
    alert = None
    if state != prev_state:
        if state == "on":
            alert = Alert("hedge", NORMAL, "🛡️ 避險燈亮起", f"觸發：{lit or '—'}。請看第六頁避險建議。")
        else:
            alert = Alert("hedge", NORMAL, "✅ 避險燈熄滅", "加權指數站回 20 日線。")
    return state, lit, alert


def stop_dates_from_value(value) -> dict[str, str]:
    """Alert_State 的 value。舊版是人數的數字，沒有日期，不回推。"""
    text = "" if value is None else str(value).strip()
    if not text or text.lstrip("-").isdigit():
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, seen in raw.items():
        iso = str(seen).strip()[:10]
        try:
            date.fromisoformat(iso)
        except ValueError:
            continue
        out[str(key)] = iso
    return out


def _previous_stops(prev: Record | None) -> tuple[set[str], dict[str, str]]:
    if prev is None or not prev.state:
        return set(), {}
    try:
        parsed = json.loads(prev.state)
    except ValueError:
        return set(), {}
    if isinstance(parsed, list):
        held = {str(item) for item in parsed}
    else:
        held = set()
    return held, stop_dates_from_value(prev.value)


def recorded_stop_dates(spreadsheet) -> dict[str, str]:
    """讀 stop_loss 的首次觀察日。分頁不存在就回空，不建立、不寫入。"""
    try:
        worksheet = spreadsheet.worksheet(ALERT_STATE_SHEET)
        rows = worksheet.get_all_records()
    except Exception:
        return {}
    for row in rows:
        if str(row.get("key") or "").strip() == "stop_loss":
            return stop_dates_from_value(row.get("value"))
    return {}


def rule_stop_loss(r: dict, prev: Record | None, now: datetime):
    from stop_rules import breach_age_label

    held, dates = _previous_stops(prev)
    today = now.astimezone(TAIPEI).date()
    current: set[str] = set()
    kept: dict[str, str] = {}
    new_rows = []
    for p in r.get("positions") or []:
        key = f"{p.get('account', '')}|{p['ticker']}"
        roi, limit = float(p["roi"]), float(p["max_loss_pct"])
        if roi <= limit:
            current.add(key)
            if key in dates:
                kept[key] = dates[key]
            elif key not in held:
                kept[key] = today.isoformat()
            if key not in held:
                new_rows.append(p)
        elif key in held and roi <= limit + STOP_CLEAR_PCT:
            current.add(key)
            if key in dates:
                kept[key] = dates[key]
    alert = None
    if new_rows:
        lines = []
        for p in new_rows:
            key = f"{p.get('account', '')}|{p['ticker']}"
            lines.append(
                f"{p.get('name') or p['ticker']}（{p['ticker']}）報酬 {float(p['roi']):.1f}%，"
                f"停損線 {float(p['max_loss_pct']):.1f}%。{breach_age_label(kept.get(key), today)}"
            )
        alert = Alert("stop_loss", NORMAL, f"⛔ 新觸發停損 {len(new_rows)} 檔", "\n".join(lines))
    return json.dumps(sorted(current), ensure_ascii=False), json.dumps(kept, ensure_ascii=False), alert


def rule_leverage(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "ok"
    lev = float(r["value"])
    if r.get("exposure_complete", True) is False:
        symbols = [str(item) for item in (r.get("unknown_delta_symbols") or []) if str(item)]
        listed = "、".join(symbols) if symbols else "有部位"
        alert = None
        if prev_state != "incomplete":
            alert = Alert(
                "leverage",
                NORMAL,
                "⚠️ 槓桿與 Beta 不完整",
                f"{listed} 缺少 Delta 或標的價格。已知槓桿 {lev:.2f}x 只含已知部位，不能當成未超標。",
            )
        return "incomplete", f"{lev:.3f}", alert
    if lev > LEVERAGE_WARN + 1e-9 or (prev_state == "high" and lev >= LEVERAGE_CLEAR):
        state = "high"
    else:
        state = "ok"
    alert = None
    if state != prev_state:
        alert = (
            Alert("leverage", NORMAL, f"⚠️ 合計槓桿 {lev:.2f}x 超過 {LEVERAGE_WARN:.1f}x")
            if state == "high"
            else Alert("leverage", NORMAL, f"✅ 合計槓桿回到 {lev:.2f}x")
        )
    return state, f"{lev:.3f}", alert


def rule_cash(r: dict, prev: Record | None, now: datetime):
    prev_state = prev.state if prev else "ok"
    cash = float(r["value"])
    if cash < CASH_HEALTHY_PCT - 1e-9 or (prev_state == "low" and cash <= CASH_CLEAR_PCT):
        state = "low"
    else:
        state = "ok"
    alert = None
    if state != prev_state:
        alert = (
            Alert("cash", NORMAL, f"⚠️ 現金 {cash:.1f}% 低於 {CASH_HEALTHY_PCT:.0f}%")
            if state == "low"
            else Alert("cash", NORMAL, f"✅ 現金回到 {cash:.1f}%")
        )
    return state, f"{cash:.2f}", alert


def rule_regime(r: dict, prev: Record | None, now: datetime):
    state = str(r["regime"])
    alert = None
    if prev and prev.state and prev.state != state:
        alert = Alert("regime", NORMAL, f"🧭 多空狀態：{prev.state} → {state}", f"總分 {r.get('total')}。")
    return state, str(r.get("total", "")), alert


def rule_quadrant(r: dict, prev: Record | None, now: datetime):
    items = {str(k): str(v) for k, v in (r.get("items") or {}).items()}
    try:
        before = json.loads(prev.state) if prev and prev.state else None
    except ValueError:
        before = None
    alert = None
    if isinstance(before, dict):
        changes = [f"{name}：{before[name]} → {cycle}" for name, cycle in sorted(items.items()) if name in before and before[name] != cycle]
        if changes:
            alert = Alert("quadrant", NORMAL, f"🔄 產業象限變動 {len(changes)} 項", "\n".join(changes))
    return json.dumps(items, ensure_ascii=False, sort_keys=True), str(len(items)), alert


RULES = {
    "margin": rule_margin,
    "drawdown": rule_drawdown,
    "macro": rule_macro,
    "hedge": rule_hedge,
    "stop_loss": rule_stop_loss,
    "leverage": rule_leverage,
    "cash": rule_cash,
    "regime": rule_regime,
    "quadrant": rule_quadrant,
}


# ---------------------------------------------------------------- 主流程


def run_check(readings: dict, previous: dict, send, now: datetime) -> CheckResult:
    """readings：key → dict 或 None（讀不到）。previous：key → Record。

    send(alert, disable_notification) 回 True 表示送達。送失敗或超過每日上限的警報不推進狀態，
    下一輪會再判斷一次（不會靜悄悄吞掉）。
    """
    now = now.astimezone(TAIPEI)
    stamp = now.isoformat(timespec="seconds")
    states = {k: Record(**vars(v)) for k, v in previous.items()}
    daily = states.get(DAILY_KEY) or Record()
    today = now.date().isoformat()
    sent_today = int(float(daily.value or 0)) if daily.state == today else 0

    result = CheckResult()
    pending: list[tuple[Alert, Record]] = []
    for key, rule in RULES.items():
        reading = readings.get(key)
        if reading is None:
            result.skipped.append(key)
            continue
        prev = previous.get(key)
        state, value, alert = rule(reading, prev, now)
        new = Record(state=state, value=value, last_sent_at=prev.last_sent_at if prev else "", updated_at=stamp)
        if alert is None:
            states[key] = new
        else:
            pending.append((alert, new))

    pending.sort(key=lambda item: 0 if item[0].severity == CRITICAL else 1)
    quiet = is_quiet(now)
    for alert, new in pending:
        if sent_today >= DAILY_CAP:
            result.suppressed.append(alert)
            continue
        ok = False
        try:
            ok = bool(send(alert, quiet and alert.severity != CRITICAL))
        except Exception:
            ok = False
        if ok:
            sent_today += 1
            new.last_sent_at = stamp
            states[alert.key] = new
            result.sent.append(alert)
        else:
            result.failed.append(alert)

    states[DAILY_KEY] = Record(state=today, value=str(sent_today), updated_at=stamp)
    result.states = states
    result.sent_today = sent_today
    return result


# ---------------------------------------------------------------- 狀態儲存


class MemoryAlertStore:
    def __init__(self, records: dict | None = None):
        self.records = dict(records or {})

    def load(self) -> dict:
        return {k: Record(**vars(v)) for k, v in self.records.items()}

    def save(self, records: dict) -> None:
        self.records = {k: Record(**vars(v)) for k, v in records.items()}


class SheetAlertStore:
    """Quant_Database 的 Alert_State 分頁。沒有就建立並寫表頭（服務帳戶需要編輯權限）。"""

    def __init__(self, spreadsheet):
        from nav_snapshot import _ensure_worksheet

        # nav_snapshot._ensure_worksheet 回傳 (worksheet, created, migrated)。
        self.worksheet, _created, _migrated = _ensure_worksheet(spreadsheet, ALERT_STATE_SHEET, ALERT_STATE_HEADERS)

    def load(self) -> dict:
        out = {}
        for row in self.worksheet.get_all_records():
            key = str(row.get("key") or "").strip()
            if key:
                out[key] = Record(*(str(row.get(h, "") if row.get(h) is not None else "") for h in ALERT_STATE_HEADERS[1:]))
        return out

    def save(self, records: dict) -> None:
        rows = [ALERT_STATE_HEADERS] + [[k, r.state, r.value, r.last_sent_at, r.updated_at] for k, r in sorted(records.items())]
        self.worksheet.batch_clear([f"A2:E{max(len(rows) + 50, 100)}"])
        # RAW：避免試算表把 JSON 或日期字串自動轉型
        self.worksheet.update(range_name="A1", values=rows, value_input_option="RAW")
