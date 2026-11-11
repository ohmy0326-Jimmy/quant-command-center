"""第六頁「策略卡／劇情雷達」。句子與卡片都從 JSON 來，這裡只排版。"""

from __future__ import annotations

from datetime import date

import streamlit as st

from strategy_cards import (
    RadarContext,
    builtin_settlement_dates,
    evaluate_radar,
    hard_rule_banner,
    load_strategy_cards,
    portfolio_drawdown_pct,
    text_of,
)


def panel_title(deck: dict | None = None) -> str:
    deck = deck if deck is not None else _load_quietly()
    if not deck:
        return "strategy-radar"
    return str((deck.get("ui") or {}).get("panel_title") or "strategy-radar")


def render_strategy_radar(snapshot, nav_frame=None, *, compact: bool = False) -> None:
    try:
        deck = load_strategy_cards()
    except Exception as exc:
        st.error(str(exc))
        return
    ui = deck.get("ui") or {}
    if deck.get("errors"):
        st.caption("；".join(deck["errors"]))
    ctx = _context(snapshot, nav_frame, deck)
    rows = evaluate_radar(deck, ctx)
    st.warning(hard_rule_banner(deck))
    phrase = next((row["block_text"] for row in rows if row["block_text"]), "")
    if phrase:
        st.error(phrase)
    if compact:
        _light_line(rows, ui)
        return
    if _is_survival_present(rows):
        st.checkbox(str(ui.get("panic_urge_label") or "panic_urge"), key="radar_panic_urge")
    for row in rows:
        if row["mode"] == "mark":
            label = text_of(row["card"].get("trigger_zh")) or row["card"]["card_id"]
            st.checkbox(label, key=f"radar_mark_{row['card']['card_id']}")
    lights_only = st.checkbox(str(ui.get("compact_toggle") or "lights"), key="radar_lights_only")
    _light_line(rows, ui)
    if lights_only:
        return
    fields = ui.get("fields") or {}
    for row in rows:
        _card_panel(row, ui, fields)


def _context(snapshot, nav_frame, deck: dict) -> RadarContext:
    metrics = dict(getattr(snapshot, "metrics", None) or {})
    nav = metrics.get("nav")
    drawdown = None
    if nav is not None:
        try:
            drawdown = portfolio_drawdown_pct(nav_frame, float(nav))
        except Exception:
            drawdown = None
    ui = deck.get("ui") or {}
    marks = {}
    lights = {}
    for card in deck.get("cards") or []:
        card_id = card["card_id"]
        marks[card_id] = bool(st.session_state.get(f"radar_mark_{card_id}"))
        lights[card_id] = _stored_light(card_id, ui)
    return RadarContext(
        metrics=metrics or None,
        drawdown_pct=drawdown,
        panic_urge=bool(st.session_state.get("radar_panic_urge")),
        settlement_dates=builtin_settlement_dates(date.today()),
        today=date.today(),
        vix_series=_vix_histories(),
        manual_marks=marks,
        manual_lights=lights,
    )


def _card_panel(row: dict, ui: dict, fields: dict) -> None:
    card = row["card"]
    mark = _mark(row["light"], ui)
    name = text_of(card.get("title"))
    title = f"{mark} {card['card_id']} {row['block_text'] or name}".strip()
    with st.expander(title, expanded=False):
        if row["block_text"]:
            st.error(row["block_text"])
            if name:
                st.markdown(name)
        meta = " · ".join(
            part
            for part in (
                text_of(card.get("category")),
                text_of(card.get("status")),
                text_of(card.get("priority")),
            )
            if part
        )
        if meta:
            st.caption(meta)
        _field(fields.get("trigger") or "trigger", card.get("trigger_zh") or card.get("trigger_rules"))
        if card.get("trigger_zh") and card.get("trigger_rules"):
            st.caption(text_of(card.get("trigger_rules")))
        _field(fields.get("actions") or "actions", card.get("actions"))
        _field(fields.get("max_loss_rule") or "max_loss_rule", card.get("max_loss_rule"))
        _field(fields.get("data_sources") or "data_sources", card.get("data_sources"))
        _field(fields.get("notes") or "notes", card.get("notes"))
        notes_url = text_of(card.get("notes_url"))
        if notes_url:
            st.markdown(f"**{fields.get('notes_url') or 'notes_url'}**")
            st.markdown(notes_url)
        if row["reasons"]:
            st.caption(" ".join(row["reasons"]))
        if row["mode"] == "manual":
            _light_picker(card["card_id"], ui)
        if card.get("card_id") == "S7":
            _s7_panel()


def _s7_panel() -> None:
    st.caption("近 10 日 VIX 最高 > 35，且當天從該高點回落至少 15%。按按鈕才抓資料。")
    if st.button("列出 S7 歷史事件", key="s7_list_events"):
        st.session_state["s7_report"] = _load_s7_report()
    report = st.session_state.get("s7_report")
    if not report:
        return
    for block in report:
        st.markdown(f"**{block['market']}**")
        if not block.get("ok"):
            st.warning(block.get("note") or "資料不足")
            continue
        if block.get("note"):
            st.caption(block["note"])
        rows = []
        for event in block["events"]:
            row = {"日期": event["date"], "VIX": event["vix"], "高點": event["peak"]}
            for horizon, result in event["horizons"].items():
                row[f"{horizon}日報酬"] = result["return"]
                row[f"{horizon}日最大回撤"] = result["max_drawdown"]
                if result.get("note"):
                    row[f"{horizon}日"] = result["note"]
            rows.append(row)
        if not rows:
            st.caption(block.get("note") or "沒有符合條件的事件。")
        else:
            import pandas as pd

            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.caption("報酬與最大回撤是事件日收盤之後的交易日。不夠長的欄是空的，不當 0。")


def _load_s7_report() -> list[dict]:
    from s7_backtest import s7_events

    blocks = []
    us_vix, us_price, us_note = _yahoo_pair("^VIX", "^GSPC")
    us = s7_events(us_vix or [], us_price or [], market="美股 VIX / 標普")
    if us_note and not us["ok"]:
        us["note"] = us_note
    blocks.append(us)
    tw_vix, tw_note = _taiex_vix_rows()
    tw_price, price_note = _yahoo_rows("^TWII")
    tw = s7_events(tw_vix or [], tw_price or [], market="台指 VIX / 加權")
    if not tw["ok"]:
        tw["note"] = tw_note or price_note or tw["note"]
    blocks.append(tw)
    return blocks


def _yahoo_pair(vix_symbol: str, price_symbol: str):
    vix, vix_note = _yahoo_rows(vix_symbol)
    price, price_note = _yahoo_rows(price_symbol)
    note = vix_note or price_note or ""
    return vix, price, note


def _yahoo_rows(symbol: str):
    try:
        import yfinance as yf

        frame = yf.Ticker(symbol).history(period="10y", auto_adjust=True)
    except Exception:
        return None, f"{symbol} 抓取失敗，資料不足。"
    if frame is None or getattr(frame, "empty", True) or "Close" not in getattr(frame, "columns", []):
        return None, f"{symbol} 沒有收盤，資料不足。"
    rows = []
    for stamp, value in frame["Close"].dropna().items():
        day = getattr(stamp, "date", lambda: None)()
        if day is None or value != value:
            continue
        rows.append({"date": day, "close": float(value)})
    if not rows:
        return None, f"{symbol} 沒有收盤，資料不足。"
    return rows, ""


def _taiex_vix_rows():
    try:
        from datetime import date

        from taiex_vix import _fetch_month, _http_get, month_stamps

        found = {}
        for stamp in month_stamps(date.today(), 24):
            for day, _clock, value in _fetch_month(_http_get, stamp):
                found[day] = float(value)
    except Exception:
        return None, "台指 VIX 抓取失敗，資料不足。"
    if len(found) < 10:
        return None, "台指 VIX 不足 10 個交易日，資料不足。"
    return [{"date": day, "close": found[day]} for day in sorted(found)], ""


def _light_picker(card_id: str, ui: dict) -> None:
    labels, ids = _choice_table(ui)
    key = f"radar_light_label_{card_id}"
    if key not in st.session_state:
        current = st.session_state.get(f"radar_light_{card_id}", ids[0] if ids else "off")
        st.session_state[key] = labels[ids.index(current)] if current in ids else labels[0]
    st.selectbox(card_id, labels, key=key)


def _stored_light(card_id: str, ui: dict):
    labels, ids = _choice_table(ui)
    picked = st.session_state.get(f"radar_light_label_{card_id}")
    if picked in labels:
        return ids[labels.index(picked)]
    stored = st.session_state.get(f"radar_light_{card_id}")
    if stored in ids:
        return stored
    return None


def _choice_table(ui: dict) -> tuple[list[str], list[str]]:
    choices = ui.get("light_choices") or [{"id": item, "label": item} for item in ("off", "green", "yellow", "red")]
    labels = [str(item.get("label") or item.get("id")) for item in choices]
    ids = [str(item.get("id") or "off") for item in choices]
    return labels, ids


def _light_line(rows: list[dict], ui: dict) -> None:
    bits = []
    for row in rows:
        card = row["card"]
        mark = _mark(row["light"], ui)
        if row["block_text"]:
            bits.append(f"{mark} {card['card_id']} {row['block_text']}")
        else:
            bits.append(f"{mark} {card['card_id']} {text_of(card.get('title'))}".strip())
    st.caption("　".join(bits))


def _field(label: str, value) -> None:
    text = text_of(value)
    if not text:
        return
    st.markdown(f"**{label}**")
    st.write(text)


def _mark(light: str, ui: dict) -> str:
    marks = ui.get("light_marks") or {}
    return str(marks.get(light) or light)


def _is_survival_present(rows: list[dict]) -> bool:
    return any(row["mode"] == "auto" and "lev>regime_cap" in (row["card"].get("trigger_rules") or "") for row in rows)


def _vix_histories() -> list[list[float]] | None:
    found: list[list[float]] = []
    try:
        from us_hedge import fetch_us_vix

        quote = fetch_us_vix()
        if isinstance(quote, dict) and quote.get("ok") and quote.get("history"):
            found.append([float(value) for value in quote["history"]])
    except Exception:
        pass
    try:
        from taiex_vix import fetch_taiex_vix

        quote = fetch_taiex_vix()
        if isinstance(quote, dict) and quote.get("ok") and quote.get("history"):
            found.append([float(value) for value in quote["history"]])
    except Exception:
        pass
    return found or None


def _load_quietly() -> dict | None:
    try:
        return load_strategy_cards()
    except Exception:
        return None
