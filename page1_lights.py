"""第一頁燈號摘要。計分沿用 macro_risk，不把新指標併進 Telegram 用的總分。

約定門檻只有 DFII10：實質殖利率達到 2.5% 為黃、3.0% 為紅（百分點，FRED 原值，不除以 10）。
30 年公債、高收益與投資級 OAS 只顯示讀數，沒有使用者約定的黃紅門檻，所以不亮燈、也不計 0 分。
缺資料或過期是「未知」，不是綠燈。
"""

from __future__ import annotations

from datetime import date, datetime

import macro_risk

DFII10_YELLOW = 2.5
DFII10_RED = 3.0
STALE_BUSINESS_DAYS = macro_risk.STALE_BUSINESS_DAYS

# 有約定燈號的新指標。不進 macro_risk.INDICATOR_ORDER，避免改到既有推播分數。
AGREED_SERIES = {
    "dfii10": {
        "name": "美國 10 年實質利率",
        "series_id": "DFII10",
        "unit": "百分點",
        "source": "FRED DFII10（10 年期 TIPS 實質殖利率）",
        "note": "約定門檻：達到 2.5 黃燈、達到 3.0 紅燈。單位是百分點，不是名目殖利率，也不把報價再除以 10。",
    },
}

# 來源可靠，但對話裡沒有約定黃紅數字。只顯示，不計分。
READING_SERIES = {
    "dgs30": {
        "name": "美國 30 年公債殖利率",
        "series_id": "DGS30",
        "unit": "百分點",
        "source": "FRED DGS30（30 年期名目公債殖利率）",
        "note": "無約定黃紅門檻，只顯示讀數，不計入燈號分數。",
    },
    "hy_oas": {
        "name": "高收益債利差",
        "series_id": "BAMLH0A0HYM2",
        "unit": "百分點",
        "source": "FRED BAMLH0A0HYM2（美銀美國高收益債 OAS）",
        "note": "無約定黃紅門檻，只顯示讀數，不計入燈號分數。",
    },
    "ig_oas": {
        "name": "投資級公司債利差",
        "series_id": "BAMLC0A0CM",
        "unit": "百分點",
        "source": "FRED BAMLC0A0CM（美銀美國投資級公司債 OAS）",
        "note": "無約定黃紅門檻，只顯示讀數，不計入燈號分數。",
    },
}

NOT_DONE = (
    {
        "name": "MOVE 債市波動",
        "why": "ICE BofA MOVE 沒有放在 FRED 的公開序列。Yahoo ^MOVE 不是交易所官方來源，常缺值，這批不接。",
    },
    {
        "name": "融資餘額與維持率",
        "why": "集中市場融資餘額要另接證交所融資融券餘額；個股維持率在券商端，沒有已接上的每日來源。",
    },
    {
        "name": "外資台指期淨空單對現貨",
        "why": "期貨部位在期交所三大法人，和現貨買賣超不是同一個檔，這批沒有接。",
    },
    {
        "name": "八大官股買賣超",
        "why": "官股名單與個股分點沒有接進現有抓取。",
    },
    {
        "name": "四大雲端廠資本支出下修",
        "why": "要看各公司財報或法說，沒有單一公開日頻序列。",
    },
)


def parse_fred_observations(text: str) -> list[tuple[date, float]]:
    """FRED graph CSV。缺值（小數點）跳過。回傳由舊到新的 (日期, 數值)。"""
    found: list[tuple[date, float]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("observation") or line.lower().startswith("date"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        raw = parts[-1]
        if raw in {"", "."}:
            continue
        try:
            day = date.fromisoformat(parts[0][:10])
            value = float(raw)
        except ValueError:
            continue
        if value != value:  # NaN
            continue
        found.append((day, value))
    found.sort(key=lambda item: item[0])
    return found


def _observation(observations, today: date) -> dict:
    if not observations:
        return {"value": None, "date": None, "status": macro_risk.STATUS_MISSING}
    day, value = observations[-1]
    status = macro_risk.freshness(day, today)
    return {"value": float(value), "date": day, "status": status}


def score_dfii10(value: float | None, status: str) -> dict:
    """只有資料正常才亮約定燈。缺漏與過期都是未知，分數保持 0 且不當成未觸發。"""
    if status != macro_risk.STATUS_OK or value is None:
        return {"light": "unknown", "points": 0, "triggered": False}
    if value >= DFII10_RED:
        return {"light": "red", "points": 0, "triggered": True}
    if value >= DFII10_YELLOW:
        return {"light": "yellow", "points": 0, "triggered": True}
    return {"light": "green", "points": 0, "triggered": False}


def _existing_light(item: dict) -> str:
    if item.get("status") != macro_risk.STATUS_OK:
        return "unknown"
    if int(item.get("points") or 0) > 0:
        return "triggered"
    return "clear"


def _row_from_macro(item: dict) -> dict:
    light = _existing_light(item)
    return {
        "key": item.get("key"),
        "name": item.get("name"),
        "role": "既有計分",
        "value": item.get("value"),
        "value_text": item.get("value_text") or "—",
        "unit": item.get("threshold") or "",
        "date": item.get("date") or "—",
        "source": item.get("source") or "",
        "status": item.get("status"),
        "status_text": macro_risk.STATUS_LABELS.get(item.get("status"), "未知"),
        "light": light,
        "light_text": {"unknown": "未知", "triggered": "已觸發", "clear": "未觸發"}[light],
        "note": item.get("note") or "",
    }


def _series_row(spec: dict, observations, today: date, *, agreed: bool) -> dict:
    obs = _observation(observations, today)
    base = {
        "key": spec["series_id"],
        "name": spec["name"],
        "role": "約定燈號" if agreed else "只顯示",
        "value": obs["value"],
        "unit": spec["unit"],
        "date": obs["date"].isoformat() if obs["date"] else "—",
        "source": spec["source"],
        "status": obs["status"],
        "status_text": macro_risk.STATUS_LABELS[obs["status"]],
        "note": spec["note"],
    }
    if not agreed:
        light = "unknown" if obs["status"] != macro_risk.STATUS_OK else "reading"
        text = "—" if obs["value"] is None else f"{obs['value']:.2f} {spec['unit']}"
        base.update(value_text=text, light=light, light_text="未知" if light == "unknown" else "無約定門檻")
        return base
    scored = score_dfii10(obs["value"], obs["status"])
    text = "—" if obs["value"] is None else f"{obs['value']:.2f} {spec['unit']}"
    base.update(
        value_text=text,
        light=scored["light"],
        light_text={"red": "紅燈", "yellow": "黃燈", "green": "綠燈", "unknown": "未知"}[scored["light"]],
    )
    return base


def assemble_board(macro_items: list[dict], series: dict | None, today: date) -> dict:
    """series：key → 觀測列表或 None（抓取失敗）。"""
    series = series or {}
    rows = [_row_from_macro(item) for item in macro_items]
    dfii = _series_row(AGREED_SERIES["dfii10"], series.get("dfii10"), today, agreed=True)
    rows.append(dfii)
    readings = [_series_row(spec, series.get(key), today, agreed=False) for key, spec in READING_SERIES.items()]
    scored_unknown = [row["name"] for row in rows if row["light"] == "unknown"]
    reading_unknown = [row["name"] for row in readings if row["light"] == "unknown"]
    return {
        "rows": rows,
        "readings": readings,
        "dfii10": dfii,
        "scored_complete": not scored_unknown,
        "readings_complete": not reading_unknown,
        "complete": not scored_unknown and not reading_unknown,
        "unknown_names": scored_unknown + reading_unknown,
        "not_done": list(NOT_DONE),
    }


def summary_lines(board: dict, macro_summary: dict) -> list[str]:
    """給畫面的短句。既有總分照舊，但不在缺資料時把整頁說成綠燈。"""
    lines = [f"既有計分 {macro_summary.get('score_text', '—')}，燈號 {macro_summary.get('label', '—')}。這段分數不含 DFII10。"]
    dfii = board["dfii10"]
    lines.append(f"DFII10 {dfii['value_text']}（{dfii['date']}，{dfii['light_text']}）。")
    if not board["complete"]:
        lines.append("資料不完整：" + "、".join(board["unknown_names"]) + "。缺漏或過期不是綠燈，也沒有當成 0 分的未觸發。")
    elif macro_summary.get("level") == "green" and dfii["light"] == "green":
        lines.append("已列入的計分指標與 DFII10 都有資料，而且目前沒有觸發約定門檻。")
    return lines


def table_records(rows: list[dict]) -> list[dict]:
    return [
        {
            "指標": row["name"],
            "角色": row["role"],
            "讀數": row["value_text"],
            "單位": row["unit"],
            "資料日期": row["date"],
            "來源": row["source"],
            "狀態": row["status_text"],
            "燈號": row["light_text"],
        }
        for row in rows
    ]


def fetch_fred_observations(series_id: str, get=None) -> list[tuple[date, float]]:
    """抓 FRED 公開 CSV。失敗或空檔丟例外，讓快取不要記住缺值。"""
    import requests

    http_get = get or requests.get
    response = http_get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": series_id},
        headers={"User-Agent": "quant-command-center/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    found = parse_fred_observations(response.text)
    if not found:
        raise ValueError(f"{series_id} 沒有有效觀測")
    return found


def render_page1_summary(st, board: dict, macro_summary: dict) -> None:
    """精簡摘要。st 是 Streamlit。新燈只顯示，不從這裡推播。"""
    st.subheader("警報燈摘要")
    level = macro_summary.get("level")
    headline = summary_lines(board, macro_summary)[0]
    if level == "red":
        st.error(headline)
    elif level == "yellow":
        st.warning(headline)
    elif not board["scored_complete"]:
        st.warning(headline)
    else:
        st.caption(headline)
    dfii = board["dfii10"]
    dfii_line = f"DFII10 {dfii['value_text']}（{dfii['date']}，{dfii['light_text']}）。只顯示，不另送 Telegram。"
    if dfii["light"] == "red":
        st.error(dfii_line)
    elif dfii["light"] == "yellow":
        st.warning(dfii_line)
    elif dfii["light"] == "unknown":
        st.warning(dfii_line + " 缺漏或過期，不是綠燈。")
    else:
        st.caption(dfii_line)
    if board["unknown_names"]:
        st.warning("資料不完整：" + "、".join(board["unknown_names"]) + "。這些不是綠燈，也沒有當成 0 分的未觸發。")
    st.table(table_records(board["rows"] + board["readings"]), hide_index=True)
    st.caption("30 年債、高收益與投資級利差沒有約定門檻，表上寫「無約定門檻」。")
    with st.expander("這批沒有接上的項目"):
        for item in board["not_done"]:
            st.markdown(f"**{item['name']}**")
            st.caption(item["why"])
