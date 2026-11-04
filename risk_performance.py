"""第四頁「風險與績效」卡片，以及第六頁現況健檢的一行摘要。

口徑（不改）：
- 槓桿＝名目曝險 ÷ 淨值
- 帳戶 Beta＝Σ(名目曝險 × Beta) ÷ 淨值
- 元大期貨權益比率＝權益數 ÷ 原始保證金
- 期貨列的市值是名目市值（報價 × 乘數 × 口數）

淨值歷史（最大回撤、回撤煞車燈）：Daily_NAV 分頁可能已移除。少於兩個歷史日點時
一律顯示「淨值歷史無法取得」，不硬算、不當成 0%。
"""

from __future__ import annotations

from dataclasses import fields

import pandas as pd

from nav_scenario_sim import Strategy
from portfolio_risk import equity_ratio_pct

LEVERAGE_WARN = 1.5
CASH_HEALTHY_PCT = 40.0


def _strategy_default(name: str) -> float:
    for item in fields(Strategy):
        if item.name == name:
            return float(item.default)
    raise KeyError(name)


# 與第六頁 NAV 情境模擬的煞車預設相同（停新倉 -12%、降到 1x -16%）。
BRAKE_PAUSE_PCT = _strategy_default("brake_pause") * 100.0
BRAKE_CUT_PCT = _strategy_default("brake_cut") * 100.0

HISTORY_UNAVAILABLE = "淨值歷史無法取得（Daily_NAV 分頁已移除或沒有歷史資料），最大回撤與回撤煞車燈暫不判斷。"


def neutrality_label(beta: float) -> str:
    if beta > 0.5:
        return "🟢 高度偏多"
    if beta > 0.2:
        return "🟢 溫和偏多"
    if beta < -0.2:
        return "🔴 偏空避險"
    return "🟡 市場中性"


def drawdown_stats(nav_series: pd.Series | None) -> dict:
    """由舊到新的淨值序列 → 目前距高點、最大回撤（百分比，負數）。少於 2 點視為沒有歷史。"""
    out = {"available": False, "points": 0, "start": None, "current_dd_pct": None, "max_dd_pct": None, "peak_nav": None}
    if nav_series is None:
        return out
    values = pd.to_numeric(pd.Series(nav_series), errors="coerce").dropna()
    values = values[values > 0]
    out["points"] = int(len(values))
    if len(values) < 2:
        return out
    running_peak = values.cummax()
    dd = values / running_peak - 1.0
    out.update(
        available=True,
        start=str(values.index[0]),
        current_dd_pct=float(dd.iloc[-1] * 100.0),
        max_dd_pct=float(dd.min() * 100.0),
        peak_nav=float(values.max()),
    )
    return out


def brake_light(current_dd_pct: float | None) -> tuple[str, str]:
    """回撤煞車燈：(燈號, 說明)。沒有歷史時為 ⚪。"""
    if current_dd_pct is None:
        return "⚪", "無淨值歷史"
    if current_dd_pct <= BRAKE_CUT_PCT + 1e-9:
        return "🔴", f"降到 1x（回撤 ≤ {BRAKE_CUT_PCT:.0f}%）"
    if current_dd_pct <= BRAKE_PAUSE_PCT + 1e-9:
        return "🟡", f"停新倉（回撤 ≤ {BRAKE_PAUSE_PCT:.0f}%）"
    return "🟢", f"正常（停新倉線 {BRAKE_PAUSE_PCT:.0f}%）"


def risk_performance_summary(metrics: dict, nav_series: pd.Series | None = None) -> dict:
    m = metrics or {}
    nav = float(m.get("nav", 0.0) or 0.0)
    leverage = float(m.get("total_leverage", m.get("leverage", 0.0)) or 0.0)
    beta = float(m.get("account_beta", m.get("beta", 0.0)) or 0.0)
    cash_pct = float(m.get("cash_pct", 0.0) or 0.0) if nav > 0 else 0.0
    dd = drawdown_stats(nav_series)
    light, light_text = brake_light(dd["current_dd_pct"])
    return {
        "nav": nav,
        "leverage": leverage,
        "account_beta": beta,
        "cash_pct": cash_pct,
        "debt": float(m.get("debt", 0.0) or 0.0),
        "leverage_ok": leverage <= LEVERAGE_WARN + 1e-9,
        "cash_ok": cash_pct + 1e-9 >= CASH_HEALTHY_PCT,
        "neutrality": neutrality_label(beta),
        "equity_ratio_pct": equity_ratio_pct(m.get("futures_equity"), m.get("futures_initial")),
        "drawdown": dd,
        "brake_light": light,
        "brake_text": light_text,
    }


def page6_health_line(metrics: dict, target_leverage: float | None = None) -> str:
    """第六頁現況健檢只留一行摘要，完整數字看第四頁。"""
    s = risk_performance_summary(metrics)
    lev = f"槓桿 {s['leverage']:.2f}x"
    if target_leverage is not None:
        lev += f"（上限 {float(target_leverage):.2f}x）"
    parts = [f"淨值 NT$ {s['nav']:,.0f}", lev, f"帳戶 Beta {s['account_beta']:.2f}", f"現金 {s['cash_pct']:.1f}%"]
    if s["equity_ratio_pct"] is not None:
        parts.append(f"元大權益比率 {s['equity_ratio_pct']:.2f}%")
    return "｜".join(parts) + "　→ 詳見第四頁「風險與績效」"


def render_risk_performance_card(st, metrics: dict, nav_series: pd.Series | None = None) -> dict:
    """一張卡：Beta／槓桿三列表＋現金、借款、最大回撤、回撤煞車燈。每個數字只出現一次。"""
    from beta_leverage_ui import render_beta_leverage_table

    s = risk_performance_summary(metrics, nav_series)
    dd = s["drawdown"]
    with st.container(border=True):
        st.subheader("📊 風險與績效")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "現金水位",
            f"{s['cash_pct']:.1f}%",
            f"{'🟢' if s['cash_ok'] else '🔴'} 健康 >{CASH_HEALTHY_PCT:.0f}%",
            delta_color="off",
        )
        k2.metric("融資／質押借款", f"NT$ {s['debt']:,.0f}")
        if dd["available"]:
            k3.metric("最大回撤", f"{dd['max_dd_pct']:.2f}%", f"距高點 {dd['current_dd_pct']:.2f}%", delta_color="off")
        else:
            k3.metric("最大回撤", "—", "無淨值歷史", delta_color="off")
        k4.metric("回撤煞車", s["brake_light"], s["brake_text"], delta_color="off")

        render_beta_leverage_table(st, metrics)
        st.caption(
            f"合計槓桿 {'🟢 未超過' if s['leverage_ok'] else '🔴 超過'}警戒線 {LEVERAGE_WARN:.1f}x；"
            f"帳戶 Beta 方向：{s['neutrality']}。"
        )
        if dd["available"]:
            st.line_chart(nav_series, height=150)
            st.caption(f"淨值歷史自 {dd['start']} 起，共 {dd['points']} 點；今天的點用即時淨值。")
        else:
            st.caption(HISTORY_UNAVAILABLE)
    return s
