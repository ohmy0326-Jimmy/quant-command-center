from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

TAIPEI = ZoneInfo("Asia/Taipei")

PAGE5_SESSION_KEYS = ("macro_sheet_cache", "macro_auto_cache", "macro_book_cache")


def taipei_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc).astimezone(TAIPEI)
    return now.astimezone(TAIPEI)


def format_refresh_time(moment: datetime) -> str:
    aware = taipei_now(moment)
    return aware.strftime("%Y-%m-%d %H:%M:%S") + "（台北時間）"


def clear_session_keys(state, keys) -> list[str]:
    removed = []
    for k in keys:
        if k in state:
            try:
                del state[k]
                removed.append(k)
            except KeyError:
                pass
    return removed


def run_clear_functions(clear_fns) -> list[str]:
    errors: list[str] = []
    for fn in clear_fns:
        if fn is None:
            continue
        try:
            fn()
        except Exception as exc:
            errors.append(str(exc) or repr(exc))
    return errors


def refresh_page_data(
    state,
    stamp_key: str,
    clear_fns=(),
    session_keys=(),
    now: datetime | None = None,
    clear_all_cache: bool = True,
) -> list[str]:
    errors: list[str] = []
    if clear_all_cache:
        try:
            st.cache_data.clear()
        except Exception as exc:
            errors.append(str(exc) or repr(exc))
    errors.extend(run_clear_functions(clear_fns))
    clear_session_keys(state, session_keys)
    state[stamp_key] = taipei_now(now)
    return errors


def ensure_refresh_stamp(state, stamp_key: str, now: datetime | None = None) -> datetime:
    if stamp_key not in state or state[stamp_key] is None:
        state[stamp_key] = taipei_now(now)
    return state[stamp_key]


def render_refresh_bar(page_key: str, clear_fns=(), session_keys=()) -> None:
    col1, col2 = st.columns([1, 3])
    stamp_key = f"{page_key}_refreshed_at"
    err_key = f"{page_key}_refresh_errors"

    clicked = col1.button(
        "🔄 重新整理",
        key=f"{page_key}_refresh_btn",
        help="清除快取，重新讀取 Google 試算表與報價",
    )
    if clicked:
        errors = refresh_page_data(
            st.session_state,
            stamp_key,
            clear_fns=clear_fns,
            session_keys=session_keys,
        )
        if errors:
            st.session_state[err_key] = errors
        st.rerun()
    else:
        stamp = ensure_refresh_stamp(st.session_state, stamp_key)
        col2.caption(f"最後更新：{format_refresh_time(stamp)}")
        if err_key in st.session_state:
            stored_errors = st.session_state.pop(err_key)
            if stored_errors:
                col2.caption("部分快取沒有清掉：" + "; ".join(stored_errors))
