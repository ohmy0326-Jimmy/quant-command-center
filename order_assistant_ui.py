"""第六頁：下單輔助。只做試算與規劃，不送委託。"""

from __future__ import annotations

import copy
import os
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from order_assistant import (
    Rules,
    TXO_MULTIPLIER,
    apply_order,
    build_checklist_text,
    build_payoff_curve,
    build_sample_snapshot,
    economic_leverage,
    format_qty_text,
    health_flags,
    hedge_leg_vols,
    hedge_profit_plan,
    hedge_stress_table,
    holding_display_values,
    iv_percentile,
    iv_traffic_light,
    option_expiry_pnl,
    loss_breaches,
    order_price_hint,
    parse_tickers,
    positions_as_dicts,
    propose_rebalance,
    order_rule_warnings,
    standard_stress,
    strategy_summary,
    suggest_addback_text,
    taiex_shock_state,
)
import taifex_quotes
import us_hedge
from order_calculators import (
    FUTURES_PRODUCTS,
    default_margin_per_lot,
    futures_order_impact,
    ledger_margin_per_lot,
    futures_trial,
    leaps_call_metrics,
)
from taiex_vix import fetch_taiex_vix
from nav_scenario_sim_ui import render_nav_scenario_simulator
from order_insights import (
    account_kind_warnings,
    account_options,
    account_view,
    beta_split_change,
    default_account,
    default_put_delta,
    futures_margin_call_level,
    hedge_suggestion,
    leveraged_beta_note,
    margin_account_call_levels,
    priority_list,
    rs_for_ticker,
    rs_warning,
)
from refresh_controls import render_refresh_bar
from beta_leverage_ui import render_beta_leverage_table
from risk_performance import page6_health_line
from beta_rules import BETA_FALLBACK_STATE_KEY, fallback_table_rows, overrides_from_rows
from macro_cycle import format_holding_tag, holding_tag
from macro_cycle_ui import cached_macro_cycle_view
from portfolio_risk import (
    attach_quotes,
    equity_ratio_pct,
    fetch_twii,
    fetch_usdtwd,
    is_taiex_future,
    normalize_positions,
    populate_market_fields,
)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _cached_rs(ticker: str, kind: str) -> dict:
    return rs_for_ticker(ticker, kind)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_taiex_vix() -> dict:
    return fetch_taiex_vix()


@st.cache_data(ttl=600, show_spinner=False)
def _cached_us_vix() -> dict:
    return us_hedge.fetch_us_vix()


@st.cache_data(ttl=600, show_spinner=False)
def _cached_us_index() -> dict:
    return us_hedge.fetch_us_index_prices()


def _priority_table(items: list[dict]):
    if not items:
        st.success("沒有追繳風險、觸發停損或超過虧損上限的部位。")
        return

    rows = []
    for idx, item in enumerate(items, 1):
        rows.append(
            {
                "順序": idx,
                "類別": item["category"],
                "標的": item["ticker"],
                "帳戶": item["account"],
                "說明": item["detail"],
                "金額 (NT$)": item["value"],
            }
        )
    df = pd.DataFrame(rows)

    def paint(row):
        cat = str(row["類別"])
        if cat in ("追繳風險", "融資追繳風險"):
            return ["background-color: #b71c1c; color: white"] * len(row)
        if cat == "觸發停損":
            return ["background-color: #e65100; color: white"] * len(row)
        return [""] * len(row)

    styler = df.style.format({"金額 (NT$)": "NT$ {:,.0f}"})
    if hasattr(styler, "apply"):
        styler = styler.apply(paint, axis=1)
    st.dataframe(styler, width="stretch", hide_index=True)


def _queue_regime_reset():
    st.session_state["oa_apply_regime_defaults"] = True


def _regime_overridden(view: dict) -> bool:
    risk = view["risk"]
    leverage = float(st.session_state["oa_target_leverage"])
    cash = float(st.session_state["oa_min_cash_pct"])
    return abs(leverage - float(risk["target_leverage"])) > 1e-9 or abs(cash - float(risk["min_cash_pct"])) > 1e-9


def _init_rule_state(view: dict):
    risk = view["risk"]
    defaults = {
        "oa_target_leverage": float(risk["target_leverage"]),
        "oa_stage2_leverage": 1.5,
        "oa_min_cash_pct": float(risk["min_cash_pct"]),
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
        st.session_state["oa_addback_text"] = suggest_addback_text(_rules_from_state(), st.session_state["oa_core_names"])


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


def _render_rule_editor(view: dict):
    risk = view["risk"]
    with st.expander("風控規則設定（存在這次工作階段，重新整理仍會留著）", expanded=False):
        if view.get("ok"):
            st.caption(
                f"多空狀態是{view['regime']}（分數 {view['total']:+d}，分數更新 {view['score_updated_at']}，產業圖 {view['industry_updated_at']}）。"
                f"第一次進來會把目標槓桿上限設成 {risk['target_leverage']:.1f} 倍、最低現金水位設成 {risk['min_cash_pct']:.0f}%。"
                "第二階段槓桿維持原來的設定。你之後改的數字會留在這次工作階段。"
            )
        else:
            detail = (view.get("errors") or ["總經檔無法使用"])[0]
            st.caption(
                f"總經檔沒有套用（{detail}）。目標槓桿上限與最低現金先用中性："
                f"{risk['target_leverage']:.1f} 倍、{risk['min_cash_pct']:.0f}%。"
            )
        st.caption("參數來自：" + ("手動" if _regime_overridden(view) else "牛熊等級"))
        if float(st.session_state["oa_stage2_leverage"]) > float(st.session_state["oa_target_leverage"]) + 1e-9:
            st.caption("第二階段槓桿比目標上限高。若要第二階段再壓低槓桿，把這個數字調到目標以下。")
        st.button("改回總經週期預設", on_click=_queue_regime_reset)
        st.caption("改完之後，下面的健檢、調倉與試算會用同一組數字。不會寫進試算表，也不會下單。回補文字要跟著新數字改，按「依目前規則重寫回補計畫」。")
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


def _load_live(load_sheets, beta_fn, load_market=None, beta_fallbacks=None):
    accounts, _nav_hist, quant = load_sheets()
    raw = normalize_positions(quant)
    pairs = tuple(dict.fromkeys(zip(raw["標的"].astype(str), raw["種類"].astype(str) if "種類" in raw.columns else [""] * len(raw)))) if not raw.empty and "標的" in raw.columns else tuple()
    if load_market is not None:
        res = load_market(pairs)
        usdtwd, twii, prices, betas, sectors = res[:5]
        extras = res[5] if len(res) > 5 else None
    else:
        usdtwd = fetch_usdtwd()
        twii = fetch_twii()
        prices, betas, sectors = ({}, {}, {})
        extras = {}
        if not raw.empty:
            prices, betas, sectors = populate_market_fields(raw, beta_fn, twii, extras=extras)
    if not raw.empty:
        raw = attach_quotes(raw, prices, betas, sectors, extras=extras, beta_fallbacks=beta_fallbacks)
    return build_snapshot_from_frames(raw, accounts, usdtwd, twii)


def _with_beta_fallbacks(snapshot, overrides):
    if not overrides:
        return snapshot
    positions = snapshot.positions.copy()
    positions.attrs["beta_fallbacks"] = dict(overrides)
    return build_snapshot_from_frames(positions, snapshot.accounts, snapshot.usdtwd, snapshot.twii)


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
    c3.metric("總 Beta", f"{after['total_beta']:.2f}", f"{after['total_beta'] - before['total_beta']:+.2f}", delta_color="off")
    if after.get("margin_ratio") is not None and before.get("margin_ratio") is not None:
        c4.metric("保證金比率", f"{after['margin_ratio']:.1f}%", f"{after['margin_ratio'] - before['margin_ratio']:+.1f}%")
    elif after.get("margin_ratio") is not None:
        c4.metric("保證金比率", f"{after['margin_ratio']:.1f}%")
    else:
        c4.metric("保證金比率", "無期貨")
    st.caption(f"台股 Beta {before['tw_beta']:.2f} → {after['tw_beta']:.2f}｜美股 Beta {before['us_beta']:.2f} → {after['us_beta']:.2f}")


def _order_kind(snapshot, ticker: str, held):
    if held:
        return held["kind"]
    if is_taiex_future(ticker):
        return "期貨"
    choice = st.selectbox("市場（新標的）", ["台股", "美股"], key=f"oa_kind_{ticker}")
    return "美股" if choice == "美股" else "台股"


@st.fragment
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
    if ticker:
        tag = format_holding_tag(holding_tag(cached_macro_cycle_view(), ticker))
        if tag:
            st.caption(f"總經配置：{tag}")
    kind = _order_kind(snapshot, ticker, held) if ticker else "台股"
    acc_options = account_options(snapshot.accounts)
    def_acc = default_account(snapshot, kind, held["account"] if held else None)
    if def_acc not in acc_options:
        acc_options.append(def_acc)
    selected_account = st.selectbox(
        "下單帳戶",
        acc_options,
        index=acc_options.index(def_acc),
        key=f"oa_account_{ticker or 'NA'}",
    )
    if held and held.get("account") and held["account"] != selected_account:
        st.caption(f"持倉在「{held['account']}」，這裡將以「{selected_account}」試算新部位（賣出若該帳戶無部位會警示）。")

    for acc_warn in account_kind_warnings(selected_account, kind):
        st.warning(acc_warn)

    beta = held["beta"] if held else 1.0
    lev = held["lev"] if held else (3.0 if ticker in {"SOXL", "TQQQ"} else 1.0)
    sector = held["sector"] if held else "其他"
    name = held["name"] if held else ticker
    with st.expander("新標的才需要改：Beta、槓桿、產業"):
        if not held:
            beta = st.number_input("Beta", value=1.0, step=0.1, key=f"oa_beta_{ticker}")
            lev = st.number_input("標的槓桿", min_value=0.1, value=float(lev), step=0.5, key=f"oa_lev_{ticker}")
            sector = st.text_input("產業", value=sector, key=f"oa_sector_{ticker}")
        else:
            st.caption(f"沿用持倉：Beta {held['beta']:.2f}、槓桿 {held['lev']:.1f}x、{held['sector']}、{held['kind']}")

        st.markdown("**槓桿 ETF 標的預設 Beta（抓不到即時一年 Beta 時才用）**")
        df_fb = pd.DataFrame(fallback_table_rows(st.session_state.get(BETA_FALLBACK_STATE_KEY)))
        edited = st.data_editor(
            df_fb,
            key="oa_beta_fallback_editor",
            hide_index=True,
            disabled=["標的", "對應槓桿 ETF", "已手動修改"],
            column_config={
                "預設 Beta": st.column_config.NumberColumn(min_value=0.1, max_value=5.0, step=0.05, format="%.2f")
            },
        )
        new = overrides_from_rows(edited.to_dict("records"))
        if new != (st.session_state.get(BETA_FALLBACK_STATE_KEY) or {}):
            st.session_state[BETA_FALLBACK_STATE_KEY] = new
            st.rerun()

        if st.button("恢復預設值", key="oa_beta_fallback_reset"):
            st.session_state.pop(BETA_FALLBACK_STATE_KEY, None)
            st.session_state.pop("oa_beta_fallback_editor", None)
            st.rerun()

        fb_used = snapshot.metrics.get("beta_fallbacks_used") or []
        if fb_used:
            names = "、".join(f"{x.get('標的')}（{x.get('Beta來源')}）" for x in fb_used)
            st.caption(f"目前用預設值的持倉：{names}")
        else:
            st.caption("目前所有槓桿 ETF 都抓到即時一年 Beta。")
        st.caption("修改後第四頁、第六頁、美股避險一起套用（同一個瀏覽器分頁）。")
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
            "account": selected_account,
        },
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**下單前**")
        _side_metrics(snapshot)
    with right:
        st.markdown("**下單後**")
        _side_metrics(after)

    st.markdown("**處置優先順序（追繳風險與觸發停損排最前）**")
    _priority_table(priority_list(snapshot, rules, after))

    st.markdown(f"**下單帳戶「{selected_account}」（下單前 → 下單後）**")
    b_acc = account_view(snapshot, selected_account)
    a_acc = account_view(after, selected_account)
    ca1, ca2, ca3, ca4 = st.columns(4)
    ca1.metric("帳戶現金", f"NT$ {a_acc['cash']:,.0f}", f"NT$ {a_acc['cash'] - b_acc['cash']:+,.0f}")
    ca2.metric("帳戶淨值", f"NT$ {a_acc['nav']:,.0f}", f"NT$ {a_acc['nav'] - b_acc['nav']:+,.0f}")
    ca3.metric("帳戶槓桿", f"{a_acc['leverage']:.2f}x", f"{a_acc['leverage'] - b_acc['leverage']:+.2f}x", delta_color="inverse")
    if a_acc["available_margin"] is not None and b_acc["available_margin"] is not None:
        ca4.metric("可動用保證金", f"NT$ {a_acc['available_margin']:,.0f}", f"NT$ {a_acc['available_margin'] - b_acc['available_margin']:+,.0f}")
    elif a_acc["available_margin"] is not None:
        ca4.metric("可動用保證金", f"NT$ {a_acc['available_margin']:,.0f}")
    else:
        ca4.metric("可動用保證金", "—")

    _metric_row(snapshot.metrics, after.metrics)
    st.caption(
        f"經濟槓桿（期貨改以契約名目計）：{economic_leverage(snapshot):.2f}x → {economic_leverage(after):.2f}x。"
        "上方的合併槓桿與第四頁相同（實質曝險 ÷ 淨值）。"
    )

    st.markdown("**Beta 拆解（下單前 → 下單後）**")
    beta_df = pd.DataFrame(beta_split_change(snapshot.metrics, after.metrics)).rename(
        columns={
            "label": "市場",
            "before": "下單前 Beta",
            "after": "下單後 Beta",
            "delta": "變動",
            "exposure_before": "下單前曝險",
            "exposure_after": "下單後曝險",
        }
    )
    st.dataframe(
        beta_df.style.format(
            {
                "下單前 Beta": "{:.2f}",
                "下單後 Beta": "{:.2f}",
                "變動": "{:+.2f}",
                "下單前曝險": "NT$ {:,.0f}",
                "下單後曝險": "NT$ {:,.0f}",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    lev_note = leveraged_beta_note(lev, beta)
    if lev_note:
        st.info(lev_note)
    st.caption("槓桿 ETF 只用標的槓桿計一次（曝險 = 市值 × 槓桿），不再乘 Beta；曝險有正負號，空單／買賣權依 Delta 計。")

    rs_on = st.toggle("計算 RS Rating（抓 13 個月股價，約數秒）", value=True, key="oa_rs_on")
    rs_str = ""
    if rs_on and ("期" not in kind and "選" not in kind):
        try:
            with st.spinner("正在計算 RS Rating..."):
                rs_res = _cached_rs(ticker, kind)
            r_val = rs_res.get("rating")
            st.metric(
                "RS Rating",
                r_val if r_val is not None else "—",
                help="IBD 式：0.4×3個月 + 0.2×6個月 + 0.2×9個月 + 0.2×12個月報酬，與對照群比百分位 1–99",
            )
            rets = rs_res.get("returns", {})
            ret_texts = []
            for p_lbl, p_val in (("3m", "3個月"), ("6m", "6個月"), ("9m", "9個月"), ("12m", "12個月")):
                if rets.get(p_lbl) is not None:
                    ret_texts.append(f"{p_val} {rets[p_lbl] * 100:+.1f}%")
            ret_line = "；".join(ret_texts) if ret_texts else "無報酬率資料"
            bench = rs_res.get("benchmark", "")
            usize = rs_res.get("universe_size", 0)
            st.caption(f"對照基準：{bench}（共 {usize} 檔）｜ 過去報酬率：{ret_line}")
            warn_msg = rs_warning(r_val)
            if warn_msg:
                st.warning(warn_msg)
            if rs_res.get("error"):
                st.caption(rs_res["error"])
            if r_val is not None:
                rs_str = f"RS Rating {r_val}"
        except Exception as exc:
            st.caption(f"RS Rating 計算失敗：{exc}")

    b_name, b_w = _top_sector(snapshot.metrics["sector_weights"])
    a_name, a_w = _top_sector(after.metrics["sector_weights"])
    st.write(f"最大產業：{b_name} {b_w:.1f}% → {a_name} {a_w:.1f}%")

    st.markdown("**追繳點位**")
    b_fmc = futures_margin_call_level(snapshot)
    a_fmc = futures_margin_call_level(after)
    fmc_note_str = ""
    if b_fmc or a_fmc:
        def _fmt_fmc(fmc):
            if not fmc:
                return "—"
            if fmc.get("already"):
                return "已追繳"
            if fmc.get("index_level") is None:
                return fmc.get("direction", "不觸發")
            return f"台指 {fmc['index_level']:,.0f}（{fmc['direction']} {abs(fmc['move_pct']):.1f}%，{fmc['points']:+,.0f} 點）"

        st.write(f"期貨追繳點位：下單前 {_fmt_fmc(b_fmc)} → 下單後 {_fmt_fmc(a_fmc)}")
        if a_fmc:
            fmc_note_str = f"期貨追繳點位下單後：{_fmt_fmc(a_fmc)}"

    m_levels = margin_account_call_levels(after)
    if m_levels:
        m_df = pd.DataFrame(m_levels).rename(
            columns={
                "account": "帳戶",
                "market_value": "持股市值",
                "debt": "融資/質押借款",
                "maintenance_now_pct": "目前維持率%",
                "trigger_drop_pct": "觸發追繳跌幅%",
                "already_call": "已追繳",
            }
        )
        m_df["已追繳"] = m_df["已追繳"].map({True: "是", False: "否"})
        st.dataframe(
            m_df.style.format(
                {
                    "持股市值": "NT$ {:,.0f}",
                    "融資/質押借款": "NT$ {:,.0f}",
                    "目前維持率%": "{:.1f}%",
                    "觸發追繳跌幅%": "{:.1f}%",
                }
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption("假設整戶維持率 130%、持股同步下跌；美股券商（IB／嘉信）實際維持率依券商規定，僅供參考。")

    st.markdown("**TXO 賣權避險建議（口數只看下單後台股淨曝險）**")
    def_pdelta = round(default_put_delta(after.twii if after.twii else 0.0), 2)
    put_delta = st.number_input(
        "TXO Put Delta（負值；只估算有效避險曝險、台股 Beta 與避險比率）",
        value=def_pdelta,
        min_value=-1.0,
        max_value=0.0,
        step=0.05,
        key="oa_order_put_delta",
        help="預設為 30 天、價外約 5%、波動率 20% 的賣權 Delta。口數 = 下單後台股淨曝險 ÷（台指 × 50），再四捨五入。有效避險曝險 = Delta × 建議口數 × 50 × 台指。填 0 則不估算有效避險。",
    )
    h_after = hedge_suggestion(after, put_delta if put_delta != 0 else None)
    h_before = hedge_suggestion(snapshot, None)
    ch1, ch2, ch3, ch4 = st.columns(4)
    ch1.metric("台股淨曝險", f"NT$ {h_after['net_exposure']:,.0f}", f"NT$ {h_after['net_exposure'] - h_before['net_exposure']:+,.0f}")
    ch2.metric("原始口數", f"{h_after['lots_raw']:.2f} 口")
    ch3.metric("建議口數", f"{h_after['suggested_lots']} 口")
    exp = h_after["delta_hedged_exposure"]
    ch4.metric("Delta 調整後避險曝險", f"NT$ {exp:,.0f}" if exp is not None else "—")
    if exp is None:
        st.caption("Delta 為 0：只顯示依淨曝險算出的口數。")
    else:
        st.write(
            f"台股 Beta {h_after['tw_beta_before']:.2f} → {h_after['tw_beta_after']:.2f}"
            f"｜避險比率 {h_after['hedge_ratio_before'] * 100:.1f}% → {h_after['hedge_ratio_after'] * 100:.1f}%"
        )
    st.caption(h_after["formula"])

    stress_view = []
    for move, label in ((-0.10, "台指 -10%"), (-0.20, "台指 -20%")):
        b = taiex_shock_state(snapshot, move)
        a = taiex_shock_state(after, move)
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

    order_note_extra = [f"下單帳戶：{selected_account}"]
    if rs_str:
        order_note_extra.append(rs_str)
    if fmc_note_str:
        order_note_extra.append(fmc_note_str)
    lot_note = f"建議 TXO Put 口數：{h_after['suggested_lots']} 口（原始 {h_after['lots_raw']:.2f}）"
    if exp is not None:
        lot_note += f"，Delta 調整後避險曝險 NT$ {exp:,.0f}"
    order_note_extra.append(lot_note)

    return f"{side} {ticker} {format_qty_text(ticker, kind, qty)} @ {price:g}。" + " ".join(order_note_extra) + f" 槓桿 {snapshot.metrics['leverage']:.2f}x → {after.metrics['leverage']:.2f}x，現金 {snapshot.metrics['cash_pct']:.1f}% → {after.metrics['cash_pct']:.1f}%。" + (
        " " + "；".join(rule_warnings)
    )


def _side_metrics(snapshot):
    metrics = snapshot.metrics
    st.write(f"淨值 NT$ {metrics['nav']:,.0f}")
    st.write(f"合併槓桿 {metrics['leverage']:.2f}x")
    st.write(f"現金 {metrics['cash_pct']:.1f}%")
    st.write(f"總 Beta {metrics['total_beta']:.2f}（台股 {metrics['tw_beta']:.2f}／美股 {metrics['us_beta']:.2f}）")
    if metrics["margin_ratio"] is None:
        st.write("保證金 —")
    else:
        st.write(f"保證金 {metrics['margin_ratio']:.1f}%")
    name, weight = _top_sector(metrics["sector_weights"])
    st.write(f"最大產業 {name} {weight:.1f}%")


def _leg(kind: str, strike: float, lots: float, premium: float, iv: float) -> dict:
    return {"kind": kind, "strike": float(strike), "signed_lots": float(lots), "premium": float(premium), "iv": float(iv)}


@st.fragment
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
    vix_quote = _cached_taiex_vix()
    vix_level = vix_quote.get("value") if vix_quote.get("ok") else None
    v1, v2 = st.columns([1, 2])
    if vix_level is None:
        v1.metric("台指 VIX", "無報價")
    else:
        v1.metric("台指 VIX", f"{float(vix_level):.2f}")
    v2.caption(f"來源：{vix_quote.get('source') or '無報價'}　{vix_quote.get('as_of') or ''}".strip())

    def _priced(kind: str, strike: float, signed_lots: float, premium: float) -> dict:
        decision = hedge_leg_vols(float(spot), float(strike), kind, float(premium), iv, t_years, rate, vix_level)
        leg = _leg(kind, strike, signed_lots, premium, decision["iv"])
        leg["iv_note"] = decision
        return leg

    put_decision = hedge_leg_vols(float(spot), float(k_put), "put", float(prem_put), iv, t_years, rate, vix_level)
    pct = iv_percentile(put_decision.get("iv_percent"), vix_quote.get("history"))
    light = iv_traffic_light(pct)
    if pct is None:
        st.caption(f"IV 百分位：無法計算。{vix_quote.get('history_label') or '近一年台指 VIX 沒有資料'}。燈號：綠 <30%、黃 30–70%、紅 >70%。")
    else:
        light_text = f"IV 百分位 {pct:.0f}%：{light['label']}（{vix_quote.get('history_label')}；綠 <30%、黃 30–70%、紅 >70%）"
        if light["color"] == "green":
            st.success(light_text)
        elif light["color"] == "red":
            st.error(light_text)
        else:
            st.warning(light_text)
    for note in (
        put_decision.get("note"),
        hedge_leg_vols(float(spot), float(k_short), "put", float(prem_short), iv, t_years, rate, vix_level).get("note"),
        hedge_leg_vols(float(spot), float(k_call), "call", float(prem_call), iv, t_years, rate, vix_level).get("note"),
    ):
        if note:
            st.caption(note)
    st.caption(f"距到期 {days} 天。有填權利金就回推隱含波動率；價外 Put 未填權利金時用台指 VIX + 3。權利金 0 且沒有 VIX 時，沿用上方波動率。")

    # 圖與 Greeks 用畫面上的現貨，不改動持倉裡的期貨價格。
    book = copy.copy(snapshot)
    book.twii = float(spot)
    strategies = {
        "保護性賣權": [_priced("put", k_put, lots, prem_put)],
        "賣權價差": [_priced("put", k_put, lots, prem_put), _priced("put", k_short, -lots, prem_short)],
        "領口": [_priced("put", k_put, lots, prem_put), _priced("call", k_call, -lots, prem_call)],
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
            st.write(f"台股 Beta {summary['tw_beta_before']:.2f} → {summary['tw_beta_after']:.2f}")
            st.write(f"美股 Beta {summary['us_beta_before']:.2f} → {summary['us_beta_after']:.2f}")
            st.write(f"總 Beta {summary['total_beta_before']:.2f} → {summary['total_beta_after']:.2f}")
            st.write(f"選擇權曝險變動 NT$ {summary['option_exposure']:,.0f}")
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
        height=480,
        margin=dict(l=10, r=10, t=64, b=72),
        legend=dict(orientation="h", yanchor="top", y=-0.22, x=0.0, xanchor="left"),
    )
    st.plotly_chart(fig, width="stretch")
    st.caption("這張圖是到期內含價值，美股部位不隨台指連動。時間價值耗損看上面的每月數字，不畫進曲線。")
    st.caption("以加權指數×曝險估算，正2等槓桿 ETF 有每日再平衡耗損。")

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

    st.caption("TW Beta = TW 曝險 / TW 淨值 (TW 淨值 = 台股市值 + 元大期貨權益數 + 台股現金 − 融資/質押)、US Beta = US 曝險 / US 淨值、總 Beta = (TW 曝險 + US 曝險) / 總淨值；選擇權曝險 = Delta × 口數 × 乘數 × 指數；權利金從現金扣除。")
    return "\n".join(plan_lines)


@st.fragment
def _render_us_hedge(snapshot, rules: Rules, beta_fn=None):
    st.markdown("### 美股選擇權避險（SPY／XSP／SPX）")
    st.caption("乘數都是 100 美元；三個策略是替代方案，權利金不要加總；美股選擇權沒有即時報價來源，權利金請手動輸入，沒填會標「無報價」並用理論價試算。")

    idx = _cached_us_index()
    prices = us_hedge.instrument_prices(idx.get("SPX"), idx.get("SPY"))

    inst_options = list(us_hedge.US_HEDGE_INSTRUMENTS.keys())
    inst = st.selectbox(
        "避險標的",
        inst_options,
        index=inst_options.index(us_hedge.DEFAULT_US_INSTRUMENT),
        format_func=lambda k: us_hedge.US_HEDGE_INSTRUMENTS[k]["label"],
        key="oa_us_inst",
    )

    m1, m2, m3 = st.columns(3)
    spx_val = prices.get("SPX")
    xsp_val = prices.get("XSP")
    spy_val = prices.get("SPY")
    m1.metric("SPX", f"{spx_val:,.2f}" if spx_val is not None else "無報價")
    m2.metric("XSP（≈ SPX ÷ 10）", f"{xsp_val:,.2f}" if xsp_val is not None else "無報價")
    m3.metric("SPY（≈ SPX ÷ 10）", f"{spy_val:,.2f}" if spy_val is not None else "無報價")

    src_label = idx.get("source", "無報價")
    spx_per_spy = prices.get("spx_per_spy")
    ratio_str = f"｜SPX ÷ SPY = {spx_per_spy:.2f}" if spx_per_spy is not None else ""
    est_notes = []
    if prices.get("spx_estimated"):
        est_notes.append("SPX 為推估")
    if prices.get("spy_estimated"):
        est_notes.append("SPY/XSP 為推估")
    est_str = f"（{', '.join(est_notes)}）" if est_notes else ""
    st.caption(f"來源：{src_label}{ratio_str} {est_str}".strip())

    cur_price = prices.get(inst)
    if cur_price is None:
        st.warning("無報價：S&P 500 價格抓不到，請手動輸入。")
        fallback_spot = 6000.0 if inst == "SPX" else 600.0
    else:
        fallback_spot = float(cur_price)

    spot = st.number_input(
        "標的價格",
        min_value=1.0,
        value=fallback_spot,
        step=1.0 if inst != "SPX" else 5.0,
        key=f"oa_us_spot_{inst}",
    )

    _ = beta_fn
    exp = us_hedge.us_spx_exposure(snapshot)
    if not exp["rows"]:
        st.info("沒有美股部位，不需要美股避險。")
        return None

    disp_rows = []
    for r in exp["rows"]:
        src = r.get("beta_source") or r.get("method") or ""
        if "預設值" in src:
            src = f"⚠️ {src}"
        disp_rows.append({
            "標的": r["ticker"],
            "名稱": r["name"],
            "市值(NT$)": r["mv_twd"],
            "槓桿": f"{r['lev']:g}x",
            "名目曝險(NT$)": r.get("notional_twd", 0.0),
            "標的代號": r["underlying"] or "—",
            "標的 Beta": f"{r['underlying_beta']:.2f}" if r["underlying_beta"] is not None else "—",
            "有效 Beta": f"{r['eff_beta']:.2f}" if r["eff_beta"] is not None else "—",
            "S&P 曝險(NT$)": r["exposure_twd"],
            "Beta來源": src,
        })
    df_exp = pd.DataFrame(disp_rows)
    st.dataframe(
        df_exp.style.format({
            "市值(NT$)": "NT$ {:,.0f}",
            "名目曝險(NT$)": "NT$ {:,.0f}",
            "S&P 曝險(NT$)": "NT$ {:,.0f}",
        }),
        width="stretch",
        hide_index=True,
    )

    if exp.get("fallback_rows"):
        st.warning(f"{'、'.join(exp['fallback_rows'])} 抓不到標的即時一年 Beta，暫用預設值（可在上方「新標的才需要改」修改）。")

    em1, em2, em3, em4 = st.columns(4)
    em1.metric("美股 S&P Beta 曝險", f"NT$ {exp['exposure_twd']:,.0f}", f"US$ {exp['exposure_usd']:,.0f}")
    em2.metric("美股槓桿（名目 ÷ 淨值）", f"{exp['us_leverage']:.2f}x", f"名目 NT$ {exp['notional_twd']:,.0f}", delta_color="off")
    em3.metric("美股部位平均 Beta", f"{exp['us_avg_beta']:.2f}")
    em4.metric("美股帳戶 Beta（÷ 美股淨值）", f"{exp['us_beta_spx']:.2f}", f"第四頁 {snapshot.metrics.get('us_beta', 0.0):.2f}", delta_color="off")
    st.caption(
        f"名目曝險＝市值 × 槓桿；Beta 曝險＝名目曝險 × 標的一年 Beta（對 S&P 500）＝市值 × 有效 Beta，槓桿只乘一次，與第四頁同一套算法。"
        f"美股帳戶 Beta {exp['us_beta_spx']:.2f}＝槓桿 {exp['us_leverage']:.2f}x × 部位平均 Beta {exp['us_avg_beta']:.2f}"
        f"（Beta 曝險 NT$ {exp['page4_us_exposure']:,.0f} ÷ 美股淨值 NT$ {exp['us_nav']:,.0f}）。"
    )
    render_beta_leverage_table(st, snapshot.metrics)

    i1, i2, i3, i4, i5 = st.columns(5)
    ratio = i1.number_input("避險比率（%）", min_value=0.0, max_value=200.0, value=100.0, step=5.0, key="oa_us_ratio")
    calc = us_hedge.hedge_contracts(exp["exposure_usd"], spot, ratio / 100.0, 100.0)
    contracts = i2.number_input(
        "口數",
        min_value=0.0,
        value=float(calc["contracts"]),
        step=1.0,
        key=f"oa_us_contracts_{inst}_{calc['contracts']}",
        help="預設為自動計算的口數；避險比率或價格改變時會重新帶入。",
    )
    expiry = i3.date_input("到期日", value=date.today() + timedelta(days=90), key="oa_us_expiry")
    rate = i4.number_input("利率（%）", min_value=0.0, max_value=15.0, value=4.0, step=0.25, key="oa_us_rate") / 100.0
    iv_fallback = i5.number_input("預設波動率（%）", min_value=1.0, max_value=150.0, value=18.0, step=1.0, key="oa_us_iv") / 100.0

    st.caption(f"{calc['formula']}（原始計算值：{calc['raw']:.2f} 口）")

    days = max((expiry - date.today()).days, 0)
    t_years = days / 365.0

    step_val = 5.0 if inst == "SPX" else 1.0
    def_put = round(spot * 0.95 / step_val) * step_val
    def_short = round(spot * 0.90 / step_val) * step_val
    def_call = round(spot * 1.05 / step_val) * step_val

    k1, k2, k3 = st.columns(3)
    k_put = k1.number_input("買進賣權履約價", min_value=0.0, value=float(def_put), step=step_val, key=f"oa_us_k_put_{inst}")
    k_short = k2.number_input("價差空頭履約價", min_value=0.0, value=float(def_short), step=step_val, key=f"oa_us_k_short_{inst}")
    k_call = k3.number_input("領口賣出買權履約價", min_value=0.0, value=float(def_call), step=step_val, key=f"oa_us_k_call_{inst}")

    p1, p2, p3 = st.columns(3)
    raw_prem_put = p1.number_input("賣權權利金（每股/點 USD，0 = 無報價）", min_value=0.0, value=0.0, step=0.5, key=f"oa_us_prem_put_{inst}")
    raw_prem_short = p2.number_input("空頭賣權權利金（每股/點 USD，0 = 無報價）", min_value=0.0, value=0.0, step=0.5, key=f"oa_us_prem_short_{inst}")
    raw_prem_call = p3.number_input("買權權利金（每股/點 USD，0 = 無報價）", min_value=0.0, value=0.0, step=0.5, key=f"oa_us_prem_call_{inst}")

    premiums = {
        "put": raw_prem_put if raw_prem_put > 0 else None,
        "short_put": raw_prem_short if raw_prem_short > 0 else None,
        "call": raw_prem_call if raw_prem_call > 0 else None,
    }

    vix = _cached_us_vix()
    v1, v2 = st.columns([1, 2])
    vix_level = float(vix["value"]) if vix.get("ok") and vix.get("value") is not None else None
    if vix_level is None:
        v1.metric("美股 VIX", "無報價")
    else:
        v1.metric("美股 VIX", f"{vix_level:.2f}")
    v2.caption(f"來源：{vix.get('source') or '無報價'}　{vix.get('as_of') or ''}".strip())

    built = us_hedge.build_us_strategies(
        spot, int(contracts), k_put, k_short, k_call, premiums, t_years, rate, iv_fallback, vix_level
    )
    for w in built.get("warnings", []):
        st.warning(w)

    put_leg = built["strategies"]["買進賣權"][0]
    short_leg = None
    call_leg = None
    if "賣權價差" in built["strategies"]:
        short_leg = built["strategies"]["賣權價差"][1]
    if "領口" in built["strategies"]:
        call_leg = built["strategies"]["領口"][1]

    for leg_name, leg_obj in (("買進賣權", put_leg), ("賣權價差空頭", short_leg), ("領口賣出買權", call_leg)):
        if leg_obj is None:
            continue
        src = leg_obj.get("source")
        note = leg_obj.get("note", "")
        if leg_obj.get("no_quote"):
            st.warning(f"{leg_name}：{note}")
        elif src == "manual":
            st.caption(note)
        elif src == "manual_bad_iv":
            st.warning(note)

    put_iv_pct = put_leg.get("iv", iv_fallback) * 100.0
    sig = us_hedge.iv_signal_vs_vix(put_iv_pct, vix.get("history") or [])
    pct = sig.get("percentile")
    label = sig.get("label", "")
    color = sig.get("color")
    hl = vix.get("history_label") or "近一年美股 VIX 沒有資料"
    if pct is None:
        st.caption(f"IV 百分位：無法計算。{hl}。燈號：綠 <30%、黃 30–70%、紅 >70%。")
    else:
        extra_note = "（理論價，非市場報價）" if put_leg.get("no_quote") else ""
        text = f"IV {put_iv_pct:.1f}% 在{hl}的百分位 {pct:.0f}%：{label}（綠 <30%、黃 30–70%、紅 >70%）{extra_note}"
        if color == "green":
            st.success(text)
        elif color == "red":
            st.error(text)
        else:
            st.warning(text)

    summaries = []
    cols = st.columns(len(built["strategies"]))
    budget = snapshot.metrics["nav"] * rules.hedge_budget_pct_nav / 100.0
    for col, (name, legs) in zip(cols, built["strategies"].items()):
        s = us_hedge.us_strategy_summary(snapshot, name, legs, spot, t_years, rate, exp["exposure_twd"], 100.0)
        summaries.append(s)
        with col:
            st.markdown(f"**{name}**")
            debit = s["net_debit"]
            debit_usd = s["net_debit_usd"]
            if debit >= 0:
                st.metric("權利金支出", f"US$ {debit_usd:,.0f}", f"NT$ {debit:,.0f}（佔淨值 {s['premium_pct_nav']:.2f}%）")
            else:
                st.metric("權利金淨收入", f"US$ {-debit_usd:,.0f}", f"NT$ {-debit:,.0f}")
            used = (max(debit, 0.0) / budget * 100.0) if budget > 0 else 0.0
            if max(debit, 0.0) > budget > 0:
                st.error(f"超過年度預算 {used:.0f}%（預算 NT$ {budget:,.0f}）")
            else:
                st.success(f"佔年度預算 {used:.0f}%（預算 NT$ {budget:,.0f}）")
            st.write(f"美股 Beta（對 S&P）{s['us_beta_before']:.2f} → {s['us_beta_after']:.2f}")
            st.write(f"台股 Beta {s['tw_beta_before']:.2f} → {s['tw_beta_after']:.2f}")
            st.write(f"總 Beta {s['total_beta_before']:.2f} → {s['total_beta_after']:.2f}")
            st.write(f"選擇權曝險變動 NT$ {s['option_exposure']:,.0f}")
            st.write(f"每月時間價值 US$ {s['theta_month_usd']:,.0f}（NT$ {s['theta_month']:,.0f}，負值是耗損）")
            if s["no_quote"]:
                st.caption("含無報價腿，用理論價估算")

    curve = us_hedge.us_payoff_curve(snapshot, summaries, spot, exp["exposure_twd"], 100.0)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=curve["漲跌幅%"], y=curve["投組"], name="美股部位（無避險）", line=dict(color="#B0BEC5", width=2)))
    colors = {"買進賣權": "#42A5F5", "賣權價差": "#FFA726", "領口": "#66BB6A"}
    for name in built["strategies"]:
        if name in curve.columns:
            fig.add_trace(go.Scatter(x=curve["漲跌幅%"], y=curve[name], name=name, line=dict(color=colors.get(name, "#EF5350"), width=2)))
    fig.add_hline(y=0, line_color="#546E7A", line_width=1)
    fig.add_vline(x=0, line_color="#546E7A", line_width=1)
    fig.update_layout(
        title=f"到期損益：美股部位 + {inst} 避險（S&P -30%～+15%）",
        xaxis_title="S&P 500 漲跌幅 %",
        yaxis_title="損益（TWD）",
        template="plotly_dark",
        height=480,
        margin=dict(l=10, r=10, t=64, b=72),
        legend=dict(orientation="h", yanchor="top", y=-0.22, x=0.0, xanchor="left"),
    )
    st.plotly_chart(fig, width="stretch", key="oa_us_payoff")
    st.caption("美股部位用 S&P 曝險 × 漲跌幅線性估算，台股不動；槓桿 ETF 有每日再平衡耗損。")

    st.markdown("**壓力表（S&P -10%／-20%／-30%，含權利金）**")
    stress_rows = []
    for s in summaries:
        for row in us_hedge.us_hedge_stress_table(
            snapshot, s, spot, exp["exposure_twd"], multiple=rules.hedge_realize_multiple, realize_fraction=rules.hedge_realize_fraction
        ):
            plan = row["profit_plan"]
            stress_rows.append({
                "策略": s["name"],
                "情境": row["name"],
                "美股部位損益": row["port_pnl"],
                "避險損益": row["hedge_pnl"],
                "避險市值": row["hedge_value"],
                "避險後淨值": row["nav"],
                "回撤": row["drawdown_pct"],
                "獲利規則": "實現" if plan["triggered"] else "未達",
            })
    if stress_rows:
        st.dataframe(
            pd.DataFrame(stress_rows).style.format({
                "美股部位損益": "NT$ {:,.0f}",
                "避險損益": "NT$ {:,.0f}",
                "避險市值": "NT$ {:,.0f}",
                "避險後淨值": "NT$ {:,.0f}",
                "回撤": "{:.1f}%",
            }),
            width="stretch",
            hide_index=True,
        )

    st.markdown("#### 美股避險獲利處理規則")
    st.write(
        f"若避險市值 ≥ 成本的 {rules.hedge_realize_multiple:.1f} 倍，實現 {rules.hedge_realize_fraction * 100:.0f}% 變成現金，其餘續抱。"
        "成本用淨支出；若策略是淨收入，改用買進賣權那一腿的權利金當成本。"
    )
    slip_col, _rest = st.columns([1, 2])
    drop = slip_col.slider("看看這個 S&P 跌幅下的處理數字", min_value=0, max_value=30, value=20, key="oa_us_hedge_drop")
    move = -drop / 100.0
    plan_lines = []
    usdtwd = float(snapshot.usdtwd)
    for s in summaries:
        spot_m = spot * (1.0 + move)
        hedge_pnl = option_expiry_pnl(spot_m, s["legs"], 100.0) * usdtwd
        value = max(hedge_pnl + s["net_debit"], 0.0)
        cost = s["net_debit"] if s["net_debit"] > 0 else s["long_premium"]
        plan = hedge_profit_plan(cost, value, rules.hedge_realize_multiple, rules.hedge_realize_fraction)
        keep_lots = contracts * plan["keep_fraction"]
        if plan["triggered"]:
            text = (
                f"{s['name']}：市值 NT$ {value:,.0f} 是成本的 {plan['value_to_cost']:.1f} 倍。"
                f"實現 NT$ {plan['realize_cash']:,.0f} 入現金，保留約 {keep_lots:.1f} 口（市值 NT$ {plan['keep_value']:,.0f}）。"
            )
        else:
            ratio_txt = f"{plan['value_to_cost']:.1f} 倍" if plan["value_to_cost"] is not None else "尚無成本"
            text = f"{s['name']}：市值 NT$ {value:,.0f}（{ratio_txt}），未達 {rules.hedge_realize_multiple:.1f} 倍，整組續抱。"
        plan_lines.append(text)
        st.write(text)

    st.caption("美股 Beta = 美股 S&P 曝險 ÷ 美股淨值；總 Beta = (台股曝險 + 美股 S&P 曝險) ÷ 總淨值；選擇權曝險 = Delta × 口數 × 100 × 價格 × 匯率；權利金從美股現金扣除。")

    strat_summaries = []
    for s in summaries:
        strat_summaries.append(f"{s['name']} 淨支出 US$ {s['net_debit_usd']:,.0f}，美股 Beta {s['us_beta_before']:.2f}→{s['us_beta_after']:.2f}")
    return f"美股避險：{inst} {contracts:g} 口，買進賣權 {k_put:g}；" + "；".join(strat_summaries)


@st.fragment
def _fragment_plan(snapshot, rules: Rules):
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
    st.markdown("**先處理：追繳風險與觸發停損**")
    _priority_table(priority_list(snapshot, rules))
    st.markdown(f"**目標槓桿 {rules.target_leverage:.2f}x 執行後**")
    _metric_row(plan["before"], plan["after"])
    _show_action_table(plan["actions"])
    for note in plan["notes"]:
        st.caption(note)
    gap = max(0.0, snapshot.metrics["exposure"] - rules.stage2_leverage * snapshot.metrics["nav"])
    st.info(f"若改走第二階段 {rules.stage2_leverage:.2f}x，以現在的曝險還要再降約 NT$ {gap:,.0f}。這是另一套方案，不是接在上面繼續賣。")
    if st.checkbox(f"計算第二階段明細（槓桿上限 {rules.stage2_leverage:.2f}x）", key="oa_show_stage2"):
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
    st.session_state["oa_plan"] = plan
    return plan


def _render_holding_tags(snapshot, view: dict):
    st.subheader("持倉的產業立場")
    if not view.get("ok"):
        st.caption("總經檔沒有套用，這次先不標加碼或減碼。")
        return
    st.caption("用總經配置裡的代表股對應產業，標在代號旁邊。對不上的不標。標了「示例」的是占位產業，不是目前定案。")
    rows = []
    for pos in positions_as_dicts(snapshot.valued):
        shown = holding_display_values(pos)
        rows.append(
            {
                "標的": pos["ticker"],
                "產業立場": format_holding_tag(holding_tag(view, pos["ticker"])),
                "名稱": pos["name"],
                "名目或市值": shown["amount"],
                "口徑": shown["basis"],
                "未實現損益": shown["pnl"],
            }
        )
    if not rows:
        st.info("目前沒有持倉。")
        return
    frame = pd.DataFrame(rows)

    def paint(value):
        text = str(value)
        if "減碼" in text:
            return "background-color: #b71c1c; color: white"
        if "加碼" in text:
            return "background-color: #1b5e20; color: white"
        if "中性" in text:
            return "background-color: #f9a825; color: #1a1a1a"
        return ""

    styler = frame.style.format({"名目或市值": "NT$ {:,.0f}", "未實現損益": "NT$ {:,.0f}"})
    if hasattr(styler, "map"):
        styler = styler.map(paint, subset=["產業立場"])
    else:
        styler = styler.applymap(paint, subset=["產業立場"])
    st.dataframe(styler, width="stretch", hide_index=True)
    st.caption("期貨口徑是名目（口數 × 價格 × 乘數），不是未實現損益，也不是保證金。")


def _render_leaps_calc(snapshot):
    st.caption("輸入 LEAPS Call 的報價，用 Black-Scholes 由權利金回推隱含波動率，再算 Delta、時間價值與約當股數。只做試算。")
    c1, c2, c3, c4 = st.columns(4)
    ticker = c1.text_input("標的代號", key="oa_leaps_ticker").strip().upper()
    hint_px = order_price_hint(ticker, snapshot) if ticker else None
    spot_default = float(hint_px) if (hint_px is not None and hint_px > 0) else 100.0
    spot_key = f"oa_leaps_spot_{ticker or 'NA'}"
    if spot_key not in st.session_state:
        st.session_state[spot_key] = spot_default
    spot = c2.number_input("標的現價", min_value=0.0, step=0.5, key=spot_key)
    strike_key = f"oa_leaps_strike_{ticker or 'NA'}"
    if strike_key not in st.session_state:
        st.session_state[strike_key] = float(round(spot * 0.8 * 2) / 2 if spot > 0 else 80.0)
    strike = c3.number_input("履約價", min_value=0.0, step=0.5, key=strike_key)
    expiry = c4.date_input("到期日", value=date.today() + timedelta(days=440), key="oa_leaps_expiry")

    c5, c6, c7, c8 = st.columns(4)
    premium = c5.number_input("權利金（每股）", min_value=0.0, value=0.0, step=0.5, key="oa_leaps_premium")
    rate_pct = c6.number_input("無風險利率（%）", min_value=0.0, value=4.0, step=0.25, key="oa_leaps_rate")
    contracts = c7.number_input("口數", min_value=1, value=1, step=1, key="oa_leaps_contracts")
    target = c8.number_input("目標曝險（美元，Delta × 股數 × 現價）", min_value=0.0, value=0.0, step=1000.0, key="oa_leaps_target")

    c9, _ = st.columns([1, 3])
    ref_pct = c9.number_input("參考波動率（%，例如歷史波動率，0 = 不填）", min_value=0.0, value=0.0, step=0.5, key="oa_leaps_ref_vol")

    if premium <= 0:
        st.info("填入權利金才會試算。")
        return None

    res = leaps_call_metrics(
        spot,
        strike,
        expiry,
        premium,
        rate=rate_pct / 100,
        today=date.today(),
        contracts=int(contracts),
        target_exposure=target or None,
        ref_vol=(ref_pct / 100 if ref_pct > 0 else None),
    )
    if not res.get("ok"):
        st.warning(res.get("error", "試算失敗"))
        return None

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Delta", f"{res['delta']:.3f}")
    sig = res.get("signal") or {}
    m2.metric("隱含波動率", f"{res['iv'] * 100:.1f}%", delta=sig.get("label"), delta_color="off")
    m3.metric(
        "時間價值占比",
        f"{res['extrinsic_pct']:.1f}%",
        help=f"內含價值 US$ {res['intrinsic']:.2f}／時間價值 US$ {res['extrinsic']:.2f}",
    )
    m4.metric(
        "約當股數",
        f"{res['equivalent_shares']:,.1f} 股",
        delta=f"{contracts} 口 × Delta × 100",
        delta_color="off",
    )

    n1, n2, n3, n4 = st.columns(4)
    n1.metric("損益兩平價", f"US$ {res['breakeven']:.2f}")
    n2.metric(
        "Delta 曝險（美元）",
        f"US$ {res['delta_exposure']:,.0f}",
        delta=f"約 NT$ {res['delta_exposure'] * snapshot.usdtwd:,.0f}",
        delta_color="off",
    )
    n3.metric("權利金總額", f"US$ {res['cost_total']:,.0f}")
    n4.metric("每月時間耗損", f"US$ {res['theta_month_total']:,.0f}")

    sig_label = sig.get("label", "")
    sig_detail = sig.get("detail", "")
    sig_text = f"IV 訊號：{sig_label}。{sig_detail}"
    if sig.get("color") == "green":
        st.success(sig_text)
    elif sig.get("color") == "yellow":
        st.info(sig_text)
    elif sig.get("color") == "red":
        st.warning(sig_text)

    n_sug = res.get("suggested_contracts")
    if n_sug:
        shares_sug = res.get("suggested_equivalent_shares", 0.0)
        cost_sug = res.get("suggested_cost", 0.0)
        st.success(f"要達到目標曝險 US$ {target:,.0f}，建議 {n_sug} 口（約當 {shares_sug:,.0f} 股，權利金 US$ {cost_sug:,.0f}）。")

    for w in res.get("warnings", []):
        st.warning(w)

    st.caption("時間價值年化 = 時間價值 ÷ 現價 ÷ 剩餘年數。Delta 用回推的 IV 計算；美股選擇權一口 100 股。")
    return f"LEAPS Call：{ticker or '標的'} 履約價 {strike:.1f} 到期日 {expiry}｜Delta {res['delta']:.3f} × {contracts} 口（約當 {res['equivalent_shares']:,.1f} 股）"


def _render_futures_calc(snapshot, rules: Rules):
    st.caption("大台 TX、小台 MTX、微台 FMF（TMF）。輸入口數、價格、停損價，看保證金、名目曝險、下單後的槓桿與 Beta，以及停損金額允許的最大口數。只做試算。")
    fc1, fc2, fc3, fc4 = st.columns(4)
    product = fc1.selectbox("商品", list(FUTURES_PRODUCTS), format_func=lambda c: f"{c}｜{FUTURES_PRODUCTS[c]}", key="oa_fut_product")
    side_str = fc2.selectbox("方向", ["買進", "賣出"], key="oa_fut_side")
    lots = fc3.number_input("口數", min_value=0, value=1, step=1, key="oa_fut_lots")

    if "oa_fut_price" not in st.session_state:
        st.session_state["oa_fut_price"] = float(round(snapshot.twii))
    price = fc4.number_input("價格", min_value=0.0, step=1.0, key="oa_fut_price")

    if st.button("帶入期交所即時報價", key="oa_fut_quote_btn"):
        try:
            quote = taifex_quotes.get_futures_quote(product, fallback_price=float(snapshot.twii))
            if quote and getattr(quote, "price", None) is not None:
                st.session_state["oa_fut_price"] = float(quote.price)
                if getattr(quote, "label", None):
                    st.session_state["oa_fut_quote_label"] = quote.label
                st.rerun()
            else:
                st.warning("期交所與加權指數都抓不到報價，請手動輸入價格。")
        except Exception as exc:
            st.warning(f"報價讀取失敗（{exc.__class__.__name__}），請手動輸入價格。")

    if st.session_state.get("oa_fut_quote_label"):
        st.caption(st.session_state["oa_fut_quote_label"])

    sc1, sc2, sc3 = st.columns(3)
    stop_price = sc1.number_input("停損價（0 = 不設停損）", min_value=0.0, value=0.0, step=1.0, key="oa_fut_stop")
    margin_info = ledger_margin_per_lot(product, snapshot.metrics)
    margin_key = f"oa_fut_margin_{product}"
    margin = sc2.number_input(
        "每口原始保證金",
        min_value=0.0,
        value=float(margin_info["value"]),
        step=1000.0,
        key=margin_key,
        help="預先帶入 Futures_Ledger 的原始保證金（依校正當下口數換算每口）；讀不到時用系統預設（TX 701,000／MTX 175,250／FMF 35,050）。可以改成元大最新公告",
    )
    margin_source = margin_info["source"]
    margin_caption = f"保證金來源：{margin_source}"
    if margin_info.get("basis"):
        margin_caption += f"（{margin_info['basis']}）"
    if abs(float(margin) - float(margin_info["value"])) > 1e-6:
        margin_caption += f"｜已手動改為 NT$ {margin:,.0f}（帶入值 NT$ {margin_info['value']:,.0f}）"
    sc2.caption(margin_caption)
    default_max_loss = float(round(rules.max_loss_pct_nav / 100 * snapshot.metrics["nav"]))
    max_loss = sc3.number_input(
        "單筆最大虧損（NT$）",
        min_value=0.0,
        value=default_max_loss,
        step=10000.0,
        key="oa_fut_max_loss",
        help=f"預設為淨值的 {rules.max_loss_pct_nav:.1f}%",
    )

    trial = futures_trial(
        product,
        int(lots),
        price,
        stop_price=(stop_price if stop_price > 0 else None),
        side=("buy" if side_str == "買進" else "sell"),
        margin_per_lot=margin,
        max_loss=max_loss,
    )
    if not trial.get("ok"):
        st.warning(trial.get("error", "試算失敗"))
        return None
    if lots == 0:
        st.info("口數填 0 只算停損上限，不試算下單後部位。")

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric(
        "所需原始保證金",
        f"NT$ {trial['margin_needed']:,.0f}",
        delta=f"{lots} 口 × NT$ {margin:,.0f}",
        delta_color="off",
    )
    mc2.metric(
        "名目曝險",
        f"NT$ {trial['notional']:,.0f}",
        delta=f"{lots} × {price:,.0f} × {trial['multiplier']:g}",
        delta_color="off",
    )
    mc3.metric(
        "停損金額",
        f"NT$ {trial['stop_loss_total']:,.0f}" if trial.get("stop_loss_total") is not None else "—",
    )
    loss_per_lot = trial.get("loss_per_lot")
    mc4.metric(
        "停損上限可下口數",
        f"{trial['max_lots_by_loss']} 口" if trial.get("max_lots_by_loss") is not None else "—",
        delta=(f"每口停損 NT$ {loss_per_lot:,.0f}" if loss_per_lot is not None else None),
        delta_color="off",
    )

    for w in trial.get("warnings", []):
        st.warning(w)

    impact = None
    if lots > 0:
        impact = futures_order_impact(snapshot, trial)
        for w in impact.get("warnings", []):
            st.warning(w)

        ic1, ic2, ic3, ic4 = st.columns(4)
        ic1.metric(
            "合併槓桿",
            f"{impact['leverage_after']:.2f}x",
            delta=f"{impact['leverage_after'] - impact['leverage_before']:+.2f}x",
            delta_color="inverse",
            help=f"下單前 {impact['leverage_before']:.2f}x｜上限 {rules.target_leverage:.2f}x",
        )
        ic2.metric(
            "總 Beta",
            f"{impact['total_beta_after']:.2f}",
            delta=f"{impact['total_beta_after'] - impact['total_beta_before']:+.2f}",
            delta_color="off",
            help=f"下單前 {impact['total_beta_before']:.2f}",
        )
        ic3.metric(
            "台股 Beta",
            f"{impact['tw_beta_after']:.2f}",
            delta=f"{impact['tw_beta_after'] - impact['tw_beta_before']:+.2f}",
            delta_color="off",
            help=f"下單前 {impact['tw_beta_before']:.2f}",
        )
        eq_after = impact.get("equity_ratio_after")
        eq_before = impact.get("equity_ratio_before")
        val_str = f"{eq_after:.1f}%" if eq_after is not None else "—"
        delta_str = f"{eq_after - eq_before:+.1f}%" if (eq_after is not None and eq_before is not None) else None
        ic4.metric(
            "權益比率（權益數 ÷ 原始保證金）",
            val_str,
            delta=delta_str,
            delta_color="off",
            help=(
                f"下單後 權益數 NT$ {impact.get('equity_after', 0.0):,.0f} ÷ 原始保證金 NT$ {impact.get('initial_margin_after', 0.0):,.0f}"
                f"｜下單前 NT$ {impact.get('equity_before', 0.0):,.0f} ÷ NT$ {impact.get('initial_margin_before', 0.0):,.0f}"
                f"｜權益數來源：{impact.get('equity_source', '')}"
            ),
        )

        if not impact["margin_sufficient"]:
            st.error(f"可動用保證金 NT$ {impact['excess_margin_before']:,.0f} 不夠，還差 NT$ {impact['margin_shortfall']:,.0f}。")
        else:
            st.caption(f"下單前可動用保證金 NT$ {impact['excess_margin_before']:,.0f}。")

        if impact["leverage_after"] > rules.target_leverage:
            st.warning(f"下單後槓桿 {impact['leverage_after']:.2f}x 超過上限 {rules.target_leverage:.2f}x。")

    st.caption("槓桿與 Beta 用第四頁同一套估價（finalize_valuation）重算；權益比率 = 權益數（元大本日餘額＋未平倉損益）÷ 原始保證金（每口值來自 Futures_Ledger，本商品用上面的欄位）。名目曝險 = 口數 × 價格 × 乘數（TX 200、MTX 50、FMF／TMF 10）。")

    if lots > 0 and impact is not None:
        return f"期貨：{side_str} {product} {lots} 口 @ {price:,.0f}（保證金 NT$ {trial['margin_needed']:,.0f}，槓桿 {impact['leverage_before']:.2f}x → {impact['leverage_after']:.2f}x）"
    return None


AI_STRESS_STATE_KEY = "ai_memo_p4"


def _render_ai_stress(snapshot, send_to_telegram) -> None:
    """AI 避險基金經理人壓力測試（原第四頁），與第四頁同一套數字與提示詞。按鈕按下才呼叫 AI。"""
    from ai_advice import generate_risk_manager_advice, page4_advice_figures
    from app_secrets import read_secret

    st.subheader("🤖 AI 避險基金經理人壓力測試與風控審查")
    memo = st.session_state.setdefault(AI_STRESS_STATE_KEY, {"text": "", "time": ""})
    if st.button("🧠 啟動 AI 經理人風控與壓力測試", key="oa_ai_stress", width="stretch"):
        with st.spinner("AI 經理人正在分析中..."):
            try:
                nav, leverage, beta, cash_pct = page4_advice_figures(snapshot.metrics)
                gemini_key = os.environ.get("GEMINI_API_KEY") or read_secret("", "GEMINI_API_KEY")
                memo["text"] = generate_risk_manager_advice(snapshot.valued, nav, leverage, beta, cash_pct, api_key=gemini_key)
                memo["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as exc:
                st.error(f"AI 呼叫失敗：{_public_error(exc)}")
    if memo.get("text"):
        st.info(f"📌 **最新風控審查（生成時間：{memo['time']}）**")
        st.markdown(memo["text"])
        if st.button("📤 推播此風控審查報告到 Telegram", key="btn_tg_p4", type="primary"):
            send_to_telegram("AI 避險基金經理人風控審查報告", memo["text"])


def render_order_assistant(send_to_telegram, load_sheets, beta_fn, load_market=None, clear_caches=None):
    view = cached_macro_cycle_view()
    if st.session_state.get("oa_apply_regime_defaults"):
        st.session_state["oa_target_leverage"] = float(view["risk"]["target_leverage"])
        st.session_state["oa_min_cash_pct"] = float(view["risk"]["min_cash_pct"])
        st.session_state["oa_apply_regime_defaults"] = False
    _init_rule_state(view)
    st.title("第六頁：下單輔助系統")
    st.caption("參數來自：" + ("手動" if _regime_overridden(view) else "牛熊等級"))
    st.caption("下單前先看這一筆對整個部位的影響：槓桿、現金、Beta、保證金與壓力淨值。只做規劃，不會送出任何委託。")

    on_cloud = bool(os.environ.get("K_SERVICE"))
    has_key = bool(os.environ.get("GCP_KEY_JSON") or os.environ.get("GOOGLE_SERVICE_KEY_JSON"))
    overrides = dict(st.session_state.get(BETA_FALLBACK_STATE_KEY) or {})
    options = ["線上持倉（與第四頁同一份試算表）", "示範持倉（離線試算）"]
    default_index = 0 if (on_cloud or has_key) else 1
    source = st.radio("資料來源", options, index=default_index, horizontal=True, key="oa_source")
    render_refresh_bar(
        "p6",
        clear_fns=(
            getattr(load_sheets, "clear", None),
            getattr(beta_fn, "clear", None),
            getattr(getattr(beta_fn, "info", None), "clear", None),
            clear_caches,
            _cached_rs.clear,
            _cached_taiex_vix.clear,
            _cached_us_vix.clear,
            _cached_us_index.clear,
            us_hedge.clear_cache,
        ),
        session_keys=(),
    )

    using_sample = source.startswith("示範")
    if not using_sample:
        try:
            with st.spinner("正在用第四頁同一套算法讀持倉..."):
                snapshot = _load_live(load_sheets, beta_fn, load_market, beta_fallbacks=overrides)
            st.caption(f"線上持倉。美元／台幣 {snapshot.usdtwd:.2f}，台指 {snapshot.twii:,.0f}。槓桿定義與第四頁相同。")
        except Exception as exc:
            st.warning(_public_error(exc))
            snapshot = _with_beta_fallbacks(build_sample_snapshot(), overrides)
            using_sample = True
    else:
        snapshot = _with_beta_fallbacks(build_sample_snapshot(), overrides)

    if using_sample:
        st.info("現在是示範持倉（台股、美股、SOXL／00663L、元大期貨），用來熟悉流程。數字不是你的真實帳戶，也不會寫回 Google 試算表。")

    _render_rule_editor(view)
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
    leaps_note = None
    futures_note = None
    hedge_note = None

    (
        tab_health,
        tab_plan,
        tab_order,
        tab_leaps,
        tab_futures,
        tab_hedge,
        tab_stress,
        tab_export,
    ) = st.tabs(
        [
            "現況健檢",
            "調倉建議",
            "單筆下單試算",
            "LEAPS 試算",
            "期貨下單試算",
            "選擇權避險",
            "壓力測試",
            "檢查清單",
        ]
    )

    with tab_health:
        # 淨值／槓桿／Beta／權益比率的完整數字只放第四頁「風險與績效」，這裡留一行摘要。
        st.info(page6_health_line(snapshot.metrics, rules.target_leverage))
        st.subheader("處置優先順序")
        _priority_table(priority_list(snapshot, rules))
        st.caption(
            f"經濟槓桿 {economic_leverage(snapshot):.2f}x：股票沿用第四頁曝險，臺指期改以指數 × 乘數 × 口數。"
            "合併槓桿則與第四頁完全相同。"
        )
        _render_holding_tags(snapshot, view)
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
        plan = _fragment_plan(snapshot, rules)

    with tab_order:
        st.subheader("單筆下單試算")
        order_note = _render_order(snapshot, rules)

    with tab_leaps:
        st.subheader("LEAPS Call 試算")
        leaps_note = _render_leaps_calc(snapshot)

    with tab_futures:
        st.subheader("期貨下單試算")
        futures_note = _render_futures_calc(snapshot, rules)

    with tab_hedge:
        st.subheader("選擇權避險試算")
        hedge_note = _render_hedge(snapshot, rules)
        st.divider()
        us_hedge_note = _render_us_hedge(snapshot, rules, beta_fn=beta_fn)
        if us_hedge_note:
            hedge_note = "\n".join(n for n in (hedge_note, us_hedge_note) if n)
        st.divider()
        st.subheader("避險預算與效果模擬（台指期／TXO，只避台股）")
        from hedge_sim_ui import render_hedge_strategy_sim

        render_hedge_strategy_sim(snapshot, view)

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
        st.divider()
        _render_ai_stress(snapshot, send_to_telegram)

    with tab_export:
        st.subheader("檢查清單與 Telegram")
        st.caption("按鈕按下去才會送。沒有按就只留在畫面上。")
        combined_order_note = " ／ ".join(n for n in (order_note, leaps_note, futures_note) if n) or None
        text = build_checklist_text(
            snapshot,
            rules,
            flags,
            plan=plan,
            order_note=combined_order_note,
            hedge_note=hedge_note,
            addback_text=st.session_state.get("oa_addback_text"),
        )
        st.text_area("摘要", value=text, height=320)
        st.download_button("下載檢查清單", data=text.encode("utf-8"), file_name="下單輔助檢查清單.txt", mime="text/plain")
        if st.button("把這份摘要推到 Telegram", type="primary"):
            payload = text if len(text) <= 3500 else text[:3500] + "\n…（後段省略）"
            send_to_telegram("下單輔助試算", payload)

    render_nav_scenario_simulator(snapshot, float(rules.target_leverage))
