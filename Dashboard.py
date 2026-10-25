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

# ==========================================
# 網頁基本設定與密碼驗證防護
# ==========================================
st.set_page_config(page_title="量化交易戰鬥艙", page_icon="⚔️", layout="wide", initial_sidebar_state="expanded")

def check_password():
    """驗證密碼是否正確"""
    def password_entered():
        if st.session_state["password"] == st.secrets["passwords"]["admin"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.markdown("## 🔐 系統安全驗證")
        st.text_input(
            "請輸入系統登入密碼", type="password", on_change=password_entered, key="password"
        )
        return False
    elif not st.session_state["password_correct"]:
        st.markdown("## 🔐 系統安全驗證")
        st.text_input(
            "請輸入系統登入密碼", type="password", on_change=password_entered, key="password"
        )
        st.error("😕 密碼錯誤，請重新輸入")
        return False
    else:
        return True

if not check_password():
    st.stop()

# 初始化 Session State
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
    page = st.radio("切換監控面板：", ["第一頁：總經與風險鬧鐘", "第二頁：板塊與主題資金輪動", "第三頁：重大行事曆與會展", "第四頁：部位監控與量化風控", "第五頁：Telegram 戰情推播"])
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
        "VIX 恐慌指數": "^VIX", 
        "美國 10 年公債": "^TNX", 
        "美元指數 (DXY)": "DX-Y.NYB", 
        "美元/台幣匯率": "USDTWD=X",
        "比特幣 (BTC)": "BTC-USD", 
        "S&P 500": "^GSPC", 
        "那斯達克 (IXIC)": "^IXIC",
        "道瓊工業 (DJI)": "^DJI", 
        "費城半導體 (SOX)": "^SOX", 
        "羅素 2000 (RUT)": "^RUT", 
        "台灣加權指數": "^TWII",
        "布蘭特原油 (Brent)": "BZ=F", 
        "黃金期貨": "GC=F"
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
                            client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
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
                            
                            fig.update_layout(
                                height=250,
                                margin=dict(l=10, r=10, t=10, b=10),
                                showlegend=True,
                                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)),
                                template="plotly_dark"
                            )
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
        "XLK": "科技 (Technology)",
        "XLC": "通訊服務 (Communication Services)",
        "XLY": "非必需消費 (Consumer Discretionary)",
        "XLF": "金融 (Financials)",
        "XLV": "醫療保健 (Health Care)",
        "XLI": "工業 (Industrials)",
        "XLE": "能源 (Energy)",
        "XLB": "原物料 (Materials)",
        "XLP": "必需消費 (Consumer Staples)",
        "XLU": "公用事業 (Utilities)",
        "XLRE": "房地產 (Real Estate)",
        "IGV": "軟體 ETF (Software)",
        "SOXX": "硬體/半導體 ETF (Semiconductors)",
        "UFO": "太空探索 ETF (Space)"
    }

    fallback_top10 = {
        "XLK": "1. MSFT (21.5%), 2. AAPL (18.2%), 3. NVDA (15.8%), 4. AVGO (6.4%), 5. PLTR (2.1%), 6. CRM (2.0%), 7. AMD (1.8%), 8. ADBE (1.7%), 9. CSCO (1.5%), 10. QCOM (1.4%)",
        "XLC": "1. GOOGL (22.5%), 2. META (21.8%), 3. NFLX (6.2%), 4. DIS (4.5%), 5. TMUS (4.2%), 6. VZ (3.8%), 7. T (3.5%), 8. CHTR (1.8%), 9. EA (1.5%), 10. OMC (1.2%)",
        "XLY": "1. AMZN (33.5%), 2. TSLA (13.2%), 3. HD (8.5%), 4. MCD (5.2%), 5. NKE (3.8%), 6. LOW (3.5%), 7. BKNG (3.2%), 8. SBUX (2.8%), 9. TJX (2.5%), 10. CMG (2.0%)",
        "XLF": "1. BRK.B (13.2%), 2. JPM (11.8%), 3. V (7.5%), 4. MA (6.8%), 5. BAC (4.5%), 6. WFC (3.2%), 7. GS (2.8%), 8. C (2.5%), 9. SPGI (2.2%), 10. BLK (2.0%)",
        "XLV": "1. LLY (12.5%), 2. UNH (9.8%), 3. JNJ (8.2%), 4. ABBV (6.5%), 5. MRK (5.2%), 6. TMO (4.2%), 7. ABT (3.8%), 8. PFE (3.5%), 9. ISRG (2.8%), 10. AMGN (2.5%)",
        "XLI": "1. GE (5.5%), 2. CAT (5.2%), 3. RTX (4.8%), 4. UNP (4.5%), 5. HON (4.2%), 6. DE (3.8%), 7. ETN (3.5%), 8. LMT (3.2%), 9. BA (2.8%), 10. GD (2.5%)",
        "XLE": "1. XOM (23.5%), 2. CVX (15.2%), 3. COP (7.8%), 4. EOG (5.2%), 5. SLB (4.8%), 6. MPC (3.8%), 7. PSX (3.5%), 8. VLO (3.2%), 9. WMB (2.8%), 10. KMI (2.5%)",
        "XLB": "1. LIN (19.5%), 2. SHW (11.2%), 3. FCX (8.5%), 4. APD (6.2%), 5. NEM (5.8%), 6. CTVA (5.2%), 7. DOW (4.5%), 8. EMN (3.2%), 9. PPG (3.0%), 10. CE (2.5%)",
        "XLP": "1. PG (14.2%), 2. WMT (13.8%), 3. COST (11.5%), 4. PM (8.8%), 5. KO (8.2%), 6. PEP (7.5%), 7. MO (3.2%), 8. CL (3.0%), 9. MDLZ (2.8%), 10. KMB (2.2%)",
        "XLU": "1. NEE (15.5%), 2. SO (8.2%), 3. DUK (7.5%), 4. CEG (6.8%), 5. AEP (6.2%), 6. SRE (4.8%), 7. PCG (4.2%), 8. EIX (3.8%), 9. D (3.5%), 10. EXC (3.2%)",
        "XLRE": "1. PLD (14.5%), 2. AMT (12.2%), 3. EQIX (8.5%), 4. CCI (6.2%), 5. SPG (5.8%), 6. O (5.2%), 7. WELL (4.8%), 8. PSA (4.2%), 9. DLR (3.8%), 10. VICI (3.2%)",
        "IGV": "1. MSFT (8.8%), 2. CRM (8.5%), 3. NOW (8.2%), 4. ORCL (7.5%), 5. ADBE (7.2%), 6. INTU (6.5%), 7. SNOW (4.2%), 8. PLTR (4.0%), 9. WDAY (3.8%), 10. DDOG (3.5%)",
        "SOXX": "1. NVDA (8.5%), 2. AVGO (8.2%), 3. QCOM (7.8%), 4. AMD (7.5%), 5. TSM (7.2%), 6. TXN (6.5%), 7. AMAT (5.8%), 8. LRCX (5.5%), 9. MU (5.0%), 10. INTC (4.8%)",
        "UFO": "1. IRDM (8.2%), 2. PL (7.8%), 3. BKSY (7.2%), 4. RKLB (6.8%), 5. SATL (6.5%), 6. SPIR (6.0%), 7. LMT (5.5%), 8. NOC (5.2%), 9. BA (4.8%), 10. MAXR (4.5%)"
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

    with st.spinner("📡 正在從華爾街同步板塊與主題 ETF 之日K、多均線與持股結構數據..."):
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
                    
                    if len(df_k) >= 2:
                        latest_close = float(df_k['Close'].iloc[-1])
                        prev_close = float(df_k['Close'].iloc[-2])
                        daily_pct = ((latest_close - prev_close) / prev_close) * 100.0
                    else:
                        daily_pct = 0.0
                        latest_close = float(df_k['Close'].iloc[-1])
                        
                    recent_month = df_k.tail(20)
                    if len(recent_month) >= 2:
                        start_p = float(recent_month['Close'].iloc[0])
                        end_p = float(recent_month['Close'].iloc[-1])
                        monthly_pct = ((end_p - start_p) / start_p) * 100.0
                    else:
                        monthly_pct = 0.0
                        
                    top10_str = get_auto_top_holdings(ticker)

                    performance_list.append({
                        "代號": ticker,
                        "板塊名稱": name,
                        "收盤價": latest_close,
                        "日漲跌幅 (%)": round(daily_pct, 2),
                        "月漲跌幅 (%)": round(monthly_pct, 2),
                        "前十大核心持股與佔比 (1-10 排序)": top10_str
                    })
            
            df_sectors = pd.DataFrame(performance_list).sort_values(by="月漲跌幅 (%)", ascending=False)
            
            st.subheader("📊 板塊與主題 ETF 漲跌排行榜與前十大持股結構")
            
            col_cfg = {
                "代號": st.column_config.TextColumn("代號", width="small"),
                "板塊名稱": st.column_config.TextColumn("板塊名稱", width="small"),
                "收盤價": st.column_config.NumberColumn("收盤價", format="%.2f", width="small"),
                "日漲跌幅 (%)": st.column_config.NumberColumn("日漲跌幅 (%)", format="%+.2f%%", width=75),
                "月漲跌幅 (%)": st.column_config.NumberColumn("月漲跌幅 (%)", format="%+.2f%%", width=75),
                "前十大核心持股與佔比 (1-10 排序)": st.column_config.TextColumn("前十大核心持股與佔比 (1-10 排序)", width="large")
            }
            
            styled_sector_df = df_sectors.style.format({
                "收盤價": "{:.2f}",
                "日漲跌幅 (%)": "{:+.2f}%",
                "月漲跌幅 (%)": "{:+.2f}%"
            }).background_gradient(subset=["日漲跌幅 (%)", "月漲跌幅 (%)"], cmap="RdYlGn")
            
            st.dataframe(styled_sector_df, column_config=col_cfg, use_container_width=True, hide_index=True, height=480)
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
            st.subheader("🤖 AI 避險基金經理人：板塊與焦點主題資金輪動深度分析")
            
            if st.button("🧠 啟動 AI 板塊與主題資金輪動解析", use_container_width=True):
                with st.spinner("AI 經理人正在解析各板塊及軟/硬/太空 ETF 強弱排行與持股佔比..."):
                    try:
                        client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
                        summary_table = df_sectors[['代號', '板塊名稱', '日漲跌幅 (%)', '月漲跌幅 (%)', '前十大核心持股與佔比 (1-10 排序)']].to_string(index=False)
                        prompt_p2 = f"身為頂級美股對沖基金經理人。以下是我剛剛透過華爾街即時抓取整理的 11 大板塊與軟體(IGV)/硬體(SOXX)/太空(UFO)主題ETF數據：\n{summary_table}\n請給予我 1.資金流向與主題輪動解讀 2.強勢板塊/主題推薦 3.脆弱板塊風險提示。"
                        res_p2 = client.models.generate_content(model='gemini-3.8-flash', contents=prompt_p2)
                        st.session_state.ai_memo_p2["text"] = res_p2.text
                        st.session_state.ai_memo_p2["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        st.error(f"AI 分析生成失敗: {e}")
            
            if st.session_state.ai_memo_p2["text"]:
                st.markdown("---")
                st.info(f"📌 **最新板塊與主題分析 (生成時間：{st.session_state.ai_memo_p2['time']})**")
                st.markdown(st.session_state.ai_memo_p2["text"])
                    
        except Exception as e:
            st.error(f"板塊與主題 ETF 資料抓取失敗: {e}")