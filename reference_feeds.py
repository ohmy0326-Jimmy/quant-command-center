"""第五頁參考數據。不進入九項總分，缺值是 None。

沒有穩定免費來源的項目只留名字，不產生數字。
"""

from __future__ import annotations

REFERENCE_SERIES = (
    {"id": "t10y2y", "name": "美債 10 年減 2 年", "source": "FRED T10Y2Y"},
    {"id": "t10y3m", "name": "美債 10 年減 3 個月", "source": "FRED T10Y3M"},
    {"id": "icsa", "name": "美國初領失業金", "source": "FRED ICSA"},
    {"id": "nfci", "name": "芝加哥聯準金融條件", "source": "FRED NFCI"},
    {"id": "move", "name": "MOVE 指數", "source": "Yahoo ^MOVE"},
    {"id": "tw_margin", "name": "台股融資餘額", "source": "證交所 MI_MARGN"},
    {"id": "tw_institutions", "name": "三大法人買賣超", "source": "證交所 BFI82U"},
    {"id": "tsmc_revenue", "name": "台積電月營收", "source": "證交所公開資訊 t187ap05_L"},
)

NOT_AVAILABLE = (
    {"id": "eight_banks", "name": "八大官股", "why": "沒有穩定的免費彙總序列，不另編數字。"},
    {"id": "cloud_capex", "name": "雲端資本支出", "why": "各公司財報沒有單一免費時間序列，不另編數字。"},
    {"id": "sector_flows", "name": "法人板塊買賣", "why": "證交所個股買賣超不能可靠加成產業，不另編數字。"},
    {"id": "foreign_futures", "name": "外資期貨部位", "why": "期交所頁面不是穩定的 JSON，這次不解析 HTML。"},
    {"id": "export_orders", "name": "外銷訂單", "why": "經濟部統計頁在這次環境回 403，沒有改用估計值。"},
)

FRED_IDS = {
    "t10y2y": "T10Y2Y",
    "t10y3m": "T10Y3M",
    "icsa": "ICSA",
    "nfci": "NFCI",
}


def reference_rows(values: dict | None) -> list[dict]:
    """values[id] 是 {value, as_of} 或 None。"""
    values = values or {}
    rows = []
    for spec in REFERENCE_SERIES:
        payload = values.get(spec["id"])
        ok = isinstance(payload, dict) and payload.get("value") is not None
        rows.append({
            "name": spec["name"],
            "source": spec["source"],
            "value": payload.get("value") if ok else None,
            "as_of": payload.get("as_of") if ok else None,
            "ok": ok,
            "note": "" if ok else "不完整",
        })
    for spec in NOT_AVAILABLE:
        rows.append({
            "name": spec["name"],
            "source": "",
            "value": None,
            "as_of": None,
            "ok": False,
            "note": spec["why"],
        })
    return rows


def parse_margin_balance(payload: dict) -> dict | None:
    tables = (payload or {}).get("tables") or []
    if not tables:
        return None
    fields = tables[0].get("fields") or []
    data = tables[0].get("data") or []
    if "今日餘額" not in fields or not data:
        return None
    index = fields.index("今日餘額")
    for row in data:
        if row and "融資" in str(row[0]) and "融券" not in str(row[0]):
            value = _number(row[index])
            if value is None:
                return None
            return {"value": value, "as_of": str(payload.get("date") or "")}
    return None


def parse_institution_net(payload: dict) -> dict | None:
    fields = (payload or {}).get("fields") or []
    data = (payload or {}).get("data") or []
    if "買賣差額" not in fields:
        return None
    index = fields.index("買賣差額")
    name_at = fields.index("單位名稱") if "單位名稱" in fields else 0
    for row in data:
        if row and "合計" in str(row[name_at]):
            value = _number(row[index])
            if value is None:
                return None
            return {"value": value, "as_of": str(payload.get("date") or "")}
    return None


def parse_tsmc_revenue(rows: list[dict]) -> dict | None:
    for row in rows or []:
        if str(row.get("公司代號") or "") != "2330":
            continue
        value = _number(row.get("營業收入-當月營收"))
        if value is None:
            return None
        return {"value": value, "as_of": str(row.get("資料年月") or "")}
    return None


def _number(value):
    if value is None or value == "":
        return None
    text = str(value).replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None
