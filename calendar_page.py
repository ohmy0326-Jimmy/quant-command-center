"""第三頁的畫面。事件內容在 calendar_events，表格列在 calendar_view。

AI 與 Telegram 只在選好一則並按下按鈕後才交給呼叫端。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

import calendar_actions
import calendar_events
import calendar_view


def _prepare(model: dict) -> dict:
    prepared = dict(model)
    events = []
    for event in model.get("events") or []:
        item = dict(event)
        item["id"] = item.get("id") or calendar_events.event_public_id(item)
        item["fingerprint"] = item.get("fingerprint") or calendar_events.event_fingerprint(item)
        events.append(item)
    prepared["events"] = events
    return prepared


def _table(rows: list[dict], show_figures: bool):
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    soon = frame["近7日"].tolist()
    columns = ["日期", "事件", "市場", "類型", "確定度", "相關持股"]
    if show_figures:
        columns.append("數字")
    shown = frame[columns].rename(columns={"數字": "預估／前值／實際"})

    def paint(row):
        if soon[row.name]:
            return ["background-color: #FFF4CC"] * len(row)
        return [""] * len(row)

    return shown.style.apply(paint, axis=1)


def render_calendar(st, model: dict, *, memos=None, gemini_key: str = "", complete_analysis=None, send=None, today=None):
    if memos is None:
        memos = {}
    prepared = _prepare(model)
    today = today or datetime.now(calendar_events.TAIPEI).date()
    st.caption("觀察清單只留在這次工作階段，不寫入試算表。")
    filters = st.columns(3)
    market = filters[0].selectbox("市場", ["全部", "台", "美"], key="p3_filter_market")
    kind = filters[1].selectbox("類型", ["全部", "結算", "財報", "總經"], key="p3_filter_type")
    horizon_label = filters[2].selectbox("範圍", ["未來 90 天", "未來 30 天"], key="p3_filter_horizon")
    horizon_days = 30 if horizon_label.startswith("未來 30") else 90
    view = calendar_view.shape_calendar(prepared, today, market=market, kind=kind, horizon_days=horizon_days)
    caveats = view["caveats"]
    with st.expander(f"資料說明與缺漏（{caveats['count']} 項）", expanded=False):
        if not caveats["groups"]:
            st.caption("沒有缺漏說明。")
        for group in caveats["groups"]:
            st.markdown(f"**{group['title']}**")
            for item in group["items"]:
                st.caption(item)
    if not view["rows"]:
        st.info("這個篩選裡沒有事件。")
    else:
        st.caption("近 7 日的列以淺色標出。")
        st.dataframe(_table(view["rows"], view["show_figures"]), hide_index=True, width="stretch")
    for note in view["type_notes"]:
        st.caption(note)
    _render_selected_actions(st, view["events"], memos, gemini_key, complete_analysis, send)
    return view


def _render_selected_actions(st, events: list[dict], memos: dict, gemini_key: str, complete_analysis, send):
    st.caption("先選一則，再按 AI 解析或推播。不會自動執行。")
    if not events:
        return
    labels = {event["id"]: f"{event.get('when_text') or event.get('date') or ''} {event.get('title') or ''}".strip() for event in events}
    selected_id = st.selectbox("選一個事件", list(labels), format_func=lambda item: labels.get(item, item), key="p3_event")
    event = next((item for item in events if item["id"] == selected_id), events[0])
    left, right = st.columns(2)
    with left:
        if st.button("🧠 AI 解析", key="p3_ai"):
            _run_analysis(st, event, memos, gemini_key, complete_analysis)
    with right:
        if st.button("📲 推播TG", key="p3_tg"):
            _send_notice(st, event, send)
    status = calendar_actions.memo_status(memos.get(event["id"]), event)
    if status == "stale":
        st.warning("這則解析對應較早的事件內容，與現在選的這一則不一致。請重新按 AI 解析。舊內容不會推播。")
    elif status == "ready":
        memo = memos[event["id"]]
        st.info(f"戰術建議（時間：{memo.get('time') or '—'}）")
        st.markdown(memo["text"])
        if st.button("📤 推播此事件建議到 TG", key="p3_advice"):
            _send_advice(st, event, memo, send)


def _run_analysis(st, event: dict, memos: dict, gemini_key: str, complete_analysis):
    reason = calendar_actions.analysis_block_reason(event, gemini_key)
    if reason:
        st.warning(reason)
        return
    if complete_analysis is None:
        st.warning("AI 解析沒有接上，這次沒有呼叫。")
        return
    with st.spinner("AI 經理人正在生成戰術..."):
        try:
            text = complete_analysis(calendar_actions.analysis_prompt(event), gemini_key)
        except Exception as exc:
            st.error(f"生成失敗: {exc}")
            return
    if not str(text or "").strip():
        st.warning("解析未完成，沒有可保存的內容。")
        return
    memos[event["id"]] = {
        "text": str(text).strip(),
        "time": datetime.now(calendar_events.TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": event["fingerprint"],
    }


def _send_notice(st, event: dict, send):
    notice = calendar_actions.event_notice(event)
    if notice is None:
        st.warning("這個事件資料缺漏，沒有推播。")
    elif send is None:
        st.warning("Telegram 沒有接上，這次沒有發送。")
    else:
        send(notice[0], notice[1])


def _send_advice(st, event: dict, memo: dict, send):
    advice = calendar_actions.advice_notice(event, memo)
    if advice is None:
        st.warning("解析未完成，或已與這一列不符，沒有推播。")
    elif send is None:
        st.warning("Telegram 沒有接上，這次沒有發送。")
    else:
        send(advice[0], advice[1])
