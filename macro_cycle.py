"""總經週期檔的讀取、驗證，以及多空狀態對應的風控預設。

分數與產業圖放在 data/macro_cycle.json。格式寫在 data/macro_cycle.schema.json。
檔案缺漏或驗證失敗時不丟例外，改回中性，讓頁面與下單輔助仍可開啟。
此模組不連網、不讀金鑰、不計算第四頁的淨值公式。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "macro_cycle.json"
SCHEMA_PATH = ROOT / "data" / "macro_cycle.schema.json"

# 九項各 -2..+2，加總 -18..+18。內部門檻與八項時期相同，+1 仍是中性。
# 多出來的 ±17、±18 併入牛／熊。
REGIME_BANDS: tuple[tuple[str, int, int], ...] = (
    ("牛", 9, 18),
    ("偏多", 4, 8),
    ("中性", -3, 3),
    ("偏空", -8, -4),
    ("熊", -18, -9),
)

# 試算表 Macro_Score 的列必須使用這些 indicator_id。自動項由程式覆寫分數。
CANONICAL_INDICATORS: tuple[dict, ...] = (
    {"id": "us_real_yield", "name": "美國實質利率", "kind": "auto"},
    {"id": "hy_spread", "name": "高收益債利差", "kind": "auto"},
    {"id": "vix", "name": "波動率 VIX", "kind": "auto"},
    {"id": "equity_ma", "name": "美股與台股年線", "kind": "auto"},
    {"id": "sox_trend", "name": "費半趨勢", "kind": "auto"},
    {"id": "pmi", "name": "製造業 PMI", "kind": "manual"},
    {"id": "earnings_revisions", "name": "企業獲利修正", "kind": "manual"},
    {"id": "valuation", "name": "評價", "kind": "manual"},
    {"id": "tw_activity", "name": "台灣景氣與出口", "kind": "manual"},
)
CANONICAL_IDS = tuple(item["id"] for item in CANONICAL_INDICATORS)
AUTO_INDICATOR_IDS = frozenset(item["id"] for item in CANONICAL_INDICATORS if item["kind"] == "auto")
CANONICAL_NAMES = {item["id"]: item["name"] for item in CANONICAL_INDICATORS}

# 對應第六頁「目標槓桿上限（倍）」與「最低現金水位（%）」。
# 第二階段槓桿不在這裡改。
REGIME_RISK: dict[str, dict[str, float]] = {
    "牛": {"target_leverage": 2.0, "min_cash_pct": 10.0},
    "偏多": {"target_leverage": 1.8, "min_cash_pct": 12.0},
    "中性": {"target_leverage": 1.5, "min_cash_pct": 15.0},
    "偏空": {"target_leverage": 1.2, "min_cash_pct": 22.0},
    "熊": {"target_leverage": 1.0, "min_cash_pct": 30.0},
}

FALLBACK_REGIME = "中性"
_TICKER_SUFFIXES = (".TWO", ".TW", ".US", ".HK")

# 四象限就是四個週期位置：左上復甦、右上成長、右下趨緩、左下衰退。
_AXIS = 1.15
_QUADRANTS = {
    "復甦": (-_AXIS, _AXIS),
    "成長": (_AXIS, _AXIS),
    "趨緩": (_AXIS, -_AXIS),
    "衰退": (-_AXIS, -_AXIS),
}
_CLOCKWISE = {
    "復甦": (1.0, 0.0),
    "成長": (0.0, -1.0),
    "趨緩": (-1.0, 0.0),
    "衰退": (0.0, 1.0),
}
_DIRECTION_SHIFT = {"上行": 0.45, "持平": 0.0, "下行": -0.45}
_PERP_SPAN = 1.35


def canonical_ticker(symbol: str) -> str:
    """2330、2330.TW、2330.tw 都變成 2330。其他後綴只去掉交易所。"""
    text = str(symbol or "").strip().upper()
    for suffix in _TICKER_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def regime_for_score(total: int) -> str:
    if isinstance(total, bool) or not isinstance(total, int):
        raise TypeError("分數必須是整數")
    for name, low, high in REGIME_BANDS:
        if low <= total <= high:
            return name
    raise ValueError(f"分數 {total} 不在 -18 到 18")


def risk_params_for_regime(regime: str) -> dict:
    params = REGIME_RISK[regime]
    return {
        "regime": regime,
        "target_leverage": float(params["target_leverage"]),
        "min_cash_pct": float(params["min_cash_pct"]),
    }


def regime_scale_text() -> str:
    parts = [f"{name} {low:+d}～{high:+d}" for name, low, high in REGIME_BANDS]
    return "、".join(parts)


def regime_rows() -> list[dict]:
    rows = []
    for name, low, high in REGIME_BANDS:
        risk = REGIME_RISK[name]
        rows.append(
            {
                "狀態": name,
                "分數": f"{low:+d}～{high:+d}",
                "目標槓桿上限": f"{risk['target_leverage']:.1f}x",
                "最低現金水位": f"{risk['min_cash_pct']:.0f}%",
            }
        )
    return rows


def load_schema(path: str | Path | None = None) -> dict:
    schema_path = Path(path) if path else SCHEMA_PATH
    return json.loads(schema_path.read_text(encoding="utf-8"))


def validate_macro_cycle(data, schema: dict | None = None) -> list[str]:
    """回傳錯誤訊息。空清單代表這份資料可以用。"""
    if schema is None:
        schema = load_schema()
    errors = _schema_errors(data, schema)
    if errors:
        return errors
    return semantic_errors(data)


def semantic_errors(data: dict) -> list[str]:
    """Schema 已通過後的跨欄位檢查。呼叫端應先過 JSON Schema。"""
    errors: list[str] = []
    score_day = _parse_iso_date(data["score"]["updated_at"])
    if score_day is None:
        errors.append(f"分數更新日不是有效日期：{data['score']['updated_at']}")

    industries = data["industries"]
    industry_day = _parse_iso_date(industries["updated_at"])
    if industry_day is None:
        errors.append(f"產業配置更新日不是有效日期：{industries['updated_at']}")
    elif industry_day.day != int(industries["as_of_day"]):
        errors.append(
            f"產業配置更新日必須落在每月 {industries['as_of_day']} 日，目前是 {industries['updated_at']}"
        )

    seen_indicator_ids: set[str] = set()
    for item in data["score"]["indicators"]:
        if item["id"] in seen_indicator_ids:
            errors.append(f"指標代號重複：{item['id']}")
        seen_indicator_ids.add(item["id"])

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    owners: dict[str, str] = {}
    for item in industries["items"]:
        if item["id"] in seen_ids:
            errors.append(f"產業代號重複：{item['id']}")
        seen_ids.add(item["id"])
        if item["name"] in seen_names:
            errors.append(f"產業名稱重複：{item['name']}")
        seen_names.add(item["name"])
        seen_here: set[str] = set()
        for ticker in item["tickers"]:
            key = canonical_ticker(ticker)
            if not key:
                errors.append(f"{item['name']} 的代表股是空的")
                continue
            if key in seen_here or key in owners:
                other = owners.get(key, item["name"])
                errors.append(f"代表股 {ticker} 同時對到「{other}」與「{item['name']}」")
                continue
            seen_here.add(key)
            owners[key] = item["name"]
    return errors


def load_macro_cycle(path: str | Path | None = None) -> dict:
    """讀取並驗證。失敗時 ok 為 false，風控預設改用中性，不丟例外。"""
    data_path = Path(path) if path is not None else DATA_PATH
    try:
        schema = load_schema()
    except FileNotFoundError:
        return _fallback(["找不到 JSON Schema：data/macro_cycle.schema.json"])
    except json.JSONDecodeError as exc:
        return _fallback([f"JSON Schema 無法解析：{exc.msg}"])
    except OSError as exc:
        return _fallback([f"無法讀取 JSON Schema：{exc.strerror or exc}"])

    try:
        if not data_path.is_file():
            return _fallback([f"找不到總經檔：{data_path.name}"])
        raw = json.loads(data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fallback([f"總經檔不是合法 JSON：第 {exc.lineno} 行 {exc.msg}"])
    except OSError as exc:
        return _fallback([f"無法讀取總經檔：{exc.strerror or exc}"])

    try:
        errors = validate_macro_cycle(raw, schema)
    except Exception as exc:
        return _fallback([f"總經檔驗證時發生錯誤：{exc}"])
    if errors:
        return _fallback(errors)
    try:
        return _view_from_valid(raw)
    except Exception as exc:
        return _fallback([f"總經檔無法轉成週期狀態：{exc}"])


def holding_tag(view: dict, ticker: str) -> dict | None:
    """對得上代表股才回產業與立場。對不上、或總經檔沒套用，回 None。"""
    if not isinstance(view, dict) or not view.get("ok"):
        return None
    found = (view.get("ticker_index") or {}).get(canonical_ticker(ticker))
    if not found:
        return None
    return dict(found)


def format_holding_tag(tag: dict | None) -> str:
    if not tag:
        return ""
    industry = str(tag.get("industry") or "").strip()
    stance = str(tag.get("stance") or "").strip()
    if not industry or not stance:
        return ""
    text = f"{industry} · {stance}"
    if tag.get("example"):
        text += "（示例）"
    return text


def industry_quadrant_points(industries: list[dict]) -> list[dict]:
    """把產業放到四個象限。上行順時針靠、下行逆時針靠，同象限的點左右錯開。"""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in industries:
        grouped.setdefault((item["cycle"], item["direction"]), []).append(item)
    points: list[dict] = []
    for (cycle, direction), items in grouped.items():
        ordered = sorted(items, key=lambda row: str(row.get("name", "")))
        if cycle not in _QUADRANTS or direction not in _DIRECTION_SHIFT:
            continue
        cx, cy = _QUADRANTS[cycle]
        dx, dy = _CLOCKWISE[cycle]
        px, py = -dy, dx
        sign = _DIRECTION_SHIFT[direction]
        count = len(ordered)
        step = _PERP_SPAN / (count - 1) if count > 1 else 0.0
        for index, item in enumerate(ordered):
            offset = (index - (count - 1) / 2.0) * step
            x, y = _keep_in_quadrant(cycle, cx + dx * sign + px * offset, cy + dy * sign + py * offset)
            points.append(
                {
                    "name": item["name"],
                    "cycle": cycle,
                    "direction": direction,
                    "stance": item["stance"],
                    "example": bool(item["example"]),
                    "tickers": list(item["tickers"]),
                    "x": x,
                    "y": y,
                    "quadrant": cycle,
                }
            )
    return points


def week_change(total: int, history: list[dict], as_of: str | None) -> int | None:
    """本週總分減去上一筆更早的週分數。沒有上週就回 None。"""
    prior = []
    for row in history or []:
        week = str(row.get("week") or "")
        if not week or (as_of and week >= as_of):
            continue
        if row.get("total") is None:
            continue
        prior.append(row)
    if not prior:
        return None
    prior.sort(key=lambda row: str(row["week"]))
    return int(total) - int(prior[-1]["total"])


def quadrant_shift(item: dict) -> str:
    """上月週期不同才回傳『成長→趨緩』，否則空字串。"""
    previous = str(item.get("prev_cycle") or "").strip()
    current = str(item.get("cycle") or "").strip()
    if not previous or previous == current:
        return ""
    return f"{previous}→{current}"


def quadrant_change_text(item: dict) -> str:
    """「上期→本期」。沒有上期資料回傳「—」。"""
    previous = str(item.get("prev_cycle") or "").strip()
    current = str(item.get("cycle") or "").strip()
    if not previous:
        return "—"
    return f"{previous}→{current}"


# 公布時程。沒有固定時程就是「未知」。製造業改看費城與紐約調查，不再用 ISM 第一個工作日。
MANUAL_RELEASE_SCHEDULE: dict[str, dict] = {
    "pmi": {
        "rule": "philly_empire",
        "label": "費城聯準會（第三個星期四）與紐約聯準會（每月 15 日或下一個工作日），取較早的下一次",
    },
    "tw_activity": {"rule": "day_of_month", "day": 27, "label": "國發會景氣對策信號：約每月 27 日"},
    "valuation": {"rule": "next_business_day", "label": "標普本益比與 FRED DFII10，每個交易日"},
}


def next_release_date(indicator_id: str, today=None) -> str:
    """依公布時程推下一次公布日（ISO）。沒有時程回傳「未知」。不考慮國定假日，所以是預估。"""
    from datetime import date, timedelta

    spec = MANUAL_RELEASE_SCHEDULE.get(str(indicator_id or ""))
    if not spec:
        return "未知"
    today = today or date.today()

    def candidate(year: int, month: int) -> date:
        rule = spec["rule"]
        if rule == "first_business_day":
            day = date(year, month, 1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return day
        if rule == "next_business_day":
            day = today + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return day
        if rule == "philly_empire":
            return _sooner_survey_date(year, month, today)
        return date(year, month, int(spec["day"]))

    if spec["rule"] == "next_business_day":
        return candidate(today.year, today.month).isoformat()
    year, month = today.year, today.month
    for _ in range(4):
        day = candidate(year, month)
        if day > today:
            return day.isoformat()
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return "未知"


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    from datetime import date, timedelta

    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _sooner_survey_date(year: int, month: int, today):
    """這個月費城第三個星期四、紐約 15 日（碰到週末就往後）裡，晚於今天的較早一天。沒有就丟回今天，讓外層改看下個月。"""
    from datetime import date, timedelta

    empire = date(year, month, 15)
    while empire.weekday() >= 5:
        empire += timedelta(days=1)
    philly = _nth_weekday(year, month, 3, 3)
    upcoming = [day for day in (empire, philly) if day > today]
    if not upcoming:
        return today
    return min(upcoming)


def manual_indicator_schedule(indicators: list[dict], today=None) -> list[dict]:
    """手動指標的最近更新日與下次公布日。更新日讀不到就寫「未知」。"""
    rows = []
    for item in indicators:
        if item.get("source") == "auto":
            continue
        rows.append(
            {
                "指標": item.get("name") or "",
                "最近更新": str(item.get("updated_at") or "").strip() or "未知",
                "下次公布（預估）": next_release_date(item.get("id"), today),
            }
        )
    return rows


def decorate_indicator(item: dict, updated_default: str, origin: str) -> dict:
    source = str(item.get("source") or "manual").strip().lower()
    if source not in {"auto", "manual"}:
        source = "manual"
    updated = str(item.get("updated_at") or updated_default or "")
    example = bool(item.get("example"))
    raw_score = item.get("score")
    score = None if raw_score is None or raw_score == "" else int(raw_score)
    scored = bool(item.get("scored", score is not None and not example))
    if example:
        layer = item.get("layer") or "示例"
    elif item.get("layer"):
        layer = item["layer"]
    elif source == "auto":
        layer = "自動"
    else:
        layer = "手動"
    return {
        "id": item.get("id"),
        "name": item.get("name") or CANONICAL_NAMES.get(item.get("id"), ""),
        "score": score,
        "example": example,
        "scored": scored,
        "layer": layer,
        "source": source,
        "source_label": "自動" if source == "auto" else "手動",
        "value": str(item.get("value") or ""),
        "trend": str(item.get("trend") or ""),
        "note": str(item.get("note") or ""),
        "updated_at": updated,
        "data_date": str(item.get("data_date") or (updated if scored else "") or ""),
        "next_release": str(item.get("next_release") or next_release_date(item.get("id")) or ""),
        "origin": origin,
        "weight": float(item["weight"]) if item.get("weight") is not None else None,
    }


def assemble_view(
    indicators: list[dict],
    industries: list[dict],
    *,
    origin: str,
    warnings: list[str] | None = None,
    commentary: dict | None = None,
    score_updated_at: str | None = None,
    industry_updated_at: str | None = None,
    industry_as_of_day: int | None = 12,
) -> dict:
    """由已整理好的指標與產業組成畫面用的字典。指標是空的就退回中性。"""
    if not indicators:
        failed = _fallback(list(warnings or ["沒有可用的多空指標"]))
        failed["data_origin"] = "neutral"
        failed["warnings"] = list(warnings or failed["errors"])
        return failed
    from macro_proxies import aggregate_scores

    summary = aggregate_scores(indicators)
    dates = [str(item.get("updated_at") or "") for item in indicators if item.get("updated_at")]
    warnings = list(warnings or [])
    if summary.get("coverage_note"):
        warnings.append(summary["coverage_note"])
    return {
        "ok": True,
        "fallback": origin == "neutral",
        "errors": [],
        "warnings": warnings,
        "data_origin": origin,
        "schema_version": "1.0.0",
        "score_updated_at": score_updated_at or (max(dates) if dates else None),
        "score_cadence": "weekly",
        "industry_updated_at": industry_updated_at,
        "industry_cadence": "monthly",
        "industry_as_of_day": industry_as_of_day,
        "indicators": indicators,
        "industries": industries,
        "total": summary["total"],
        "raw_regime": summary["raw_regime"],
        "regime": summary["regime"],
        "risk": summary["risk"],
        "coverage": summary["coverage"],
        "coverage_status": summary["coverage_status"],
        "coverage_note": summary["coverage_note"],
        "ticker_index": _ticker_index(industries),
        "commentary": dict(commentary or {}),
        "history": [],
        "week_change": None,
    }


def _view_from_valid(raw: dict) -> dict:
    updated = raw["score"]["updated_at"]
    indicators = [decorate_indicator(item, updated, "json") for item in raw["score"]["indicators"]]
    industries = [
        dict(item, origin="json", cycle_layer="示例", cycle_note="倉庫示例，不是價格推估")
        for item in raw["industries"]["items"]
    ]
    view = assemble_view(
        indicators,
        industries,
        origin="json",
        warnings=[],
        commentary=raw.get("commentary") or {},
        score_updated_at=updated,
        industry_updated_at=raw["industries"]["updated_at"],
        industry_as_of_day=int(raw["industries"]["as_of_day"]),
    )
    view["history"] = list(raw.get("history") or [])
    view["week_change"] = None if view["total"] is None else week_change(view["total"], view["history"], updated)
    return view


def _ticker_index(industries: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in industries:
        payload = {
            "industry": item["name"],
            "stance": item["stance"],
            "example": bool(item["example"]),
            "cycle": item["cycle"],
            "direction": item["direction"],
        }
        for ticker in item["tickers"]:
            index[canonical_ticker(ticker)] = payload
    return index


def _fallback(errors: list[str]) -> dict:
    risk = risk_params_for_regime(FALLBACK_REGIME)
    return {
        "ok": False,
        "fallback": True,
        "errors": list(errors)[:12],
        "schema_version": None,
        "score_updated_at": None,
        "score_cadence": None,
        "industry_updated_at": None,
        "industry_cadence": None,
        "industry_as_of_day": None,
        "indicators": [],
        "industries": [],
        "total": None,
        "regime": FALLBACK_REGIME,
        "risk": risk,
        "ticker_index": {},
        "commentary": {},
        "warnings": list(errors)[:12],
        "data_origin": "neutral",
    }


def _parse_iso_date(text: str):
    try:
        from datetime import date

        return date.fromisoformat(str(text))
    except ValueError:
        return None


def _path_key(error) -> tuple:
    key = []
    for part in error.absolute_path:
        if isinstance(part, int):
            key.append((0, part))
        else:
            key.append((1, str(part)))
    return tuple(key)


def _schema_errors(data, schema: dict) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return ["環境缺少 jsonschema，無法驗證總經檔，已改用中性預設。"]
    try:
        validator = Draft202012Validator(schema)
        found = sorted(validator.iter_errors(data), key=_path_key)
    except SchemaError as exc:
        return [f"JSON Schema 本身有問題：{exc.message}"]
    messages = [_zh_schema_message(error) for error in found[:8]]
    return messages


def _zh_schema_message(error) -> str:
    loc = ".".join(str(part) for part in error.absolute_path) or "根節點"
    validator = error.validator
    if validator == "required":
        return f"{loc} 缺少必要欄位（{error.message}）"
    if validator == "const":
        return f"{loc} 必須是 {error.validator_value!r}"
    if validator == "enum":
        return f"{loc} 不在允許的選項裡"
    if validator in {"minimum", "maximum"}:
        return f"{loc} 超出範圍（{error.message}）"
    if validator in {"minItems", "maxItems"}:
        return f"{loc} 筆數不符（{error.message}）"
    if validator == "type":
        return f"{loc} 型別不符（{error.message}）"
    if validator == "additionalProperties":
        return f"{loc} 有未定義的欄位（{error.message}）"
    if validator == "pattern":
        return f"{loc} 格式不符"
    if validator == "uniqueItems":
        return f"{loc} 有重複值"
    return f"{loc}：{error.message}"


def _keep_in_quadrant(cycle: str, x: float, y: float) -> tuple[float, float]:
    margin = 0.2
    limit = 2.05
    x = max(-limit, min(limit, x))
    y = max(-limit, min(limit, y))
    if cycle in ("復甦", "衰退"):
        x = min(x, -margin)
    else:
        x = max(x, margin)
    if cycle in ("復甦", "成長"):
        y = max(y, margin)
    else:
        y = min(y, -margin)
    return x, y
