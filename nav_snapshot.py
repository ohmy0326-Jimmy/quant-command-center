"""每日淨值快照（NAV_Snapshots）與上／下檔捕獲率。

寫入：API 的 POST /v1/nav-snapshot 每天呼叫一次 take_snapshot()，用第四頁同一套估價
（normalize_positions → 報價 → value_positions_safely）算出淨值，依台北日期 upsert 一列。

出入金（新台幣，入金正、出金負）＝當天
  Futures_Ledger 的 出入金／入金／出金 列
＋ Cash_Flows 分頁（日期、帳戶、金額、幣別、備註；美元以當下 USDTWD 換算）。

捕獲率：
  日報酬 r_t ＝ (淨值_t − 出入金_t) ÷ 淨值_{t−1} − 1（當天的出入金不算績效，先從當天淨值扣掉）
  月報酬 ＝ 同一個月內 Π(1 + r_t) − 1
  基準在同樣的快照區間取收盤（^TWII 取當天；^GSPC 取前一天，因為台北下午快照時美股還沒收當天盤）
  上檔捕獲 ＝ 基準上漲月份的組合平均月報酬 ÷ 基準平均月報酬 × 100；下檔同理。
  平日的日報酬少於 60 個時顯示「樣本不足」。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

from sheet_dates import parse_sheet_date, taipei_today

SNAPSHOT_SHEET = "NAV_Snapshots"
CASH_FLOWS_SHEET = "Cash_Flows"
LEGACY_SNAPSHOT_HEADER = ["日期", "總淨值", "台股淨值", "美股淨值", "名目曝險", "槓桿", "帳戶Beta", "出入金", "寫入時間"]
# 2026-11 起多兩欄：寫入時用的美元／台幣匯率與來源（live／cached／last_known／fallback）。舊表頭會自動補上這兩欄。
SNAPSHOT_HEADER = LEGACY_SNAPSHOT_HEADER + ["USDTWD", "匯率來源"]
CASH_FLOWS_HEADER = ["日期", "帳戶", "金額", "幣別", "備註"]
MIN_CAPTURE_DAYS = 60

_TWD = {"", "TWD", "NTD", "NT$", "NT", "台幣", "新台幣", "臺幣", "新臺幣"}
_USD = {"USD", "US$", "美元", "美金"}


# ---------------------------------------------------------------- 出入金

def _number(value) -> float | None:
    from sheet_numbers import parse_sheet_number

    return parse_sheet_number(value)


def cash_flows_tab_flows(frame: pd.DataFrame | None, usdtwd: float) -> tuple[list[dict], list[str]]:
    """Cash_Flows 分頁 → [{"date", "amount"(新台幣), "account"}]。金額正＝入金、負＝出金。"""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return [], []
    flows, warnings = [], []
    for idx, row in frame.iterrows():
        day = parse_sheet_date(row.get("日期"))
        amount = _number(row.get("金額"))
        if day is None or amount is None:
            if any(str(v).strip() for v in row.values):
                warnings.append(f"Cash_Flows 第 {int(idx) + 2} 列缺少日期或金額，已略過。")
            continue
        currency = str(row.get("幣別") or "").strip().upper()
        if currency in _TWD:
            rate = 1.0
        elif currency in _USD:
            rate = float(usdtwd)
        else:
            warnings.append(f"Cash_Flows 第 {int(idx) + 2} 列幣別「{row.get('幣別')}」看不懂，已略過（只接受 TWD／USD）。")
            continue
        flows.append({"date": day, "amount": amount * rate, "account": str(row.get("帳戶") or "").strip()})
    return flows, warnings


def net_flow_on(day: date, *flow_lists) -> float:
    total = 0.0
    for flows in flow_lists:
        for item in flows or []:
            if item.get("date") == day:
                total += float(item["amount"])
    return total


# ---------------------------------------------------------------- 寫入

def snapshot_row(day: date, metrics: dict, flow: float, written_at: datetime, usdtwd: float | None = None, fx_source: str = "live") -> dict:
    m = metrics or {}
    return {
        "日期": day.isoformat(),
        "總淨值": round(float(m.get("nav", 0.0) or 0.0), 0),
        "台股淨值": round(float(m.get("tw_nav", 0.0) or 0.0), 0),
        "美股淨值": round(float(m.get("us_nav", 0.0) or 0.0), 0),
        "名目曝險": round(float(m.get("notional_exposure", 0.0) or 0.0), 0),
        "槓桿": round(float(m.get("total_leverage", 0.0) or 0.0), 4),
        "帳戶Beta": round(float(m.get("account_beta", 0.0) or 0.0), 4),
        "出入金": round(float(flow), 0),
        "寫入時間": written_at.strftime("%Y-%m-%d %H:%M:%S"),
        "USDTWD": "" if usdtwd is None else round(float(usdtwd), 4),
        "匯率來源": str(fx_source or ""),
    }


def last_known_usdtwd(nav_data: pd.DataFrame | None) -> tuple[float, str] | None:
    """NAV_Snapshots 最後一筆「匯率來源＝live」的 USDTWD → (匯率, 日期)。舊列沒有這欄就回 None。"""
    if nav_data is None or nav_data.empty or "USDTWD" not in nav_data.columns or "日期" not in nav_data.columns:
        return None
    best = None
    for _, r in nav_data.iterrows():
        source = str(r.get("匯率來源", "live") or "live").strip()
        rate = _number(r.get("USDTWD"))
        day = parse_sheet_date(r.get("日期"))
        if source != "live" or rate is None or day is None or not 20.0 < rate < 50.0:
            continue
        if best is None or day >= best[1]:
            best = (rate, day)
    return None if best is None else (best[0], best[1].isoformat())


def _ensure_worksheet(spreadsheet, title: str, header: list[str], legacy: list[str] | None = None):
    """取得分頁；沒有就建立並寫表頭。舊表頭（legacy）是新表頭的前綴時，只在右邊補欄名（不動任何資料）。

    回傳 (worksheet, created, migrated)。表頭對不上時丟例外，避免寫錯欄。
    """
    try:
        from gspread.exceptions import WorksheetNotFound
    except ImportError:  # pragma: no cover
        WorksheetNotFound = KeyError
    try:
        worksheet = spreadsheet.worksheet(title)
    except (WorksheetNotFound, KeyError):
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header))
        worksheet.update(range_name="A1", values=[header], value_input_option="USER_ENTERED")
        return worksheet, True, False
    first = [str(v).strip() for v in worksheet.row_values(1)]
    while first and not first[-1]:
        first.pop()
    if not first:
        _ensure_cols(worksheet, len(header))
        worksheet.update(range_name="A1", values=[header], value_input_option="USER_ENTERED")
        return worksheet, False, False
    if first[: len(header)] == header:
        return worksheet, False, False
    if legacy and first == legacy and header[: len(legacy)] == legacy:
        extra = header[len(legacy):]
        _ensure_cols(worksheet, len(header))
        start = _col_letter(len(legacy) + 1)
        end = _col_letter(len(header))
        worksheet.update(range_name=f"{start}1:{end}1", values=[extra], value_input_option="USER_ENTERED")
        return worksheet, False, True
    raise RuntimeError(f"{title} 表頭不是預期的 {header}，為避免寫錯欄位已停止。")


def _ensure_cols(worksheet, needed: int) -> None:
    count = getattr(worksheet, "col_count", None)
    if isinstance(count, int) and count < needed:
        worksheet.add_cols(needed - count)


def _col_letter(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def upsert_snapshot(spreadsheet, row: dict) -> dict:
    """依日期 upsert 一列。同一天重複呼叫只會覆蓋那一列（冪等）。"""
    worksheet, created, migrated = _ensure_worksheet(spreadsheet, SNAPSHOT_SHEET, SNAPSHOT_HEADER, LEGACY_SNAPSHOT_HEADER)
    values = [row[col] for col in SNAPSHOT_HEADER]
    day = parse_sheet_date(row["日期"])
    existing = worksheet.col_values(1)
    target = None
    for i, raw in enumerate(existing[1:], start=2):
        if parse_sheet_date(raw) == day:
            target = i
            break
    last = _col_letter(len(SNAPSHOT_HEADER))
    if target is None:
        target = max(len(existing), 1) + 1
        action = "inserted"
    else:
        action = "updated"
    worksheet.update(range_name=f"A{target}:{last}{target}", values=[values], value_input_option="USER_ENTERED")
    return {"action": action, "row_number": target, "sheet_created": created, "header_migrated": migrated}


def _live_market(positions: pd.DataFrame):
    from portfolio_risk import fetch_twii, market_beta_and_sector, populate_market_fields_parallel

    extras: dict = {}
    if positions is None or positions.empty or "標的" not in positions.columns:
        return {}, {}, {}, extras
    prices, betas, sectors = populate_market_fields_parallel(positions, market_beta_and_sector, fetch_twii(), extras=extras)
    return prices, betas, sectors, extras


def macro_candidate_from_sheet(spreadsheet, today: str) -> dict:
    """試算表九項都在才寫總分。缺項留空，不用倉庫示例，也不把缺項當 0。覆蓋率仍寫下來。"""
    from macro_cycle import CANONICAL_IDS, regime_for_score
    from macro_cycle_live import read_macro_tabs
    from macro_cycle_sheet import parse_score_rows
    from macro_proxies import coverage_from_ids, weighted_total

    tabs = read_macro_tabs(spreadsheet)
    parsed, _warnings = parse_score_rows(tabs.get("scores") or [])
    present = [item_id for item_id in CANONICAL_IDS if item_id in parsed]
    coverage = coverage_from_ids(present)
    if set(parsed) != set(CANONICAL_IDS):
        return {"week": today, "total": None, "regime": None, "coverage": coverage}
    scores = {item_id: int(parsed[item_id]["score"]) for item_id in CANONICAL_IDS}
    total = weighted_total(scores)
    return {"week": today, "total": total, "regime": regime_for_score(total), "coverage": coverage}


def _history_rows(spreadsheet) -> list[dict]:
    from macro_cycle_sheet import HISTORY_TAB

    try:
        from gspread.exceptions import WorksheetNotFound
    except ImportError:  # pragma: no cover
        WorksheetNotFound = KeyError
    try:
        worksheet = spreadsheet.worksheet(HISTORY_TAB)
    except (WorksheetNotFound, KeyError):
        return []
    try:
        from sheet_numbers import read_worksheet_records

        return read_worksheet_records(worksheet)
    except Exception:
        return []


def _cell(value):
    return "" if value is None else value


def record_macro_history(spreadsheet, today: str, candidate: dict | None = None) -> dict:
    """把台北當天的總分寫進 Macro_Score_History。同一天只有一列。"""
    from macro_cycle_sheet import HISTORY_HEADERS, HISTORY_TAB
    from macro_score_trend import plan_history_upsert

    chosen = candidate if candidate is not None else macro_candidate_from_sheet(spreadsheet, today)
    plan = plan_history_upsert(_history_rows(spreadsheet), chosen, today, replace_same_day=True)
    summary = {
        "status": {"insert": "written", "update": "written", "keep": "kept", "reject": "skipped"}.get(plan["action"], "skipped"),
        "action": plan["action"],
        "sheet": HISTORY_TAB,
        "date": plan.get("week") or today,
        "total": plan.get("total"),
        "regime": plan.get("regime"),
        "coverage": chosen.get("coverage"),
        "detail": plan["reason"],
    }
    if plan["action"] not in {"insert", "update"}:
        return summary
    from macro_cycle_sheet import HISTORY_WRITE_HEADERS

    worksheet, _created, _migrated = _ensure_worksheet(
        spreadsheet, HISTORY_TAB, list(HISTORY_WRITE_HEADERS), legacy=list(HISTORY_HEADERS)
    )
    values = [_cell(plan.get("week")), _cell(plan.get("total")), _cell(plan.get("regime")), _cell(chosen.get("coverage"))]
    existing = worksheet.col_values(1)
    target = None
    for index, raw in enumerate(existing[1:], start=2):
        if parse_sheet_date(raw) == parse_sheet_date(plan.get("week")):
            target = index
            break
    if target is None:
        target = max(len(existing), 1) + 1
    worksheet.update(range_name=f"A{target}:D{target}", values=[values], value_input_option="USER_ENTERED")
    summary["sheet_created"] = _created
    return summary


def attach_macro_history(payload: dict, spreadsheet, today: str) -> dict:
    """分數寫入失敗只記在摘要裡，不推翻已經寫好的淨值快照。"""
    try:
        payload["macro_history"] = record_macro_history(spreadsheet, today)
    except Exception:
        logger.exception("macro history accumulation failed")
        payload["macro_history"] = {
            "status": "failed",
            "action": "failed",
            "sheet": "Macro_Score_History",
            "date": str(today),
            "total": None,
            "regime": None,
            "detail": "分數累積失敗，淨值快照已寫入。",
        }
    return payload


def attach_breadth(payload: dict, spreadsheet, today: str, compute=None) -> dict:
    """廣度寫入失敗只記在摘要裡，不推翻已經寫好的淨值快照。"""
    if compute is False:
        payload["bear_breadth"] = {"status": "skipped", "sheet": "Bear_Breadth", "as_of": str(today), "detail": "這次沒有重算廣度。"}
        return payload
    try:
        from bear_warning import compute_stored_breadth, record_breadth

        result = compute_stored_breadth() if compute is None else compute()
        written = str((payload.get("row") or {}).get("寫入時間") or "")
        payload["bear_breadth"] = record_breadth(spreadsheet, str(today), result or {}, written)
    except Exception:
        logger.exception("bear breadth accumulation failed")
        payload["bear_breadth"] = {
            "status": "failed",
            "sheet": "Bear_Breadth",
            "as_of": str(today),
            "detail": "廣度沒有寫入。淨值快照已寫入。",
        }
    return payload


def take_snapshot(now: datetime | None = None, spreadsheet=None, usdtwd: float | None = None, market=None, breadth_compute=None) -> dict:
    """算出今天（台北）的淨值並寫入 NAV_Snapshots。回傳寫入的那一列與摘要。"""
    from futures_ledger import FUTURES_LEDGER_SHEET, ledger_cash_flows
    from portfolio_risk import (
        _optional_sheet,
        FxQuote,
        load_service_account_info,
        normalize_positions,
        open_quant_spreadsheet,
        read_quant_sheets,
        resolve_usdtwd,
        value_positions_safely,
    )
    from portfolio_briefing import taipei_now

    moment = now or taipei_now()
    day = taipei_today(moment.date())
    book = spreadsheet if spreadsheet is not None else open_quant_spreadsheet(load_service_account_info())
    accounts, nav_history, quant = read_quant_sheets(book)
    if usdtwd is None:
        fx = resolve_usdtwd(last_known=last_known_usdtwd(nav_history))
    elif isinstance(usdtwd, FxQuote):
        fx = usdtwd
    else:
        fx = FxQuote(float(usdtwd))
    rate = float(fx.rate)
    positions = normalize_positions(quant)
    prices, betas, sectors, extras = (market or _live_market)(positions)
    _valued, _accounts, metrics, row_warnings = value_positions_safely(
        positions, accounts, rate, prices, betas, sectors, extras=extras
    )
    if float(metrics.get("nav", 0.0) or 0.0) <= 0:
        raise RuntimeError("淨值算出來不是正數，不寫入快照。")
    ledger_flows = ledger_cash_flows(_optional_sheet(book, FUTURES_LEDGER_SHEET))
    tab_flows, flow_warnings = cash_flows_tab_flows(_optional_sheet(book, CASH_FLOWS_SHEET), rate)
    flow = net_flow_on(day, ledger_flows, tab_flows)
    row = snapshot_row(day, metrics, flow, moment, usdtwd=rate, fx_source=fx.source)
    result = upsert_snapshot(book, row)
    fx_warnings = []
    if fx.is_fallback:
        # 不默默寫：列上「匯率來源」標記退回值，回應也帶警告。同一天稍後重跑會覆蓋成即時匯率。
        fx_warnings.append(f"{fx.warning()} 這一列已標記「匯率來源＝{fx.source}」，匯率恢復後同一天重跑會覆蓋。")
    if result["header_migrated"]:
        fx_warnings.append("NAV_Snapshots 舊表頭已在右邊補上 USDTWD、匯率來源兩欄（舊資料未變動）。")
    payload = {
        "date": row["日期"],
        "action": result["action"],
        "sheet": SNAPSHOT_SHEET,
        "sheet_created": result["sheet_created"],
        "row_number": result["row_number"],
        "row": row,
        "flow_sources": {
            "futures_ledger": round(net_flow_on(day, ledger_flows), 0),
            "cash_flows": round(net_flow_on(day, tab_flows), 0),
        },
        "header_migrated": result["header_migrated"],
        "usdtwd": rate,
        "usdtwd_source": fx.source,
        "warnings": [str(w) for w in fx_warnings + list(row_warnings) + flow_warnings],
    }
    return attach_breadth(attach_macro_history(payload, book, row["日期"]), book, row["日期"], compute=breadth_compute)


# ---------------------------------------------------------------- 捕獲率

def history_frame(nav_data: pd.DataFrame | None) -> pd.DataFrame:
    """NAV_Snapshots（或舊 Daily_NAV）→ 以日期為索引的 nav、tw_nav、us_nav、flow。"""
    cols = ["nav", "tw_nav", "us_nav", "flow"]
    if nav_data is None or nav_data.empty or "日期" not in nav_data.columns or "總淨值" not in nav_data.columns:
        return pd.DataFrame(columns=cols)
    rows = {}
    for _, r in nav_data.iterrows():
        day = parse_sheet_date(r.get("日期"))
        nav = _number(r.get("總淨值"))
        if day is None or nav is None or nav <= 0:
            continue
        rows[day] = {
            "nav": nav,
            "tw_nav": _number(r.get("台股淨值")),
            "us_nav": _number(r.get("美股淨值")),
            "flow": _number(r.get("出入金")) or 0.0,
        }
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    out.index = pd.to_datetime(out.index)
    return out[cols]


def flow_adjusted_returns(hist: pd.DataFrame) -> pd.Series:
    if hist is None or len(hist) < 2:
        return pd.Series(dtype=float)
    nav = hist["nav"].astype(float)
    flow = hist["flow"].fillna(0.0).astype(float)
    r = (nav - flow) / nav.shift(1) - 1.0
    return r.iloc[1:].dropna()


def trading_day_count(returns: pd.Series) -> int:
    if returns is None or returns.empty:
        return 0
    return int(sum(1 for d in returns.index if pd.Timestamp(d).weekday() < 5))


def benchmark_returns(close: pd.Series | None, dates: pd.DatetimeIndex, lag_days: int = 0) -> pd.Series:
    """基準在快照日期上的區間報酬（取 ≤ 快照日 − lag 的最近收盤）。"""
    if close is None or len(close) == 0:
        return pd.Series(dtype=float)
    series = pd.Series(close).dropna().astype(float)
    idx = pd.to_datetime(series.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    series.index = idx.normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    probe = pd.DatetimeIndex(dates) - pd.Timedelta(days=lag_days)
    levels = pd.Series(series.reindex(series.index.union(probe)).ffill().reindex(probe).values, index=pd.DatetimeIndex(dates))
    return (levels / levels.shift(1) - 1.0).iloc[1:]


def blended_returns(tw: pd.Series, us: pd.Series, hist: pd.DataFrame) -> pd.Series:
    """依前一天台股／美股淨值加權。缺分區淨值的日子為 NaN。"""
    tw_nav = hist["tw_nav"].astype(float)
    us_nav = hist["us_nav"].astype(float)
    total = (tw_nav.clip(lower=0) + us_nav.clip(lower=0)).shift(1)
    w_tw = tw_nav.clip(lower=0).shift(1) / total
    w_us = us_nav.clip(lower=0).shift(1) / total
    frame = pd.DataFrame({"tw": tw, "us": us, "w_tw": w_tw.reindex(tw.index), "w_us": w_us.reindex(tw.index)})
    return frame["tw"] * frame["w_tw"] + frame["us"] * frame["w_us"]


def monthly_chain(daily: pd.Series) -> pd.Series:
    daily = daily.dropna()
    if daily.empty:
        return pd.Series(dtype=float)
    keys = pd.DatetimeIndex(daily.index).to_period("M")
    return (1.0 + daily).groupby(keys).prod() - 1.0


def capture_ratios(port_daily: pd.Series, bench_daily: pd.Series) -> dict:
    both = pd.DataFrame({"p": port_daily, "b": bench_daily}).dropna()
    pm = monthly_chain(both["p"])
    bm = monthly_chain(both["b"])
    up = bm > 0
    down = bm < 0
    out = {"months": int(len(bm)), "up_months": int(up.sum()), "down_months": int(down.sum()), "up": None, "down": None}
    if up.any() and bm[up].mean() != 0:
        out["up"] = float(pm[up].mean() / bm[up].mean() * 100.0)
    if down.any() and bm[down].mean() != 0:
        out["down"] = float(pm[down].mean() / bm[down].mean() * 100.0)
    return out


BENCHMARKS = (("台股 ^TWII", "^TWII", 0), ("美股 ^GSPC", "^GSPC", 1))


def capture_summary(nav_data: pd.DataFrame | None, closes: dict | None = None, min_days: int = MIN_CAPTURE_DAYS) -> dict:
    """回傳 {"ok", "days", "need", "rows": [...]}。樣本不足時 ok=False、不需要基準資料。"""
    hist = history_frame(nav_data)
    port = flow_adjusted_returns(hist)
    days = trading_day_count(port)
    out = {"ok": False, "days": days, "need": int(min_days), "rows": [], "start": None}
    if not hist.empty:
        out["start"] = hist.index[0].date().isoformat()
    if days < min_days:
        return out
    closes = closes or {}
    dates = pd.DatetimeIndex(hist.index)
    bench = {}
    for label, ticker, lag in BENCHMARKS:
        bench[label] = benchmark_returns(closes.get(ticker), dates, lag)
    rows = []
    for label, _ticker, _lag in BENCHMARKS:
        if bench[label].empty:
            rows.append({"基準": label, "上檔捕獲": None, "下檔捕獲": None, "月數": 0, "上漲月": 0, "下跌月": 0})
            continue
        cap = capture_ratios(port, bench[label])
        rows.append({"基準": label, "上檔捕獲": cap["up"], "下檔捕獲": cap["down"], "月數": cap["months"], "上漲月": cap["up_months"], "下跌月": cap["down_months"]})
    tw_b, us_b = bench["台股 ^TWII"], bench["美股 ^GSPC"]
    if not tw_b.empty and not us_b.empty:
        blend = blended_returns(tw_b, us_b, hist)
        cap = capture_ratios(port, blend)
        rows.append({"基準": "台／美淨值加權", "上檔捕獲": cap["up"], "下檔捕獲": cap["down"], "月數": cap["months"], "上漲月": cap["up_months"], "下跌月": cap["down_months"]})
    out.update(ok=True, rows=rows)
    return out


def fetch_benchmark_closes(start: str, history=None) -> dict:
    """抓 ^TWII、^GSPC 自 start（往前多抓 10 天）起的收盤。任一檔抓不到就丟例外（快取層不會記住失敗）。"""
    if history is None:
        import yfinance as yf

        def history(ticker, since):
            return yf.Ticker(ticker).history(start=since, auto_adjust=False)

    since = (pd.Timestamp(start) - pd.Timedelta(days=10)).date().isoformat()
    out = {}
    for _label, ticker, _lag in BENCHMARKS:
        frame = history(ticker, since)
        if frame is None or getattr(frame, "empty", True) or "Close" not in frame.columns:
            raise RuntimeError(f"{ticker} 歷史收盤抓不到")
        out[ticker] = frame["Close"].dropna()
        if out[ticker].empty:
            raise RuntimeError(f"{ticker} 歷史收盤抓不到")
    return out


def capture_status_text(capture: dict | None) -> str:
    if not capture:
        return "捕獲率：樣本不足（尚無 NAV_Snapshots 歷史）。"
    if capture.get("error"):
        return f"捕獲率：{capture['error']}"
    if not capture.get("ok"):
        return f"捕獲率：樣本不足（目前 {capture.get('days', 0)} 個交易日，需 {capture.get('need', MIN_CAPTURE_DAYS)}）。"
    return f"捕獲率：自 {capture.get('start')} 起 {capture.get('days')} 個交易日，日報酬已扣出入金，按月連乘。"
