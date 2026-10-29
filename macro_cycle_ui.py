"""總經週期頁。只讀本機 JSON，不打行情、不改第四頁公式。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from macro_cycle import (
    DATA_PATH,
    industry_quadrant_points,
    load_macro_cycle,
    regime_rows,
    regime_scale_text,
)


@st.cache_data(ttl=300, show_spinner=False)
def _load_cached(path: str, mtime_ns: int) -> dict:
    return load_macro_cycle(path)


def cached_macro_cycle_view() -> dict:
    path = Path(DATA_PATH)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _load_cached(str(path), mtime_ns)


def render_macro_cycle_page():
    view = cached_macro_cycle_view()
    st.title("總經週期與配置")
    st.caption("多空分數每週更新，二十個產業的週期位置每月 12 日更新。數字來自 data/macro_cycle.json，這一頁不抓行情。")

    if not view.get("ok"):
        st.warning("總經檔讀不到或格式不符，這一頁先不套用分數與產業。下單輔助的目標槓桿上限與最低現金會暫用中性：1.5 倍、現金 15%。")
        with st.expander("為什麼沒有套用"):
            for message in view.get("errors") or ["沒有進一步的訊息。"]:
                st.write(message)
        return

    commentary = view.get("commentary") or {}
    if commentary.get("score") or commentary.get("industries"):
        lines = [text for text in (commentary.get("score"), commentary.get("industries")) if text]
        st.info("\n\n".join(lines))

    total = int(view["total"])
    regime = view["regime"]
    risk = view["risk"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("多空分數", f"{total:+d}", "區間 -16～+16")
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


def _render_indicators(indicators: list[dict]):
    st.subheader("八項多空指標")
    if indicators and all(item.get("example") for item in indicators):
        st.caption("這八項分數目前都標成示例，加總只為了把週期跑起來，還不是正式週報。每項 -2 到 +2。")
    elif any(item.get("example") for item in indicators):
        st.caption("標成示例的項目還不是正式週報。每項 -2 到 +2。")
    else:
        st.caption("每項 -2 到 +2，八項加總就是上面的多空分數。")

    frame = pd.DataFrame(
        [
            {
                "指標": item["name"],
                "分數": int(item["score"]),
                "性質": "示例" if item.get("example") else "正式",
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
        title="八項分數",
        template="plotly_dark",
        height=360,
        margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(title="分數", range=[-2.5, 2.5], dtick=1),
        yaxis=dict(title=""),
    )
    st.plotly_chart(fig, width="stretch")


def _render_industries(industries: list[dict]):
    st.subheader("二十個產業")
    st.caption("「立場已定」只代表加碼、中性或減碼是目前配置，這幾列的週期位置、方向與代表股仍是示例。標成「示例」的產業整列都是占位。")
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
