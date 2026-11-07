import os
import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import gspread
import requests
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from google import genai
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from oauth2client.service_account import ServiceAccountCredentials
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
from app_secrets import read_secret
from macro_cycle_ui import render_macro_cycle_page
from order_assistant_ui import render_order_assistant
from beta_rules import BETA_FALLBACK_STATE_KEY
from cb_holdings import convertible_quote_context, is_convertible_row
from risk_performance import render_risk_performance_card
from portfolio_risk import (
    attach_quotes,
    coerce_ticker_kind_pairs,
    equity_ratio_pct,
    fetch_quant_sheets,
    fetch_twii,
    fetch_usdtwd_live,
    resolve_usdtwd,
    finalize_valuation,
    load_service_account_info,
    open_quant_spreadsheet,
    market_beta_and_sector,
    market_beta_info,
    normalize_positions,
    populate_market_fields_parallel,
    ticker_kind_pairs,
    value_positions_safely,
)
from sheet_dates import nav_flow_map, nav_growth_series
from nav_snapshot import capture_summary, fetch_benchmark_closes, last_known_usdtwd
import taifex_quotes
import taiex_vix
import us_hedge
import macro_risk
import page1_lights
import sector_board
import chart_history
import calendar_events
import calendar_actions
import calendar_page

# ==========================================
# 網頁基本設定與密碼驗證防護（支援環境變數與 Secret）
# ==========================================
_is_test_site = os.environ.get("APP_ENV", "").strip() == "test"
_page_title = "量化交易戰鬥艙（測試站）" if _is_test_site else "量化交易戰鬥艙"
st.set_page_config(page_title=_page_title, page_icon="⚔️", layout="wide", initial_sidebar_state="expanded")
if _is_test_site:
    st.warning("這是測試站。")

_env_password = os.environ.get("ADMIN_PASSWORD")
ADMIN_PASSWORD = _env_password if _env_password else read_secret("", "passwords", "admin")

def check_password():
    def password_entered():
        if "password" in st.session_state and st.session_state["password"] == ADMIN_PASSWORD:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.markdown("## 🔐 系統安全驗證")
        st.text_input("請輸入系統登入密碼", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.markdown("## 🔐 系統安全驗證")
        st.text_input("請輸入系統登入密碼", type="password", on_change=password_entered, key="password")
        st.error("😕 密碼錯誤，請重新輸入")
        return False
    else:
        return True

if not check_password():
    st.stop()

# ==========================================
# 共用 Telegram 推播輔助函式
# ==========================================
def send_to_telegram(title, text_content):
    try:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or read_secret("", "telegram", "bot_token") or read_secret("", "TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or read_secret("", "telegram", "chat_id") or read_secret("", "TELEGRAM_CHAT_ID")
        
        if bot_token and chat_id:
            from telegram_html import send_message, titled_message

            res = send_message(bot_token, chat_id, titled_message(title, text_content), post=requests.post, timeout=5)
            if res.status_code == 200:
                st.success("✅ 成功推播至 Telegram！")
            else:
                st.error(f"❌ 推播失敗，回應碼：{res.status_code}")
        else:
            st.warning("⚠️ 尚未設定 Telegram Bot Token 或 Chat ID。")
    except Exception as e:
        st.error(f"❌ 發生例外錯誤：{e}")


@st.cache_data(ttl=300)
def get_market_beta_and_sector(ticker, is_tw):
    return market_beta_and_sector(ticker, bool(is_tw))


@st.cache_data(ttl=300)
def get_market_beta_info(ticker, is_tw):
    return market_beta_info(ticker, bool(is_tw))


get_market_beta_and_sector.info = get_market_beta_info


def _google_key_from_secrets():
    data = read_secret(None, "google_key")
    if not isinstance(data, dict) and not hasattr(data, "items"):
        raise RuntimeError("找不到 Google 服務帳戶金鑰")
    return dict(data)


@st.cache_data(ttl=60)
def load_quant_database():
    key_data = load_service_account_info(_google_key_from_secrets)
    return fetch_quant_sheets(key_data)


@st.cache_data(ttl=60, show_spinner=False)
def cached_stop_dates() -> dict:
    """只讀 Alert_State。讀不到就當沒有日期，不建立分頁、不寫入。"""
    try:
        from alert_engine import recorded_stop_dates

        book = open_quant_spreadsheet(load_service_account_info(_google_key_from_secrets))
        return recorded_stop_dates(book)
    except Exception:
        return {}


# 即時匯率快取 3 分鐘；抓不到會丟例外，st.cache_data 不快取例外，下次重整再抓。
@st.cache_data(ttl=180, show_spinner=False)
def cached_usdtwd_live():
    return fetch_usdtwd_live()


def usdtwd_quote(nav_data=None):
    """即時 → 本程序最近成功值 → NAV_Snapshots 最後一筆即時匯率 → 32.5。回傳 FxQuote（含來源）。"""
    return resolve_usdtwd(live=cached_usdtwd_live, last_known=last_known_usdtwd(nav_data))


def cached_usdtwd():
    return usdtwd_quote().rate


cached_usdtwd.clear = cached_usdtwd_live.clear


@st.cache_data(ttl=180, show_spinner=False)
def cached_twii():
    return fetch_twii()


def _ticker_kind_pairs(frame: pd.DataFrame) -> tuple:
    return ticker_kind_pairs(frame)


@st.cache_data(ttl=60, show_spinner=False)
def cached_quote_bundle(pairs: tuple, cb_context: tuple = ()):
    """同一批持倉的報價與 Beta。60 秒內改數字不會重抓。

    cb_context 是可轉債／CBAS 的 (標的, 標的股票)。債本身沒有 Yahoo 報價，這裡只為了抓標的股價。
    """
    twii = fetch_twii()
    pairs = coerce_ticker_kind_pairs(pairs)
    frame = pd.DataFrame([{"標的": t, "種類": k} for t, k in pairs]) if pairs else pd.DataFrame(columns=["標的", "種類"])
    if cb_context:
        extra = pd.DataFrame(list(cb_context), columns=["標的", "標的代號"]).drop_duplicates(subset=["標的"], keep="first")
        frame = frame.merge(extra, on="標的", how="left")
    extras = {}
    prices, betas, sectors = populate_market_fields_parallel(frame, market_beta_and_sector, twii, extras=extras)
    return {"prices": prices, "betas": betas, "sectors": sectors, "twii": float(twii), "extras": extras}


def load_book_for_page5():
    """第四頁同一套快取與 finalize_valuation。第五頁用來對槓桿與現金。"""
    account_data, _nav, quant = load_quant_database()
    frame = normalize_positions(quant)
    usdtwd = cached_usdtwd()
    if not frame.empty and "標的" in frame.columns:
        bundle = cached_quote_bundle(_ticker_kind_pairs(frame), convertible_quote_context(frame))
        frame = attach_quotes(
            frame,
            bundle["prices"],
            bundle["betas"],
            bundle["sectors"],
            extras=bundle.get("extras"),
            beta_fallbacks=st.session_state.get(BETA_FALLBACK_STATE_KEY),
        )
    valued, _accounts, metrics = finalize_valuation(frame, account_data, usdtwd)
    positions = []
    if valued is not None and not valued.empty and "即時市值(台幣)" in valued.columns:
        for _, row in valued.iterrows():
            positions.append({"ticker": str(row.get("標的") or ""), "mv": float(row.get("即時市值(台幣)") or 0)})
    return {
        "ok": True,
        "positions": positions,
        "leverage": float(metrics.get("leverage") or 0),
        "cash_pct": float(metrics.get("cash_pct") or 0),
    }


def load_market_for_page6(tickers: tuple):
    quote = usdtwd_quote()
    usdtwd = quote.rate
    if not tickers:
        return usdtwd, cached_twii(), {}, {}, {}, {}, quote
    pairs = tuple(dict.fromkeys((t, "") if isinstance(t, str) else (str(t[0]), str(t[1])) for t in tickers))
    bundle = cached_quote_bundle(pairs)
    return usdtwd, bundle["twii"], bundle["prices"], bundle["betas"], bundle["sectors"], bundle.get("extras") or {}, quote


# K 線快取：成功的結果快取 5 分鐘；空資料／抓取失敗會丟例外，st.cache_data 不快取例外，
# 所以失敗不會被記住，下次重整（或重新執行）就會重抓。
@st.cache_data(ttl=300, show_spinner=False)
def cached_history(ticker: str, period: str):
    return chart_history.fetch_history_strict(ticker, period)


def cached_history_map(pairs: tuple):
    """多檔 K 線同時下載。每檔各自快取；抓不到的回空表，不會被快取。"""
    ctx = get_script_run_ctx()

    def load(ticker, period):
        if ctx is not None:
            add_script_run_ctx(threading.current_thread(), ctx)
        return cached_history(ticker, period)

    return chart_history.fetch_history_map(pairs, load)


cached_history_map.clear = cached_history.clear


# 捕獲率的基準收盤。抓不到會丟例外，st.cache_data 不快取例外。
@st.cache_data(ttl=3600, show_spinner=False)
def cached_benchmark_closes(start_iso):
    return fetch_benchmark_closes(start_iso)


def page4_capture(nav_data):
    """NAV_Snapshots → 上／下檔捕獲率。不到 60 個交易日不抓基準。"""
    capture = capture_summary(nav_data)
    if capture["days"] >= capture["need"] and capture.get("start"):
        try:
            capture = capture_summary(nav_data, cached_benchmark_closes(capture["start"]))
        except Exception:
            capture["error"] = "基準指數（^TWII／^GSPC）歷史暫時抓不到，稍後重整再試。"
    return capture


@st.cache_data(ttl=3600, show_spinner=False)
def cached_top_holdings_map(tickers: tuple):
    """ETF 前十大持股。抓不到時回傳空字串，由頁面改用內建備援。"""

    def one(ticker):
        try:
            funds = yf.Ticker(ticker).funds_data
            th = funds.top_holdings
            if th is None or th.empty:
                return ticker, ""
            holdings_list = []
            for idx, row in th.iterrows():
                sym = str(idx) if isinstance(idx, str) else str(row.get("symbol", idx))
                w_col = [c for c in th.columns if any(k in c.lower() for k in ["percent", "weight", "holding"])]
                val = 0.0
                if w_col:
                    raw_val = float(row[w_col[0]])
                    val = raw_val * 100 if raw_val <= 1.0 else raw_val
                holdings_list.append((sym, val))
            holdings_list.sort(key=lambda item: item[1], reverse=True)
            text = ", ".join([f"{i+1}. {sym} ({weight:.1f}%)" for i, (sym, weight) in enumerate(holdings_list[:10])])
            return ticker, text
        except Exception:
            return ticker, ""

    found = {}
    workers = max(1, min(8, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ticker, text in pool.map(one, tickers):
            found[ticker] = text
    return found


@st.cache_data(ttl=3600, show_spinner=False)
def cached_etf_holding_rows(tickers: tuple):
    """真實前十大。抓不到是空清單，不放示例持股。"""
    found = {}
    for ticker in tickers:
        table = None
        try:
            table = yf.Ticker(ticker).funds_data.top_holdings
        except Exception:
            table = None
        found[ticker] = sector_board.holdings_from_frame(table)
    return found


@st.cache_data(ttl=3600, show_spinner=False)
def cached_symbol_closes(symbols: tuple):
    """強勢股用的收盤。整批快取一小時，失敗的代號留空序列。"""
    if not symbols:
        return {}
    try:
        raw = yf.download(list(symbols), period="1mo", auto_adjust=True, progress=False, threads=False)
    except Exception:
        raw = None
    return sector_board.closes_from_download(raw, symbols)


# 第一頁風險計分板的資料。抓不到時 fetch 會丟例外，st.cache_data 不快取例外，下次重整會重抓。
@st.cache_data(ttl=300, show_spinner=False)
def cached_macro_history(ticker, period):
    return macro_risk.fetch_history(ticker, period)


@st.cache_data(ttl=900, show_spinner=False)
def cached_foreign_flows(today_iso):
    return macro_risk.fetch_foreign_flows(datetime.fromisoformat(today_iso).date())


def clear_macro_risk_caches():
    cached_macro_history.clear()
    cached_foreign_flows.clear()
    cached_fred_observations.clear()


@st.cache_data(ttl=3600, show_spinner=False)
def cached_fred_observations(series_id: str):
    return page1_lights.fetch_fred_observations(series_id)


# 第三頁財報日快取 6 小時。抓取失敗會丟例外，st.cache_data 不快取例外。
@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_yahoo_earnings(symbol: str):
    return calendar_events.fetch_yahoo_calendar(symbol)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_fmp_raw(start_iso: str, end_iso: str, api_key: str):
    return calendar_events.fetch_fmp_calendar(api_key, datetime.fromisoformat(start_iso).date(), datetime.fromisoformat(end_iso).date())


def load_calendar_model(watchlist: str):
    today = datetime.now().date()
    fetched_at = datetime.now(calendar_events.TAIPEI)
    try:
        _account_data, _nav_data, quant_data = load_quant_database()
        rows = normalize_positions(quant_data)
    except Exception:
        rows = None
    if rows is None:
        symbols = calendar_events.earnings_symbols(calendar_events.watch_from_text(watchlist)[0])
    else:
        held, _notes = calendar_events.holdings_from_positions(rows)
        watched, _watch_notes = calendar_events.watch_from_text(watchlist)
        symbols = calendar_events.earnings_symbols(held + watched)
    symbols = symbols[: calendar_events.MAX_EARNINGS_LOOKUPS]
    payloads = {}
    for symbol in symbols:
        try:
            payloads[symbol] = cached_yahoo_earnings(symbol)
        except Exception:
            payloads[symbol] = {"error": "來源失敗"}
    fmp_key = os.environ.get("FMP_API_KEY") or read_secret("", "FMP_API_KEY")
    fmp_items = None
    fmp_error = ""
    if fmp_key:
        try:
            fmp_items = cached_fmp_raw(today.isoformat(), (today + timedelta(days=90)).isoformat(), fmp_key)
        except Exception:
            fmp_items = []
            fmp_error = "FMP 來源失敗，這一段缺漏。其餘事件仍顯示。不會改用寫死日期。"
    return calendar_events.build_calendar(
        rows,
        watchlist,
        payloads,
        fmp_items,
        today,
        fetched_at,
        fmp_error=fmp_error,
    )


def load_page1_board(macro_items: list):
    today = datetime.now(macro_risk.TPE).date()
    series = {}
    for key, spec in {**page1_lights.AGREED_SERIES, **page1_lights.READING_SERIES}.items():
        try:
            series[key] = cached_fred_observations(spec["series_id"])
        except Exception:
            series[key] = None
    return page1_lights.assemble_board(macro_items, series, today)


def load_macro_risk():
    today_tpe = datetime.now(macro_risk.TPE).date().isoformat()
    loaders = {
        "vix": lambda: cached_macro_history("^VIX", "1mo"),
        "tnx": lambda: cached_macro_history("^TNX", "1mo"),
        "foreign": lambda: cached_foreign_flows(today_tpe),
        "usdtwd": lambda: cached_macro_history("USDTWD=X", "3mo"),
        "taiex": lambda: cached_macro_history("^TWII", "3mo"),
    }
    return macro_risk.evaluate(loaders)


def _render_page1_charts(hist, indicators):
    cols = st.columns(2)
    for idx, (name, ticker) in enumerate(indicators.items()):
        try:
            df_item = hist.get((ticker, "1y"), pd.DataFrame())
            if df_item is None:
                df_item = pd.DataFrame()
            else:
                df_item = df_item.copy()
            if df_item.empty:
                continue
            df_item["MA5"] = df_item["Close"].rolling(window=5).mean()
            df_item["MA20"] = df_item["Close"].rolling(window=20).mean()
            df_item["MA60"] = df_item["Close"].rolling(window=60).mean()
            df_item["MA200"] = df_item["Close"].rolling(window=200).mean()
            curr = float(df_item["Close"].iloc[-1])
            prev = float(df_item["Close"].iloc[-2])
            pct = ((curr - prev) / prev) * 100
            with cols[idx % 2]:
                st.markdown(f"**{name}**：`{curr:,.2f}`  (`{pct:+.2f}%`)")
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df_item.index, y=df_item["Close"], name="收盤價", line=dict(color="#FFFFFF", width=1.2)))
                fig.add_trace(go.Scatter(x=df_item.index, y=df_item["MA5"], name="5MA", line=dict(color="#FFEE58", width=1)))
                fig.add_trace(go.Scatter(x=df_item.index, y=df_item["MA20"], name="20MA", line=dict(color="#29B6F6", width=1.5)))
                fig.add_trace(go.Scatter(x=df_item.index, y=df_item["MA60"], name="60MA", line=dict(color="#FFA726", width=1.5)))
                fig.add_trace(go.Scatter(x=df_item.index, y=df_item["MA200"], name="200MA", line=dict(color="#AB47BC", width=2)))
                fig.update_layout(height=250, margin=dict(l=10, r=10, t=10, b=10), showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)), template="plotly_dark")
                st.plotly_chart(fig, use_container_width=True)
        except Exception:
            continue


def clear_market_caches():
    load_quant_database.clear()
    cached_usdtwd.clear()
    cached_twii.clear()
    cached_quote_bundle.clear()
    cached_history_map.clear()
    cached_top_holdings_map.clear()
    clear_macro_risk_caches()
    get_market_beta_and_sector.clear()
    get_market_beta_info.clear()
    taifex_quotes.clear_cache()
    taiex_vix.clear_cache()
    us_hedge.clear_cache()


# 初始化各頁面 AI 記憶狀態
if 'ai_memo_p1' not in st.session_state:
    st.session_state.ai_memo_p1 = {"text": "", "time": ""}
if 'ai_memo_p2' not in st.session_state:
    st.session_state.ai_memo_p2 = {"text": "", "time": ""}
if 'ai_memo_p3' not in st.session_state:
    st.session_state.ai_memo_p3 = {}
if 'ai_memo_p4' not in st.session_state:
    st.session_state.ai_memo_p4 = {"text": "", "time": ""}

with st.sidebar:
    st.title("⚙️ 系統導覽")
    page = st.radio(
        "切換監控面板：",
        [
            "第一頁：總經與風險鬧鐘",
            "第二頁：板塊與主題資金輪動",
            "第三頁：重大行事曆與會展",
            "第四頁：部位監控與量化風控",
            "第五頁：總經週期與配置",
            "第六頁：下單輔助系統",
        ],
        key="nav_page",
    )
    if page == "第六頁：下單輔助系統":
        st.caption("風控規則在第六頁最上方，會留在這次工作階段。")
    st.markdown("---")
    st.subheader("📝 後端資料庫 (Notion)")
    st.markdown("[📊 Notion 交易與覆盤日誌](https://www.notion.so/)")
    st.markdown("[🧪 Notion 策略實驗室](https://www.notion.so/)")

# ==========================================
# 第一頁：總經與風險鬧鐘
# ==========================================
if page == "第一頁：總經與風險鬧鐘":
    c1, c2 = st.columns([0.85, 0.15])
    with c1:
        st.title("🌍 第一頁：總經與風險鬧鐘 (宏觀指揮總部)")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p1", use_container_width=True):
            cached_history_map.clear()
            clear_macro_risk_caches()
            st.rerun()
    st.markdown("---")
    
    indicators = {
        "VIX 恐慌指數": "^VIX", "美國 10 年公債": "^TNX", "美元指數 (DXY)": "DX-Y.NYB", 
        "美元/台幣匯率": "USDTWD=X", "比特幣 (BTC)": "BTC-USD", "S&P 500": "^GSPC", 
        "那斯達克 (IXIC)": "^IXIC", "道瓊工業 (DJI)": "^DJI", "費城半導體 (SOX)": "^SOX", 
        "羅素 2000 (RUT)": "^RUT", "台灣加權指數": "^TWII", "布蘭特原油 (Brent)": "BZ=F", "黃金期貨": "GC=F"
    }

    with st.spinner('📡 正在從華爾街精準抓取 1 年期總經日K與風險指標...'):
        try:
            history_pairs = tuple(sorted({(ticker, "5d") for ticker in indicators.values()} | {(ticker, "1y") for ticker in indicators.values()}))
            hist = cached_history_map(history_pairs)
            risk_items, risk_summary = load_macro_risk()

            st.markdown("### 🛡️ 宏觀市場風險計分板 (標準化評估)")
            rc1, rc2, rc3 = st.columns([1.5, 2, 4.5])
            with rc1:
                level = risk_summary["level"]
                if level == "green":
                    st.success(risk_summary["label"])
                elif level == "yellow":
                    st.warning(risk_summary["label"])
                elif level == "red":
                    st.error(risk_summary["label"])
                else:
                    st.info(risk_summary["label"])
            with rc2:
                st.metric("當前綜合風險評分", risk_summary["score_text"], f"滿分 {risk_summary['max_score']} 分", delta_color="off")
            with rc3:
                if risk_summary["triggered"]:
                    st.error(" | ".join(risk_summary["triggered"]))
                elif risk_summary["missing_count"]:
                    st.info("目前可用的指標都沒有觸發，但部分資料缺漏，不能據此判斷為 Risk-On。")
                else:
                    st.info("✅ 目前無重大宏觀警訊，市場流動性與波動率維持健康 (Risk-On)。")
                if risk_summary["missing_count"]:
                    st.warning("資料缺漏：" + "、".join(risk_summary["missing_names"]) + "。這些項目不計分，也不算「沒觸發」。")
            st.dataframe(pd.DataFrame(macro_risk.table_rows(risk_items)), use_container_width=True, hide_index=True)
            st.caption(
                f"燈號：0–{macro_risk.GREEN_MAX} 分 🟢、{macro_risk.GREEN_MAX + 1}–{macro_risk.YELLOW_MAX} 分 🟡、"
                f"{macro_risk.YELLOW_MAX + 1} 分以上 🔴（滿分 {macro_risk.MAX_SCORE}）。核心指標缺漏且分數落在綠燈區時顯示 ⚪ 資料不足。"
                f"資料超過 {macro_risk.STALE_BUSINESS_DAYS} 個工作日未更新視為過期、不計分。"
                " DFII10 與下面的利差讀數不進這個分數，也不新增 Telegram 推播。"
            )
            page1_board = load_page1_board(risk_items)
            page1_lights.render_page1_summary(st, page1_board, risk_summary)

            st.markdown("---")
            
            with st.expander("🤖 點擊展開：AI 避險基金經理人今日總經晨報與操盤提醒", expanded=True):
                if st.button("🧠 一鍵生成今日總經盤勢速覽", use_container_width=True):
                    with st.spinner("AI 經理人正在解讀各大總經指標..."):
                        try:
                            gemini_key = os.environ.get("GEMINI_API_KEY") or read_secret("", "GEMINI_API_KEY")
                            client = genai.Client(api_key=gemini_key)
                            snapshot_str = f"- 宏觀風險計分：{risk_summary['score_text']}（{risk_summary['label']}）\n"
                            for risk_item in risk_items:
                                status = macro_risk.STATUS_LABELS[risk_item["status"]]
                                snapshot_str += f"- {risk_item['name']}: {risk_item['value_text']}（{risk_item['date'] or '無日期'}，{status}）\n"
                            
                            for k, t in indicators.items():
                                try:
                                    s_df = hist.get((t, "5d"), pd.DataFrame())
                                    if s_df is not None and not s_df.empty:
                                        c_val = float(s_df['Close'].iloc[-1])
                                        p_val = float(s_df['Close'].iloc[-2])
                                        pct_chg = ((c_val - p_val) / p_val) * 100
                                        snapshot_str += f"- {k}: {c_val:,.2f} ({pct_chg:+.2f}%)\n"
                                except:
                                    pass
                                    
                            p_text = f"""
                            你是一位頂級美股對沖基金經理人。以下是我剛剛抓取的今日市場核心總經數據即時快照：
                            {snapshot_str}
                            
                            請針對目前的波動率、債市殖利率與跨市場表現，為我提供一份精煉的「今日總經晨報」，包含：
                            1. 宏觀解讀（資金流向與潛在風險）。
                            2. 今日操作提醒（應對當前環境的現金水位或避險方針）。
                            請給出專業、直擊重點的繁體中文建議。
                            """
                            res = client.models.generate_content(model='gemini-3.8-flash', contents=p_text)
                            st.session_state.ai_memo_p1["text"] = res.text
                            st.session_state.ai_memo_p1["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        except Exception as e:
                            st.error(f"晨報生成失敗: {e}")
                
                if st.session_state.ai_memo_p1["text"]:
                    st.markdown("---")
                    st.info(f"📌 **最新總經晨報 (生成時間：{st.session_state.ai_memo_p1['time']})**")
                    st.markdown(st.session_state.ai_memo_p1["text"])
                    if st.button("📤 推播此總經晨報到 Telegram", key="btn_tg_p1"):
                        send_to_telegram("AI 總經晨報與操盤提醒", st.session_state.ai_memo_p1["text"])
                                
            st.markdown("---")

            with st.expander("完整日 K（可摺疊）", expanded=False):
                _render_page1_charts(hist, indicators)
        except Exception as e:
            st.error(f"總經圖表載入失敗: {e}")

# ==========================================
# 第二頁：板塊與主題資金輪動
# ==========================================
elif page == "第二頁：板塊與主題資金輪動":
    c1, c2 = st.columns([0.85, 0.15])
    with c1:
        st.title("🔥 第二頁：美股 11 大板塊與焦點主題 ETF (軟體/硬體/太空) 戰情解構")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p2", use_container_width=True):
            cached_history_map.clear()
            cached_top_holdings_map.clear()
            cached_etf_holding_rows.clear()
            cached_symbol_closes.clear()
            st.rerun()
    st.markdown("---")
    st.info("追蹤 11 大 SPDR 與主題 ETF 的價格漲跌。上方的相對強弱是對 S&P 500 的價格比較，不是資金流。")

    sectors_dict = {
        "XLK": "科技", "XLC": "通訊服務", "XLY": "非必需消費", "XLF": "金融", "XLV": "醫療保健", 
        "XLI": "工業", "XLE": "能源", "XLB": "原物料", "XLP": "必需消費", "XLU": "公用事業", 
        "XLRE": "房地產", "IGV": "軟體 ETF", "SOXX": "半導體 ETF", "UFO": "太空 ETF"
    }

    with st.spinner("📡 正在同步板塊與主題 ETF 數據..."):
        try:
            performance_list = []
            sector_data_cache = {}
            history_keys = tuple((ticker, "1y") for ticker in sectors_dict) + (("SPY", "1y"),)
            etf_hist = cached_history_map(history_keys)
            benchmark = etf_hist.get(("SPY", "1y"), pd.DataFrame())
            holding_rows = cached_etf_holding_rows(tuple(sectors_dict.keys()))
            from macro_cycle_live import macro_view_for_orders

            macro_view = macro_view_for_orders()
            regime = (macro_view or {}).get("regime")
            board_rows = []
            holdings_map = {}
            for ticker, name in sectors_dict.items():
                frame = etf_hist.get((ticker, "1y"), pd.DataFrame())
                board_rows.append(sector_board.board_row(ticker, name, frame, benchmark, macro_view, regime))
                holdings_map[ticker] = sector_board.format_holdings(holding_rows.get(ticker) or [])
            symbols = tuple(sorted({item["symbol"] for rows in holding_rows.values() for item in rows}))
            closes = cached_symbol_closes(symbols)
            strong_by_etf = {
                ticker: sector_board.strong_holdings(holding_rows.get(ticker) or [], closes)
                for ticker in sectors_dict
            }
            sector_board.render_sector_summary(st, board_rows, strong_by_etf)
            st.markdown("---")
            for ticker, name in sectors_dict.items():
                df_k = etf_hist.get((ticker, "1y"), pd.DataFrame())
                if df_k is None:
                    df_k = pd.DataFrame()
                else:
                    df_k = df_k.copy()
                if not df_k.empty:
                    df_k['MA5'] = df_k['Close'].rolling(window=5).mean()
                    df_k['MA20'] = df_k['Close'].rolling(window=20).mean()
                    df_k['MA60'] = df_k['Close'].rolling(window=60).mean()
                    df_k['MA200'] = df_k['Close'].rolling(window=200).mean()
                    sector_data_cache[ticker] = df_k
                    
                    latest_close = float(df_k['Close'].iloc[-1])
                    daily_pct = ((latest_close - float(df_k['Close'].iloc[-2])) / float(df_k['Close'].iloc[-2])) * 100.0 if len(df_k) >= 2 else 0.0
                    
                    recent_month = df_k.tail(20)
                    monthly_pct = ((float(recent_month['Close'].iloc[-1]) - float(recent_month['Close'].iloc[0])) / float(recent_month['Close'].iloc[0])) * 100.0 if len(recent_month) >= 2 else 0.0
                    
                    performance_list.append({
                        "代號": ticker, "板塊名稱": name, "收盤價": latest_close,
                        "日漲跌幅 (%)": round(daily_pct, 2), "月漲跌幅 (%)": round(monthly_pct, 2),
                        "前十大真實持股": holdings_map.get(ticker) or "未取得真實持股"
                    })
            
            df_sectors = pd.DataFrame(performance_list).sort_values(by="月漲跌幅 (%)", ascending=False)
            st.subheader("📊 板塊與主題 ETF 漲跌排行榜")
            st.dataframe(df_sectors.style.format({"收盤價": "{:.2f}", "日漲跌幅 (%)": "{:+.2f}%", "月漲跌幅 (%)": "{:+.2f}%"}).background_gradient(subset=["日漲跌幅 (%)", "月漲跌幅 (%)"], cmap="RdYlGn"), use_container_width=True, hide_index=True)
            st.markdown("---")

            with st.expander("板塊日 K（可摺疊）", expanded=False):
                tickers_sorted = df_sectors["代號"].tolist()
                for i in range(0, len(tickers_sorted), 2):
                    cols = st.columns(2)
                    for j in range(2):
                        if i + j >= len(tickers_sorted):
                            continue
                        t_code = tickers_sorted[i + j]
                        t_name = sectors_dict[t_code]
                        df_chart = sector_data_cache.get(t_code)
                        if df_chart is None or df_chart.empty:
                            continue
                        with cols[j]:
                            st.markdown(f"**{t_code} - {t_name}**")
                            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.75, 0.25])
                            fig.add_trace(go.Candlestick(
                                x=df_chart.index, open=df_chart["Open"], high=df_chart["High"], low=df_chart["Low"], close=df_chart["Close"], name="K線"
                            ), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart["MA5"], name="5MA", line=dict(color="#FFEE58", width=1)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart["MA20"], name="20MA", line=dict(color="#29B6F6", width=1.5)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart["MA60"], name="60MA", line=dict(color="#FFA726", width=1.5)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart["MA200"], name="200MA", line=dict(color="#AB47BC", width=2)), row=1, col=1)
                            colors = ["#EF5350" if row["Close"] < row["Open"] else "#26A69A" for _, row in df_chart.iterrows()]
                            fig.add_trace(go.Bar(x=df_chart.index, y=df_chart["Volume"], name="成交量", marker_color=colors), row=2, col=1)
                            fig.update_layout(
                                height=320, margin=dict(l=10, r=10, t=10, b=10),
                                xaxis_rangeslider_visible=False, showlegend=True,
                                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                                template="plotly_dark",
                            )
                            st.plotly_chart(fig, use_container_width=True)

            st.markdown("---")

            if st.button("🧠 啟動 AI 板塊與主題資金輪動解析", use_container_width=True):
                with st.spinner("AI 經理人正在解析..."):
                    try:
                        gemini_key = os.environ.get("GEMINI_API_KEY") or read_secret("", "GEMINI_API_KEY")
                        client = genai.Client(api_key=gemini_key)
                        summary_table = df_sectors.to_string(index=False)
                        res_p2 = client.models.generate_content(model='gemini-3.8-flash', contents=f"身為對沖基金經理人，解析以下板塊與主題ETF數據：\n{summary_table}\n請提供資金流向與強勢/弱勢板塊解讀。")
                        st.session_state.ai_memo_p2["text"] = res_p2.text
                        st.session_state.ai_memo_p2["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        st.error(f"生成失敗: {e}")
            
            if st.session_state.ai_memo_p2["text"]:
                st.info(f"📌 **分析結果（時間：{st.session_state.ai_memo_p2['time']}）**")
                st.markdown(st.session_state.ai_memo_p2["text"])
                if st.button("📤 推播到 Telegram", key="btn_tg_p2"):
                    send_to_telegram("AI 板塊與主題資金輪動深度分析", st.session_state.ai_memo_p2["text"])
        except Exception as e:
            st.error(f"資料抓取失敗: {e}")

# ==========================================
# 第三頁：重大行事曆與會展 (已串接 FMP API)
# ==========================================
elif page == "第三頁：重大行事曆與會展":
    c1, c2 = st.columns([0.85, 0.15])
    with c1:
        st.title("📅 第三頁：持倉與觀察清單行事曆")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p3", use_container_width=True):
            cached_yahoo_earnings.clear()
            cached_fmp_raw.clear()
            st.rerun()
    st.markdown("---")
    watchlist = st.text_input(
        "觀察清單（選填，逗號分隔。只留在這次工作階段，不寫入試算表）",
        key="calendar_watchlist",
    )
    try:
        calendar_model = load_calendar_model(watchlist)
    except Exception as exc:
        calendar_model = calendar_events.build_calendar(
            None,
            watchlist,
            {},
            None,
            datetime.now().date(),
            datetime.now(calendar_events.TAIPEI),
            fmp_error=f"行事曆組裝失敗：{exc}",
        )
    if any("持倉讀不到" in note for note in calendar_model["notes"]):
        st.warning("持倉讀不到。台股制度事件仍顯示，部位對照這次空白。")
    gemini_key = os.environ.get("GEMINI_API_KEY") or read_secret("", "GEMINI_API_KEY") or ""
    calendar_page.render_calendar(
        st,
        calendar_model,
        memos=st.session_state.ai_memo_p3,
        gemini_key=str(gemini_key),
        complete_analysis=calendar_actions.complete_event_analysis,
        send=send_to_telegram,
    )

# ==========================================
# 第四頁：部位監控與量化風控 (完整風控與 Telegram 推播)
# ==========================================
elif page == "第四頁：部位監控與量化風控":
    c1, c2 = st.columns([0.85, 0.15])
    with c1:
        st.title("🛡️ 第四頁：部位監控、Beta 中性化與精細產業配置戰情室")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p4", use_container_width=True):
            clear_market_caches()
            st.rerun()
            
    st.write("數據來源：Notion 交易日誌 ➔ Make.com ➔ Google Sheets (基礎計價幣別：TWD 台幣)")
    st.markdown("---")

    with st.spinner('📡 正在穿透雲端，即時連線抓取各個持股報價與計算動態風險...'):
        try:
            account_data, nav_data, quant_data = load_quant_database()
            fx_quote = usdtwd_quote(nav_data)
            usdtwd_rate = fx_quote.rate
            if fx_quote.is_fallback:
                st.warning(f"⚠️ {fx_quote.warning()}")
            df = normalize_positions(quant_data)
            bundle = {}
            if not df.empty and "標的" in df.columns:
                bundle = cached_quote_bundle(_ticker_kind_pairs(df), convertible_quote_context(df))
            df, account_data, metrics, row_warnings = value_positions_safely(
                df, account_data, usdtwd_rate,
                bundle.get("prices"), bundle.get("betas"), bundle.get("sectors"), extras=bundle.get("extras"),
                beta_fallbacks=st.session_state.get(BETA_FALLBACK_STATE_KEY),
            )
            for row_warning in row_warnings:
                st.warning(f"⚠️ {row_warning}")
            total_net_liq = metrics["nav"]

            # 淨值歷史：讀 NAV_Snapshots（API 每日寫入），沒有才退回舊的 Daily_NAV；都沒有時只有今天一點。
            nav_series = nav_growth_series(nav_data, total_net_liq)
            capture = page4_capture(nav_data)
            # 回撤用扣除出入金的淨值指數；今天若已寫快照，當天出入金也一併扣掉。
            nav_flows = nav_flow_map(nav_data)

            if not df.empty:
                from zoneinfo import ZoneInfo

                from stop_rules import stop_alarm_text

                stop_dates = cached_stop_dates()
                today = datetime.now(ZoneInfo("Asia/Taipei")).date()
                alarms = df[(df['最大虧損比例'] > -100) & (df['報酬率 (%)'] <= df['最大虧損比例'])]
                if not alarms.empty:
                    st.error("🚨 **【系統風控警報】偵測到部位觸及最大虧損限制，請評估是否停損處理！**")
                    for idx, row in alarms.iterrows():
                        name_display = row['名稱'] if row['名稱'] else row['標的']
                        source = str(row["停損來源"]) if "停損來源" in df.columns else ""
                        seen = stop_dates.get(f"{row['帳戶']}|{str(row['標的']).upper()}")
                        st.warning(stop_alarm_text(name_display, row['標的'], float(row['報酬率 (%)']), float(row['最大虧損比例']), source, seen, today))
                    
                    if st.button("🚨 一鍵推播停損警報至 Telegram", type="primary"):
                        send_to_telegram("量化戰情室風控警報", "偵測到部位觸及最大虧損限制，請立即前往戰情室檢查！")
                    st.markdown("---")

            # 風險與績效：Beta／槓桿三列表、現金、借款、最大回撤、回撤煞車燈合併成一張卡
            render_risk_performance_card(st, metrics, nav_series, capture, nav_flows)
            if not df.empty:
                known_exposure = pd.to_numeric(df['實質曝險(台幣)'], errors="coerce")
                total_exposure_sum = float(known_exposure[np.isfinite(known_exposure)].sum()) if known_exposure.notna().any() else 0.0
                if metrics.get("expired_removed"):
                    st.info(f"ℹ️ 已自動過期排除之選擇權標的：{', '.join(metrics['expired_removed'])}")
                st.markdown("---")

                st.subheader("🏢 各精細產業別持股曝險與資金佔比分佈")
                if total_exposure_sum <= 0:
                    st.caption("已知部位沒有名目曝險。缺 Delta 的部位不畫進圓餅圖，也不當成 0。")
                else:
                    known_rows = df[df["曝險已知"] != False] if "曝險已知" in df.columns else df
                    sector_grouped = known_rows.groupby('產業別').agg(實質曝險合計=('實質曝險(台幣)', 'sum'), 標的數量=('標的', 'count')).reset_index()
                    sector_grouped['產業曝險佔比 (%)'] = (sector_grouped['實質曝險合計'] / total_exposure_sum) * 100.0
                    sector_grouped = sector_grouped.sort_values(by='產業曝險佔比 (%)', ascending=False)

                    sec_col1, sec_col2 = st.columns([1.5, 2.5])
                    with sec_col1:
                        st.dataframe(sector_grouped.style.format({"實質曝險合計": "NT$ {:,.0f}", "產業曝險佔比 (%)": "{:.2f}%"}).background_gradient(subset=["產業曝險佔比 (%)"], cmap="Blues"), use_container_width=True, hide_index=True)
                    with sec_col2:
                        import plotly.express as px
                        fig_sec = px.pie(sector_grouped, names='產業別', values='產業曝險佔比 (%)', title="各精細產業別資金曝險佔比圓餅圖", hole=0.4, template="plotly_dark")
                        fig_sec.update_traces(textinfo='percent+label')
                        fig_sec.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
                        st.plotly_chart(fig_sec, use_container_width=True)

            st.markdown("---")
            st.subheader("🏦 各帳戶動態風險與全局槓桿 (即時市價結算)")
            if not account_data.empty:
                if metrics.get("futures_initial", 0) > 0:
                    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
                    fc1.metric("元大權益數", f"NT$ {metrics.get('futures_equity', 0):,.0f}", f"本日餘額 NT$ {metrics.get('futures_cash_ledger', 0):,.0f}")
                    risk_ind = equity_ratio_pct(metrics.get("futures_equity"), metrics.get("futures_initial"))
                    fc2.metric("權益比率(權益/原始)", "—" if risk_ind is None else f"{risk_ind:.2f}%")
                    fc3.metric("可動用保證金", f"NT$ {metrics.get('excess_margin', 0):,.0f}")
                    fc4.metric("原始/維持保證金", f"{metrics.get('futures_initial', 0):,.0f} / {metrics.get('futures_maint', 0):,.0f}")

                    mc_dist = metrics.get("margin_call_distance", 0)
                    mc_pts = metrics.get("margin_call_points")
                    if metrics.get("margin_call"):
                        fc5.error(f"距追繳: NT$ {mc_dist:,.0f}")
                    else:
                        delta_str = f"約 {mc_pts:,.0f} 點" if (mc_pts is not None and not pd.isna(mc_pts)) else None
                        fc5.metric("距追繳", f"NT$ {mc_dist:,.0f}", delta_str)

                labels = metrics.get("futures_price_labels")
                if labels:
                    lbl_text = "期貨報價：" + "；".join(labels)
                    if any("備援" in l or "無報價" in l for l in labels):
                        st.warning(lbl_text)
                    else:
                        st.caption(lbl_text)

                acc_cols = [c for c in ['帳戶名稱', '現金餘額', '融資/質押借款', '名目曝險', '帳戶淨值', '可動用保證金', '距追繳金額', '槓桿率 (倍)', '權益比率%/現金水位%'] if c in account_data.columns]

                def _known_num(pattern: str):
                    def _fmt(value):
                        try:
                            number = float(value)
                        except (TypeError, ValueError):
                            return "未計入"
                        if number != number:
                            return "未計入"
                        return pattern.format(number)
                    return _fmt

                fmt_dict = {
                    "現金餘額": "NT$ {:,.0f}", "融資/質押借款": "NT$ {:,.0f}", "名目曝險": _known_num("NT$ {:,.0f}"),
                    "帳戶淨值": "NT$ {:,.0f}", "可動用保證金": "NT$ {:,.0f}", "距追繳金額": "NT$ {:,.0f}",
                    "槓桿率 (倍)": _known_num("{:.2f}x"), "權益比率%/現金水位%": "{:.1f}%"
                }
                st.dataframe(account_data[acc_cols].style.format(fmt_dict), use_container_width=True, hide_index=True)
                st.caption("元大期貨：現金餘額＝本日餘額；帳戶淨值＝權益數＝本日餘額＋期貨未實現損益（期交所即時/夜盤價）；權益比率＝權益數÷原始保證金。名目曝險顯示「未計入」代表這個帳戶的部位都缺 Delta，不是曝險為 0。")
                ledger_label = str(metrics.get("futures_cash_label") or "")
                if ledger_label:
                    st.caption(f"元大明細：{ledger_label}")
                if metrics.get("futures_cash_detail"):
                    st.caption(str(metrics.get("futures_cash_detail")))
                if metrics.get("futures_cash_stale"):
                    st.warning("校正已超過 30 天，請用元大期貨截圖校正 Futures_Ledger（日期、本日餘額、當時持倉均價）。")
                if metrics.get("futures_cash_warning"):
                    st.warning(str(metrics.get("futures_cash_warning")))

            st.markdown("---")
            st.subheader("🎯 微觀持倉、Beta 值與精細產業即時損益追蹤")
            if not df.empty:
                total_invested_twd = float((df['實質佔用本金(原幣)'] * df['換算匯率']).sum())
                total_pnl_twd = float((df['淨未實現損益(原幣)'] * df['換算匯率']).sum())
                total_roi_pct = float((total_pnl_twd / total_invested_twd) * 100.0) if total_invested_twd > 0 else 0.0
                
                c_a, c_b = st.columns(2)
                c_a.metric("全帳戶持倉投入總金額 (TWD)", f"NT$ {total_invested_twd:,.0f}", "")
                c_b.metric("總淨未實現損益 (TWD)", f"NT$ {total_pnl_twd:,.0f}", f"{total_roi_pct:+.2f}%")
                
                base_cols = ['帳戶', '產業別', '種類', '標的', '名稱', 'Beta值', '有效Beta', 'Beta來源', '真實風險權重 (%)', '標的槓桿', '持有數量', '平均成本', '即時報價']
                has_convertible = "種類" in df.columns and any(is_convertible_row(row) for _, row in df.iterrows())
                if has_convertible:
                    base_cols.extend(['Delta', '轉換價值(百元)', '轉換溢價(%)'])
                if '報價來源' in df.columns:
                    base_cols.append('報價來源')
                base_cols.extend(['顯示_投入總金額(原幣)', '顯示_即時市值(原幣)', '實質曝險(台幣)', '曝險Beta', 'Beta曝險(台幣)', '淨未實現損益(原幣)', '報酬率 (%)', '最大虧損比例', '停損來源'])
                cols = [c for c in base_cols if c in df.columns]
                display_df = df[cols].copy()
                display_df.rename(columns={'顯示_投入總金額(原幣)': '投入總金額', '顯示_即時市值(原幣)': '即時市值(原幣)', '淨未實現損益(原幣)': '淨損益(原幣)', '實質曝險(台幣)': '名目曝險(台幣)'}, inplace=True)
                display_df['最大虧損比例'] = display_df['最大虧損比例'].apply(lambda x: f"{x:.0f}%" if x > -100 else "未設定")

                def _blank_num(pattern: str):
                    def _fmt(value):
                        try:
                            number = float(value)
                        except (TypeError, ValueError):
                            return "—"
                        if number != number:
                            return "—"
                        return pattern.format(number)
                    return _fmt

                st.dataframe(display_df.style.format({
                    "Beta值": "{:.2f}", "有效Beta": "{:.2f}", "標的槓桿": "{:.1f}x", "持有數量": "{:,.0f}",
                    "平均成本": "{:,.2f}", "即時報價": "{:,.2f}", "投入總金額": "{:,.0f}",
                    "即時市值(原幣)": "{:,.0f}", "淨損益(原幣)": "{:,.0f}", "報酬率 (%)": "{:+.2f}%",
                    "Delta": _blank_num("{:.2f}"), "轉換價值(百元)": _blank_num("{:,.2f}"), "轉換溢價(%)": _blank_num("{:+.1f}%"),
                    "名目曝險(台幣)": _blank_num("{:,.0f}"), "Beta曝險(台幣)": _blank_num("{:,.0f}"), "曝險Beta": _blank_num("{:.2f}"),
                    "真實風險權重 (%)": _blank_num("{:.2f}%"),
                }, precision=2), use_container_width=True, hide_index=True)
                st.caption("名目曝險空白表示 Delta 未知，沒有把這筆算成 0。停損來源「試算表」是持倉上寫的數字；「預設」是空白時依個股 −10%、2 倍 −15%、3 倍 −20% 補上。期貨、選擇權、可轉債沒有預設。")

                if metrics.get("beta_fallbacks_used"):
                    names = "、".join(f"{item.get('標的')}（{item.get('Beta來源')}）" for item in metrics["beta_fallbacks_used"])
                    st.warning(f"以下持倉抓不到標的即時一年 Beta，暫用預設值：{names}。預設值可在第六頁「下單輔助系統」→「新標的才需要改：Beta、槓桿、產業」修改。")
                st.caption("Beta值＝這檔自己的一年 Beta；有效Beta＝套在市值上的 Beta（槓桿 ETF＝槓桿 × 標的一年 Beta，對 S&P 500 或加權指數）；曝險Beta＝套在名目曝險上的 Beta（槓桿 ETF＝標的一年 Beta）。Beta曝險＝名目曝險 × 曝險Beta＝市值 × 有效Beta，槓桿只乘一次。期貨的即時市值是名目市值（報價 × 乘數 × 口數），不是未實現損益。")
                if has_convertible:
                    st.caption("可轉債／CBAS：市值＝百元報價 × 面額／100 × 張數，計入淨值。名目曝險＝Delta × 轉換價值（不加標的槓桿）。轉換價值（百元）＝股價／轉換價格 × 100。不把 CB 與 CBAS 互抵，也不做跨市場對沖。")

            st.markdown("---")
            st.info("🤖 AI 壓力測試已移到第六頁「下單輔助系統」→「壓力測試」分頁，壓力測試集中在同一處。")

        except Exception as e:
            st.error(f"連線發生錯誤。系統訊息: {e}")

# ==========================================
# 第五頁：總經週期與配置（本機 JSON，不打外部 API）
# ==========================================
elif page == "第五頁：總經週期與配置":
    render_macro_cycle_page(load_book=load_book_for_page5, clear_caches=clear_market_caches)

# ==========================================
# 第六頁：下單輔助（只試算，不送委託）
# ==========================================
elif page == "第六頁：下單輔助系統":
    render_order_assistant(
        send_to_telegram,
        load_quant_database,
        get_market_beta_and_sector,
        load_market=load_market_for_page6,
        clear_caches=clear_market_caches,
        stop_dates_fn=cached_stop_dates,
    )