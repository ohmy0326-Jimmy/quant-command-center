"""試算表列的檢查，以及和倉庫 JSON、自動分數的合併。

這個模組不連網、不建分頁、不寫入試算表。
"""

from __future__ import annotations

import copy

from macro_cycle import (
    AUTO_INDICATOR_IDS,
    CANONICAL_IDS,
    CANONICAL_NAMES,
    REGIME_BANDS,
    assemble_view,
    canonical_ticker,
    decorate_indicator,
    week_change,
)

# Quant_Database（1nMpEaNcIqymaTHiqJDhMxm5gLzCWrlfk5wkpCMJMjGM）的分頁。
# 程式只讀這些表，不會建立或寫入。
QUANT_SPREADSHEET_ID = "1nMpEaNcIqymaTHiqJDhMxm5gLzCWrlfk5wkpCMJMjGM"
MACRO_SCORE_TAB = "Macro_Score"
INDUSTRY_CYCLE_TAB = "Industry_Cycle"

MACRO_SCORE_HEADERS = (
    "indicator_id",
    "name",
    "score",
    "source",
    "value",
    "trend",
    "note",
    "updated_at",
)
INDUSTRY_CYCLE_HEADERS = (
    "industry",
    "cycle",
    "direction",
    "stance",
    "tickers",
    "rationale",
    "prev_cycle",
    "note",
    "updated_at",
)
HISTORY_TAB = "Macro_Score_History"
HISTORY_HEADERS = ("week", "total", "regime")
HEAVY_UNDERWEIGHT_PCT = 10.0

CYCLES = ("復甦", "成長", "趨緩", "衰退")
DIRECTIONS = ("上行", "持平", "下行")
STANCES = ("加碼", "中性", "減碼")
SHEET_CACHE_SECONDS = 15 * 60
AUTO_CACHE_SECONDS = 60 * 60

_SOURCE_ALIASES = {
    "auto": "auto",
    "manual": "manual",
    "自動": "auto",
    "手動": "manual",
}


def headers_missing(headers: list[str], required: tuple[str, ...]) -> list[str]:
    found = {str(item).strip() for item in headers}
    return [name for name in required if name not in found]


def parse_score_rows(rows: list[dict]) -> tuple[dict[str, dict], list[str]]:
    accepted: dict[str, dict] = {}
    warnings: list[str] = []
    for index, row in enumerate(rows or [], start=2):
        parsed, error = parse_score_row(row)
        if error:
            warnings.append(f"Macro_Score 第 {index} 列略過：{error}")
            continue
        if parsed["id"] not in CANONICAL_IDS:
            warnings.append(f"Macro_Score 第 {index} 列略過：未知指標 {parsed['id']}")
            continue
        if parsed["id"] in accepted:
            warnings.append(f"Macro_Score 第 {index} 列略過：指標 {parsed['id']} 重複")
            continue
        accepted[parsed["id"]] = parsed
    return accepted, warnings


def parse_score_row(row: dict) -> tuple[dict | None, str | None]:
    indicator_id = str(row.get("indicator_id") or "").strip()
    if not indicator_id:
        return None, "沒有 indicator_id"
    score = _parse_score(row.get("score"))
    if score is None:
        return None, f"{indicator_id} 的 score 不是 -2 到 2 的整數"
    source = _parse_source(row.get("source"))
    if source is None:
        return None, f"{indicator_id} 的 source 必須是 auto 或 manual"
    updated = parse_sheet_date(row.get("updated_at"))
    if updated is None:
        return None, f"{indicator_id} 的 updated_at 不是日期"
    name = str(row.get("name") or "").strip() or CANONICAL_NAMES.get(indicator_id, indicator_id)
    return {
        "id": indicator_id,
        "name": name,
        "score": score,
        "source": source,
        "value": str(row.get("value") or "").strip(),
        "note": str(row.get("note") or "").strip(),
        "trend": str(row.get("trend") or "").strip(),
        "updated_at": updated,
        "example": False,
    }, None


def parse_industry_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    accepted: list[dict] = []
    warnings: list[str] = []
    seen_names: set[str] = set()
    owners: dict[str, str] = {}
    for index, row in enumerate(rows or [], start=2):
        parsed, error = parse_industry_row(row)
        if error:
            warnings.append(f"Industry_Cycle 第 {index} 列略過：{error}")
            continue
        if parsed["name"] in seen_names:
            warnings.append(f"Industry_Cycle 第 {index} 列略過：產業 {parsed['name']} 重複")
            continue
        clash = _ticker_clash(parsed, owners)
        if clash:
            warnings.append(f"Industry_Cycle 第 {index} 列略過：{clash}")
            continue
        seen_names.add(parsed["name"])
        for ticker in parsed["tickers"]:
            owners[canonical_ticker(ticker)] = parsed["name"]
        accepted.append(parsed)
    return accepted, warnings


def parse_industry_row(row: dict) -> tuple[dict | None, str | None]:
    name = str(row.get("industry") or "").strip()
    if not name:
        return None, "沒有 industry"
    cycle = str(row.get("cycle") or "").strip()
    direction = str(row.get("direction") or "").strip()
    stance = str(row.get("stance") or "").strip()
    if cycle not in CYCLES:
        return None, f"{name} 的 cycle 不是復甦／成長／趨緩／衰退"
    if direction not in DIRECTIONS:
        return None, f"{name} 的 direction 不是上行／持平／下行"
    if stance not in STANCES:
        return None, f"{name} 的 stance 不是加碼／中性／減碼"
    tickers = split_tickers(row.get("tickers"))
    if not tickers:
        return None, f"{name} 沒有代表股"
    updated = parse_sheet_date(row.get("updated_at"))
    if updated is None:
        return None, f"{name} 的 updated_at 不是日期"
    previous = str(row.get("prev_cycle") or "").strip()
    if previous and previous not in CYCLES:
        return None, f"{name} 的 prev_cycle 不是上月週期"
    return {
        "id": _industry_id(name),
        "name": name,
        "cycle": cycle,
        "direction": direction,
        "stance": stance,
        "tickers": tickers,
        "example": False,
        "note": str(row.get("note") or "").strip(),
        "rationale": str(row.get("rationale") or "").strip(),
        "prev_cycle": previous,
        "updated_at": updated,
        "origin": "sheet",
    }, None


def parse_history_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    accepted = []
    warnings = []
    seen = set()
    regimes = {name for name, _low, _high in REGIME_BANDS}
    for index, row in enumerate(rows or [], start=2):
        week = parse_sheet_date(row.get("week"))
        total = _parse_history_total(row.get("total"))
        regime = str(row.get("regime") or "").strip()
        if week is None or total is None or regime not in regimes:
            warnings.append(f"Macro_Score_History 第 {index} 列略過")
            continue
        if week in seen:
            warnings.append(f"Macro_Score_History 第 {index} 列略過：{week} 重複")
            continue
        seen.add(week)
        accepted.append({"week": week, "total": total, "regime": regime})
    accepted.sort(key=lambda item: item["week"])
    return accepted, warnings


def exposure_by_industry(positions: list[dict], industries: list[dict]) -> tuple[dict[str, dict], float]:
    """持倉市值對到代表股。回傳各產業市值與佔全部持股市值的百分比。"""
    index = {}
    for item in industries:
        for ticker in item.get("tickers") or []:
            index[canonical_ticker(ticker)] = item["name"]
    totals: dict[str, float] = {}
    grand = 0.0
    for pos in positions or []:
        mv = float(pos.get("mv") or 0)
        if mv <= 0:
            continue
        grand += mv
        name = index.get(canonical_ticker(pos.get("ticker")))
        if not name:
            continue
        totals[name] = totals.get(name, 0.0) + mv
    out = {}
    for name, mv in totals.items():
        out[name] = {"mv": mv, "pct": (mv / grand * 100.0) if grand else 0.0}
    return out, grand


def underweight_warnings(industries: list[dict], exposure: dict[str, dict], heavy: float = HEAVY_UNDERWEIGHT_PCT) -> list[str]:
    notes = []
    for item in industries:
        if item.get("stance") != "減碼":
            continue
        pct = float(exposure.get(item["name"], {}).get("pct") or 0)
        if pct >= heavy:
            notes.append(f"{item['name']} 是減碼，持股佔 {pct:.1f}%")
    return notes


def parse_fred_csv(text: str) -> list[float]:
    """FRED graph CSV：第一列是表頭，缺值是小數點。"""
    values = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("observation") or line.lower().startswith("date"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        raw = parts[-1].strip()
        if raw in {"", "."}:
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def split_tickers(value) -> list[str]:
    text = str(value or "").replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    found = []
    for part in text.split(","):
        token = part.strip()
        if token and token not in found:
            found.append(token)
    return found


def parse_sheet_date(value) -> str | None:
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        text = value.isoformat()[:10]
    else:
        text = str(value).strip().replace("/", "-")[:10]
    from macro_cycle import _parse_iso_date

    if _parse_iso_date(text) is None:
        return None
    return text


def compose_macro_view(json_view: dict, sheet: dict | None, auto: dict | None, today: str | None = None) -> dict:
    """試算表優先，缺的列用 JSON，最後讓自動分數覆寫自動項。都沒有就中性。"""
    from datetime import date

    today = today or date.today().isoformat()
    sheet = sheet or {"ok": False, "error": "沒有試算表資料"}
    auto = auto or {}
    warnings: list[str] = []
    json_ok = bool(json_view.get("ok"))
    json_indicators = list(json_view.get("indicators") or []) if json_ok else []
    json_industries = list(json_view.get("industries") or []) if json_ok else []

    if not sheet.get("ok"):
        warnings.append(str(sheet.get("error") or "試算表讀不到，改用倉庫 JSON。"))
        if not json_ok:
            failed = copy.deepcopy(json_view)
            failed["warnings"] = warnings + list(json_view.get("errors") or [])
            failed["data_origin"] = "neutral"
            return failed
        view = copy.deepcopy(json_view)
        view["data_origin"] = "json"
        view["warnings"] = warnings
        _apply_auto(view, auto, warnings, today)
        view["warnings"] = warnings
        return _refresh_totals(view)

    if sheet.get("score_error"):
        warnings.append(str(sheet["score_error"]))
    if sheet.get("industry_error"):
        warnings.append(str(sheet["industry_error"]))

    score_rows, score_warnings = parse_score_rows(sheet.get("scores") or [])
    industry_rows, industry_warnings = parse_industry_rows(sheet.get("industries") or [])
    warnings.extend(score_warnings)
    warnings.extend(industry_warnings)

    use_sheet_scores = bool(score_rows) and not sheet.get("score_error")
    use_sheet_industries = bool(industry_rows) and not sheet.get("industry_error")
    if not use_sheet_scores and not use_sheet_industries:
        warnings.append("試算表沒有可用的列，改用倉庫 JSON。")
        if not json_ok:
            failed = copy.deepcopy(json_view)
            failed["warnings"] = warnings
            failed["data_origin"] = "neutral"
            return failed
        view = copy.deepcopy(json_view)
        view["data_origin"] = "json"
        view["warnings"] = warnings
        _apply_auto(view, auto, warnings, today)
        view["warnings"] = warnings
        return _refresh_totals(view)

    indicators = _merge_indicators(json_indicators, score_rows if use_sheet_scores else {}, warnings)
    industries, industry_updated = _merge_industries(
        json_industries,
        industry_rows if use_sheet_industries else [],
        warnings,
        json_view.get("industry_updated_at"),
    )
    commentary = {} if (use_sheet_scores or use_sheet_industries) else (json_view.get("commentary") or {})
    if not use_sheet_scores:
        commentary = json_view.get("commentary") or {}
    view = assemble_view(
        indicators,
        industries,
        origin="sheet" if indicators else "neutral",
        warnings=warnings,
        commentary=commentary if not use_sheet_scores else {},
        industry_updated_at=industry_updated,
        industry_as_of_day=json_view.get("industry_as_of_day") or 12,
    )
    if not view.get("ok"):
        return view
    _apply_auto(view, auto, view["warnings"], today)
    _attach_history(view, sheet, json_view)
    return _refresh_totals(view)


def _attach_history(view: dict, sheet: dict, json_view: dict) -> None:
    warnings = view.setdefault("warnings", [])
    if sheet.get("history_error"):
        warnings.append(str(sheet["history_error"]))
        view["history"] = list(json_view.get("history") or [])
        return
    rows, extra = parse_history_rows(sheet.get("history") or [])
    warnings.extend(extra)
    view["history"] = rows or list(json_view.get("history") or [])


def score_ma_gap(gap: float) -> int:
    """收盤相對 200 日均線的差距（0.03 = 高 3%）。"""
    if gap >= 0.03:
        return 2
    if gap >= 0.01:
        return 1
    if gap > -0.01:
        return 0
    if gap > -0.03:
        return -1
    return -2


def score_vs_ma200(closes: list[float]) -> tuple[int, float] | None:
    if len(closes) < 200:
        return None
    window = [float(item) for item in closes[-200:]]
    average = sum(window) / 200.0
    if average <= 0:
        return None
    gap = window[-1] / average - 1.0
    return score_ma_gap(gap), gap


def score_real_yield_change(values: list[float]) -> tuple[int, float, float] | None:
    """10 年期實質利率（百分點）。回傳分數、最新值、約三個月變化。"""
    series = [float(item) for item in values]
    if len(series) < 40:
        return None
    latest = series[-1]
    past = series[-63] if len(series) >= 63 else series[0]
    delta = latest - past
    if delta <= -0.25:
        score = 2
    elif delta <= -0.10:
        score = 1
    elif delta < 0.10:
        score = 0
    elif delta < 0.25:
        score = -1
    else:
        score = -2
    return score, latest, delta


def score_hy_spread(level: float) -> int:
    """美銀美林高收益債 OAS，單位是百分點。"""
    level = float(level)
    if level <= 3.5:
        return 2
    if level <= 4.5:
        return 1
    if level <= 5.5:
        return 0
    if level <= 7.5:
        return -1
    return -2


def score_vix(level: float) -> int:
    level = float(level)
    if level < 15:
        return 2
    if level < 20:
        return 1
    if level < 25:
        return 0
    if level < 32:
        return -1
    return -2


def score_auto_from_markets(markets: dict, today: str) -> dict:
    """把已經抓到的數列轉成五個自動指標。缺資料的項目 ok 為 false。"""
    found = {}
    found["us_real_yield"] = _yield_result(markets, today)
    found["hy_spread"] = _hy_result(markets, today)
    found["vix"] = _vix_result(markets, today)
    found["equity_ma"] = _equity_result(markets, today)
    found["sox_trend"] = _sox_result(markets, today)
    return found


def _merge_indicators(json_indicators: list[dict], sheet_by_id: dict[str, dict], warnings: list[str]) -> list[dict]:
    json_by_id = {item.get("id"): item for item in json_indicators}
    merged = []
    for indicator_id in CANONICAL_IDS:
        if indicator_id in sheet_by_id:
            row = sheet_by_id[indicator_id]
            merged.append(decorate_indicator(row, row["updated_at"], "sheet"))
            continue
        if indicator_id in json_by_id:
            warnings.append(f"{CANONICAL_NAMES[indicator_id]} 試算表沒有可用列，沿用倉庫備援。")
            merged.append(decorate_indicator(json_by_id[indicator_id], json_by_id[indicator_id].get("updated_at", ""), "json"))
    return merged


def _merge_industries(json_industries, sheet_rows: list[dict], warnings: list[str], json_updated: str | None):
    if not sheet_rows:
        warnings.append("Industry_Cycle 沒有可用列，產業圖沿用倉庫備援。")
        copied = [dict(item) for item in json_industries]
        return copied, json_updated
    merged = [dict(item) for item in sheet_rows]
    names = {item["name"] for item in merged}
    owners = {}
    for item in merged:
        for ticker in item["tickers"]:
            owners[canonical_ticker(ticker)] = item["name"]
    for item in json_industries:
        if item["name"] in names or len(merged) >= 20:
            continue
        clash = _ticker_clash(item, owners)
        if clash:
            continue
        filler = dict(item)
        filler["origin"] = "json"
        merged.append(filler)
        names.add(item["name"])
        for ticker in item.get("tickers") or []:
            owners[canonical_ticker(ticker)] = item["name"]
        warnings.append(f"{item['name']} 試算表沒有這一列，沿用倉庫備援。")
    dates = [item.get("updated_at") for item in merged if item.get("origin") == "sheet" and item.get("updated_at")]
    updated = max(dates) if dates else json_updated
    return merged[:20], updated


def _apply_auto(view: dict, auto: dict, warnings: list[str], today: str) -> None:
    for item in view.get("indicators") or []:
        if item.get("id") not in AUTO_INDICATOR_IDS:
            continue
        result = (auto or {}).get(item["id"]) or {}
        if result.get("ok"):
            item["score"] = int(result["score"])
            item["value"] = str(result.get("value") or "")
            if result.get("trend"):
                item["trend"] = str(result["trend"])
            item["source"] = "auto"
            item["source_label"] = "自動"
            item["updated_at"] = str(result.get("updated_at") or today)
            item["example"] = False
            item["origin"] = "auto"
            if result.get("note"):
                item["note"] = str(result["note"])
            continue
        if item.get("origin") == "sheet":
            warnings.append(f"{item['name']} 自動抓取失敗，沿用試算表。")
        else:
            warnings.append(f"{item['name']} 自動抓取失敗，沿用倉庫備援。")


def _refresh_totals(view: dict) -> dict:
    indicators = view.get("indicators") or []
    if not indicators:
        return view
    dates = [str(item.get("updated_at") or "") for item in indicators if item.get("updated_at")]
    refreshed = assemble_view(
        indicators,
        view.get("industries") or [],
        origin=view.get("data_origin") or "json",
        warnings=view.get("warnings") or [],
        commentary=view.get("commentary") or {},
        score_updated_at=max(dates) if dates else view.get("score_updated_at"),
        industry_updated_at=view.get("industry_updated_at"),
        industry_as_of_day=view.get("industry_as_of_day"),
    )
    refreshed["history"] = list(view.get("history") or [])
    refreshed["week_change"] = week_change(refreshed["total"], refreshed["history"], refreshed.get("score_updated_at"))
    return refreshed


def _parse_history_total(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != int(number):
        return None
    total = int(number)
    if total < -18 or total > 18:
        return None
    return total


def _parse_score(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != int(number):
        return None
    score = int(number)
    if score < -2 or score > 2:
        return None
    return score


def _parse_source(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[text]
    raw = str(value or "").strip()
    return _SOURCE_ALIASES.get(raw)


def _industry_id(name: str) -> str:
    slug = "".join(ch for ch in name.lower() if ch.isascii() and ch.isalnum())
    if slug:
        return "sheet_" + slug
    return "sheet_" + str(abs(hash(name)) % 1_000_000)


def _ticker_clash(item: dict, owners: dict[str, str]) -> str | None:
    for ticker in item.get("tickers") or []:
        key = canonical_ticker(ticker)
        other = owners.get(key)
        if other and other != item["name"]:
            return f"代表股 {ticker} 已經屬於 {other}"
    return None


def _yield_result(markets: dict, today: str) -> dict:
    parsed = score_real_yield_change(markets.get("dfii10") or [])
    if parsed is None:
        return {"ok": False}
    score, latest, delta = parsed
    nominal = markets.get("dgs10") or []
    extra = f"，名目 10 年 {nominal[-1]:.2f}%" if nominal else ""
    return {
        "ok": True,
        "score": score,
        "updated_at": today,
        "value": f"實質 {latest:.2f}%（約三個月 {delta:+.2f} 百分點）{extra}",
        "trend": "下降" if delta <= -0.10 else ("上升" if delta >= 0.10 else "持平"),
        "note": "自動：FRED DFII10。實質利率下降加分。",
    }


def _hy_result(markets: dict, today: str) -> dict:
    series = markets.get("hy") or []
    if not series:
        return {"ok": False}
    level = float(series[-1])
    return {
        "ok": True,
        "score": score_hy_spread(level),
        "updated_at": today,
        "value": f"OAS {level:.2f}%",
        "trend": "偏緊" if level <= 4.5 else ("偏寬" if level > 5.5 else "中性"),
        "note": "自動：FRED BAMLH0A0HYM2。利差低加分。",
    }


def _vix_result(markets: dict, today: str) -> dict:
    level = markets.get("vix")
    if level is None:
        return {"ok": False}
    level = float(level)
    return {
        "ok": True,
        "score": score_vix(level),
        "updated_at": today,
        "value": f"VIX {level:.1f}",
        "trend": "偏低" if level < 20 else ("偏高" if level >= 25 else "中性"),
        "note": "自動：VIX 收盤。低波動加分。",
    }


def _equity_result(markets: dict, today: str) -> dict:
    parts = []
    labels = []
    for key, label in (("spx", "標普"), ("twii", "台股")):
        scored = score_vs_ma200(markets.get(key) or [])
        if scored is None:
            continue
        score, gap = scored
        parts.append(score)
        labels.append(f"{label} {gap * 100:+.1f}%")
    if not parts:
        return {"ok": False}
    combined = int(max(-2, min(2, round(sum(parts) / len(parts)))))
    return {
        "ok": True,
        "score": combined,
        "updated_at": today,
        "value": "、".join(labels) + "（相對 200 日）",
        "trend": "高於年線" if combined > 0 else ("低於年線" if combined < 0 else "貼近年線"),
        "note": "自動：高於年線加分。兩市都有資料時取平均後四捨五入。",
    }


def _sox_result(markets: dict, today: str) -> dict:
    scored = score_vs_ma200(markets.get("sox") or [])
    if scored is None:
        return {"ok": False}
    score, gap = scored
    return {
        "ok": True,
        "score": score,
        "updated_at": today,
        "value": f"費半相對 200 日 {gap * 100:+.1f}%",
        "trend": "高於年線" if score > 0 else ("低於年線" if score < 0 else "貼近年線"),
        "note": "自動：費半高於年線加分。",
    }
