"""第五頁：試算表優先，自動指標只在這一頁抓。第六頁不在這裡打網路。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from macro_cycle import industry_quadrant_points, load_macro_cycle, quadrant_shift, regime_rows, regime_scale_text
from macro_cycle_live import fetch_auto_results, load_service_key, macro_view_for_orders, read_quant_macro_tabs, remember_live_view
from macro_cycle_sheet import (
    AUTO_CACHE_SECONDS,
    HEAVY_UNDERWEIGHT_PCT,
    SHEET_CACHE_SECONDS,
    compose_macro_view,
    exposure_by_industry,
    underweight_warnings,
)

RESEARCH_URL = "https://cursor.com/canvas/store-9fa877c7-1d47-4b9a-a8f8-eaeb67263eba/aaaf4e7c-fb03-4170-93ac-a7f93481fbb7"


def cached_macro_cycle_view() -> dict:
    """第六頁用。只拿 15 分鐘內已經算好的結果，否則讀倉庫 JSON。"""
    return macro_view_for_orders()


def render_macro_cycle_page(load_book=None):
    with st.spinner("正在讀試算表與自動指標…"):
        view = _page5_view()
    book = _cached_book(load_book)
    st.title("第五頁：總經週期與配置")
    st.markdown(f"[研究報告]({RESEARCH_URL})")
    _show_warnings(view)
    if not view.get("ok"):
        st.warning("總經資料沒有套用。下單輔助暫用中性：1.5 倍、現金 15%。")
        return
    _render_summary(view, book)
    _render_indicators(view["indicators"])
    _render_quadrant(view["industries"])
    _render_industries(view["industries"], book)
    _render_framework(view["industries"], book)
    _render_history(view)
    with st.expander("制度對照"):
        st.caption(regime_scale_text())
        _render_risk_table(view["regime"])


def _page5_view() -> dict:
    now = time.time()
    sheet = _cached_value("macro_sheet_cache", SHEET_CACHE_SECONDS, now)
    auto = _cached_value("macro_auto_cache", AUTO_CACHE_SECONDS, now)
    if sheet is None or auto is None:
        key = None
        if sheet is None:
            try:
                key = load_service_key()
            except Exception as exc:
                sheet = {
                    "ok": False,
                    "error": f"沒有試算表金鑰（{exc.__class__.__name__}）",
                    "scores": [],
                    "industries": [],
                    "score_error": "",
                    "industry_error": "",
                }
                st.session_state["macro_sheet_cache"] = {"t": now, "v": sheet}
        pool = ThreadPoolExecutor(max_workers=2)
        sheet_job = pool.submit(read_quant_macro_tabs, key) if sheet is None and key is not None else None
        auto_job = pool.submit(fetch_auto_results) if auto is None else None
        if sheet_job is not None:
            try:
                sheet = sheet_job.result(timeout=12)
            except Exception as exc:
                sheet = {"ok": False, "error": f"試算表讀不到（{exc.__class__.__name__}）", "scores": [], "industries": [], "score_error": "", "industry_error": ""}
            st.session_state["macro_sheet_cache"] = {"t": now, "v": sheet}
        if auto_job is not None:
            try:
                auto = auto_job.result(timeout=12)
            except Exception:
                auto = {}
            st.session_state["macro_auto_cache"] = {"t": now, "v": auto}
        pool.shutdown(wait=False, cancel_futures=True)
    view = compose_macro_view(load_macro_cycle(), sheet, auto)
    remember_live_view(view, now)
    return view


def _cached_value(key: str, ttl: int, now: float):
    bucket = st.session_state.get(key)
    if not bucket:
        return None
    if now - float(bucket.get("t") or 0) > ttl:
        return None
    return bucket.get("v")


def _show_warnings(view: dict) -> None:
    warnings = [str(item) for item in (view.get("warnings") or []) if item]
    if not warnings:
        return
    st.warning(warnings[0] if len(warnings) == 1 else f"有 {len(warnings)} 則資料改用備援或略過。")
    with st.expander("資料檢查"):
        for message in warnings:
            st.write(message)


def _cached_book(load_book):
    now = time.time()
    hit = _cached_value("macro_book_cache", 180, now)
    if hit is not None:
        return hit
    if load_book is None:
        book = {"ok": False}
    else:
        try:
            book = load_book()
        except Exception:
            book = {"ok": False}
    st.session_state["macro_book_cache"] = {"t": now, "v": book}
    return book


def _render_summary(view: dict, book: dict):
    regime = view["regime"]
    total = int(view["total"])
    risk = view["risk"]
    change = view.get("week_change")
    change_text = "尚無上週" if change is None else f"{change:+d}"
    st.header(regime)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總分", f"{total:+d}", change_text)
    c2.metric("槓桿上限", f"{risk['target_leverage']:.1f}x")
    c3.metric("現金下限", f"{risk['min_cash_pct']:.0f}%")
    if book.get("ok"):
        leverage = float(book["leverage"])
        cash = float(book["cash_pct"])
        leverage_ok = leverage <= float(risk["target_leverage"]) + 1e-9
        cash_ok = cash + 1e-9 >= float(risk["min_cash_pct"])
        c4.metric("目前槓桿 / 現金", f"{leverage:.2f}x", f"現金 {cash:.1f}%")
        if leverage_ok and cash_ok:
            st.success(f"綠燈。槓桿 {leverage:.2f}x 未過上限，現金 {cash:.1f}% 高於下限。")
        else:
            bits = []
            if not leverage_ok:
                bits.append(f"槓桿 {leverage:.2f}x 高於 {risk['target_leverage']:.1f}x")
            if not cash_ok:
                bits.append(f"現金 {cash:.1f}% 低於 {risk['min_cash_pct']:.0f}%")
            st.error("紅燈。" + "，".join(bits) + "。")
    else:
        c4.metric("目前槓桿 / 現金", "—")
        st.caption("持倉未讀到，只顯示制度上限。")


def _render_indicators(indicators: list[dict]):
    st.subheader("多空指標")
    frame = pd.DataFrame(
        [
            {
                "指標": item["name"],
                "讀數": item.get("value") or "—",
                "分數": int(item["score"]),
                "趨勢": item.get("trend") or "—",
                "來源": item.get("source_label") or ("自動" if item.get("source") == "auto" else "手動"),
                "更新": item.get("updated_at") or "",
            }
            for item in indicators
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)


def _render_industries(industries: list[dict], book: dict):
    st.subheader("二十個產業")
    positions = book.get("positions") or [] if book.get("ok") else []
    exposure, _grand = exposure_by_industry(positions, industries)
    choice = st.radio("立場", ["全部", "加碼", "中性", "減碼"], horizontal=True, key="macro_stance_filter")
    rows = []
    for item in industries:
        if choice != "全部" and item.get("stance") != choice:
            continue
        held = exposure.get(item["name"], {})
        shift = quadrant_shift(item)
        rows.append(
            {
                "產業": item["name"],
                "週期": item["cycle"],
                "方向": item["direction"],
                "立場": item["stance"],
                "一句話": item.get("rationale") or "—",
                "換象限": shift or "—",
                "你的持股": held.get("mv", 0.0),
                "佔比": held.get("pct", 0.0),
                "代表股": "、".join(item.get("tickers") or []),
            }
        )
    if not rows:
        st.caption("這個立場目前沒有產業。")
        return
    frame = pd.DataFrame(rows)
    styler = frame.style
    paint = _paint_stance
    if hasattr(styler, "map"):
        styler = styler.map(paint, subset=["立場"])
    else:
        styler = styler.applymap(paint, subset=["立場"])
    st.dataframe(
        styler.format({"你的持股": "NT$ {:,.0f}", "佔比": "{:.1f}%"}),
        width="stretch",
        hide_index=True,
    )


def _render_framework(industries: list[dict], book: dict):
    st.subheader("持倉與框架")
    if not book.get("ok"):
        st.caption("持倉未讀到。")
        return
    exposure, _grand = exposure_by_industry(book.get("positions") or [], industries)
    notes = underweight_warnings(industries, exposure)
    for note in notes:
        st.error(note)
    if not notes:
        st.success(f"減碼產業的持股都低於 {HEAVY_UNDERWEIGHT_PCT:.0f}%。")
    rows = []
    for item in industries:
        held = exposure.get(item["name"])
        if not held:
            continue
        rows.append({"產業": item["name"], "立場": item["stance"], "佔比": held["pct"], "市值": held["mv"]})
    if not rows:
        st.caption("代表股對不上目前持倉。")
        return
    frame = pd.DataFrame(rows).sort_values("佔比", ascending=False)
    st.dataframe(
        frame.style.format({"佔比": "{:.1f}%", "市值": "NT$ {:,.0f}"}),
        width="stretch",
        hide_index=True,
    )


def _render_history(view: dict):
    st.subheader("總分歷史")
    history = list(view.get("history") or [])
    if not history:
        st.caption("尚無週分數歷史。")
        return
    frame = pd.DataFrame(history).sort_values("week")
    fig = go.Figure(go.Scatter(x=frame["week"], y=frame["total"], mode="lines+markers", name="總分"))
    fig.update_layout(
        title="每週總分",
        template="plotly_dark",
        height=320,
        margin=dict(l=10, r=10, t=40, b=10),
        xaxis_title="週",
        yaxis_title="總分",
    )
    st.plotly_chart(fig, width="stretch")


def _render_quadrant(industries: list[dict]):
    st.subheader("四象限")
    st.caption("左上是復甦、右上是成長、右下是趨緩、左下是衰退。同一象限裡，上行往下一階段（順時針）靠，下行往回靠，持平留在中間。實心點的立場是目前配置，空心點整列是示例。")
    points = industry_quadrant_points(industries)
    fig = go.Figure()
    quads = [
        (-2.45, 0, 0, 2.45, "rgba(66, 165, 245, 0.14)"),
        (0, 2.45, 0, 2.45, "rgba(102, 187, 106, 0.14)"),
        (0, 2.45, -2.45, 0, "rgba(255, 167, 38, 0.14)"),
        (-2.45, 0, -2.45, 0, "rgba(239, 83, 80, 0.12)"),
    ]
    for x0, x1, y0, y1, color in quads:
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=color, line_width=0, layer="below")
    fig.add_hline(y=0, line_color="#90A4AE", line_width=1)
    fig.add_vline(x=0, line_color="#90A4AE", line_width=1)
    for x, y, label in ((-1.2, 2.2, "復甦"), (1.2, 2.2, "成長"), (1.2, -2.2, "趨緩"), (-1.2, -2.2, "衰退")):
        fig.add_annotation(x=x, y=y, text=label, showarrow=False, font=dict(size=16, color="#ECEFF1"))

    colors = {"加碼": "#66BB6A", "中性": "#FFD54F", "減碼": "#EF5350"}
    for stance in ("加碼", "中性", "減碼"):
        subset = [point for point in points if point["stance"] == stance]
        if not subset:
            continue
        color = colors[stance]
        fig.add_trace(
            go.Scatter(
                x=[point["x"] for point in subset],
                y=[point["y"] for point in subset],
                mode="markers+text",
                text=[point["name"] for point in subset],
                textposition="top center",
                textfont=dict(size=11, color="#ECEFF1"),
                name=stance,
                marker=dict(
                    size=16,
                    color=color,
                    symbol=["circle-open" if point["example"] else "circle" for point in subset],
                    line=dict(width=2, color=color),
                ),
                hovertext=[_hover(point) for point in subset],
                hoverinfo="text",
            )
        )
    fig.update_layout(
        title="產業四象限",
        template="plotly_dark",
        height=680,
        margin=dict(l=10, r=10, t=50, b=10),
        legend=dict(orientation="h", y=1.02),
        xaxis=dict(visible=False, range=[-2.55, 2.55]),
        yaxis=dict(visible=False, range=[-2.55, 2.55], scaleanchor="x", scaleratio=1),
    )
    st.plotly_chart(fig, width="stretch")


def _render_risk_table(current: str):
    st.subheader("對應下單輔助的預設")
    st.caption("只帶入目標槓桿上限與最低現金水位。第二階段槓桿維持第六頁原本的設定，不跟著總經改。")
    frame = pd.DataFrame(regime_rows())

    def paint(value):
        if value == current:
            return "background-color: #1565c0; color: white"
        return ""

    styler = frame.style
    if hasattr(styler, "map"):
        styler = styler.map(paint, subset=["狀態"])
    else:
        styler = styler.applymap(paint, subset=["狀態"])
    st.dataframe(styler, width="stretch", hide_index=True)


def _hover(point: dict) -> str:
    mark = "（示例）" if point["example"] else ""
    tickers = "、".join(point["tickers"])
    return f"{point['name']}{mark}<br>立場：{point['stance']}<br>週期：{point['cycle']}　方向：{point['direction']}<br>代表股：{tickers}"


def _score_color(score: int) -> str:
    if score > 0:
        return "#43A047"
    if score < 0:
        return "#E53935"
    return "#90A4AE"


def _paint_stance(value: str) -> str:
    text = str(value)
    if "減碼" in text:
        return "background-color: #b71c1c; color: white"
    if "加碼" in text:
        return "background-color: #1b5e20; color: white"
    if "中性" in text:
        return "background-color: #f9a825; color: #1a1a1a"
    return ""
