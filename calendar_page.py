"""第三頁的畫面。事件內容在 calendar_events，這裡不抓網路、不寫試算表、不推播。"""

from __future__ import annotations

import calendar_events


def _updated(event: dict) -> str:
    moment = event.get("updated_at")
    if moment is None:
        return "—"
    try:
        return moment.astimezone(calendar_events.TAIPEI).strftime("%Y-%m-%d %H:%M 台北")
    except Exception:
        return "—"


def render_calendar(st, model: dict):
    st.caption("財報只查持倉與這次工作階段輸入的觀察清單。試算表沒有觀察清單欄位，這裡不寫入、也不填示例名單。")
    st.caption("結算日沒有逐月核對期交所公告。能對上休市表的才標規則推估，對不上的標未知。這批沒有「官方確認」。")
    for note in model.get("notes") or []:
        st.caption(note)
    if not model.get("symbols"):
        st.caption("目前沒有可查財報的公司代號。可以在上方輸入觀察清單，或等持倉讀到股票、個股期、有標的的可轉債。")
    for gap in model.get("gaps") or []:
        st.caption(f"缺漏：{gap}")
    events = model.get("events") or []
    if not events:
        st.info("這個區間沒有可列出的事件。")
    for event in events:
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
    with st.expander("這批沒有做的項目", expanded=False):
        for item in model.get("not_done") or []:
            st.caption(f"{item['name']}：{item['why']}")
