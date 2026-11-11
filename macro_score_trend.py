"""第五頁總經分數的 4 週趨勢。只計算，不寫試算表、不推播、不排程。

週頻定義：每一列的 week 是一個週分數日期，不是交易日。
4 週不是「往上數 4 列」。最新一週要對到大約 28 天前的那一列（26～32 天），
中間相鄰日期都要落在 5～10 天，而且窗口裡剛好 5 個有效分數。
缺週不補 0、不插值。這盞燈不是淨值回撤的 -8% / -12%。
"""

from __future__ import annotations

from datetime import date, timedelta

from macro_cycle import regime_for_score

FOUR_WEEK_DAYS = 28
ANCHOR_MIN_DAYS = 26
ANCHOR_MAX_DAYS = 32
STEP_MIN_DAYS = 5
STEP_MAX_DAYS = 10
WINDOW_POINTS = 5
YELLOW_DROP = 3
MIN_BACKTEST_WEEKS = 52
SCORE_MIN = -18
SCORE_MAX = 18


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_total(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        number = value
    else:
        try:
            raw = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        if raw != int(raw):
            return None
        number = int(raw)
    if number < SCORE_MIN or number > SCORE_MAX:
        return None
    return number


def prepare_points(rows: list[dict], today: str | date | None = None) -> dict:
    """整理週分數。重複日期留第一筆。未來日期、無效分數、狀態對不上總分的列不進入計算。"""
    cutoff = _as_date(today) if today else None
    accepted: list[dict] = []
    seen: set[str] = set()
    notes: list[str] = []
    future: list[str] = []
    for row in rows or []:
        day = _as_date(row.get("week"))
        total = _as_total(row.get("total"))
        if day is None or total is None:
            notes.append("略過無效日期或總分。")
            continue
        key = day.isoformat()
        if key in seen:
            notes.append(f"{key} 重複，計算只用第一筆。")
            continue
        seen.add(key)
        if cutoff is not None and day > cutoff:
            future.append(key)
            notes.append(f"{key} 晚於 {cutoff.isoformat()}，不當作已發生的週分數。")
            continue
        regime = str(row.get("regime") or "").strip()
        try:
            expected = regime_for_score(total)
        except (TypeError, ValueError):
            notes.append(f"{key} 的總分不在 -18 到 18。")
            continue
        if regime and regime != expected:
            notes.append(f"{key} 的狀態「{regime}」與總分 {total} 不一致，這一列不進入 4 週計算。")
            continue
        accepted.append({"week": key, "total": total, "regime": expected})
    accepted.sort(key=lambda item: item["week"])
    gaps = []
    for earlier, later in zip(accepted, accepted[1:]):
        span = (_as_date(later["week"]) - _as_date(earlier["week"])).days
        if span > STEP_MAX_DAYS:
            gaps.append({"from": earlier["week"], "to": later["week"], "days": span})
    return {"points": accepted, "notes": notes, "future": future, "gaps": gaps}


def _window(points: list[dict]) -> tuple[list[dict] | None, str]:
    if len(points) < WINDOW_POINTS:
        return None, f"樣本不足：有效週分數 {len(points)} 筆，4 週窗口要 {WINDOW_POINTS} 筆。"
    latest = _as_date(points[-1]["week"])
    candidates = []
    for item in points[:-1]:
        span = (latest - _as_date(item["week"])).days
        if ANCHOR_MIN_DAYS <= span <= ANCHOR_MAX_DAYS:
            candidates.append((abs(span - FOUR_WEEK_DAYS), item))
    if not candidates:
        return None, "樣本不足：沒有距最新一週 26～32 天的那一筆，不能把較早的列當成 4 週。"
    anchor = sorted(candidates, key=lambda pair: (pair[0], pair[1]["week"]))[0][1]
    window = [item for item in points if anchor["week"] <= item["week"] <= points[-1]["week"]]
    if len(window) != WINDOW_POINTS:
        return None, f"樣本不足：這 4 週裡有 {len(window)} 筆，不是剛好 {WINDOW_POINTS} 筆。缺週沒有補 0。"
    for earlier, later in zip(window, window[1:]):
        span = (_as_date(later["week"]) - _as_date(earlier["week"])).days
        if span < STEP_MIN_DAYS or span > STEP_MAX_DAYS:
            return None, f"樣本不足：{earlier['week']} 到 {later['week']} 相隔 {span} 天，不是一週。沒有插值。"
    return window, ""


def assess_trend(rows: list[dict], today: str | date | None = None, *, origin: str = "json", example: bool = False) -> dict:
    prepared = prepare_points(rows, today)
    points = prepared["points"]
    window, reason = _window(points)
    delta = None
    average = None
    arrow = "未知"
    light = "未知"
    if window is not None:
        delta = int(window[-1]["total"]) - int(window[0]["total"])
        average = sum(int(item["total"]) for item in window) / WINDOW_POINTS
        if delta > 0:
            arrow = "上升"
        elif delta < 0:
            arrow = "下降"
        else:
            arrow = "持平"
        light = "黃燈" if delta <= -YELLOW_DROP else "未達黃燈"
        reason = ""
    first = points[0]["week"] if points else ""
    last = points[-1]["week"] if points else ""
    return {
        "as_of": cutoff.isoformat() if (cutoff := (_as_date(today) if today else None)) else "",
        "origin": origin,
        "example": bool(example),
        "count": len(points),
        "first": first,
        "last": last,
        "gaps": prepared["gaps"],
        "future": prepared["future"],
        "notes": prepared["notes"],
        "window": window or [],
        "delta": delta,
        "average": average,
        "arrow": arrow,
        "light": light,
        "reason": reason,
        "yellow_drop": YELLOW_DROP,
        "backtest": backtest_block(points, example=example, origin=origin),
    }


def backtest_block(points: list[dict], *, example: bool, origin: str) -> dict:
    """門檻回測需要當時可見的分數。現有歷史沒有這些欄位，所以不產生績效數字。"""
    missing = [
        "每週九項指標各自的當時分數（Macro_Score_History 只有 week、total、regime）",
        "各項當時的公布日或可見版本；不能用今天修訂後的讀數回填當年",
        f"至少 {MIN_BACKTEST_WEEKS} 個連續週。目前有效週分數 {len(points)} 筆",
        "與當時淨值、持倉對得上的日期。本批不計算報酬",
    ]
    if example or origin != "sheet":
        missing.insert(0, "目前這段歷史是倉庫示例或試算表備援，不是已核對的真實週報")
    return {
        "ready": False,
        "label": "不可回測",
        "missing": missing,
        "performance": None,
    }


def chart_rows(rows: list[dict], today: str | date | None = None) -> list[dict]:
    """只放有效週。某一週沒有完整 4 週窗口時，均線是空的，不補 0。"""
    points = prepare_points(rows, today)["points"]
    found = []
    for index, item in enumerate(points):
        window, _reason = _window(points[: index + 1])
        average = None
        if window is not None and window[-1]["week"] == item["week"]:
            average = sum(int(point["total"]) for point in window) / WINDOW_POINTS
        found.append({"week": item["week"], "total": item["total"], "average": average})
    return found


def _blank_history_row(key: str) -> dict:
    return {"week": key, "total": None, "regime": None}


def _plan_result(action: str, reason: str, rows: list[dict], week: str, total, regime) -> dict:
    return {
        "action": action,
        "reason": reason,
        "rows": rows,
        "week": week,
        "total": total,
        "regime": regime,
        "enabled": False,
    }


def plan_history_upsert(existing: list[dict], candidate: dict, today: str | date | None = None, replace_same_day: bool = False) -> dict:
    """排出當天要不要寫一列。這個函式自己不寫試算表。

    總分缺值是 None，不當 0，也不推多空狀態。
    同一天已有不同總分時，預設保留原列。replace_same_day 只更新候選日當天那一列。
    """
    cutoff = _as_date(today)
    day = _as_date(candidate.get("week") if isinstance(candidate, dict) else None)
    raw_total = None if not isinstance(candidate, dict) else candidate.get("total")
    missing_total = raw_total is None or (isinstance(raw_total, str) and not raw_total.strip())
    total = None if missing_total else _as_total(raw_total)
    regime = str((candidate or {}).get("regime") or "").strip() if isinstance(candidate, dict) else ""
    rows = [dict(row) for row in existing or []]
    if day is None:
        return _plan_result("reject", "日期無效，不新增。", rows, "", None, None)
    if not missing_total and total is None:
        return _plan_result("reject", "總分不是 -18 到 18 的整數，不把缺值寫成 0。", rows, day.isoformat(), None, None)
    if cutoff is not None and day > cutoff:
        return _plan_result("reject", "未來日期不寫入。", rows, day.isoformat(), total, None)
    key = day.isoformat()
    if total is None:
        for row in rows:
            if str(row.get("week") or "")[:10] != key:
                continue
            if _as_total(row.get("total")) is None:
                return _plan_result("keep", "這一週已經是空白總分，不另加一列。", rows, key, None, None)
            return _plan_result("keep", "這次沒有總分，保留原列，不把已記錄的分數清成空白。", rows, key, _as_total(row.get("total")), row.get("regime"))
        rows.append(_blank_history_row(key))
        rows.sort(key=lambda item: str(item.get("week")))
        return _plan_result("insert", "沒有總分，這一列總分與狀態留空。", rows, key, None, None)
    try:
        expected = regime_for_score(total)
    except (TypeError, ValueError):
        return _plan_result("reject", "總分不在 -18 到 18。", rows, key, None, None)
    if regime and regime != expected:
        return _plan_result("reject", "狀態與總分不一致，不寫入。", rows, key, total, None)
    for row in rows:
        if str(row.get("week") or "")[:10] != key:
            continue
        current = _as_total(row.get("total"))
        current_regime = str(row.get("regime") or "").strip()
        if current == total and current_regime in {"", expected}:
            return _plan_result("keep", "這一週已有相同總分，不另加一列。", rows, key, total, expected)
        if replace_same_day and cutoff is not None and day == cutoff:
            row["total"] = total
            row["regime"] = expected
            return _plan_result("update", "同一天重跑，只更新這一列。", rows, key, total, expected)
        return _plan_result("keep", "這一週已有不同總分，保留原記錄。", rows, key, current, current_regime or None)
    rows.append({"week": key, "total": total, "regime": expected})
    rows.sort(key=lambda item: str(item.get("week")))
    return _plan_result("insert", "新增這一週的總分。", rows, key, total, expected)


def arrow_symbol(arrow: str) -> str:
    return {"上升": "↑", "下降": "↓", "持平": "→"}.get(arrow, "？")


def format_average(value) -> str:
    if value is None:
        return "—"
    if float(value) == int(value):
        return f"{int(value):+d}"
    return f"{float(value):+.1f}"
