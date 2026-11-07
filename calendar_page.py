"""第三頁的畫面。事件內容在 calendar_events。

AI 與 Telegram 只在對應按鈕按下時交給呼叫端，這裡不主動發送。
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import calendar_actions
import calendar_events


def _updated(event: dict) -> str:
    moment = event.get("updated_at")
    if moment is None:
        return "—"
    try:
        return moment.astimezone(calendar_events.TAIPEI).strftime("%Y-%m-%d %H:%M 台北")
    except Exception:
        return "—"


def widget_key(prefix: str, event_id: str) -> str:
    digest = hashlib.sha1(str(event_id).encode("utf-8")).hexdigest()[:12]
    return f"p3_{prefix}_{digest}"


def render_calendar(st, model: dict, *, memos=None, gemini_key: str = "", complete_analysis=None, send=None):
    if memos is None:
        memos = {}
    st.caption("財報只查持倉與這次工作階段輸入的觀察清單。試算表沒有觀察清單欄位，這裡不寫入、也不填示例名單。")
    st.caption("結算日沒有逐月核對期交所公告。能對上休市表的才標規則推估，對不上的標未知。這批沒有「官方確認」。")
    for note in model.get("notes") or []:
        st.caption(note)
    if not model.get("symbols"):
        st.caption("目前沒有可查財報的公司代號。可以在上方輸入觀察清單，或等持倉讀到股票、個股期、有標的的可轉債。")
    for gap in model.get("gaps") or []:
        st.caption(f"缺漏：{gap}")
    events = model.get("events") or []
    st.caption("AI 解析與 Telegram 只在按下該列按鈕後執行。還沒完成解析的事件，不能推播建議。")
    if not events:
        st.info("這個區間沒有可列出的事件。")
    for event in events:
        event_id = event.get("id") or calendar_events.event_public_id(event)
        event["id"] = event_id
        event["fingerprint"] = event.get("fingerprint") or calendar_events.event_fingerprint(event)
        st.markdown(f"**{event.get('when_text') or '日期未知'}**　{event.get('title')}")
        st.caption(
            f"{event.get('certainty') or '未知'}｜{event.get('session') or '時段未知'}｜來源：{event.get('source') or '未知'}｜更新：{_updated(event)}"
        )
        st.caption(
            "預估 "
            + calendar_events.format_number(event.get("estimate"))
            + "｜前值 "
            + calendar_events.format_number(event.get("previous"))
            + "｜實際 "
            + calendar_events.format_number(event.get("actual"))
        )
        if event.get("note"):
            st.caption(event["note"])
        for line in event.get("impacts") or []:
            st.caption(line)
        _render_actions(st, event, memos, gemini_key, complete_analysis, send)
    with st.expander("這批沒有做的項目", expanded=False):
        for item in model.get("not_done") or []:
            st.caption(f"{item['name']}：{item['why']}")


def _render_actions(st, event: dict, memos: dict, gemini_key: str, complete_analysis, send):
    event_id = event["id"]
    left, right = st.columns(2)
    with left:
        if st.button("🧠 AI 解析", key=widget_key("ai", event_id)):
            reason = calendar_actions.analysis_block_reason(event, gemini_key)
            if reason:
                st.warning(reason)
            elif complete_analysis is None:
                st.warning("AI 解析沒有接上，這次沒有呼叫。")
            else:
                with st.spinner("AI 經理人正在生成戰術..."):
                    try:
                        text = complete_analysis(calendar_actions.analysis_prompt(event), gemini_key)
                    except Exception as exc:
                        st.error(f"生成失敗: {exc}")
                    else:
                        if not str(text or "").strip():
                            st.warning("解析未完成，沒有可保存的內容。")
                        else:
                            memos[event_id] = {
                                "text": str(text).strip(),
                                "time": datetime.now(calendar_events.TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
                                "fingerprint": event["fingerprint"],
                            }
    with right:
        if st.button("📲 推播TG", key=widget_key("tg", event_id)):
            notice = calendar_actions.event_notice(event)
            if notice is None:
                st.warning("這個事件資料缺漏，沒有推播。")
            elif send is None:
                st.warning("Telegram 沒有接上，這次沒有發送。")
            else:
                send(notice[0], notice[1])
    status = calendar_actions.memo_status(memos.get(event_id), event)
    if status == "stale":
        st.warning("這則解析對應較早的事件內容，與現在這一列不一致。請重新按 AI 解析。舊內容不會推播。")
    elif status == "ready":
        memo = memos[event_id]
        st.info(f"📌 **戰術建議（時間：{memo.get('time') or '—'}）**")
        st.markdown(memo["text"])
        if st.button("📤 推播此事件建議到 TG", key=widget_key("advice", event_id)):
            advice = calendar_actions.advice_notice(event, memo)
            if advice is None:
                st.warning("解析未完成，或已與這一列不符，沒有推播。")
            elif send is None:
                st.warning("Telegram 沒有接上，這次沒有發送。")
            else:
                send(advice[0], advice[1])
