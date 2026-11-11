"""第六頁把避險試算和淨值劇本放在同一個選擇器裡，結果共用一張表。"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from sim_panel import beta_status, shared_result_rows


def render_combined_sim_panel(snapshot, macro_view, default_target: float) -> None:
    st.subheader("避險與劇本")
    st.caption("用上面的選擇器切換。台股避險只用台指期、小台、TXO。美股只用 SPX、XSP、SPY，仍在「選擇權避險試算」那一頁，不拿來避台股。")
    _show_beta(snapshot, macro_view)
    mode = st.selectbox("情境", ["台股避險試算", "淨值劇本"], key="p6_sim_mode")
    if mode == "台股避險試算":
        from hedge_sim_ui import render_hedge_strategy_sim

        metrics = getattr(snapshot, "metrics", {}) or {}
        status = beta_status(metrics, (macro_view or {}).get("regime"), "TW")
        render_hedge_strategy_sim(snapshot, macro_view, actual_beta=status["beta"])
    else:
        from nav_scenario_sim_ui import render_nav_scenario_simulator

        render_nav_scenario_simulator(snapshot, float(default_target))
    st.markdown("**結果對照**")
    hedge = (st.session_state.get("hss_payload") or {}).get("result") or {}
    scenario = st.session_state.get("nss_payload") or {}
    rows = shared_result_rows(hedge.get("table"), scenario.get("frame"))
    if not rows:
        st.caption("還沒有結果。缺的數字留空，不當 0。")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("這張表同時留台股避險回測和淨值劇本。沒跑過的那一邊不會補 0。")


def _show_beta(snapshot, macro_view) -> None:
    metrics = getattr(snapshot, "metrics", {}) or {}
    regime = (macro_view or {}).get("regime")
    tw = beta_status(metrics, regime, "TW")
    us = beta_status(metrics, regime, "US")
    st.caption(_beta_text("台股", tw) + "。" + _beta_text("美股", us) + "。目標區間是程式預設，不是已確認的策略參數。")


def _beta_text(label: str, status: dict) -> str:
    beta = "不完整" if status["beta"] is None else f"{status['beta']:.2f}"
    if status["band"] is None:
        band = "不完整"
    else:
        low, high = status["band"]
        band = f"{low:.2f}–{high:.2f}"
    return f"{label}實際 Beta {beta}，目標 {band}"
