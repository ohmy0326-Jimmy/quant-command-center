"""第六頁策略模擬器裡的「避險策略模擬」面板。

提供四種避險工具的歷史回測比較（無避險、觸發式期貨、月滾賣權、賣權價差），
以及以加權指數現況與第四頁同口徑 Beta 曝險為基礎的目前避險建議與觸發燈號。
只給建議，不下單。
"""

from __future__ import annotations

import datetime
import math
import re

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import taifex_quotes
from hedge_sim import (
    ASSUMPTIONS,
    DEFAULT_TOOL,
    EVENT_WINDOWS,
    LIMITATIONS,
    TOOL_LABELS,
    TOOLS,
    TRIGGER_LABELS,
    TRIGGERS,
    HedgeParams,
    drawdown,
    format_quote,
    recommend_hedge,
    simulate,
    trigger_status,
)
from nav_scenario_sim import _COLORS, _setup_cjk_font

_MONTHLY_RE = re.compile(r"^\d{6}$")


def pick_txo_expiry(
    expiries: list[str] | tuple[str, ...],
    expiry_date_fn,
    today: datetime.date,
    tenor_days: int,
) -> tuple[str | None, datetime.date | None]:
    """在月選擇權（6 碼年月）中挑選到期日距離 today 最接近 tenor_days 的合約。

    略過過期（<= today）或無到期日者；天數差距相同時選較早到期的合約。
    若無可用合約回傳 (None, None)。
    """
    candidates: list[tuple[int, datetime.date, str]] = []
    for exp in expiries or []:
        if not _MONTHLY_RE.match(str(exp)):
            continue
        try:
            d = expiry_date_fn(exp)
        except Exception:
            d = None
        if d is None or d <= today:
            continue
        diff_target = abs((d - today).days - tenor_days)
        candidates.append((diff_target, d, str(exp)))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], item[1]))
    _, best_date, best_code = candidates[0]
    return best_code, best_date


def light_text(value: bool | None) -> str:
    """觸發燈號轉繁體中文標籤。"""
    if value is True:
        return "🔴 成立"
    if value is False:
        return "🟢 未成立"
    return "⚪ 無資料"


def recommendation_rows(rec: dict) -> list[dict]:
    """將 recommend_hedge 的 options legs 轉為表格呈現列。"""
    rows = []
    for leg in rec.get("legs", []):
        rows.append(
            {
                "腳位": "買進賣權" if leg.get("is_long") else "賣出賣權",
                "履約價": int(round(leg.get("strike", 0))),
                "即時報價": format_quote(leg.get("quote")),
                "BS 估計（點）": round(float(leg.get("model_premium", 0.0)), 1),
                "採用（點）": round(float(leg.get("used_premium", 0.0)), 1),
                "來源": leg.get("source", ""),
            }
        )
    return rows


def make_nav_figure(result: dict, tools: tuple[str, ...] | list[str]) -> plt.Figure:
    """繪製淨值走勢與回撤曲線（兩列），標註歷史壓力事件。"""
    _setup_cjk_font()
    fig, (ax_top, ax_bot) = plt.subplots(
        2,
        1,
        figsize=(12, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    nav_map = result.get("nav", {})
    first_tool = tools[0] if tools else DEFAULT_TOOL
    base_dates = nav_map[first_tool].index if first_tool in nav_map else None

    for idx, tool in enumerate(tools):
        series = nav_map.get(tool)
        if series is None or len(series) == 0:
            continue
        color = _COLORS[TOOLS.index(tool) % len(_COLORS)] if tool in TOOLS else _COLORS[idx % len(_COLORS)]
        label = TOOL_LABELS.get(tool, tool)
        ax_top.plot(series.index, series.values / 1e6, label=label, color=color, lw=1.6)

        dd = drawdown(series) * 100.0
        ax_bot.plot(dd.index, dd.values, label=label, color=color, lw=1.2)

    ax_top.set_yscale("log")
    from matplotlib.ticker import FuncFormatter

    plain = FuncFormatter(lambda y, _pos: f"{y:g}")
    ax_top.yaxis.set_major_formatter(plain)
    ax_top.yaxis.set_minor_formatter(plain)
    ax_top.set_ylabel("淨值（百萬 NTD，對數）")
    ax_top.grid(True, which="both", ls=":", alpha=0.5)

    ax_bot.set_ylabel("回撤 (%)")
    ax_bot.axhline(-20.0, ls="--", color="black", lw=1.0, alpha=0.7)
    ax_bot.grid(True, ls=":", alpha=0.5)

    if base_dates is not None and len(base_dates) > 0:
        d_min, d_max = base_dates[0], base_dates[-1]
        for start_s, end_s, name in EVENT_WINDOWS:
            t0 = pd.to_datetime(start_s)
            t1 = pd.to_datetime(end_s)
            if t1 < d_min or t0 > d_max:
                continue
            ax_top.axvspan(t0, t1, color="red", alpha=0.12)
            ax_bot.axvspan(t0, t1, color="red", alpha=0.12)
            mid = t0 + (t1 - t0) / 2
            if d_min <= mid <= d_max:
                ax_top.text(
                    mid,
                    0.98,
                    name,
                    transform=ax_top.get_xaxis_transform(),
                    ha="center",
                    va="top",
                    fontsize=8,
                    color="#880000",
                    alpha=0.8,
                )

    fig.suptitle("避險策略模擬（歷史回測）— 示意，非預測", fontsize=14, y=0.98)
    handles, labels = ax_top.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=min(4, len(tools)), frameon=False)
    fig.tight_layout(rect=(0, 0.06, 1, 0.95))
    return fig


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_taiex_history(start: str = "2015-01-01") -> pd.Series:
    """載入加權指數歷史日收盤價。"""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("未安裝 yfinance，無法載入加權指數歷史。") from exc

    for ticker in ("^TWII", "0050.TW"):
        try:
            raw = yf.download(ticker, start=start, progress=False, auto_adjust=True)
            if raw is None or len(raw) == 0:
                continue
            close = raw["Close"].squeeze().dropna()
            if len(close) >= 500:
                s = close.astype(float)
                if getattr(s.index, "tz", None) is not None:
                    s.index = s.index.tz_localize(None)
                s.name = ticker
                return s
        except Exception:
            continue
    raise RuntimeError("讀不到加權指數歷史")


@st.cache_data(ttl=60, show_spinner=False)
def txo_put_quote(strike: float, expiry: str) -> float | None:
    """抓取 TXO 賣權即時報價，失敗或無效時回傳 None。"""
    try:
        q = taifex_quotes.get_option_quote(float(strike), "P", expiry)
        p = getattr(q, "price", None)
        if p is not None:
            p = float(p)
            if math.isfinite(p) and p > 0:
                return p
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def txo_expiry(tenor_days: int, today_iso: str) -> tuple[str | None, str | None]:
    """計算距指定日期天數最接近的 TXO 月合約代碼與到期日 ISO 字串。"""
    try:
        d = datetime.date.fromisoformat(today_iso)
        expiries = taifex_quotes.list_option_expiries(cid="TXO")
        code, exp_d = pick_txo_expiry(expiries, taifex_quotes.option_expiry_date, d, tenor_days)
        return code, (exp_d.isoformat() if exp_d else None)
    except Exception:
        return None, None


def render_hedge_strategy_sim(snapshot, macro_view: dict | None = None) -> None:
    """在第六頁策略模擬器中渲染「避險策略模擬」面板。"""
    st.warning("歷史回測示意，非預測。這裡只給避險建議，不會下單。")
    with st.expander("模擬假設與限制", expanded=False):
        st.markdown("**核心假設：**")
        for item in ASSUMPTIONS:
            st.markdown(f"- {item}")
        st.markdown("**模型限制：**")
        for item in LIMITATIONS:
            st.markdown(f"- {item}")


    col_a1, col_a2, col_a3, col_a4 = st.columns(4)
    with col_a1:
        alloc_pct = st.number_input(
            "獲利提撥 %",
            min_value=0.0,
            max_value=100.0,
            value=10.0,
            step=1.0,
            key="hss_alloc_pct",
        )
    with col_a2:
        annual_cap_pct = st.number_input(
            "年度成本上限 %NAV",
            min_value=0.0,
            max_value=20.0,
            value=2.0,
            step=0.5,
            key="hss_annual_cap_pct",
        )
    with col_a3:
        cov_normal_pct = st.number_input(
            "正常覆蓋率 %",
            min_value=0.0,
            max_value=200.0,
            value=50.0,
            step=5.0,
            key="hss_cov_normal",
        )
    with col_a4:
        cov_alert_pct = st.number_input(
            "警戒覆蓋率 %",
            min_value=0.0,
            max_value=200.0,
            value=100.0,
            step=5.0,
            key="hss_cov_alert",
        )

    col_b1, col_b2, col_b3 = st.columns(3)
    with col_b1:
        otm_pct = st.number_input(
            "履約價 OTM %",
            min_value=0.0,
            max_value=30.0,
            value=5.0,
            step=1.0,
            key="hss_otm_pct",
        )
    with col_b2:
        spread_width_pct = st.number_input(
            "價差寬度 %",
            min_value=1.0,
            max_value=30.0,
            value=7.0,
            step=1.0,
            help="賣出更低履約 = OTM + 寬度",
            key="hss_spread_width",
        )
    with col_b3:
        tenor_days = st.slider(
            "到期天數",
            min_value=45,
            max_value=60,
            value=50,
            step=1,
            key="hss_tenor_days",
        )

    trigger_options = [TRIGGER_LABELS[t] for t in TRIGGERS]
    selected_trigger_labels = st.multiselect(
        "觸發條件（任一成立即警戒；收盤站回 20DMA 解除）",
        options=trigger_options,
        default=trigger_options,
        key="hss_triggers",
    )
    selected_triggers = tuple(t for t in TRIGGERS if TRIGGER_LABELS[t] in selected_trigger_labels)

    tool_options = [TOOL_LABELS[m] for m in TOOLS]
    selected_tool_labels = st.multiselect(
        "比較的工具",
        options=tool_options,
        default=tool_options,
        key="hss_tools",
    )
    selected_tools = tuple(m for m in TOOLS if TOOL_LABELS[m] in selected_tool_labels)

    rec_tool_label = st.radio(
        "建議用的工具",
        options=tool_options,
        index=TOOLS.index(DEFAULT_TOOL),
        horizontal=True,
        key="hss_rec_tool",
    )
    st.caption("預設期貨：回測裡最大回撤最低、成本最低。")
    chosen_rec_tool = next((k for k, v in TOOL_LABELS.items() if v == rec_tool_label), DEFAULT_TOOL)

    with st.expander("進階設定", expanded=False):
        c_adv1, c_adv2, c_adv3 = st.columns(3)
        with c_adv1:
            skew_vol_pt = st.number_input("偏斜加碼 vol 點", value=3.0, step=0.5, key="hss_skew_vol")
            fin_rate_pct = st.number_input("融資利率 %", value=3.0, step=0.25, key="hss_fin_rate")
        with c_adv2:
            futures_slip_pct = st.number_input(
                "期貨滑價 %",
                value=0.05,
                step=0.01,
                format="%.2f",
                key="hss_futures_slip",
            )
            tp_mult = st.number_input("獲利幾倍平半", value=3.0, step=0.5, key="hss_tp_mult")
        with c_adv3:
            sim_beta = st.number_input("回測 β", value=2.0, step=0.1, key="hss_beta")
            sim_lev = st.number_input("回測槓桿", value=1.5, step=0.1, key="hss_leverage")
            nav0 = st.number_input("起始淨值 NT$", value=3450000.0, step=50000.0, key="hss_nav0")

    try:
        params = HedgeParams(
            alloc_pct=alloc_pct / 100.0,
            annual_cap_pct=annual_cap_pct / 100.0,
            cov_normal=cov_normal_pct / 100.0,
            cov_alert=cov_alert_pct / 100.0,
            otm_pct=otm_pct / 100.0,
            spread_width_pct=spread_width_pct / 100.0,
            tenor_days=int(tenor_days),
            triggers=selected_triggers,
            skew_vol=skew_vol_pt / 100.0,
            fin_rate=fin_rate_pct / 100.0,
            futures_slip=futures_slip_pct / 100.0,
            take_profit_multiple=float(tp_mult),
            beta=float(sim_beta),
            leverage=float(sim_lev),
            nav0=float(nav0),
        )
    except ValueError as exc:
        st.error(f"參數不合法：{exc}")
        return

    px = None
    try:
        px = load_taiex_history()
    except Exception as exc:
        st.error(f"讀不到加權指數歷史，回測與燈號先停著。{exc}")

    # ===== 目前建議避險 =====
    st.markdown("---")
    st.markdown("**目前建議避險（以第四頁同口徑的 Beta 曝險計算）**")

    metrics_dict = getattr(snapshot, "metrics", {}) or {}
    nav_actual = float(metrics_dict.get("nav", 0.0) or 0.0)

    scope_choice = st.radio(
        "避險範圍",
        options=["合計（帳戶 β）", "只算台股"],
        index=0,
        horizontal=True,
        key="hss_scope",
    )
    st.caption("合計用帳戶 β（台股＋美股 Beta 曝險，同第四頁）；台指工具對美股部位只是交叉避險，美股另見「選擇權避險」分頁的 SPY／XSP。")
    if scope_choice == "只算台股":
        beta_exposure = float(metrics_dict.get("tw_beta_exposure", 0.0) or 0.0)
    else:
        beta_exposure = float(metrics_dict.get("beta_exposure", 0.0) or 0.0)

    if nav_actual <= 0:
        st.info("目前淨值為 0 或讀不到，暫時無法計算實際避險建議。")
    else:
        snap_twii = getattr(snapshot, "twii", None)
        if snap_twii and float(snap_twii) > 0:
            index_level = float(snap_twii)
        elif px is not None and len(px) > 0:
            index_level = float(px.iloc[-1])
        else:
            index_level = 0.0

        score_history = macro_view.get("history") if macro_view else None
        score_total = macro_view.get("total") if macro_view else None
        score_as_of = macro_view.get("score_updated_at") if macro_view else None

        status = None
        if px is not None and len(px) >= 60:
            try:
                status = trigger_status(
                    px,
                    params,
                    score_history=score_history,
                    score_total=score_total,
                    score_as_of=score_as_of,
                )
            except Exception as exc:
                st.caption(f"燈號計算失敗：{exc}")

        if status:
            col_l1, col_l2, col_l3 = st.columns(3)
            for col, key in zip((col_l1, col_l2, col_l3), TRIGGERS):
                with col:
                    st.metric(TRIGGER_LABELS[key], light_text(status["lights"].get(key)))
                    if key not in params.triggers:
                        st.caption("（未勾選）")

            fired = [TRIGGER_LABELS[k] for k in params.triggers if status["lights"].get(k) is True]
            if status["alert"]:
                st.error("警戒：" + "、".join(fired) + "。期貨工具改為放空避險；選擇權覆蓋率提高到警戒值。")
            else:
                release = "；收盤已站回 20DMA" if status.get("release") else ""
                st.success("正常：勾選的觸發條件都沒有成立" + release + "。期貨工具不動作；選擇權用正常覆蓋率。")

            c_val = f"{status['close']:,.0f}" if status["close"] is not None else "—"
            m20_val = f"{status['ma20']:,.0f}" if status["ma20"] is not None else "—"
            m60_val = f"{status['ma60']:,.0f}" if status["ma60"] is not None else "—"
            dd_val = f"{status['drawdown']:+.1%}" if status["drawdown"] is not None else "—"
            vol_val = f"{status['vol20']:.1%}" if status["vol20"] is not None else "—"
            st.caption(
                f"加權指數：{c_val} ｜ 20DMA：{m20_val} ｜ 60DMA：{m60_val} ｜ "
                f"回撤：{dd_val} ｜ 20日波動率：{vol_val}"
            )

        else:
            st.caption("沒有指數歷史，燈號無資料，先視為正常狀態。")

        col_bud1, col_bud2 = st.columns(2)
        annual_cap_val = params.annual_cap_pct * nav_actual
        with col_bud1:
            pool_balance = st.number_input(
                "避險資金池餘額 NT$",
                min_value=0.0,
                value=float(round(annual_cap_val)),
                step=10000.0,
                key="hss_pool_bal",
                help="試算表沒有資金池欄位，先用年度上限當預設，請改成實際餘額。",
            )
        with col_bud2:
            spent_ytd = st.number_input(
                "今年已用避險成本 NT$",
                min_value=0.0,
                value=0.0,
                step=5000.0,
                key="hss_spent_ytd",
            )

        txo_code, txo_exp_date = None, None
        if chosen_rec_tool in ("put", "spread"):
            txo_code, txo_exp_date = txo_expiry(params.tenor_days, datetime.date.today().isoformat())

        def _quote_fn(strike: float):
            if txo_code:
                return txo_put_quote(strike, txo_code)
            return None

        alert_active = status["alert"] if status else False
        vol20_active = status["vol20"] if status else 0.20

        try:
            rec = recommend_hedge(
                nav=nav_actual,
                beta_exposure=beta_exposure,
                index_level=index_level,
                vol20=vol20_active or 0.20,
                alert=alert_active,
                tool=chosen_rec_tool,
                params=params,
                pool_balance=float(pool_balance),
                spent_ytd=float(spent_ytd),
                quote_fn=_quote_fn if chosen_rec_tool in ("put", "spread") else None,
                strike_step=50,
            )
        except Exception as exc:
            st.error(f"避險建議計算失敗：{exc}")
            rec = None

        if rec:
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("帳戶 β", f"{rec['account_beta']:.2f}")
            col_m2.metric("建議覆蓋率", f"{rec['coverage']:.0%}")
            col_m3.metric("避險名目", f"NT$ {rec['hedge_notional']:,.0f}")

            lots_info = rec.get("lots_info", {})
            if chosen_rec_tool == "futures":
                mtx = lots_info.get("mtx", 0)
                tx = lots_info.get("tx", 0)
                col_m4.metric(
                    "期貨口數建議",
                    f"小台 {mtx} 口 / 大台 {tx} 口",
                    f"殘餘 β：小台 {lots_info.get('mtx_residual_beta', 0.0):.2f}／大台 {lots_info.get('tx_residual_beta', 0.0):.2f}",
                    delta_color="off",
                )
            elif chosen_rec_tool in ("put", "spread"):
                exp_label = f"{txo_code}（{txo_exp_date}）" if txo_code else "無報價（讀不到到期月份）"
                col_m4.metric("選擇權口數", f"{lots_info.get('lots', 0)} 口", exp_label)
            else:
                col_m4.metric("操作建議", "無避險")

            if chosen_rec_tool in ("put", "spread") and rec.get("legs"):
                rows = recommendation_rows(rec)
                if rows:
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                col_p1, col_p2 = st.columns(2)
                col_p1.metric("淨權利金點數", f"{rec['net_premium_points']:,.1f} 點")
                col_p2.metric("預估總權利金", f"NT$ {rec['total_cost']:,.0f}")
                st.caption(
                    "沒有即時報價的履約價顯示「無報價」，權利金改用 BS（20 日實現波動 + 偏斜加碼）估計。"
                )

            col_b1, col_b2, col_b3, col_b4 = st.columns(4)
            col_b1.metric("資金池餘額", f"NT$ {rec['pool_balance']:,.0f}")
            col_b2.metric("今年已用成本", f"NT$ {rec['spent_ytd']:,.0f}")
            col_b3.metric("今年剩餘上限", f"NT$ {rec['remaining_cap']:,.0f}")
            col_b4.metric("目前可用預算", f"NT$ {rec['available']:,.0f}")

            if chosen_rec_tool == "futures":
                st.caption(f"預估進出單次滑價成本約 NT$ {lots_info.get('est_slip_cost', 0.0):,.0f}。")
            elif chosen_rec_tool in ("put", "spread"):
                if rec["budget_ok"]:
                    st.success("預算足以全額覆蓋。")
                else:
                    st.warning(f"預算不足：最多可買 {rec['affordable_lots']} 口。")

            for note in rec.get("notes", []):
                st.caption(note)
            st.caption("只給建議，不下單。Beta 曝險已含現有期貨／選擇權部位。")

    # ===== 歷史回測 =====
    st.markdown("---")
    st.markdown("**避險工具歷史回測比較**")

    if not selected_tools:
        st.error("請至少選擇一種比較的避險工具。")
        return

    stamp = (
        params,
        tuple(selected_tools),
        str(px.index[-1]) if px is not None and len(px) > 0 else None,
    )

    if st.button("開始回測", type="primary", key="hss_run"):
        if px is None or len(px) < 80:
            st.error("歷史價格資料不足，無法回測。")
        else:
            with st.spinner("回測計算中..."):
                res = simulate(px, params, tools=selected_tools)
                st.session_state["hss_payload"] = {
                    "stamp": stamp,
                    "result": res,
                    "tools": selected_tools,
                }

    payload = st.session_state.get("hss_payload")
    if not payload:
        st.caption("還沒回測。調整完參數後按「開始回測」。")
    else:
        if payload.get("stamp") != stamp:
            st.info("參數已經改過。下面仍是上一次的結果，要更新再按一次「開始回測」。")

        res = payload["result"]
        tools_run = payload["tools"]
        fig = make_nav_figure(res, tools_run)
        st.pyplot(fig, clear_figure=True)

        tab = res.get("table")
        if tab is not None:
            format_rules = {
                "期末淨值": "NT$ {:,.0f}",
                "CAGR": "{:.1%}",
                "最大回撤": "{:.1%}",
                "最差月份": "{:.1%}",
                "累計避險成本": "NT$ {:,.0f}",
                "回撤超過20%月數": "{:d}",
            }
            styled = tab.style.format(
                {k: v for k, v in format_rules.items() if k in tab.columns}
            )
            st.dataframe(styled, width="stretch", hide_index=False)

        if px is not None and len(px) > 0:
            st.caption(
                f"資料來源：{getattr(px, 'name', '加權指數')} "
                f"（{px.index[0].strftime('%Y-%m-%d')} ~ {px.index[-1].strftime('%Y-%m-%d')}，"
                f"共 {len(px)} 個交易日）"
            )
