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

# 八項各 -2..+2，加總 -16..+16。+1 落在中性。
REGIME_BANDS: tuple[tuple[str, int, int], ...] = (
    ("牛", 9, 16),
    ("偏多", 4, 8),
    ("中性", -3, 3),
    ("偏空", -8, -4),
    ("熊", -16, -9),
)

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
    raise ValueError(f"分數 {total} 不在 -16 到 16")


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


def _view_from_valid(raw: dict) -> dict:
    indicators = list(raw["score"]["indicators"])
    industries = list(raw["industries"]["items"])
    total = sum(int(item["score"]) for item in indicators)
    regime = regime_for_score(total)
    return {
        "ok": True,
        "fallback": False,
        "errors": [],
        "schema_version": raw["schema_version"],
        "score_updated_at": raw["score"]["updated_at"],
        "score_cadence": raw["score"]["cadence"],
        "industry_updated_at": raw["industries"]["updated_at"],
        "industry_cadence": raw["industries"]["cadence"],
        "industry_as_of_day": int(raw["industries"]["as_of_day"]),
        "indicators": indicators,
        "industries": industries,
        "total": total,
        "regime": regime,
        "risk": risk_params_for_regime(regime),
        "ticker_index": _ticker_index(industries),
        "commentary": dict(raw.get("commentary") or {}),
    }


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
