"""第二頁板塊表。價格相對強弱與進場檢查，不把漲跌說成資金流。

4 週＝20 個共同交易日，12 週＝60 個共同交易日。報酬＝區間最後收盤 ÷ 區間起點收盤 − 1。
相對強弱＝板塊報酬 − 基準（預設 S&P 500）報酬。日期只留兩邊都有收盤的交易日。
持平帶是程式預設：超額報酬絕對值小於 0.5 個百分點。這不是使用者逐條確認過的策略參數。

進場關卡只看價格結構與相對大盤連兩週。總經不擋進場：防禦板塊在總經偏弱時仍可進場，
總經只決定總槓桿上限（REGIME_RISK 不在這裡改）。任一關資料不夠就是未知，
未知不算通過，也不算不通過。不另加第三關。
"""

from __future__ import annotations

import math

import pandas as pd

from sector_map import industries_for_board

LOOKBACK_4W = 20
LOOKBACK_12W = 60
WEEK_DAYS = 5
# 程式預設：超額報酬落在這個帶內視為持平，不標成轉強或轉弱。
FLAT_ABS = 0.005
STRONG_RETURN = 0.10
STRONG_SESSIONS = 3
CROSS_WINDOW = 5

ATTACK = frozenset({"XLK", "XLY", "XLC", "XLF", "XLI", "XLB", "XLE"})
DEFENSE = frozenset({"XLP", "XLU", "XLV", "XLRE"})

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def close_series(frame) -> pd.Series:
    """收盤序列，去掉空值，索引收成日期。"""
    if frame is None:
        return pd.Series(dtype=float)
    if isinstance(frame, pd.Series):
        series = frame.copy()
    else:
        if getattr(frame, "empty", True) or "Close" not in frame.columns:
            return pd.Series(dtype=float)
        series = frame["Close"].copy()
    series = pd.to_numeric(series, errors="coerce").dropna()
    series = series[series > 0]
    if series.empty:
        return series
    index = pd.to_datetime(series.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)
    series.index = index.normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def align_prices(asset, benchmark) -> pd.DataFrame:
    """只留資產與基準都有收盤的日期。"""
    left = close_series(asset)
    right = close_series(benchmark)
    if left.empty or right.empty:
        return pd.DataFrame(columns=["asset", "bench"])
    frame = pd.concat([left.rename("asset"), right.rename("bench")], axis=1, join="inner").dropna()
    return frame.sort_index()


def _window_return(series: pd.Series, sessions: int) -> float | None:
    if len(series) < sessions + 1:
        return None
    start = _finite(series.iloc[-1 - sessions])
    end = _finite(series.iloc[-1])
    if start is None or end is None:
        return None
    return end / start - 1.0


def _direction(excess: float | None) -> str:
    if excess is None:
        return "未知"
    if abs(excess) < FLAT_ABS:
        return "持平"
    return "轉強" if excess > 0 else "轉弱"


def relative_strength(asset, benchmark) -> dict:
    aligned = align_prices(asset, benchmark)
    asset_px = aligned["asset"] if not aligned.empty else pd.Series(dtype=float)
    bench_px = aligned["bench"] if not aligned.empty else pd.Series(dtype=float)
    out = {"aligned_days": int(len(aligned)), "rows": {}}
    for label, sessions in (("4週", LOOKBACK_4W), ("12週", LOOKBACK_12W)):
        asset_ret = _window_return(asset_px, sessions)
        bench_ret = _window_return(bench_px, sessions)
        excess = None if asset_ret is None or bench_ret is None else asset_ret - bench_ret
        out["rows"][label] = {
            "asset_return": asset_ret,
            "bench_return": bench_ret,
            "excess": excess,
            "direction": _direction(excess),
        }
    return out


def role_of(ticker: str) -> str:
    key = str(ticker or "").upper()
    if key in ATTACK:
        return "攻擊"
    if key in DEFENSE:
        return "防禦"
    return "主題（不歸攻防）"


def stance_text(ticker: str, view: dict | None) -> str:
    """只顯示第五頁已經有的產業。對不到就寫未對應，不把漲跌說成加碼或減碼。"""
    names = industries_for_board(ticker)
    if not names:
        return "未對應第五頁產業"
    items = {item.get("name"): item for item in (view or {}).get("industries") or []}
    parts = []
    for industry in names:
        item = items.get(industry)
        if not item:
            parts.append(f"{industry}（第五頁沒有這列）")
            continue
        stance = item.get("stance") or "立場空白"
        text = f"{industry} · {stance}"
        if item.get("example"):
            text += "（示例）"
        parts.append(text)
    return "；".join(parts)


def _state_text(state: str) -> str:
    return {"pass": "通過", "fail": "未過", "unknown": "未知"}[state]


def _crossed_up(fast: pd.Series, slow: pd.Series) -> bool | None:
    if len(fast) < CROSS_WINDOW + 1 or len(slow) < CROSS_WINDOW + 1:
        return None
    for offset in range(1, CROSS_WINDOW + 1):
        prev_fast = _finite(fast.iloc[-1 - offset])
        prev_slow = _finite(slow.iloc[-1 - offset])
        now_fast = _finite(fast.iloc[-offset])
        now_slow = _finite(slow.iloc[-offset])
        if None in {prev_fast, prev_slow, now_fast, now_slow}:
            return None
        if prev_fast <= prev_slow and now_fast > now_slow:
            return True
    return False


def price_gate(frame) -> dict:
    """收盤站上 60 日與 200 日、近 5 個交易日內 20 日線上穿 60 日線、量大於 20 日均量。"""
    detail = (
        f"程式預設：上穿看最近 {CROSS_WINDOW} 個交易日。"
        "均線或成交量不夠就是未知，不算未過。"
    )
    if frame is None or getattr(frame, "empty", True):
        return {"name": "價格結構", "state": UNKNOWN, "detail": detail}
    if "Close" not in frame.columns or "Volume" not in frame.columns:
        return {"name": "價格結構", "state": UNKNOWN, "detail": detail}
    close = pd.to_numeric(frame["Close"], errors="coerce")
    volume = pd.to_numeric(frame["Volume"], errors="coerce")
    if len(close.dropna()) < 200 or len(volume.dropna()) < 20:
        return {"name": "價格結構", "state": UNKNOWN, "detail": detail}
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma200 = close.rolling(200).mean()
    vol20 = volume.rolling(20).mean()
    last_close = _finite(close.iloc[-1])
    last_ma60 = _finite(ma60.iloc[-1])
    last_ma200 = _finite(ma200.iloc[-1])
    last_vol = _finite(volume.iloc[-1])
    last_vol20 = _finite(vol20.iloc[-1])
    crossed = _crossed_up(ma20, ma60)
    if None in {last_close, last_ma60, last_ma200, last_vol, last_vol20} or crossed is None:
        return {"name": "價格結構", "state": UNKNOWN, "detail": detail}
    ok = last_close > last_ma60 and last_close > last_ma200 and last_vol > last_vol20 and crossed
    return {"name": "價格結構", "state": PASS if ok else FAIL, "detail": detail}


def _block_excess(aligned: pd.DataFrame, end: int, sessions: int = WEEK_DAYS) -> float | None:
    start = end - sessions
    if start < 0 or end >= len(aligned):
        return None
    asset0 = _finite(aligned["asset"].iloc[start])
    asset1 = _finite(aligned["asset"].iloc[end])
    bench0 = _finite(aligned["bench"].iloc[start])
    bench1 = _finite(aligned["bench"].iloc[end])
    if None in {asset0, asset1, bench0, bench1}:
        return None
    return (asset1 / asset0 - 1.0) - (bench1 / bench0 - 1.0)


def relative_gate(asset, benchmark) -> dict:
    """最近兩段各 5 個共同交易日的超額報酬都要為正。資料不夠是未知。"""
    detail = (
        "程式預設：一週＝5 個共同交易日。連 2 週＝最近兩段超額報酬都大於 0。"
        "持平或資料不夠都不是通過。這不是使用者逐條確認過的週曆。"
    )
    aligned = align_prices(asset, benchmark)
    if len(aligned) < WEEK_DAYS * 2 + 1:
        return {"name": "相對大盤連兩週", "state": UNKNOWN, "detail": detail}
    latest = _block_excess(aligned, len(aligned) - 1)
    prior = _block_excess(aligned, len(aligned) - 1 - WEEK_DAYS)
    if latest is None or prior is None:
        return {"name": "相對大盤連兩週", "state": UNKNOWN, "detail": detail}
    state = PASS if latest > 0 and prior > 0 else FAIL
    return {"name": "相對大盤連兩週", "state": state, "detail": detail}


def summarize_gates(gates: list[dict]) -> dict:
    passed = sum(1 for gate in gates if gate["state"] == PASS)
    failed = sum(1 for gate in gates if gate["state"] == FAIL)
    unknown_names = [gate["name"] for gate in gates if gate["state"] == UNKNOWN]
    unknown = len(unknown_names)
    text = f"通過 {passed}、未過 {failed}、未知 {unknown}。"
    if unknown_names:
        text += "未知是" + "、".join(unknown_names) + "。"
    text += "未知沒有算成通過或未過。"
    return {"passed": passed, "failed": failed, "unknown": unknown, "text": text, "gates": gates}


def evaluate_entry(frame, benchmark) -> dict:
    """進場只看價格結構與相對大盤。總經狀態不進這兩關。"""
    gates = [price_gate(frame), relative_gate(frame, benchmark)]
    summary = summarize_gates(gates)
    summary["advisory"] = "只輔助看盤，不擋下單。"
    return summary


def holdings_from_frame(frame) -> list[dict]:
    """真實前十大。空表回空清單，不補示例。"""
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for idx, row in frame.iterrows():
        symbol = str(idx).strip() if not isinstance(idx, tuple) else ""
        if not symbol or symbol.lower() == "nan":
            symbol = str(row.get("symbol") or row.get("Symbol") or "").strip()
        if not symbol:
            continue
        weight = None
        for column in frame.columns:
            if any(token in str(column).lower() for token in ("percent", "weight", "holding")):
                try:
                    raw = float(row[column])
                except (TypeError, ValueError):
                    raw = None
                if raw is not None and math.isfinite(raw):
                    weight = raw * 100 if raw <= 1 else raw
                break
        rows.append({"symbol": symbol, "weight": weight})
    rows.sort(key=lambda item: (item["weight"] is None, -(item["weight"] or 0)))
    return rows[:10]


def format_holdings(rows: list[dict]) -> str:
    if not rows:
        return "未取得真實持股"
    parts = []
    for index, item in enumerate(rows, start=1):
        if item["weight"] is None:
            parts.append(f"{index}. {item['symbol']}")
        else:
            parts.append(f"{index}. {item['symbol']} ({item['weight']:.1f}%)")
    return ", ".join(parts)


def strong_holdings(holdings: list[dict], closes: dict | None, sessions: int = STRONG_SESSIONS, threshold: float = STRONG_RETURN) -> dict:
    """三個交易日報酬達到門檻才列入。歷史不夠的略過，不當成漲 0。"""
    if not holdings:
        return {"rows": [], "skipped": [], "note": "沒有真實前十大，不列強勢股。"}
    closes = closes or {}
    kept = []
    skipped = []
    for item in holdings:
        symbol = item["symbol"]
        series = close_series(closes.get(symbol))
        change = _window_return(series, sessions)
        if change is None:
            skipped.append(symbol)
            continue
        if change >= threshold:
            kept.append({"symbol": symbol, "weight": item.get("weight"), "return": change})
    note = ""
    if skipped:
        note = "歷史不足、沒有列入（不是漲 0）：" + "、".join(skipped)
    if not kept and not note:
        note = f"真實持股裡沒有 {sessions} 個交易日漲幅達到 {threshold:.0%} 的股票。"
    return {"rows": kept, "skipped": skipped, "note": note}


def closes_from_download(raw, symbols) -> dict:
    """把 yfinance 下載結果收成代號 → 收盤。抓不到的代號是空序列，不是 0。"""
    symbols = tuple(symbols)
    out = {symbol: pd.Series(dtype=float) for symbol in symbols}
    if raw is None or getattr(raw, "empty", True):
        return out
    columns = raw.columns
    if isinstance(columns, pd.MultiIndex):
        level0 = {str(item) for item in columns.get_level_values(0)}
        for symbol in symbols:
            if symbol in columns.get_level_values(0) and "Close" in getattr(raw[symbol], "columns", []):
                out[symbol] = close_series(raw[symbol])
            elif "Close" in level0 and symbol in columns.get_level_values(1):
                out[symbol] = close_series(raw["Close"][symbol])
    elif "Close" in columns and len(symbols) == 1:
        out[symbols[0]] = close_series(raw)
    return out


def render_sector_summary(st, rows: list[dict], strong_by_etf: dict) -> None:
    """第二頁上方的表。st 是 Streamlit。"""
    st.caption(
        "4 週＝20 個共同交易日，12 週＝60 個。相對大盤＝板塊報酬減 S&P 500 報酬，只對齊兩邊都有收盤的日期。"
        "這是價格比較，不是資金流，也不是投信或外資買賣超。進場關卡只輔助，不擋下單。總經不在這兩關裡。"
    )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.subheader("前十大裡，三個交易日漲幅達到 10% 的股票")
    shown = False
    for ticker, result in strong_by_etf.items():
        if result["rows"]:
            shown = True
            text = "、".join(f"{item['symbol']} {item['return'] * 100:+.1f}%" for item in result["rows"])
            st.markdown(f"**{ticker}**：{text}")
        if result["note"]:
            st.caption(f"{ticker}：{result['note']}")
    if not shown:
        st.caption("目前沒有同時具備真實持股與足夠股價歷史、且三個交易日漲幅達到 10% 的股票。")
    st.caption(
        "持平帶是超額報酬 ±0.5 個百分點。上穿看最近 5 個交易日。連兩週是最近兩段各 5 個共同交易日的超額報酬都大於 0。"
        "這些是程式預設，不是使用者逐條確認過的策略參數。未知關卡沒有算成通過或未過。"
    )


def board_row(ticker: str, name: str, frame, benchmark, view: dict | None) -> dict:
    strength = relative_strength(frame, benchmark)
    entry = evaluate_entry(frame, benchmark)
    four = strength["rows"]["4週"]
    twelve = strength["rows"]["12週"]

    def pct(value):
        return "未知" if value is None else f"{value * 100:+.1f}%"

    return {
        "代號": ticker,
        "名稱": name,
        "類型": role_of(ticker),
        "4週報酬": pct(four["asset_return"]),
        "4週相對大盤": pct(four["excess"]),
        "4週方向": four["direction"],
        "12週報酬": pct(twelve["asset_return"]),
        "12週相對大盤": pct(twelve["excess"]),
        "12週方向": twelve["direction"],
        "第五頁立場": stance_text(ticker, view),
        "進場關卡": entry["text"],
        "對齊天數": strength["aligned_days"],
    }
