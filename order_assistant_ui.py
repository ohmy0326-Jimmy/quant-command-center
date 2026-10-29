"""第六頁：下單輔助。只做試算與規劃，不送委託。"""

from __future__ import annotations

import copy
import os
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from order_assistant import (
    Rules,
    TXO_MULTIPLIER,
    apply_order,
    beta_equivalent,
    build_checklist_text,
    build_payoff_curve,
    build_sample_snapshot,
    economic_leverage,
    format_qty_text,
    health_flags,
    hedge_profit_plan,
    hedge_stress_table,
    loss_breaches,
    parse_tickers,
    positions_as_dicts,
    propose_rebalance,
    order_rule_warnings,
    run_stress,
    standard_stress,
    strategy_summary,
    suggest_addback_text,
    taiex_dollar_delta,
)
from portfolio_risk import attach_quotes, fetch_twii, fetch_usdtwd, is_taiex_future, normalize_positions, populate_market_fields


def _init_rule_state():
    defaults = {
        "oa_target_leverage": 1.8,
        "oa_stage2_leverage": 1.5,
        "oa_min_cash_pct": 15.0,
        "oa_max_loss_pct": 2.0,
        "oa_hedge_budget_pct": 1.5,
        "oa_beta_low": 1.5,
        "oa_beta_high": 2.0,
        "oa_max_dd": -20.0,
        "oa_realize_mult": 3.0,
        "oa_realize_frac": 50.0,
        "oa_sox_shock": -30.0,
        "oa_fx_shock": 5.0,
        "oa_core_names": "2330, 0050",
        "oa_rotate_pct": 30.0,
        "oa_strict_sector": False,
        "oa_ticker": "2330",
        "oa_side": "買進",
        "oa_qty": 0.0,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if "oa_addback_text" not in st.session_state:
        st.session_state["oa_addback_text"] = suggest_addback_text(Rules(), st.session_state["oa_core_names"])


def _rules_from_state() -> Rules:
    return Rules(
        target_leverage=float(st.session_state["oa_target_leverage"]),
        stage2_leverage=float(st.session_state["oa_stage2_leverage"]),
        min_cash_pct=float(st.session_state["oa_min_cash_pct"]),
        max_loss_pct_nav=float(st.session_state["oa_max_loss_pct"]),
        hedge_budget_pct_nav=float(st.session_state["oa_hedge_budget_pct"]),
        beta_low=float(st.session_state["oa_beta_low"]),
        beta_high=float(st.session_state["oa_beta_high"]),
        max_drawdown_pct=float(st.session_state["oa_max_dd"]),
        hedge_realize_multiple=float(st.session_state["oa_realize_mult"]),
        hedge_realize_fraction=float(st.session_state["oa_realize_frac"]) / 100.0,
        sox_shock=float(st.session_state["oa_sox_shock"]) / 100.0,
        fx_shock=float(st.session_state["oa_fx_shock"]) / 100.0,
    )


def _render_rule_editor():
    with st.expander("風控規則設定（存在這次工作階段，重新整理仍會留著）", expanded=False):
        st.caption("改完之後，下面的健檢、調倉與試算會用同一組數字。不會寫進試算表，也不會下單。")
        c1, c2, c3 = st.columns(3)
        c1.number_input("目標槓桿上限（倍）", min_value=0.2, max_value=10.0, step=0.1, key="oa_target_leverage")
        c2.number_input("第二階段槓桿上限（倍）", min_value=0.2, max_value=10.0, step=0.1, key="oa_stage2_leverage")
        c3.number_input("最低現金水位（%）", min_value=0.0, max_value=100.0, step=1.0, key="oa_min_cash_pct")
        c4, c5, c6 = st.columns(3)
        c4.number_input("單一部位虧損上限（佔淨值 %）", min_value=0.1, max_value=20.0, step=0.1, key="oa_max_loss_pct")
        c5.number_input("年度避險權利金預算（佔淨值 %）", min_value=0.0, max_value=20.0, step=0.1, key="oa_hedge_budget_pct")
        c6.number_input("壓力回撤容忍（%）", min_value=-80.0, max_value=0.0, step=1.0, key="oa_max_dd")
        c7, c8 = st.columns(2)
        c7.number_input("Beta 目標下緣", min_value=0.0, max_value=5.0, step=0.1, key="oa_beta_low")
        c8.number_input("Beta 目標上緣", min_value=0.0, max_value=5.0, step=0.1, key="oa_beta_high")
        st.markdown("**壓力情境幅度**")
        s1, s2, s3, s4 = st.columns(4)
        s1.number_input("避險獲利倍數", min_value=1.0, max_value=10.0, step=0.5, key="oa_realize_mult")
        s2.number_input("達標後實現比例（%）", min_value=0.0, max_value=100.0, step=5.0, key="oa_realize_frac")
        s3.number_input("費半情境（%）", min_value=-80.0, max_value=0.0, step=1.0, key="oa_sox_shock")
        s4.number_input("新台幣貶值（%）", min_value=0.0, max_value=30.0, step=0.5, key="oa_fx_shock")
        if st.button("依目前規則重寫回補計畫"):
            st.session_state["oa_addback_text"] = suggest_addback_text(_rules_from_state(), st.session_state.get("oa_core_names", "2330, 0050"))
        st.text_area("條件式回補（可改寫，按上面按鈕會覆蓋）", height=180, key="oa_addback_text")


def _public_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(token in lowered for token in ("private_key", "begin private", "api_key", "secret")):
        return "線上持倉讀取失敗（已隱藏憑證內容）。下面改用示範持倉，方便先看流程。"
    return f"線上持倉讀取失敗，下面改用示範持倉。原因：{text[:300]}"


def _load_live(load_sheets, beta_fn):
    accounts, _nav_hist, quant = load_sheets()
    usdtwd = fetch_usdtwd()
    raw = normalize_positions(quant)
    twii = fetch_twii()
    if not raw.empty:
        prices, betas, sectors = populate_market_fields(raw, beta_fn, twii)
        raw = attach_quotes(raw, prices, betas, sectors)
    return build_snapshot_from_frames(raw, accounts, usdtwd, twii)


def build_snapshot_from_frames(raw, accounts, usdtwd, twii):
    from order_assistant import Snapshot

    if raw is None or raw.empty:
        raw = pd.DataFrame(columns=["帳戶", "種類", "標的", "名稱", "持有數量", "平均成本", "投入總金額", "最大虧損比例", "標的槓桿", "即時報價", "Beta值", "產業別"])
    return Snapshot(raw, accounts, usdtwd, twii)


def _find(snapshot, ticker: str):
    token = str(ticker).strip().upper()
    for pos in positions_as_dicts(snapshot.valued):
        if pos["ticker"] == token:
            return pos
    return None


def _top_sector(weights: dict) -> tuple[str, float]:
    if not weights:
        return "—", 0.0
    name = max(weights, key=weights.get)
    return str(name), float(weights[name])


def _flag_table(flags: list[dict]):
    frame = pd.DataFrame(flags)[["name", "status", "detail"]].rename(columns={"name": "項目", "status": "狀態", "detail": "說明"})

    def paint(value):
        if value == "符合":
            return "background-color: #1b5e20; color: white"
        if value == "超出":
            return "background-color: #b71c1c; color: white"
        return ""

    styler = frame.style
    if hasattr(styler, "map"):
        styler = styler.map(paint, subset=["狀態"])
    else:
        styler = styler.applymap(paint, subset=["狀態"])
    st.dataframe(styler, width="stretch", hide_index=True)


def _actions_frame(actions: list[dict]) -> pd.DataFrame:
    rows = []
    for action in actions:
        rows.append(
            {
                "標的": action["ticker"],
                "名稱": action.get("name", ""),
                "目前市值": action.get("current_value", 0.0),
                "動作": action["action"],
                "數量": format_qty_text(action["ticker"], action.get("kind", ""), action["qty"]) if action["qty"] else "—",
                "交易金額": action.get("value", 0.0),
                "理由": action.get("reason", ""),
                "動作後槓桿": action.get("leverage_after", 0.0),
                "動作後現金%": action.get("cash_pct_after", 0.0),
                "動作後 Beta": action.get("beta_after", 0.0),
            }
        )
    return pd.DataFrame(rows)


def _show_action_table(actions: list[dict]):
    if not actions:
        st.info("目前槓桿與現金已在目標內，沒有必做的減碼。弱勢股若要換股，把換股比例調高即可。")
        return
    frame = _actions_frame(actions)
    st.dataframe(
        frame.style.format(
            {
                "目前市值": "NT$ {:,.0f}",
                "交易金額": "NT$ {:,.0f}",
                "動作後槓桿": "{:.2f}x",
                "動作後現金%": "{:.1f}%",
                "動作後 Beta": "{:.2f}",
            }
        ),
        width="stretch",
        hide_index=True,
    )


def _metric_row(before: dict, after: dict):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("合併槓桿", f"{after['leverage']:.2f}x", f"{after['leverage'] - before['leverage']:+.2f}x", delta_color="inverse")
    c2.metric("現金水位", f"{after['cash_pct']:.1f}%", f"{after['cash_pct'] - before['cash_pct']:+.1f}%")
    c3.metric("Beta", f"{after['beta']:.2f}", f"{after['beta'] - before['beta']:+.2f}", delta_color="off")
    if after.get("margin_ratio") is not None and before.get("margin_ratio") is not None:
        c4.metric("保證金比率", f"{after['margin_ratio']:.1f}%", f"{after['margin_ratio'] - before['margin_ratio']:+.1f}%")
    elif after.get("margin_ratio") is not None:
        c4.metric("保證金比率", f"{after['margin_ratio']:.1f}%")
    else:
        c4.metric("保證金比率", "無期貨")


def _order_kind(snapshot, ticker: str, held):
    if held:
        return held["kind"]
    if is_taiex_future(ticker):
        return "期貨"
    choice = st.selectbox("市場（新標的）", ["台股", "美股"], key=f"oa_kind_{ticker}")
    return "美股" if choice == "美股" else "台股"


def _render_order(snapshot, rules: Rules):
    st.caption("輸入後立即重算整組淨值、槓桿、Beta、現金、保證金與產業。數量 0 代表還沒有要試的單。")
    if st.button("帶入範例：買進 2330 共 1000 股"):
        st.session_state["oa_ticker"] = "2330"
        st.session_state["oa_side"] = "買進"
        st.session_state["oa_qty"] = 1000.0
    c1, c2, c3, c4 = st.columns(4)
    ticker = c1.text_input("代號", key="oa_ticker").strip().upper()
    c2.selectbox("方向", ["買進", "賣出"], key="oa_side")
    c3.number_input("數量（股或口）", min_value=0.0, step=1.0, key="oa_qty")
    held = _find(snapshot, ticker) if ticker else None
    market_px = held["price"] if held else 0.0
    price_key = f"oa_px_{ticker or 'NA'}"
    if price_key not in st.session_state:
        st.session_state[price_key] = float(market_px)
    price = c4.number_input("成交價", min_value=0.0, step=0.5, key=price_key)
    kind = _order_kind(snapshot, ticker, held) if ticker else "台股"
    beta = held["beta"] if held else 1.0
    lev = held["lev"] if held else (3.0 if ticker in {"SOXL", "TQQQ"} else 1.0)
    sector = held["sector"] if held else "其他"
    name = held["name"] if held else ticker
    account = held["account"] if held else None
    with st.expander("新標的才需要改：Beta、槓桿、產業"):
        if not held:
            beta = st.number_input("Beta", value=1.0, step=0.1, key=f"oa_beta_{ticker}")
            lev = st.number_input("標的槓桿", min_value=0.1, value=float(lev), step=0.5, key=f"oa_lev_{ticker}")
            sector = st.text_input("產業", value=sector, key=f"oa_sector_{ticker}")
        else:
            st.caption(f"沿用持倉：Beta {held['beta']:.2f}、槓桿 {held['lev']:.1f}x、{held['sector']}、{held['kind']}")
    qty = float(st.session_state["oa_qty"])
    side = st.session_state["oa_side"]
    if qty <= 0 or not ticker:
        st.info("填了數量才會試算。這頁不會把單子送到券商。")
        return None
    if price <= 0:
        st.warning("請填成交價。")
        return None
    after, warnings = apply_order(
        snapshot,
        {
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "price": price,
            "kind": kind,
            "beta": beta,
            "leverage": lev,
            "sector": sector,
            "name": name,
            "account": account,
        },
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**下單前**")
        _side_metrics(snapshot)
    with right:
        st.markdown("**下單後**")
        _side_metrics(after)
    _metric_row(snapshot.metrics, after.metrics)
    st.caption(
        f"經濟槓桿（期貨改以契約名目計）：{economic_leverage(snapshot):.2f}x → {economic_leverage(after):.2f}x。"
        "上方的合併槓桿與第四頁相同（實質曝險 ÷ 淨值）。"
    )
    b_name, b_w = _top_sector(snapshot.metrics["sector_weights"])
    a_name, a_w = _top_sector(after.metrics["sector_weights"])
    st.write(f"最大產業：{b_name} {b_w:.1f}% → {a_name} {a_w:.1f}%")

    stress_view = []
    for move, label in ((-0.10, "台指 -10%"), (-0.20, "台指 -20%")):
        b = run_stress(snapshot, label, taiex=move)
        a = run_stress(after, label, taiex=move)
        stress_view.append(
            {
                "情境": label,
                "下單前淨值": b["nav"],
                "下單後淨值": a["nav"],
                "下單後回撤": a["drawdown_pct"],
                "下單後保證金%": a["margin_ratio"] if a["margin_ratio"] is not None else float("nan"),
                "下單後追繳": "是" if a["margin_call"] else "否",
            }
        )
    st.dataframe(
        pd.DataFrame(stress_view).style.format(
            {"下單前淨值": "NT$ {:,.0f}", "下單後淨值": "NT$ {:,.0f}", "下單後回撤": "{:.1f}%", "下單後保證金%": "{:.1f}%"},
            na_rep="—",
        ),
        width="stretch",
        hide_index=True,
    )
    rebuilt = []
    for row in stress_view:
        rebuilt.append(
            {
                "name": row["情境"],
                "drawdown_pct": row["下單後回撤"],
                "margin_call": row["下單後追繳"] == "是",
            }
        )
    rule_warnings = order_rule_warnings(snapshot, after, rules, rebuilt)
    notes = [item for item in warnings if item]
    if notes:
        for note in notes:
            st.warning(note)
    if rule_warnings == ["這筆試算沒有踩到目前的風控規則"]:
        st.success(rule_warnings[0])
    else:
        for note in rule_warnings:
            st.error(note)
    return f"{side} {ticker} {format_qty_text(ticker, kind, qty)} @ {price:g}。槓桿 {snapshot.metrics['leverage']:.2f}x → {after.metrics['leverage']:.2f}x，現金 {snapshot.metrics['cash_pct']:.1f}% → {after.metrics['cash_pct']:.1f}%。" + (
        " " + "；".join(rule_warnings)
    )


def _side_metrics(snapshot):
    metrics = snapshot.metrics
    st.write(f"淨值 NT$ {metrics['nav']:,.0f}")
    st.write(f"合併槓桿 {metrics['leverage']:.2f}x")
    st.write(f"現金 {metrics['cash_pct']:.1f}%")
    st.write(f"Beta {metrics['beta']:.2f}")
    if metrics["margin_ratio"] is None:
        st.write("保證金 —")
    else:
        st.write(f"保證金 {metrics['margin_ratio']:.1f}%")
    name, weight = _top_sector(metrics["sector_weights"])
    st.write(f"最大產業 {name} {weight:.1f}%")


def _leg(kind: str, strike: float, lots: float, premium: float, iv: float) -> dict:
    return {"kind": kind, "strike": float(strike), "signed_lots": float(lots), "premium": float(premium), "iv": float(iv)}


def _render_hedge(snapshot, rules: Rules):
    spot = float(snapshot.twii) if snapshot.twii else 23000.0
    st.caption("台指選擇權（TXO）乘數 NT$50／點。三個策略並排，是替代方案，權利金不要加總。預設用 Black-Scholes，權利金填 0 代表用理論價。")
    c1, c2, c3, c4 = st.columns(4)
    spot = c1.number_input("台指現貨", min_value=1000.0, value=float(spot), step=50.0, key="oa_txo_spot")
    iv = c2.number_input("隱含波動率（%）", min_value=1.0, max_value=150.0, value=20.0, step=1.0, key="oa_iv") / 100.0
    rate = c3.number_input("利率（%）", min_value=0.0, max_value=15.0, value=1.5, step=0.1, key="oa_rate") / 100.0
    expiry = c4.date_input("到期日", value=date.today() + timedelta(days=30), key="oa_expiry")
    days = max((expiry - date.today()).days, 0)
    t_years = days / 365.0
    d1, d2, d3, d4 = st.columns(4)
    lots = d1.number_input("口數", min_value=0.0, value=2.0, step=1.0, key="oa_lots")
    k_put = d2.number_input("買進賣權履約價", min_value=0.0, value=float(int(round(spot * 0.95 / 50.0) * 50)), step=50.0, key="oa_k_put")
    k_short = d3.number_input("價差空頭履約價", min_value=0.0, value=float(int(round(spot * 0.90 / 50.0) * 50)), step=50.0, key="oa_k_short")
    k_call = d4.number_input("領口賣出買權履約價", min_value=0.0, value=float(int(round(spot * 1.05 / 50.0) * 50)), step=50.0, key="oa_k_call")
    p1, p2, p3 = st.columns(3)
    prem_put = p1.number_input("賣權權利金（點，0=理論價）", min_value=0.0, value=0.0, step=1.0, key="oa_prem_put")
    prem_short = p2.number_input("空頭賣權權利金（點，0=理論價）", min_value=0.0, value=0.0, step=1.0, key="oa_prem_short")
    prem_call = p3.number_input("買權權利金（點，0=理論價）", min_value=0.0, value=0.0, step=1.0, key="oa_prem_call")
    st.caption(f"距到期 {days} 天。Greeks 用你填的波動率；若有填權利金，成本用市價、Delta 仍用波動率。")

    # 圖與 Greeks 用畫面上的現貨，不改動持倉裡的期貨價格。
    book = copy.copy(snapshot)
    book.twii = float(spot)
    strategies = {
        "保護性賣權": [_leg("put", k_put, lots, prem_put, iv)],
        "賣權價差": [_leg("put", k_put, lots, prem_put, iv), _leg("put", k_short, -lots, prem_short, iv)],
        "領口": [_leg("put", k_put, lots, prem_put, iv), _leg("call", k_call, -lots, prem_call, iv)],
    }
    if k_short >= k_put:
        st.warning("賣權價差的空頭履約價需要低於買進履約價，這欄先不列入比較。")
        strategies.pop("賣權價差")
    summaries = []
    cols = st.columns(len(strategies))
    budget = book.metrics["nav"] * rules.hedge_budget_pct_nav / 100.0
    for col, (name, legs) in zip(cols, strategies.items()):
        summary = strategy_summary(book, name, legs, t_years, rate, TXO_MULTIPLIER)
        summaries.append(summary)
        with col:
            st.markdown(f"**{name}**")
            debit = summary["net_debit"]
            if debit >= 0:
                st.metric("權利金支出", f"NT$ {debit:,.0f}", f"佔淨值 {summary['premium_pct_nav']:.2f}%")
            else:
                st.metric("權利金淨收入", f"NT$ {-debit:,.0f}")
            used = (max(debit, 0.0) / budget * 100.0) if budget > 0 else 0.0
            if max(debit, 0.0) > budget > 0:
                st.error(f"超過年度預算 {used:.0f}%（預算 NT$ {budget:,.0f}）")
            else:
                st.success(f"佔年度預算 {used:.0f}%（預算 NT$ {budget:,.0f}）")
            st.write(f"避險後台指等值 Beta {summary['beta_before']:.2f} → {summary['beta_after']:.2f}")
            st.write(f"每月時間價值 NT$ {summary['theta_month']:,.0f}（負值是耗損）")
            st.caption(f"選擇權 Delta {summary['delta_per_point']:,.0f} 元／點")

    curve = build_payoff_curve(book, {item["name"]: item["legs"] for item in summaries}, TXO_MULTIPLIER)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=curve["漲跌幅%"], y=curve["投組"], name="投組（無避險）", line=dict(color="#B0BEC5", width=2)))
    colors = {"保護性賣權": "#42A5F5", "賣權價差": "#FFA726", "領口": "#66BB6A"}
    for name in strategies:
        if name in curve.columns:
            fig.add_trace(go.Scatter(x=curve["漲跌幅%"], y=curve[name], name=name, line=dict(color=colors.get(name, "#EF5350"), width=2)))
    fig.add_hline(y=0, line_color="#546E7A", line_width=1)
    fig.add_vline(x=0, line_color="#546E7A", line_width=1)
    fig.update_layout(
        title="到期損益：投組 + 避險（台指 -30%～+15%）",
        xaxis_title="台指漲跌幅 %",
        yaxis_title="損益（TWD）",
        template="plotly_dark",
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=1.12),
    )
    st.plotly_chart(fig, width="stretch")
    st.caption("這張圖是到期內含價值，美股部位不隨台指連動。時間價值耗損看上面的每月數字，不畫進曲線。")

    st.markdown("**壓力表（-10%／-20%／-30%，含權利金）**")
    stress_rows = []
    for summary in summaries:
        for row in hedge_stress_table(book, summary):
            plan = row["profit_plan"]
            stress_rows.append(
                {
                    "策略": summary["name"],
                    "情境": row["name"],
                    "避險後淨值": row["nav"],
                    "回撤": row["drawdown_pct"],
                    "避險損益": row["hedge_pnl"],
                    "避險市值": row["hedge_value"],
                    "保證金%": row["margin_ratio"] if row["margin_ratio"] is not None else float("nan"),
                    "追繳": "是" if row["margin_call"] else "否",
                    "獲利規則": "實現" if plan["triggered"] else "未達",
                }
            )
    if stress_rows:
        st.dataframe(
            pd.DataFrame(stress_rows).style.format(
                {"避險後淨值": "NT$ {:,.0f}", "回撤": "{:.1f}%", "避險損益": "NT$ {:,.0f}", "避險市值": "NT$ {:,.0f}", "保證金%": "{:.1f}%"},
                na_rep="—",
            ),
            width="stretch",
            hide_index=True,
        )

    st.markdown("### 避險獲利處理規則")
    st.write(
        f"若避險市值 ≥ 成本的 {rules.hedge_realize_multiple:.1f} 倍，實現 {rules.hedge_realize_fraction * 100:.0f}% 變成現金，其餘續抱。"
        "成本用淨支出；若策略是淨收入，改用買進賣權那一腿的權利金當成本。"
    )
    slip_col, _rest = st.columns([1, 2])
    drop = slip_col.slider("看看這個跌幅下的處理數字", min_value=0, max_value=30, value=20, key="oa_hedge_drop")
    move = -drop / 100.0
    plan_lines = []
    for summary in summaries:
        spot_m = float(book.twii) * (1.0 + move)
        from order_assistant import option_expiry_pnl

        hedge_pnl = option_expiry_pnl(spot_m, summary["legs"], TXO_MULTIPLIER)
        value = max(hedge_pnl + summary["net_debit"], 0.0)
        cost = summary["net_debit"] if summary["net_debit"] > 0 else summary["long_premium"]
        plan = hedge_profit_plan(cost, value, rules.hedge_realize_multiple, rules.hedge_realize_fraction)
        keep_lots = lots * plan["keep_fraction"]
        if plan["triggered"]:
            text = (
                f"{summary['name']}：市值 NT$ {value:,.0f} 是成本的 {plan['value_to_cost']:.1f} 倍。"
                f"實現 NT$ {plan['realize_cash']:,.0f} 入現金，保留約 {keep_lots:.1f} 口（市值 NT$ {plan['keep_value']:,.0f}）。"
            )
        else:
            ratio = f"{plan['value_to_cost']:.1f} 倍" if plan["value_to_cost"] is not None else "尚無成本"
            text = f"{summary['name']}：市值 NT$ {value:,.0f}（{ratio}），未達 {rules.hedge_realize_multiple:.1f} 倍，整組續抱。"
        plan_lines.append(text)
        st.write(text)

    with st.expander("美股指數 Put（選擇性，不與台指圖混在一起）"):
        u1, u2, u3, u4 = st.columns(4)
        us_spot = u1.number_input("指數點數", min_value=1.0, value=5600.0, step=10.0, key="oa_us_spot")
        us_strike = u2.number_input("賣權履約價", min_value=1.0, value=5300.0, step=10.0, key="oa_us_strike")
        us_lots = u3.number_input("口數 ", min_value=0.0, value=1.0, step=1.0, key="oa_us_lots")
        us_mult = u4.number_input("乘數（美元／點）", min_value=1.0, value=100.0, step=1.0, key="oa_us_mult")
        us_prem = st.number_input("權利金（點，0=理論價）", min_value=0.0, value=0.0, step=1.0, key="oa_us_prem")
        us_legs = [_leg("put", us_strike, us_lots, us_prem, iv)]
        # 用一個假的現貨做 BS，淨值仍是本帳戶的台幣淨值。
        us_book_spot = us_spot
        from order_assistant import resolve_leg_premium

        resolved = [resolve_leg_premium(us_book_spot, leg, t_years, rate) for leg in us_legs]
        premium_usd = resolved[0]["premium"] * us_lots * us_mult
        premium_twd = premium_usd * snapshot.usdtwd
        st.write(f"權利金約 US$ {premium_usd:,.0f}（NT$ {premium_twd:,.0f}，匯率 {snapshot.usdtwd:.2f}）")
        nav = snapshot.metrics["nav"] or 1.0
        st.write(f"佔淨值 {premium_twd / nav * 100:.2f}%，佔年度預算 {(premium_twd / budget * 100.0) if budget else 0:.0f}%")
        us_rows = []
        for move_us in (-0.10, -0.20, -0.30):
            from order_assistant import option_expiry_pnl

            pnl_usd = option_expiry_pnl(us_spot * (1.0 + move_us), resolved, us_mult)
            us_rows.append({"指數漲跌": f"{move_us * 100:.0f}%", "到期損益（USD）": pnl_usd, "到期損益（TWD）": pnl_usd * snapshot.usdtwd})
        st.dataframe(
            pd.DataFrame(us_rows).style.format({"到期損益（USD）": "US$ {:,.0f}", "到期損益（TWD）": "NT$ {:,.0f}"}),
            width="stretch",
            hide_index=True,
        )
        st.caption("這裡假設美股指數自己走，不跟台指圖疊加。")

    base_delta = taiex_dollar_delta(book)
    st.caption(
        f"目前台指經濟 Delta（未含選擇權）約 NT$ {base_delta:,.0f}／點，"
        f"等值 Beta {beta_equivalent(base_delta, book.twii, book.metrics['nav']):.2f}。"
        "這是市值與契約名目算出來的，和第四頁用曝險加權的 Beta 不同，避險才用得到。"
    )
    return "\n".join(plan_lines)


def render_order_assistant(send_to_telegram, load_sheets, beta_fn):
    _init_rule_state()
    st.title("第六頁：下單輔助系統")
    st.caption("下單前先看這一筆對整個部位的影響：槓桿、現金、Beta、保證金與壓力淨值。只做規劃，不會送出任何委託。")

    on_cloud = bool(os.environ.get("K_SERVICE"))
    has_key = bool(os.environ.get("GCP_KEY_JSON") or os.environ.get("GOOGLE_SERVICE_KEY_JSON"))
    options = ["線上持倉（與第四頁同一份試算表）", "示範持倉（離線試算）"]
    default_index = 0 if (on_cloud or has_key) else 1
    source = st.radio("資料來源", options, index=default_index, horizontal=True, key="oa_source")
    refresh_col, _rest = st.columns([1, 3])
    if refresh_col.button("重新讀取持倉", key="oa_reload"):
        load_sheets.clear()
        beta_fn.clear()
        st.rerun()

    using_sample = source.startswith("示範")
    if not using_sample:
        try:
            with st.spinner("正在用第四頁同一套算法讀持倉..."):
                snapshot = _load_live(load_sheets, beta_fn)
            st.caption(f"線上持倉。美元／台幣 {snapshot.usdtwd:.2f}，台指 {snapshot.twii:,.0f}。槓桿定義與第四頁相同。")
        except Exception as exc:
            st.warning(_public_error(exc))
            snapshot = build_sample_snapshot()
            using_sample = True
    else:
        snapshot = build_sample_snapshot()

    if using_sample:
        st.info("現在是示範持倉（台股、美股、SOXL／00663L、元大期貨），用來熟悉流程。數字不是你的真實帳戶，也不會寫回 Google 試算表。")

    _render_rule_editor()
    rules = _rules_from_state()
    stress_rows = standard_stress(snapshot, rules)
    flags = health_flags(snapshot, rules, stress_rows)
    plan = {
        "actions": [],
        "before": snapshot.metrics,
        "after": snapshot.metrics,
        "notes": [],
        "target_leverage": rules.target_leverage,
    }
    order_note = None
    hedge_note = None

    tab_health, tab_plan, tab_order, tab_hedge, tab_stress, tab_export = st.tabs(
        ["現況健檢", "調倉建議", "單筆下單試算", "選擇權避險", "壓力測試", "檢查清單"]
    )

    with tab_health:
        metrics = snapshot.metrics
        h1, h2, h3, h4, h5 = st.columns(5)
        h1.metric("淨值", f"NT$ {metrics['nav']:,.0f}")
        h2.metric("實質曝險", f"NT$ {metrics['exposure']:,.0f}")
        h3.metric("合併槓桿", f"{metrics['leverage']:.2f}x", f"上限 {rules.target_leverage:.2f}x", delta_color="off")
        h4.metric("現金水位", f"{metrics['cash_pct']:.1f}%", f"下限 {rules.min_cash_pct:.0f}%", delta_color="off")
        h5.metric("Beta", f"{metrics['beta']:.2f}", f"目標 {rules.beta_low:.1f}–{rules.beta_high:.1f}", delta_color="off")
        if metrics["margin_ratio"] is not None:
            st.metric(
                "期貨保證金比率（權益 ÷ 維持保證金）",
                f"{metrics['margin_ratio']:.1f}%",
                f"可動用保證金 NT$ {metrics['excess_margin']:,.0f}",
                delta_color="off",
            )
        st.caption(
            f"經濟槓桿 {economic_leverage(snapshot):.2f}x：股票沿用第四頁曝險，臺指期改以指數 × 乘數 × 口數。"
            "合併槓桿則與第四頁完全相同。"
        )
        _flag_table(flags)
        breaches = loss_breaches(snapshot, rules)
        st.subheader("超過單一部位虧損上限的持倉")
        if not breaches:
            st.success(f"沒有部位的浮虧或停損金額超過淨值的 {rules.max_loss_pct_nav:.1f}%。")
        else:
            breach_df = pd.DataFrame(breaches)[
                ["ticker", "name", "current_loss", "stop_risk", "budget", "trim_qty", "trim_value", "reason"]
            ].rename(
                columns={
                    "ticker": "標的",
                    "name": "名稱",
                    "current_loss": "目前浮虧",
                    "stop_risk": "停損金額",
                    "budget": "上限",
                    "trim_qty": "建議減碼數量",
                    "trim_value": "建議減碼市值",
                    "reason": "原因",
                }
            )
            st.dataframe(
                breach_df.style.format(
                    {"目前浮虧": "NT$ {:,.0f}", "停損金額": "NT$ {:,.0f}", "上限": "NT$ {:,.0f}", "建議減碼數量": "{:,.1f}", "建議減碼市值": "NT$ {:,.0f}"}
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption("減碼數量 = 持有數量 ×（1 − 上限 ÷ 目前損失）。減完之後，這檔的損失金額會回到淨值上限以內。")

    with tab_plan:
        st.subheader("調倉建議")
        st.caption("先賣槓桿型（同樣一塊錢砍掉最多曝險），還不夠再賣弱勢的非核心持股。核心預設不賣。換股是另外一筆，把弱勢股換成你指定的核心。")
        p1, p2, p3 = st.columns(3)
        p1.text_input("核心代號", key="oa_core_names")
        p2.number_input("弱勢股換股比例（%）", min_value=0.0, max_value=100.0, step=5.0, key="oa_rotate_pct")
        p3.checkbox("嚴格同產業（沒有同業核心就先不換）", key="oa_strict_sector")
        cores = parse_tickers(st.session_state["oa_core_names"])
        plan = propose_rebalance(
            snapshot,
            rules,
            core_tickers=cores,
            rotate_fraction=float(st.session_state["oa_rotate_pct"]) / 100.0,
            strict_sector=bool(st.session_state["oa_strict_sector"]),
        )
        st.markdown(f"**目標槓桿 {rules.target_leverage:.2f}x 執行後**")
        _metric_row(plan["before"], plan["after"])
        _show_action_table(plan["actions"])
        for note in plan["notes"]:
            st.caption(note)
        gap = max(0.0, snapshot.metrics["exposure"] - rules.stage2_leverage * snapshot.metrics["nav"])
        st.info(f"若改走第二階段 {rules.stage2_leverage:.2f}x，以現在的曝險還要再降約 NT$ {gap:,.0f}。下面是另一套方案，不是接在上面繼續賣。")
        with st.expander(f"第二階段方案（槓桿上限 {rules.stage2_leverage:.2f}x）"):
            plan2 = propose_rebalance(
                snapshot,
                rules,
                core_tickers=cores,
                rotate_fraction=float(st.session_state["oa_rotate_pct"]) / 100.0,
                strict_sector=bool(st.session_state["oa_strict_sector"]),
                target_leverage=rules.stage2_leverage,
            )
            _metric_row(plan2["before"], plan2["after"])
            _show_action_table(plan2["actions"])
        st.subheader("條件式回補")
        st.text(st.session_state["oa_addback_text"])

    with tab_order:
        st.subheader("單筆下單試算")
        order_note = _render_order(snapshot, rules)

    with tab_hedge:
        st.subheader("選擇權避險試算")
        hedge_note = _render_hedge(snapshot, rules)

    with tab_stress:
        st.subheader("壓力測試")
        st.caption("台指情境：一般台股用 Beta、槓桿 ETF 用標的槓桿、臺指期點數同幅移動，美股不變。費半情境：半導體相關部位（槓桿 ETF 另乘槓桿）重估，其餘不變。貶值只影響美元資產的台幣市值。")
        stress_df = pd.DataFrame(
            [
                {
                    "情境": row["name"],
                    "淨值": row["nav"],
                    "回撤": row["drawdown_pct"],
                    "槓桿": row["leverage"],
                    "現金%": row["cash_pct"],
                    "保證金%": row["margin_ratio"] if row["margin_ratio"] is not None else float("nan"),
                    "追繳": "是" if row["margin_call"] else "否",
                    "回撤是否在容忍內": "是" if row["drawdown_pct"] >= rules.max_drawdown_pct else "否",
                }
                for row in stress_rows
            ]
        )
        st.dataframe(
            stress_df.style.format(
                {"淨值": "NT$ {:,.0f}", "回撤": "{:.1f}%", "槓桿": "{:.2f}x", "現金%": "{:.1f}%", "保證金%": "{:.1f}%"},
                na_rep="—",
            ),
            width="stretch",
            hide_index=True,
        )
        called = [row["name"] for row in stress_rows if row["margin_call"]]
        if called:
            st.error("這些情境的期貨權益會低於維持保證金：" + "、".join(called))
        else:
            st.success("這四個情境都還沒碰到維持保證金。")

    with tab_export:
        st.subheader("檢查清單與 Telegram")
        st.caption("按鈕按下去才會送。沒有按就只留在畫面上。")
        text = build_checklist_text(
            snapshot,
            rules,
            flags,
            plan=plan,
            order_note=order_note,
            hedge_note=hedge_note,
            addback_text=st.session_state.get("oa_addback_text"),
        )
        st.text_area("摘要", value=text, height=320)
        st.download_button("下載檢查清單", data=text.encode("utf-8"), file_name="下單輔助檢查清單.txt", mime="text/plain")
        if st.button("把這份摘要推到 Telegram", type="primary"):
            payload = text if len(text) <= 3500 else text[:3500] + "\n…（後段省略）"
            send_to_telegram("下單輔助試算", payload)
