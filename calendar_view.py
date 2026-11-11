"""第三頁表格用的列。不改日期計算，只把既有事件收成短列。"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = "一二三四五六日"
KIND_LABEL = {
    "earnings": "財報",
    "settlement": "結算",
    "weekly": "結算",
    "tw_macro": "總經",
    "us_macro": "總經",
    "rate": "總經",
    "fx": "總經",
    "index": "總經",
}
MARKET_LABEL = {"TW": "台", "US": "美", "FX": "匯率", "BOTH": "台美"}
TYPE_NOTES = {
    "結算": "結算與週選是規則推估或未知，沒有逐月核對期交所公告，也沒有標成官方確認。",
    "財報": "財報日是估計。有時區才換算成台北，並標美股原來的盤前或盤後。",
    "總經": "預估、前值、實際只在來源有數字時顯示。缺值不是 0。",
}


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def kind_label(event: dict) -> str:
    return KIND_LABEL.get(str(event.get("kind") or ""), "其他")


def market_label(event: dict) -> str:
    return MARKET_LABEL.get(str(event.get("market") or ""), "—")


def certainty_badge(event: dict) -> str:
    text = str(event.get("certainty") or "")
    if "待確認" in text:
        return "待確認"
    if "官方" in text:
        return "官方"
    if not text or "未知" in text:
        return "未知"
    return "推估"


def figures_text(event: dict) -> str:
    from calendar_events import format_number

    bits = []
    for label, key in (("預估", "estimate"), ("前值", "previous"), ("實際", "actual")):
        if event.get(key) is None:
            continue
        bits.append(f"{label} {format_number(event.get(key))}")
    return "｜".join(bits)


def short_holdings(impacts) -> str:
    """相關持股只留前三個名稱或檔數，不重複整段推論。"""
    names: list[str] = []
    count = None
    for line in impacts or []:
        found = re.search(r"等\s*(\d+)\s*檔", str(line))
        if found:
            count = int(found.group(1))
        if "：" not in str(line):
            continue
        tail = str(line).split("：", 1)[1]
        tail = tail.split("。")[0]
        tail = re.split(r"\s*等\s*\d+\s*檔", tail)[0]
        for part in tail.split("、"):
            name = part.strip()
            if not name or name in {"沒有", "沒有台股持倉", "沒有美股持倉"}:
                continue
            if name not in names:
                names.append(name)
    if not names and count is None:
        return "—"
    shown = "、".join(names[:3])
    if count is not None and count > len(names[:3]):
        return f"{shown} 等{count}檔" if shown else f"{count}檔"
    return shown or "—"


def _unique(items) -> list[str]:
    found = []
    for item in items or []:
        text = str(item or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def caveat_groups(model: dict) -> dict:
    groups = []
    gaps = _unique(model.get("gaps"))
    notes = _unique(model.get("notes"))
    undone = _unique(f"{item['name']}：{item['why']}" for item in (model.get("not_done") or []) if item.get("name"))
    if not model.get("symbols"):
        notes = _unique(notes + ["目前沒有可查財報的公司代號。"])
    if gaps:
        groups.append({"title": "缺漏", "items": gaps})
    if notes:
        groups.append({"title": "說明", "items": notes})
    if undone:
        groups.append({"title": "未做", "items": undone})
    count = sum(len(group["items"]) for group in groups)
    return {"count": count, "groups": groups}


def shape_calendar(model: dict, today: date, *, market: str = "全部", kind: str = "全部", horizon_days: int = 90) -> dict:
    """篩選既有事件。horizon 是從 today 起的天數，不是重新計算日期。"""
    rows = []
    events = []
    for event in model.get("events") or []:
        day = _as_date(event.get("date") or event.get("when_text"))
        if day is None or day < today or (day - today).days > horizon_days:
            continue
        row_market = market_label(event)
        row_kind = kind_label(event)
        if market != "全部" and row_market != market:
            continue
        if kind != "全部" and row_kind != kind:
            continue
        soon = (day - today).days <= 7
        weekday = WEEKDAYS[day.weekday()]
        rows.append({
            "日期": f"{day.isoformat()}（{weekday}）",
            "事件": str(event.get("title") or ""),
            "市場": row_market,
            "類型": row_kind,
            "確定度": certainty_badge(event),
            "相關持股": short_holdings(event.get("impacts")),
            "數字": figures_text(event),
            "近7日": soon,
            "id": event.get("id") or "",
        })
        events.append(event)
    present_types = []
    for row in rows:
        if row["類型"] in TYPE_NOTES and row["類型"] not in present_types:
            present_types.append(row["類型"])
    show_figures = any(row["數字"] for row in rows)
    return {
        "rows": rows,
        "events": events,
        "type_notes": [TYPE_NOTES[name] for name in present_types],
        "caveats": caveat_groups(model),
        "show_figures": show_figures,
    }
