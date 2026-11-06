"""第六頁底部的策略模擬器。計算在 nav_scenario_sim，按按鈕才跑。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from nav_scenario_sim import (
    DEFAULT_SCENARIOS,
    GROUP_LABELS,
    GROUPS,
    N_DAYS,
    N_PATHS,
    SEED,
    TRIGGER_LABELS,
    TRIGGERS,
    Scenario,
    Strategy,
    build_book,
    calibrate_market,
    cash_basis_text,
    make_figure,
    result_frame,
    scenario_title,
    simulate,
    sleeve_rows,
    strategy_hold,
)

_TRIGGER_OPTIONS = [TRIGGER_LABELS[key] for key in TRIGGERS]
_LABEL_TO_TRIGGER = {label: key for key, label in TRIGGER_LABELS.items()}


def _brake_release(pause: float, cut: float) -> float:
    """原型解除線是 -8%。降到 1x 的門檻若比這淺，就改放在門檻一半的位置。"""
    release = -0.08
    if cut < release < 0:
        return release
    return cut / 2.0


@st.fragment
def render_nav_scenario_simulator(snapshot, default_target: float):
    """接在下單輔助頁面最下方。頁面載入只顯示部位，不跑蒙地卡羅。"""
    st.markdown("---")
    st.subheader("淨值情境模擬（策略模擬器）")
    st.warning("示意模型，非預測。路徑是假設的台指／費半情境，不是對未來的預測。")
    st.caption(
        "三個情境共用固定亂數種子，同一情境裡的策略走同一組路徑。"
        f"每個情境 {N_PATHS} 條、{N_DAYS} 個交易日。按「開始模擬」才計算，結果留在這次工作階段。"
    )

    try:
        book = build_book(snapshot.valued, snapshot.metrics)
    except Exception as exc:
        st.error(f"讀不到持倉，模擬先停著。{exc}")
        return

    _show_book(book, default_target)
    strategies = _strategy_forms(default_target)
    weights, vol_mults = _scenario_controls()
    stamp = _stamp(strategies, weights, vol_mults, book.nav, book.gross_exposure)
    if st.button("開始模擬", type="primary", key="nss_run"):
        if any(item is None for item in strategies):
            st.error("有策略的參數還沒排好，修正上面的紅字再按一次。")
        else:
            _run(book, strategies, weights, vol_mults, stamp)

    payload = st.session_state.get("nss_payload")
    if not payload:
        st.caption("還沒模擬。調整完參數後按「開始模擬」。")
        return
    if payload.get("stamp") != stamp:
        st.info("參數或持倉已經改過。下面仍是上一次的結果，要更新再按一次「開始模擬」。")
    _show_payload(payload)


def _show_book(book, default_target: float):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("淨值", f"NT$ {book.nav:,.0f}")
    c2.metric("有效曝險", f"NT$ {book.gross_exposure:,.0f}", f"{book.leverage:.2f}x")
    c3.metric("報表現金", f"{book.reported_cash_pct:.1f}%", f"NT$ {book.reported_cash:,.0f}")
    c4.metric("模擬融資殘值", f"NT$ {book.cash_residual:,.0f}")
    cap = float(default_target)
    if cap < 1 or cap > 2:
        st.caption(f"這一頁的槓桿上限是 {cap:.2f}x。模擬的降槓目標限制在 1.0–2.0x，預設先用 {min(max(cap, 1.0), 2.0):.1f}x。")
    else:
        st.caption(f"策略 B 的預設降槓目標用這一頁目前的上限 {cap:.2f}x。")
    with st.expander("現金與曝險怎麼算（跟 /v1/portfolio 對過）", expanded=False):
        st.write(cash_basis_text(book))
        if book.notes:
            for note in book.notes:
                st.caption(note)
    rows = sleeve_rows(book)
    if rows:
        frame = pd.DataFrame(rows)
        st.dataframe(
            frame.style.format({"市值或名目": "NT$ {:,.0f}", "有效曝險": "NT$ {:,.0f}", "倍數": "{:.2f}x"}),
            width="stretch",
            hide_index=True,
        )
    st.caption("期貨列的是名目，不是保證金。2x／3x 列的是市值，有效曝險已乘倍數。選擇權列的有效曝險是 Delta。")


def _scenario_controls():
    st.markdown("**情境權重與波動倍數**")
    st.caption("六個月的台指／費半目標與原型相同，不能在這裡改。權重會自動加總成 100%。波動倍數 1 代表用校準波動。")
    weights = []
    vol_mults = []
    cols = st.columns(3)
    for idx, scenario in enumerate(DEFAULT_SCENARIOS):
        with cols[idx]:
            st.markdown(f"**{scenario.name}**")
            st.caption(f"台指 {scenario.tw_target:+.0%}　費半 {scenario.sox_target:+.0%}")
            weights.append(
                st.number_input(
                    "權重",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(scenario.weight * 100),
                    step=5.0,
                    key=f"nss_w{idx}",
                )
            )
            vol_mults.append(
                st.number_input(
                    "波動倍數",
                    min_value=0.2,
                    max_value=3.0,
                    value=float(scenario.vol_mult),
                    step=0.05,
                    key=f"nss_v{idx}",
                )
            )
    total = float(sum(weights))
    if total <= 0:
        st.error("三個權重不能都是 0。")
    else:
        shown = "、".join(f"{w / total:.0%}" for w in weights)
        st.caption("正規化之後的權重：" + shown)
    return weights, vol_mults


def _strategy_forms(default_target: float) -> list[Strategy | None]:
    count = st.number_input("策略組數", min_value=2, max_value=4, value=2, step=1, key="nss_count")
    target0 = float(np.clip(default_target, 1.0, 2.0))
    out = []
    for idx in range(int(count)):
        out.append(_one_strategy(idx, target0))
    return out


def _one_strategy(idx: int, target0: float) -> Strategy | None:
    preset_name = ("A 續抱", "B 停損回補", "C 自訂", "D 自訂")[idx]
    hold_default = idx == 0
    with st.expander(f"策略 {idx + 1}", expanded=idx < 2):
        name = st.text_input("名稱", value=preset_name, key=f"nss_s{idx}_name")
        hold = st.checkbox("續抱：不降槓、不回補、不避險、不煞車", value=hold_default, key=f"nss_s{idx}_hold")
        target = st.number_input(
            "降槓目標（倍）",
            min_value=1.0,
            max_value=2.0,
            value=target0,
            step=0.1,
            key=f"nss_s{idx}_target",
            disabled=hold,
        )
        st.caption("減碼順序由上到下。期貨減的是名目，ETF 減的是市值，曝險用倍數換算。")
        picked = []
        for rank, default_group in enumerate(GROUPS):
            picked.append(
                st.selectbox(
                    f"第 {rank + 1} 個減",
                    list(GROUPS),
                    index=list(GROUPS).index(default_group),
                    format_func=lambda key: GROUP_LABELS[key],
                    key=f"nss_s{idx}_p{rank}",
                    disabled=hold,
                )
            )
        trigger_key = f"nss_s{idx}_triggers"
        if trigger_key not in st.session_state:
            st.session_state[trigger_key] = list(_TRIGGER_OPTIONS)
        chosen = st.multiselect(
            "回補條件（依勾選順序對到各批）",
            _TRIGGER_OPTIONS,
            key=trigger_key,
            disabled=hold,
        )
        batches = st.number_input(
            "回補批數",
            min_value=1,
            max_value=6,
            value=3,
            step=1,
            key=f"nss_s{idx}_batches",
            disabled=hold,
        )
        s1, s2, s3 = st.columns(3)
        stop_stock = s1.number_input("個股停損 %", min_value=1.0, max_value=40.0, value=8.0, step=1.0, key=f"nss_s{idx}_ss", disabled=hold)
        stop_2x = s2.number_input("2x 停損 %", min_value=1.0, max_value=40.0, value=8.0, step=1.0, key=f"nss_s{idx}_s2", disabled=hold)
        stop_3x = s3.number_input("3x 停損 %", min_value=1.0, max_value=40.0, value=8.0, step=1.0, key=f"nss_s{idx}_s3", disabled=hold)
        hedge = st.checkbox("放 TXO 賣權價差（買較高履約、賣更低履約）", value=False, key=f"nss_s{idx}_hedge", disabled=hold)
        hedge_ratio = st.number_input(
            "避險比例（佔目前台股多頭曝險 %）",
            min_value=0.0,
            max_value=100.0,
            value=0.0,
            step=5.0,
            key=f"nss_s{idx}_hr",
            disabled=hold or not hedge,
        )
        h1, h2 = st.columns(2)
        long_k = h1.number_input("買進履約（相對現貨）", min_value=0.5, max_value=1.2, value=0.95, step=0.01, key=f"nss_s{idx}_k1", disabled=hold or not hedge)
        short_k = h2.number_input("賣出更低履約", min_value=0.3, max_value=1.1, value=0.85, step=0.01, key=f"nss_s{idx}_k2", disabled=hold or not hedge)
        brake = st.checkbox("回撤煞車", value=True, key=f"nss_s{idx}_brake", disabled=hold)
        b1, b2 = st.columns(2)
        pause = b1.number_input(
            "停新倉回撤 %",
            min_value=1.0,
            max_value=40.0,
            value=-float(Strategy.brake_pause) * 100.0,
            step=1.0,
            key=f"nss_s{idx}_pause",
            disabled=hold or not brake,
        )
        cut = b2.number_input(
            "降到 1x 的回撤 %",
            min_value=2.0,
            max_value=50.0,
            value=-float(Strategy.brake_cut) * 100.0,
            step=1.0,
            key=f"nss_s{idx}_cut",
            disabled=hold or not brake,
        )
        st.caption(
            f"停新倉預設 {Strategy.brake_pause:.0%}（黃燈）、降到 1x 預設 {Strategy.brake_cut:.0%}（紅燈），與第四頁回撤煞車燈相同。"
            f"硬煞車在回撤回到 {Strategy.brake_release:.0%} 以內後解除。"
            f"若「降到 1x」設得比 {-Strategy.brake_release:.0%} 淺，解除線會改放在該門檻的一半。"
            "每批停損用的是該批自己的損益。"
        )

    if hold:
        try:
            return strategy_hold(name.strip() or preset_name)
        except ValueError as exc:
            st.error(str(exc))
            return None
    if len(set(picked)) != 4:
        st.error(f"{name or preset_name} 的減碼順序有重複，四個都要各出現一次。")
        return None
    if not chosen:
        st.error(f"{name or preset_name} 至少要選一個回補條件。")
        return None
    try:
        return Strategy(
            name=name.strip() or preset_name,
            hold=False,
            target_leverage=float(target),
            reduce_priority=tuple(picked),
            triggers=tuple(_LABEL_TO_TRIGGER[item] for item in chosen),
            n_batches=int(batches),
            stop_stock=-float(stop_stock) / 100.0,
            stop_2x=-float(stop_2x) / 100.0,
            stop_3x=-float(stop_3x) / 100.0,
            hedge_enabled=bool(hedge),
            hedge_ratio=float(hedge_ratio) / 100.0,
            hedge_long_strike=float(long_k),
            hedge_short_strike=float(short_k),
            brake_enabled=bool(brake),
            brake_pause=-float(pause) / 100.0,
            brake_cut=-float(cut) / 100.0,
            brake_release=_brake_release(-float(pause) / 100.0, -float(cut) / 100.0),
        )
    except ValueError as exc:
        st.error(str(exc))
        return None


def _stamp(strategies, weights, vol_mults, nav, exposure) -> tuple:
    packed = []
    for item in strategies:
        if item is None:
            packed.append(None)
        else:
            packed.append(
                (
                    item.name,
                    item.hold,
                    round(item.target_leverage, 4),
                    item.reduce_priority,
                    item.triggers,
                    item.n_batches,
                    round(item.stop_stock, 4),
                    round(item.stop_2x, 4),
                    round(item.stop_3x, 4),
                    item.hedge_enabled,
                    round(item.hedge_ratio, 4),
                    round(item.hedge_long_strike, 4),
                    round(item.hedge_short_strike, 4),
                    item.brake_enabled,
                    round(item.brake_pause, 4),
                    round(item.brake_cut, 4),
                )
            )
    return (tuple(packed), tuple(round(w, 4) for w in weights), tuple(round(v, 4) for v in vol_mults), round(nav, 2), round(exposure, 2))


def _run(book, strategies, weights, vol_mults, stamp):
    scenarios = tuple(
        Scenario(base.name, base.tw_target, base.sox_target, float(vol), float(weight))
        for base, vol, weight in zip(DEFAULT_SCENARIOS, vol_mults, weights)
    )
    try:
        with st.spinner("正在模擬。同一情境的策略共用一組路徑。"):
            market = calibrate_market()
            result = simulate(
                book,
                list(strategies),
                market=market,
                scenarios=scenarios,
                weights=weights,
                n_paths=N_PATHS,
                n_days=N_DAYS,
                seed=SEED,
            )
    except Exception as exc:
        st.error(f"模擬沒有跑完。{exc}")
        return
    st.session_state["nss_payload"] = {
        "stamp": stamp,
        "frame": result_frame(result),
        "quantiles": result.quantiles,
        "scenario_names": result.scenario_names,
        "strategy_names": result.strategy_names,
        "weights": result.weights,
        "nav0": result.nav0,
        "n_paths": result.n_paths,
        "n_days": result.n_days,
        "market_note": result.market_note,
        "opening_leverage": result.opening_leverage,
        "opening_nav": result.opening_nav,
        "scenarios": scenarios,
    }


def _show_payload(payload: dict):
    st.caption(payload["market_note"])
    bits = [
        f"{name} 開盤槓桿 {payload['opening_leverage'][name]:.2f}x、開盤淨值 NT$ {payload['opening_nav'][name]:,.0f}"
        for name in payload["strategy_names"]
    ]
    st.caption("；".join(bits) + "。追繳風險是示意條件：台指自起點跌逾 10%，而且淨曝險仍高於淨值的 2 倍。")
    # 用存下來的分位數重畫，換頁回來不用再模擬。
    from nav_scenario_sim import SimResult

    ghost = SimResult(
        scenario_names=list(payload["scenario_names"]),
        strategy_names=list(payload["strategy_names"]),
        weights=list(payload["weights"]),
        stats={},
        weighted={},
        quantiles=payload["quantiles"],
        nav0=float(payload["nav0"]),
        opening_leverage=payload["opening_leverage"],
        opening_nav=payload["opening_nav"],
        market_note=payload["market_note"],
        n_paths=int(payload["n_paths"]),
        n_days=int(payload["n_days"]),
        seed=SEED,
    )
    fig = make_figure(ghost, payload["scenarios"])
    st.pyplot(fig, clear_figure=True)
    st.caption("色帶是 10–90 百分位，實線是中位數。灰虛線是模擬前的淨值。圖例在圖的下方。")
    frame = payload["frame"]
    st.dataframe(
        frame.style.format(
            {
                "期末淨值中位數": "NT$ {:,.0f}",
                "最差一成(P10)": "NT$ {:,.0f}",
                "中位最大回撤": "{:.1%}",
                "觸及-20%機率": "{:.1%}",
                "追繳風險機率": "{:.1%}",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption("最後幾列是三個情境按權重混合之後的結果。示意模型，非預測。")
