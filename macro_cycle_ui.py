"""第五頁：試算表優先，自動指標只在這一頁抓。第六頁不在這裡打網路。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from macro_cycle import industry_quadrant_points, load_macro_cycle, regime_rows, regime_scale_text
from macro_cycle_live import fetch_auto_results, load_service_key, macro_view_for_orders, read_quant_macro_tabs, remember_live_view
from macro_cycle_sheet import AUTO_CACHE_SECONDS, SHEET_CACHE_SECONDS, compose_macro_view


def cached_macro_cycle_view() -> dict:
    """第六頁用。只拿 15 分鐘內已經算好的結果，否則讀倉庫 JSON。"""
    return macro_view_for_orders()


def render_macro_cycle_page():
    with st.spinner("正在讀試算表與自動指標…"):
        view = _page5_view()
    st.title("第五頁：總經週期與配置")
    origin = view.get("data_origin")
    if origin == "sheet":
        st.caption("平時改 Quant_Database 的 Macro_Score、Industry_Cycle 就好，不必為了每週數字重新部署。試算表快取 15 分鐘，自動指標快取 1 小時。這一頁才會抓資料。")
    elif origin == "json":
        st.caption("試算表這次沒可用，先用倉庫裡的 data/macro_cycle.json。第六頁不會為了這個再去抓。")
    else:
        st.caption("試算表和倉庫備援都沒有可用分數。")

    _show_warnings(view)
    if not view.get("ok"):
        st.warning("總經資料沒有套用。下單輔助的目標槓桿上限與最低現金會暫用中性：1.5 倍、現金 15%。")
        return

    commentary = view.get("commentary") or {} if view.get("data_origin") == "json" else {}
    if commentary.get("score") or commentary.get("industries"):
        lines = [text for text in (commentary.get("score"), commentary.get("industries")) if text]
        st.info("\n\n".join(lines))

    total = int(view["total"])
    regime = view["regime"]
    risk = view["risk"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("多空分數", f"{total:+d}", "區間 -18～+18")
    c2.metric("多空狀態", regime)
    c3.metric("分數更新", view["score_updated_at"], "每週")
    c4.metric("產業圖更新", view["industry_updated_at"], "每月 12 日")
    st.caption(
        f"分數對照：{regime_scale_text()}。現在 {total:+d} 是{regime}。"
        f"第六頁會以此為預設，目標槓桿上限 {risk['target_leverage']:.1f} 倍、最低現金水位 {risk['min_cash_pct']:.0f}%。進第六頁之後仍可手動改。"
    )
    st.caption(f"資料格式 {view['schema_version']}。")

    _render_indicators(view["indicators"])
    _render_industries(view["industries"])
    _render_quadrant(view["industries"])
    _render_risk_table(regime)


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


def _render_indicators(indicators: list[dict]):
    st.subheader("多空指標")
    st.caption("每項 -2 到 +2。來源「自動」是這一頁算出來的，「手動」來自試算表或倉庫備援。壞掉的列會略過，並寫在上面的檢查裡。")

    frame = pd.DataFrame(
        [
            {
                "指標": item["name"],
                "分數": int(item["score"]),
                "來源": item.get("source_label") or ("自動" if item.get("source") == "auto" else "手動"),
                "更新": item.get("updated_at") or "",
                "觀測值": item.get("value") or "",
                "備註": item.get("note") or "",
            }
            for item in indicators
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)

    names = [item["name"] for item in indicators][::-1]
    scores = [int(item["score"]) for item in indicators][::-1]
    colors = [_score_color(score) for score in scores]
    fig = go.Figure(go.Bar(x=scores, y=names, orientation="h", marker_color=colors, hovertemplate="%{y}：%{x:+d}<extra></extra>"))
    fig.update_layout(
        title="九項分數",
        template="plotly_dark",
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(title="分數", range=[-2.5, 2.5], dtick=1),
        yaxis=dict(title=""),
    )
    st.plotly_chart(fig, width="stretch")


def _render_industries(industries: list[dict]):
    st.subheader("二十個產業")
    st.caption("試算表有的產業用試算表。沒有的列才沿用倉庫，那些列的備註會標示例。")
    frame = pd.DataFrame(
        [
            {
                "產業": item["name"],
                "標記": "示例" if item.get("example") else "立場已定",
                "週期位置": item["cycle"],
                "方向": item["direction"],
                "立場": item["stance"],
                "代表股": "、".join(item["tickers"]),
                "備註": item.get("note") or "",
            }
            for item in industries
        ]
    )
    styler = frame.style
    paint = _paint_stance
    if hasattr(styler, "map"):
        styler = styler.map(paint, subset=["立場"])
    else:
        styler = styler.applymap(paint, subset=["立場"])
    st.dataframe(styler, width="stretch", hide_index=True)


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
