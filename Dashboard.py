import os
import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import gspread
import requests
import re
from datetime import datetime, timedelta
from google import genai
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from oauth2client.service_account import ServiceAccountCredentials
from order_assistant_ui import render_order_assistant
from portfolio_risk import (
    attach_quotes,
    fetch_quant_sheets,
    fetch_twii,
    fetch_usdtwd,
    finalize_valuation,
    load_service_account_info,
    market_beta_and_sector,
    normalize_positions,
    populate_market_fields,
)

# ==========================================
# 網頁基本設定與密碼驗證防護（支援環境變數與 Secret）
# ==========================================
st.set_page_config(page_title="量化交易戰鬥艙", page_icon="⚔️", layout="wide", initial_sidebar_state="expanded")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or st.secrets.get("passwords", {}).get("admin", "")

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
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "") or st.secrets.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "") or st.secrets.get("TELEGRAM_CHAT_ID", "")
        
        if bot_token and chat_id:
            tg_msg = f"🤖 **【{title}】**\n\n{text_content}"
            res = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": tg_msg}, timeout=5)
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


@st.cache_data(ttl=60)
def load_quant_database():
    key_data = load_service_account_info(lambda: dict(st.secrets["google_key"]))
    return fetch_quant_sheets(key_data)


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
    page = st.radio("切換監控面板：", ["第一頁：總經與風險鬧鐘", "第二頁：板塊與主題資金輪動", "第三頁：重大行事曆與會展", "第四頁：部位監控與量化風控", "第五頁：Telegram 戰情推播", "第六頁：下單輔助系統"])
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
            vix_df = yf.Ticker("^VIX").history(period="5d")
            vix_val = float(vix_df['Close'].iloc[-1]) if not vix_df.empty else 15.0
            
            tnx_df = yf.Ticker("^TNX").history(period="5d")
            tnx_val = float(tnx_df['Close'].iloc[-1]) / 10.0 if not tnx_df.empty else 4.0
            
            risk_score = 0
            risk_labels = []
            
            if vix_val > 25:
                risk_score += 4
                risk_labels.append(f"🚨 VIX 市場極度恐慌 ({vix_val:.2f} > 25)")
            elif vix_val > 20:
                risk_score += 2
                risk_labels.append(f"⚠️ VIX 恐慌指數偏高 ({vix_val:.2f} > 20)")
                
            if tnx_val > 4.5:
                risk_score += 2
                risk_labels.append(f"⚠️ 10年債殖利率處於高檔 ({tnx_val:.2f}% > 4.5%)")
                
            st.markdown("### 🛡️ 宏觀市場風險計分板 (標準化評估)")
            rc1, rc2, rc3 = st.columns([1.5, 2, 4.5])
            with rc1:
                if risk_score <= 1:
                    st.success("🟢 市場氣氛：【風險偏好 (Risk-On)】")
                elif risk_score <= 3:
                    st.warning("🟡 市場氣氛：【中性觀望 / 局部震盪】")
                else:
                    st.error("🔴 市場氣氛：【高風險警戒 (Risk-Off)】")
            with rc2:
                st.metric("當前綜合風險評分", f"{risk_score} / 6 分", f"VIX: {vix_val:.2f} | 債息: {tnx_val:.2f}%")
            with rc3:
                if risk_labels:
                    st.error(" | ".join(risk_labels))
                else:
                    st.info("✅ 目前無重大宏觀警訊，市場流動性與波動率維持健康 (Risk-On)。")
                    
            st.markdown("---")
            
            with st.expander("🤖 點擊展開：AI 避險基金經理人今日總經晨報與操盤提醒", expanded=True):
                if st.button("🧠 一鍵生成今日總經盤勢速覽", use_container_width=True):
                    with st.spinner("AI 經理人正在解讀各大總經指標..."):
                        try:
                            gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                            client = genai.Client(api_key=gemini_key)
                            snapshot_str = f"- VIX恐慌指數: {vix_val:.2f}\n- 10年公債殖利率: {tnx_val:.2f}%\n"
                            
                            for k, t in indicators.items():
                                try:
                                    s_df = yf.Ticker(t).history(period="5d")
                                    if not s_df.empty:
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

            cols = st.columns(2)
            for idx, (name, ticker) in enumerate(indicators.items()):
                try:
                    df_item = yf.Ticker(ticker).history(period="1y")
                    if not df_item.empty:
                        df_item['MA5'] = df_item['Close'].rolling(window=5).mean()
                        df_item['MA20'] = df_item['Close'].rolling(window=20).mean()
                        df_item['MA60'] = df_item['Close'].rolling(window=60).mean()
                        df_item['MA200'] = df_item['Close'].rolling(window=200).mean()
                        
                        curr = float(df_item['Close'].iloc[-1])
                        prev = float(df_item['Close'].iloc[-2])
                        pct = ((curr - prev) / prev) * 100
                        
                        with cols[idx % 2]:
                            st.markdown(f"**{name}**：`{curr:,.2f}`  (`{pct:+.2f}%`)")
                            
                            fig = go.Figure()
                            fig.add_trace(go.Scatter(x=df_item.index, y=df_item['Close'], name='收盤價', line=dict(color='#FFFFFF', width=1.2)))
                            fig.add_trace(go.Scatter(x=df_item.index, y=df_item['MA5'], name='5MA', line=dict(color='#FFEE58', width=1)))
                            fig.add_trace(go.Scatter(x=df_item.index, y=df_item['MA20'], name='20MA', line=dict(color='#29B6F6', width=1.5)))
                            fig.add_trace(go.Scatter(x=df_item.index, y=df_item['MA60'], name='60MA', line=dict(color='#FFA726', width=1.5)))
                            fig.add_trace(go.Scatter(x=df_item.index, y=df_item['MA200'], name='200MA', line=dict(color='#AB47BC', width=2)))
                            
                            fig.update_layout(height=250, margin=dict(l=10, r=10, t=10, b=10), showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)), template="plotly_dark")
                            st.plotly_chart(fig, use_container_width=True)
                except:
                    pass
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
            st.rerun()
    st.markdown("---")
    st.info("💡 追蹤 11 大核心 SPDR 行業 ETF 之外，同步解構焦點主題 ETF：軟體 (IGV)、硬體 (SOXX)、太空 (UFO) 之日／月漲跌幅與前十大持股。")

    sectors_dict = {
        "XLK": "科技", "XLC": "通訊服務", "XLY": "非必需消費", "XLF": "金融", "XLV": "醫療保健", 
        "XLI": "工業", "XLE": "能源", "XLB": "原物料", "XLP": "必需消費", "XLU": "公用事業", 
        "XLRE": "房地產", "IGV": "軟體 ETF", "SOXX": "半導體 ETF", "UFO": "太空 ETF"
    }

    fallback_top10 = {
        "XLK": "1. MSFT (21.5%), 2. AAPL (18.2%), 3. NVDA (15.8%), 4. AVGO (6.4%)",
        "XLC": "1. GOOGL (22.5%), 2. META (21.8%), 3. NFLX (6.2%)",
        "XLY": "1. AMZN (33.5%), 2. TSLA (13.2%), 3. HD (8.5%)",
        "XLF": "1. BRK.B (13.2%), 2. JPM (11.8%), 3. V (7.5%)",
        "XLV": "1. LLY (12.5%), 2. UNH (9.8%), 3. JNJ (8.2%)",
        "XLI": "1. GE (5.5%), 2. CAT (5.2%), 3. RTX (4.8%)",
        "XLE": "1. XOM (23.5%), 2. CVX (15.2%), 3. COP (7.8%)",
        "XLB": "1. LIN (19.5%), 2. SHW (11.2%), 3. FCX (8.5%)",
        "XLP": "1. PG (14.2%), 2. WMT (13.8%), 3. COST (11.5%)",
        "XLU": "1. NEE (15.5%), 2. SO (8.2%), 3. DUK (7.5%)",
        "XLRE": "1. PLD (14.5%), 2. AMT (12.2%), 3. EQIX (8.5%)",
        "IGV": "1. MSFT (8.8%), 2. CRM (8.5%), 3. NOW (8.2%)",
        "SOXX": "1. NVDA (8.5%), 2. AVGO (8.2%), 3. QCOM (7.8%)",
        "UFO": "1. IRDM (8.2%), 2. PL (7.8%), 3. BKSY (7.2%)"
    }

    def get_auto_top_holdings(ticker_symbol):
        try:
            funds = yf.Ticker(ticker_symbol).funds_data
            th = funds.top_holdings
            if th is not None and not th.empty:
                holdings_list = []
                for idx, row in th.iterrows():
                    sym = str(idx) if isinstance(idx, str) else str(row.get("symbol", idx))
                    w_col = [c for c in th.columns if any(k in c.lower() for k in ["percent", "weight", "holding"])]
                    val = 0.0
                    if w_col:
                        raw_val = float(row[w_col[0]])
                        val = raw_val * 100 if raw_val <= 1.0 else raw_val
                    holdings_list.append((sym, val))
                holdings_list.sort(key=lambda x: x[1], reverse=True)
                formatted_str = ", ".join([f"{idx+1}. {s} ({w:.1f}%)" for idx, (s, w) in enumerate(holdings_list[:10])])
                if formatted_str:
                    return formatted_str
        except:
            pass
        return fallback_top10.get(ticker_symbol, "資料同步中...")

    with st.spinner("📡 正在同步板塊與主題 ETF 數據..."):
        try:
            performance_list = []
            sector_data_cache = {}
            for ticker, name in sectors_dict.items():
                df_k = yf.Ticker(ticker).history(period="1y")
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
                        "前十大核心持股與佔比": get_auto_top_holdings(ticker)
                    })
            
            df_sectors = pd.DataFrame(performance_list).sort_values(by="月漲跌幅 (%)", ascending=False)
            st.subheader("📊 板塊與主題 ETF 漲跌排行榜")
            st.dataframe(df_sectors.style.format({"收盤價": "{:.2f}", "日漲跌幅 (%)": "{:+.2f}%", "月漲跌幅 (%)": "{:+.2f}%"}).background_gradient(subset=["日漲跌幅 (%)", "月漲跌幅 (%)"], cmap="RdYlGn"), use_container_width=True, hide_index=True)
            st.markdown("---")

            st.subheader("📈 板塊與焦點主題 ETF 1 年期多均線互動式日 K 線與成交量戰情圖")
            tickers_sorted = df_sectors['代號'].tolist()

            for i in range(0, len(tickers_sorted), 2):
                cols = st.columns(2)
                for j in range(2):
                    if i + j < len(tickers_sorted):
                        t_code = tickers_sorted[i + j]
                        t_name = sectors_dict[t_code]
                        df_chart = sector_data_cache[t_code]

                        with cols[j]:
                            st.markdown(f"**{t_code} - {t_name}**")

                            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.75, 0.25])

                            fig.add_trace(go.Candlestick(
                                x=df_chart.index, open=df_chart['Open'], high=df_chart['High'], low=df_chart['Low'], close=df_chart['Close'], name='K線'
                            ), row=1, col=1)

                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['MA5'], name='5MA', line=dict(color='#FFEE58', width=1)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['MA20'], name='20MA', line=dict(color='#29B6F6', width=1.5)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['MA60'], name='60MA', line=dict(color='#FFA726', width=1.5)), row=1, col=1)
                            fig.add_trace(go.Scatter(x=df_chart.index, y=df_chart['MA200'], name='200MA', line=dict(color='#AB47BC', width=2)), row=1, col=1)

                            colors = ['#EF5350' if row['Close'] < row['Open'] else '#26A69A' for index, row in df_chart.iterrows()]
                            fig.add_trace(go.Bar(x=df_chart.index, y=df_chart['Volume'], name='成交量', marker_color=colors), row=2, col=1)

                            fig.update_layout(
                                height=380, margin=dict(l=10, r=10, t=10, b=10),
                                xaxis_rangeslider_visible=False, showlegend=True,
                                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                                template="plotly_dark"
                            )
                            st.plotly_chart(fig, use_container_width=True)

            st.markdown("---")

            if st.button("🧠 啟動 AI 板塊與主題資金輪動解析", use_container_width=True):
                with st.spinner("AI 經理人正在解析..."):
                    try:
                        gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
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
        st.title("📅 第三頁：重大行事曆與 M7 財報戰情室 (FMP 動態整合)")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p3", use_container_width=True):
            st.rerun()
    st.markdown("---")
    st.info("💡 系統已透過 **Financial Modeling Prep (FMP) API** 自動抓取未來 3 個月內的全球總經事件、通膨與央行決議，並同步整合 M7 財報日。")

    today = datetime.now().date()
    three_months_later = today + timedelta(days=90)

    # 1. 動態透過 FMP 抓取經濟行事曆
    @st.cache_data(ttl=3600)
    def get_fmp_economic_calendar():
        fmp_key = os.environ.get("FMP_API_KEY") or st.secrets.get("FMP_API_KEY", "")
        if not fmp_key:
            return []
        
        today_str = today.strftime("%Y-%m-%d")
        future_str = three_months_later.strftime("%Y-%m-%d")
        url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today_str}&to={future_str}&apikey={fmp_key}"
        
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                events = []
                for item in data:
                    country = item.get("country", "")
                    if country in ["US", "United States", "Global", "EMU"]:
                        ev_date = str(item.get("date", ""))[:10]
                        ev_name = item.get("event", "總經事件")
                        impact = item.get("impact", "Medium")
                        estimate = item.get("estimate")
                        prev = item.get("previous")
                        
                        impact_label = "🔥 高" if impact == "High" else ("⚡ 中" if impact == "Medium" else "📌 低")
                        effect_desc = f"重要性: {impact_label} | 預估值: {str(estimate)} | 前值: {str(prev)}"
                        
                        events.append({
                            "日期": ev_date,
                            "項目": f"{ev_name} ({country})",
                            "影響": effect_desc
                        })
                return events
        except:
            pass
        return []

    # 2. 動態抓取 M7 財報日
    @st.cache_data(ttl=3600)
    def get_m7_earnings_events():
        m7_dict = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "GOOGL": "Alphabet", "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla"}
        events = []
        for ticker, name in m7_dict.items():
            try:
                t_obj = yf.Ticker(ticker)
                cal = t_obj.calendar
                earnings_date = None
                if isinstance(cal, dict):
                    for k in cal.keys():
                        if 'earnings' in k.lower():
                            val = cal[k]
                            if isinstance(val, list) and len(val) > 0:
                                earnings_date = pd.to_datetime(val[0]).date()
                            elif isinstance(val, (datetime, pd.Timestamp)):
                                earnings_date = val.date()
                elif hasattr(cal, 'loc') and not cal.empty:
                    if 'Earnings Date' in cal.index:
                        earnings_date = pd.to_datetime(cal.loc['Earnings Date'].iloc[0]).date()
                
                if earnings_date:
                    events.append({
                        "日期": earnings_date.strftime("%Y-%m-%d"),
                        "項目": f"美股 M7 財報：{name} ({ticker})",
                        "影響": "左右全球 AI 資本支出與科技股情緒連動"
                    })
            except:
                pass
        return events

    fmp_events = get_fmp_economic_calendar()
    m7_events = get_m7_earnings_events()
    
    fallback_base = [
        {"日期": "2026-10-29", "項目": "FOMC 利率決議與聲明 (清晨02:00)", "影響": "基準利率調整與流動性震盪"},
        {"日期": "2026-11-06", "項目": "美國 10 月非農就業報告 (NFP)", "影響": "經濟軟著陸預期與勞動市場體檢"},
        {"日期": "2026-11-12", "項目": "美國 10 月 CPI 消費者物價指數", "影響": "終端通膨黏著度與 Fed 走向"},
        {"日期": "2026-12-10", "項目": "FOMC 利率決議與聲明 (清晨02:00)", "影響": "年底前關鍵利率決議"}
    ]
    
    all_events = (fmp_events if fmp_events else fallback_base) + m7_events
    filtered_events = [ev for ev in all_events if today <= datetime.strptime(ev["日期"], "%Y-%m-%d").date() <= three_months_later]
    filtered_events.sort(key=lambda x: x["日期"])

    if filtered_events:
        st.markdown("### 📌 未來 3 個月動態排程事件")
        for idx, ev in enumerate(filtered_events):
            event_key = f"{ev['日期']}_{ev['項目']}"
            with st.container():
                cols = st.columns([1.2, 2.5, 2.5, 3.8])
                cols[0].markdown(f"**📅 {ev['日期']}**")
                cols[1].markdown(f"**{ev['項目']}**")
                cols[2].markdown(f"⚡ {ev['影響']}")
                
                with cols[3]:
                    sub_c1, sub_c2 = st.columns(2)
                    with sub_c1:
                        if st.button(f"🧠 AI 解析", key=f"ai_event_btn_{idx}"):
                            with st.spinner("AI 經理人正在生成戰術..."):
                                try:
                                    gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                                    client = genai.Client(api_key=gemini_key)
                                    res = client.models.generate_content(model='gemini-3.8-flash', contents=f"身為避險基金經理人，面對事件：【{ev['項目']}】（影響：{ev['影響']}），請給出具體資產配置與風控建議。")
                                    st.session_state.ai_memo_p3[event_key] = {"text": res.text, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                                except Exception as e:
                                    st.error(f"生成失敗: {e}")
                    with sub_c2:
                        if st.button(f"📲 推播TG", key=f"tg_event_btn_{idx}"):
                            send_to_telegram(f"行事曆提醒：{ev['項目']}", f"日期：{ev['日期']}\n影響：{ev['影響']}")
                
                if event_key in st.session_state.ai_memo_p3:
                    memo = st.session_state.ai_memo_p3[event_key]
                    st.info(f"📌 **戰術建議（時間：{memo['time']}）**")
                    st.markdown(memo["text"])
                    if st.button(f"📤 推播此事件建議到 TG", key=f"btn_tg_p3_{idx}"):
                        send_to_telegram(f"行事曆戰術建議：{ev['項目']}", memo["text"])
            st.markdown("---")
    else:
        st.info("目前未來 3 個月內無登錄的重大排程事件。")

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
            load_quant_database.clear()
            st.rerun()
            
    st.write("數據來源：Notion 交易日誌 ➔ Make.com ➔ Google Sheets (基礎計價幣別：TWD 台幣)")
    st.markdown("---")

    with st.spinner('📡 正在穿透雲端，即時連線抓取各個持股報價與計算動態風險...'):
        try:
            account_data, nav_data, quant_data = load_quant_database()
            usdtwd_rate = fetch_usdtwd()
            df = normalize_positions(quant_data)
            if not df.empty:
                prices, betas, sectors = populate_market_fields(df, get_market_beta_and_sector, fetch_twii())
                df = attach_quotes(df, prices, betas, sectors)
            df, account_data, metrics = finalize_valuation(df, account_data, usdtwd_rate)
            total_net_liq = metrics["nav"]
            portfolio_beta = metrics["beta"]
            total_exposure = metrics["exposure"]
            total_cash = metrics["cash"]
            total_debt = metrics["debt"]
            global_leverage = round(metrics["leverage"], 2) if total_net_liq > 0 else 0.0
            global_cash_pct = round(metrics["cash_pct"], 1) if total_net_liq > 0 else 0.0

            st.subheader("📈 歷史資金成長曲線與最大回撤 (MDD) [以台幣計價]")
            if not nav_data.empty and '日期' in nav_data.columns and '總淨值' in nav_data.columns:
                nav_data['日期'] = pd.to_datetime(nav_data['日期']).dt.strftime('%Y-%m-%d')
                nav_data.set_index('日期', inplace=True)
                nav_data['總淨值'] = pd.to_numeric(nav_data['總淨值'], errors='coerce')
                
                today_str = datetime.now().strftime('%Y-%m-%d')
                if not nav_data.index.str.contains(today_str).any():
                    nav_data.loc[today_str] = total_net_liq
                else:
                    nav_data.loc[today_str, '總淨值'] = total_net_liq
                    
                nav_series = nav_data['總淨值'].dropna()
                if not nav_series.empty:
                    current_nav, max_nav = nav_series.iloc[-1], nav_series.max()
                    current_dd_pct = ((current_nav - max_nav) / max_nav) * 100
                    st.metric("最新總淨值 (TWD)", f"NT$ {current_nav:,.0f}", f"距離歷史最高點: {current_dd_pct:.2f}%")
                    st.line_chart(nav_series, height=150)
            else:
                st.metric("最新總淨值 (TWD)", f"NT$ {total_net_liq:,.0f}", "即時市價計算")
            
            st.markdown("---")

            if not df.empty:
                alarms = df[(df['最大虧損比例'] > -100) & (df['報酬率 (%)'] <= df['最大虧損比例'])]
                if not alarms.empty:
                    st.error("🚨 **【系統風控警報】偵測到部位觸及最大虧損限制，請評估是否停損處理！**")
                    for idx, row in alarms.iterrows():
                        name_display = row['名稱'] if row['名稱'] else row['標的']
                        st.warning(f"⚠️ **{name_display} ({row['標的']})**：目前淨報酬率 **{row['報酬率 (%)']:.2f}%** (已跌破設定的 {row['最大虧損比例']:.0f}%)")
                    
                    if st.button("🚨 一鍵推播停損警報至 Telegram", type="primary"):
                        send_to_telegram("量化戰情室風控警報", "偵測到部位觸及最大虧損限制，請立即前往戰情室檢查！")
                    st.markdown("---")

            # 投資組合加權 Beta 與市場中性化判斷
            if not df.empty:
                total_exposure_sum = df['實質曝險(台幣)'].sum()
                portfolio_beta = metrics["beta"]

                if portfolio_beta > 0.5:
                    neutrality_status = "🟢 高度偏多 (Aggressive Long)"
                elif portfolio_beta > 0.2:
                    neutrality_status = "🟢 溫和偏多 (Mild Long)"
                elif portfolio_beta < -0.2:
                    neutrality_status = "🔴 偏空避險 (Short/Hedging)"
                else:
                    neutrality_status = "🟡 市場中性 (Market-Neutral)"

                st.subheader("⚖️ 投資組合 Beta 係數與中性化戰情計分板")
                b_col1, b_col2, b_col3 = st.columns(3)
                b_col1.metric("整體資產加權 Beta", f"{portfolio_beta:.2f}", "量化中性化指標")
                b_col2.metric("部位中性化判定", neutrality_status, "區間: -0.2 ~ +0.2 為中性")
                b_col3.metric("總實質曝險部位", f"NT$ {total_exposure_sum:,.0f}", "")
                st.markdown("---")

                st.subheader("🏢 各精細產業別持股曝險與資金佔比分佈")
                sector_grouped = df.groupby('產業別').agg(實質曝險合計=('實質曝險(台幣)', 'sum'), 標的數量=('標的', 'count')).reset_index()
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
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("總帳戶淨值/權益數", f"NT$ {total_net_liq:,.0f}", "")
                c2.metric("總實質曝險 (TWD)", f"NT$ {total_exposure:,.0f}", "")
                c3.metric("總借款負債 (TWD)", f"NT$ {total_debt:,.0f}", "")
                c4.metric("🌍 合併總槓桿率", f"{global_leverage}x", "警戒線: 1.5x")
                c5.metric("💰 總現金水位", f"{global_cash_pct}%", "健康區間: >40%")
                
                st.dataframe(account_data[['帳戶名稱', '現金餘額', '融資/質押借款', '名目曝險', '帳戶淨值', '槓桿率 (倍)', '權益比率%/現金水位%']].style.format({
                    "現金餘額": "NT$ {:,.0f}", "融資/質押借款": "NT$ {:,.0f}", "名目曝險": "NT$ {:,.0f}",
                    "帳戶淨值": "NT$ {:,.0f}", "槓桿率 (倍)": "{:.2f}x", "權益比率%/現金水位%": "{:.1f}%"
                }), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("🎯 微觀持倉、Beta 值與精細產業即時損益追蹤")
            if not df.empty:
                total_invested_twd = float((df['實質佔用本金(原幣)'] * df['換算匯率']).sum())
                total_pnl_twd = float((df['淨未實現損益(原幣)'] * df['換算匯率']).sum())
                total_roi_pct = float((total_pnl_twd / total_invested_twd) * 100.0) if total_invested_twd > 0 else 0.0
                
                c_a, c_b = st.columns(2)
                c_a.metric("全帳戶持倉投入總金額 (TWD)", f"NT$ {total_invested_twd:,.0f}", "")
                c_b.metric("總淨未實現損益 (TWD)", f"NT$ {total_pnl_twd:,.0f}", f"{total_roi_pct:+.2f}%")
                
                display_df = df[['帳戶', '產業別', '種類', '標的', '名稱', 'Beta值', '真實風險權重 (%)', '標的槓桿', '持有數量', '平均成本', '即時報價', '顯示_投入總金額(原幣)', '顯示_即時市值(原幣)', '淨未實現損益(原幣)', '報酬率 (%)', '最大虧損比例']].copy()
                display_df.rename(columns={'顯示_投入總金額(原幣)': '投入總金額', '顯示_即時市值(原幣)': '即時市值(原幣)', '淨未實現損益(原幣)': '淨損益(原幣)'}, inplace=True)
                display_df['最大虧損比例'] = display_df['最大虧損比例'].apply(lambda x: f"{x:.0f}%" if x > -100 else "未設定")

                st.dataframe(display_df.style.format({
                    "Beta值": "{:.2f}", "真實風險權重 (%)": "{:.2f}%", "標的槓桿": "{:.1f}x", "持有數量": "{:,.0f}",
                    "平均成本": "{:,.2f}", "即時報價": "{:,.2f}", "投入總金額": "{:,.0f}",
                    "即時市值(原幣)": "{:,.0f}", "淨損益(原幣)": "{:,.0f}", "報酬率 (%)": "{:+.2f}%"
                }, precision=2), use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("🤖 AI 避險基金經理人即時壓力測試與風控審查")
            
            if st.button("🧠 啟動 AI 經理人風控與壓力測試", use_container_width=True):
                with st.spinner("AI 經理人正在分析中..."):
                    try:
                        gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                        client = genai.Client(api_key=gemini_key)
                        positions_summary = df[['標的', '名稱', '產業別', 'Beta值', '真實風險權重 (%)', '標的槓桿', '報酬率 (%)']].to_string(index=False) if not df.empty else "無持倉"
                        prompt_text = f"總帳戶淨值: NT$ {total_net_liq:,.0f}\n合併總槓桿率: {global_leverage}x\n投資組合加權Beta: {portfolio_beta:.2f}\n總現金水位: {global_cash_pct}%\n持倉明細:\n{positions_summary}\n請給予 3 點風控建議。"
                        response = client.models.generate_content(model='gemini-3.8-flash', contents=prompt_text)
                        st.session_state.ai_memo_p4["text"] = response.text
                        st.session_state.ai_memo_p4["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        st.error(f"AI 呼叫失敗: {e}")
            
            if st.session_state.ai_memo_p4["text"]:
                st.markdown("---")
                st.info(f"📌 **最新風控審查 (生成時間：{st.session_state.ai_memo_p4['time']})**")
                st.markdown(st.session_state.ai_memo_p4["text"])
                
                # 補上第四頁 AI 風控報告的 Telegram 手動推播按鈕
                if st.button("📤 推播此風控審查報告到 Telegram", key="btn_tg_p4", type="primary"):
                    send_to_telegram("AI 避險基金經理人風控審查報告", st.session_state.ai_memo_p4["text"])

        except Exception as e:
            st.error(f"連線發生錯誤。系統訊息: {e}")

# ==========================================
# 第五頁：Telegram 戰情推播
# ==========================================
elif page == "第五頁：Telegram 戰情推播":
    st.title("📱 第五頁：Telegram 戰情推播與即時通知中心")
    st.markdown("---")
    test_msg = st.text_area("輸入要發送至 Telegram 的戰情訊息：", "🚀 【量化交易戰鬥艙】系統運作正常！")
    if st.button("🚀 發送 Telegram 測試訊息", type="primary", use_container_width=True):
        send_to_telegram("手動測試訊息", test_msg)

# ==========================================
# 第六頁：下單輔助（只試算，不送委託）
# ==========================================
elif page == "第六頁：下單輔助系統":
    render_order_assistant(send_to_telegram, load_quant_database, get_market_beta_and_sector)