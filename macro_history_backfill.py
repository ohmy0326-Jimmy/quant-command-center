"""用自動指標的歷史數列重建每週總分。不填手動分數，缺項不當 0。

寫入試算表只由使用者執行的腳本觸發，而且不覆蓋已有總分的列。
"""

from __future__ import annotations

from datetime import date

from macro_cycle import CANONICAL_INDICATORS, regime_for_score
from macro_cycle_sheet import score_auto_from_markets
from macro_proxies import aggregate_scores
from macro_score_trend import _window, prepare_points

BACKFILL_SOURCE = "backfill"


def reconstruct_week(as_of: date, markets: dict) -> dict:
    """markets 用 score_auto_from_markets 的形狀，而且只能含 as_of 以前的資料。"""
    auto = score_auto_from_markets(markets or {}, as_of.isoformat())
    indicators = []
    for item in CANONICAL_INDICATORS:
        result = auto.get(item["id"]) or {}
        if item["kind"] == "auto" and result.get("ok") and result.get("score") is not None:
            indicators.append({"id": item["id"], "score": int(result["score"]), "scored": True, "example": False})
        else:
            indicators.append({"id": item["id"], "score": None, "scored": False, "example": False})
    summary = aggregate_scores(indicators)
    total = summary["total"]
    regime = None if total is None else regime_for_score(int(total))
    return {
        "week": as_of.isoformat(),
        "total": total,
        "regime": regime,
        "coverage": summary["coverage"],
        "source": BACKFILL_SOURCE,
    }


def reconstruct_fridays(bundle: dict, fridays: list[date] | None = None) -> list[dict]:
    """每個星期五只用當天以前的自動數列。手動四項保持缺值。"""
    days = fridays or _fridays(bundle)
    rows = []
    for day in days:
        markets = {
            "dfii10": _until(bundle, "dfii10", day),
            "dgs10": _until(bundle, "dgs10", day),
            "hy": _until(bundle, "hy", day),
            "vix": _last(bundle, "vix", day),
            "spx": _until(bundle, "spx", day),
            "twii": _until(bundle, "twii", day),
            "sox": _until(bundle, "sox", day),
        }
        rows.append(reconstruct_week(day, markets))
    return rows


def _until(bundle: dict, key: str, day: date) -> list[float]:
    return [float(value) for stamp, value in bundle.get(key) or [] if stamp <= day]


def _last(bundle: dict, key: str, day: date):
    values = _until(bundle, key, day)
    return values[-1] if values else None


def _fridays(bundle: dict) -> list[date]:
    stamps = []
    for rows in (bundle or {}).values():
        for stamp, _value in rows or []:
            if stamp.weekday() == 4:
                stamps.append(stamp)
    return sorted(set(stamps))


def plan_backfill_inserts(existing: list[dict], proposed: list[dict]) -> dict:
    """已有總分的週不覆蓋。新列標記 backfill。"""
    taken = set()
    for row in existing or []:
        week = str(row.get("week") or "")[:10]
        if _has_total(row.get("total")):
            taken.add(week)
    inserts = []
    skipped = []
    for row in proposed or []:
        week = str(row.get("week") or "")[:10]
        if not week:
            continue
        if week in taken:
            skipped.append(week)
            continue
        item = dict(row)
        item["week"] = week
        item["source"] = BACKFILL_SOURCE
        inserts.append(item)
        taken.add(week)
    return {"insert": inserts, "skipped": skipped}


def yellow_threshold_backtest(rows: list[dict]) -> dict:
    """4 週跌 3 分算一次黃燈。只數次數，不計算報酬。"""
    usable = [row for row in rows or [] if _has_total(row.get("total"))]
    windows = 0
    yellow = 0
    insufficient = 0
    for index, _row in enumerate(usable):
        points = prepare_points(usable[: index + 1])["points"]
        window, _reason = _window(points)
        if not window:
            insufficient += 1
            continue
        windows += 1
        delta = int(window[-1]["total"]) - int(window[0]["total"])
        if delta <= -3:
            yellow += 1
    return {
        "weeks": len(usable),
        "windows": windows,
        "yellow": yellow,
        "insufficient": insufficient,
        "performance": None,
        "note": "只統計黃燈次數。沒有報酬，也沒有把缺週補成 0。",
    }


def append_backfill(worksheet, existing: list[dict], proposed: list[dict]) -> dict:
    """把新的 backfill 列加到表尾。已有總分的週不寫。"""
    plan = plan_backfill_inserts(existing, proposed)
    for row in plan["insert"]:
        total = "" if row.get("total") is None else row.get("total")
        regime = "" if row.get("regime") is None else row.get("regime")
        coverage = "" if row.get("coverage") is None else row.get("coverage")
        worksheet.append_row([row["week"], total, regime, coverage, BACKFILL_SOURCE], value_input_option="USER_ENTERED")
    return plan


def _has_total(value) -> bool:
    if value is None or value == "":
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == int(number)
