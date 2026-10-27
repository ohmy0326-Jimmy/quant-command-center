import os
import logging
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

logger = logging.getLogger("trading_dashboard")

# Yahoo ^TNX（CBOE 10 年期公債）有兩種報價：
# 舊制是殖利率指數，收盤價 = 殖利率（%）× 10，例如 49.86 代表 4.986%。
# 新制直接以百分比報價，例如 4.986 代表 4.986%。
# 2026-10 實測 chart API 的 regularMarketPrice 約 4.99，近五年日線約在 1.3–5.4，
# 一律除以 10 會顯示成約 0.50%，並讓「殖利率 > 4.5 加分」不會觸發。
# 現代 10 年債殖利率未曾站上 20%（1981 年高點約 15.8%），
# 因此原始報價 > 20 才視為舊制指數並除以 10；否則已是百分比。
# 舊制在殖利率低於 2% 時原始報價也會 <= 20，無法從單一數字分辨，這種情況維持不除。
TNX_INDEX_QUOTE_THRESHOLD = 20.0
DEFAULT_VIX = 15.0
DEFAULT_TNX_YIELD_PERCENT = 4.0


def tnx_quote_to_yield_percent(raw_close):
    """把單筆 Yahoo ^TNX 收盤價轉成百分比殖利率，相容舊制指數與新制百分比。"""
    value = float(raw_close)
    if value > TNX_INDEX_QUOTE_THRESHOLD:
        return value / 10.0
    return value


def tnx_series_to_yield_percent(series):
    """整段歷史沿用最新有效報價的慣例，避免逐筆門檻把同一序列拆成兩種刻度。"""
    values = pd.to_numeric(series, errors="coerce")
    valid = values.dropna()
    if not valid.empty and float(valid.iloc[-1]) > TNX_INDEX_QUOTE_THRESHOLD:
        return values / 10.0
    return values


def last_valid_close(history_df):
    """回傳最後一筆有效收盤價。空表或全為空值時丟出 ValueError，讓呼叫端改走預設值並示警。"""
    if history_df is None or getattr(history_df, "empty", True) or "Close" not in getattr(history_df, "columns", []):
        raise ValueError("歷史資料為空")
    closes = pd.to_numeric(history_df["Close"], errors="coerce").dropna()
    if closes.empty:
        raise ValueError("收盤價為空")
    return float(closes.iloc[-1])

# ==========================================
# 網頁基本設定與密碼驗證防護（支援環境變數與 Secret）
# ==========================================
st.set_page_config(page_title="量化交易戰鬥艙", page_icon="⚔️", layout="wide", initial_sidebar_state="expanded")

# 安全讀取登入密碼（支援雲端環境變數或本地 secrets）
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or st.secrets.get("passwords", {}).get("admin", "")

def check_password():
    """驗證密碼是否正確"""
    def password_entered():
        if "password" in st.session_state and st.session_state["password"] == ADMIN_PASSWORD:
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

# ==========================================
# 共用 Telegram 推播輔助函式
# ==========================================
def send_to_telegram(title, text_content):
    """共用的 Telegram 推播輔助函式，讓各頁面 AI 報告都能一鍵手動推播"""
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
            vix_is_fallback = False
            tnx_is_fallback = False

            try:
                vix_val = last_valid_close(yf.Ticker("^VIX").history(period="5d"))
            except Exception:
                logger.exception("抓取 VIX 恐慌指數（^VIX）失敗，改用預設值 %.2f", DEFAULT_VIX)
                vix_val = DEFAULT_VIX
                vix_is_fallback = True
                st.warning("⚠️ VIX 恐慌指數（^VIX）抓取失敗，已改用預設值 15.00。此數值為回退預設，不代表即時市價，風險分數可能失真。")

            try:
                tnx_val = tnx_quote_to_yield_percent(last_valid_close(yf.Ticker("^TNX").history(period="5d")))
            except Exception:
                logger.exception("抓取美國 10 年公債殖利率（^TNX）失敗，改用預設值 %.2f%%", DEFAULT_TNX_YIELD_PERCENT)
                tnx_val = DEFAULT_TNX_YIELD_PERCENT
                tnx_is_fallback = True
                st.warning("⚠️ 美國 10 年公債殖利率（^TNX）抓取失敗，已改用預設值 4.00%。此數值為回退預設，不代表即時殖利率，風險分數可能失真。")

            # 分數只來自兩項：VIX 最高 4 分、10 年債殖利率最高 2 分，滿分 6。
            vix_points = 4 if vix_val > 25 else (2 if vix_val > 20 else 0)
            tnx_points = 2 if tnx_val > 4.5 else 0
            risk_score = vix_points + tnx_points
            risk_score_max = 6
            risk_labels = []

            if vix_points == 4:
                risk_labels.append(f"🚨 VIX 市場極度恐慌 ({vix_val:.2f} > 25，+4 分)")
            elif vix_points == 2:
                risk_labels.append(f"⚠️ VIX 恐慌指數偏高 ({vix_val:.2f} > 20，+2 分)")

            if tnx_points == 2:
                risk_labels.append(f"⚠️ 10年債殖利率處於高檔 ({tnx_val:.2f}% > 4.5%，+2 分)")

            vix_tag = "（預設值）" if vix_is_fallback else ""
            tnx_tag = "（預設值）" if tnx_is_fallback else ""

            st.markdown("### 🛡️ 宏觀市場風險計分板（僅 VIX 與美國 10 年債）")
            st.caption(
                "此分數不是全市場綜合評分，只計入兩個因子，滿分 6 分："
                "VIX 恐慌指數最高 4 分（>25 得 4 分、>20 得 2 分、其餘 0 分）；"
                "美國 10 年公債殖利率最高 2 分（>4.5% 得 2 分、其餘 0 分）。"
                "美元指數、匯率、美股、台股、原油與黃金都不計入。"
            )
            rc1, rc2, rc3 = st.columns([1.5, 2.2, 4.3])
            with rc1:
                if risk_score <= 1:
                    st.success("🟢 市場氣氛：【風險偏好 (Risk-On)】")
                elif risk_score <= 3:
                    st.warning("🟡 市場氣氛：【中性觀望 / 局部震盪】")
                else:
                    st.error("🔴 市場氣氛：【高風險警戒 (Risk-Off)】")
            with rc2:
                st.metric(
                    "VIX＋10年債風險評分",
                    f"{risk_score} / {risk_score_max} 分",
                    f"VIX {vix_points}/4 分 + 10年債 {tnx_points}/2 分",
                    delta_color="off",
                    help="只計 VIX（最高 4 分）與美國 10 年公債殖利率（最高 2 分），滿分 6 分。",
                )
            with rc3:
                st.markdown(
                    f"- **VIX 恐慌指數**：{vix_val:.2f}{vix_tag} → **{vix_points} / 4 分**\n"
                    f"- **10 年公債殖利率**：{tnx_val:.2f}%{tnx_tag} → **{tnx_points} / 2 分**\n"
                    f"- **合計**：{risk_score} / {risk_score_max} 分"
                )
                if risk_labels:
                    st.error(" | ".join(risk_labels))
                else:
                    st.info("✅ 上述兩項因子目前皆未達加分門檻（VIX ≤ 20，且 10 年債殖利率 ≤ 4.5%）。")
                    
            st.markdown("---")
            
            with st.expander("🤖 點擊展開：AI 避險基金經理人今日總經晨報與操盤提醒", expanded=True):
                if st.button("🧠 一鍵生成今日總經盤勢速覽", use_container_width=True):
                    with st.spinner("AI 經理人正在解讀各大總經指標..."):
                        try:
                            gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                            client = genai.Client(api_key=gemini_key)
                            vix_note = "（預設回退值，非即時報價）" if vix_is_fallback else ""
                            tnx_note = "（預設回退值，非即時報價）" if tnx_is_fallback else ""
                            snapshot_str = (
                                f"- VIX恐慌指數: {vix_val:.2f}{vix_note}\n"
                                f"- 10年公債殖利率: {tnx_val:.2f}%{tnx_note}\n"
                            )
                            
                            for k, t in indicators.items():
                                try:
                                    s_df = yf.Ticker(t).history(period="5d")
                                    if s_df.empty or "Close" not in s_df.columns or len(s_df) < 2:
                                        raise ValueError("歷史資料不足")
                                    closes = tnx_series_to_yield_percent(s_df["Close"]) if t == "^TNX" else s_df["Close"]
                                    valid_closes = pd.to_numeric(closes, errors="coerce").dropna()
                                    if len(valid_closes) < 2:
                                        raise ValueError("有效收盤價不足")
                                    c_val = float(valid_closes.iloc[-1])
                                    p_val = float(valid_closes.iloc[-2])
                                    if p_val == 0:
                                        raise ValueError("前收為 0")
                                    pct_chg = ((c_val - p_val) / p_val) * 100
                                    snapshot_str += f"- {k}: {c_val:,.2f} ({pct_chg:+.2f}%)\n"
                                except Exception:
                                    logger.exception("總經快照 %s（%s）抓取失敗", k, t)
                                    snapshot_str += f"- {k}: 抓取失敗（未納入，無預設值）\n"
                                    st.warning(f"⚠️ {k}（{t}）即時快照抓取失敗，AI 晨報不含此項即時數據，且未代入預設數字。")
                                    
                            p_text = f"""
                            你是一位頂級美股對沖基金經理人。以下是我剛剛抓取的今日市場核心總經數據即時快照：
                            {snapshot_str}
                            
                            其中「VIX恐慌指數」與「10年公債殖利率」是風險計分實際採用的百分比數值（若標註預設回退值，代表該來源抓取失敗）。
                            請針對目前的波動率、債市殖利率與跨市場表現，為我提供一份精煉的「今日總經晨報」，包含：
                            1. 宏觀解讀（資金流向與潛在風險）。
                            2. 今日操作提醒（應對當前環境的現金水位或避險方針）。
                            請給出專業、直擊重點的繁體中文建議。
                            """
                            res = client.models.generate_content(model='gemini-2.5-flash', contents=p_text)
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
                    if df_item.empty:
                        logger.error("總經指標 %s（%s）歷史資料為空，圖表略過", name, ticker)
                        st.warning(f"⚠️ {name}（{ticker}）資料抓取失敗或為空，此圖表已略過，未使用預設價格。")
                    else:
                        if ticker == "^TNX":
                            df_item = df_item.copy()
                            df_item["Close"] = tnx_series_to_yield_percent(df_item["Close"])
                        df_item['MA5'] = df_item['Close'].rolling(window=5).mean()
                        df_item['MA20'] = df_item['Close'].rolling(window=20).mean()
                        df_item['MA60'] = df_item['Close'].rolling(window=60).mean()
                        df_item['MA200'] = df_item['Close'].rolling(window=200).mean()
                        
                        curr = float(df_item['Close'].iloc[-1])
                        prev = float(df_item['Close'].iloc[-2])
                        if pd.isna(curr) or pd.isna(prev):
                            valid_close = pd.to_numeric(df_item['Close'], errors='coerce').dropna()
                            if len(valid_close) < 2:
                                raise ValueError("有效收盤價不足")
                            curr = float(valid_close.iloc[-1])
                            prev = float(valid_close.iloc[-2])
                        # BZ=F（布蘭特原油）換月時，Yahoo 連續合約的前後收盤可能來自不同月份，
                        # 單日漲跌幅會出現與現貨不符的假性跳動；此處仍依原始收盤價計算，不另作調整。
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
                except Exception:
                    logger.exception("總經指標 %s（%s）圖表載入失敗", name, ticker)
                    st.warning(f"⚠️ {name}（{ticker}）資料抓取失敗，此圖表已略過，未使用預設價格。")
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
            logger.warning("ETF %s 前十大持股為空，改用內建預設名單", ticker_symbol)
        except Exception:
            logger.exception("抓取 ETF %s 前十大持股失敗，改用內建預設名單", ticker_symbol)
        fallback = fallback_top10.get(ticker_symbol)
        if fallback:
            st.warning(f"⚠️ {ticker_symbol} 前十大持股抓取失敗，已改用內建預設名單（非即時持股）。")
            return f"（預設名單）{fallback}"
        st.warning(f"⚠️ {ticker_symbol} 前十大持股抓取失敗，且無內建預設名單。")
        return "資料同步中（抓取失敗，無預設名單）"

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
                else:
                    logger.error("板塊 ETF %s（%s）歷史資料為空，已略過", ticker, name)
                    st.warning(f"⚠️ {name}（{ticker}）歷史資料抓取失敗或為空，此板塊已略過，未使用估算漲跌幅。")
            
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
                        gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                        client = genai.Client(api_key=gemini_key)
                        summary_table = df_sectors[['代號', '板塊名稱', '日漲跌幅 (%)', '月漲跌幅 (%)', '前十大核心持股與佔比 (1-10 排序)']].to_string(index=False)
                        prompt_p2 = f"身為頂級美股對沖基金經理人。以下是我剛剛透過華爾街即時抓取整理的 11 大板塊與軟體(IGV)/硬體(SOXX)/太空(UFO)主題ETF數據：\n{summary_table}\n請給予我 1.資金流向與主題輪動解讀 2.強勢板塊/主題推薦 3.脆弱板塊風險提示。"
                        res_p2 = client.models.generate_content(model='gemini-2.5-flash', contents=prompt_p2)
                        st.session_state.ai_memo_p2["text"] = res_p2.text
                        st.session_state.ai_memo_p2["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        st.error(f"AI 分析生成失敗: {e}")
            
            if st.session_state.ai_memo_p2["text"]:
                st.markdown("---")
                st.info(f"📌 **最新板塊與主題分析 (生成時間：{st.session_state.ai_memo_p2['time']})**")
                st.markdown(st.session_state.ai_memo_p2["text"])
                
                if st.button("📤 推播此板塊分析到 Telegram", key="btn_tg_p2"):
                    send_to_telegram("AI 板塊與主題資金輪動深度分析", st.session_state.ai_memo_p2["text"])
                    
        except Exception as e:
            st.error(f"板塊與主題 ETF 資料抓取失敗: {e}")

# ==========================================
# 第三頁：重大行事曆與會展
# ==========================================
elif page == "第三頁：重大行事曆與會展":
    c1, c2 = st.columns([0.85, 0.15])
    with c1:
        st.title("📅 第三頁：重大行事曆與 M7 財報戰情室 (AI 經理人風控導航)")
    with c2:
        st.write("")
        if st.button("🔄 重新整理", key="refresh_p3", use_container_width=True):
            st.rerun()
    st.markdown("---")
    st.info("💡 系統自動整合未來 3 個月內的 FOMC 決議、CPI/PCE 通膨、就業魔王數據以及美股 M7 科技巨頭（AAPL、MSFT、NVDA、GOOGL、AMZN、META、TSLA）財報行事曆。")

    today = datetime.now().date()
    three_months_later = today + timedelta(days=90)

    @st.cache_data(ttl=3600)
    def get_m7_earnings_events():
        m7_dict = {
            "AAPL": "Apple (蘋果)",
            "MSFT": "Microsoft (微軟)",
            "NVDA": "NVIDIA (輝達)",
            "GOOGL": "Alphabet (Google)",
            "AMZN": "Amazon (亞馬遜)",
            "META": "Meta (臉書)",
            "TSLA": "Tesla (特斯拉)"
        }
        events = []
        failed = []
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
                        val = cal.loc['Earnings Date'].iloc[0]
                        earnings_date = pd.to_datetime(val).date()
                
                if earnings_date:
                    date_str = earnings_date.strftime("%Y-%m-%d")
                    events.append({
                        "日期": date_str,
                        "項目": f"美股 M7 財報：{name} ({ticker})",
                        "影響": f"左右全球 AI 資本支出、科技股情緒與相關供應鏈（如半導體、伺服器）連動"
                    })
                else:
                    logger.warning("%s（%s）財報日為空，未列入行事曆", name, ticker)
            except Exception:
                logger.exception("抓取 %s（%s）財報日失敗，此事件不列入行事曆", name, ticker)
                failed.append(f"{name}（{ticker}）")
        return events, failed

    base_events = [
        {"日期": "2026-10-25", "項目": "美國 PCE 物價指數公佈", "影響": "Fed 通膨目標核心觀測與降息路徑判定"},
        {"日期": "2026-10-29", "項目": "FOMC 利率決議與華許聲明 (清晨02:00)", "影響": "全市場基準利率調整、流動性與債市/股市大震盪"},
        {"日期": "2026-11-06", "項目": "美國 10 月非農就業報告 (NFP) & 失業率", "影響": "每月就業數據魔王，左右經濟軟著陸預期"},
        {"日期": "2026-11-12", "項目": "美國 10 月消費者物價指數 (CPI)", "影響": "終端通膨黏著度，直接影響 Fed 下一步利率走向"},
        {"日期": "2026-11-20", "項目": "台指期結算日 (11月)", "影響": "台股期貨與選擇權短線結算波動"},
        {"日期": "2026-12-04", "項目": "美國 11 月非農就業報告 (NFP) & 失業率", "影響": "年底前最重要的勞動市場體檢"},
        {"日期": "2026-12-10", "項目": "FOMC 利率決議與華許聲明 (清晨02:00)", "影響": "年底前關鍵利率決議與跨年資金定價"},
        {"日期": "2026-12-10", "項目": "美國 11 月消費者物價指數 (CPI)", "影響": "年底消費旺季前夕的通膨檢驗"},
        {"日期": "2026-12-18", "項目": "美國 11 月 PCE 物價指數公佈", "影響": "Fed 最看重的通膨終值數據"},
        {"日期": "2027-01-08", "項目": "美國 12 月非農就業報告 (NFP)", "影響": "新年開局的第一個就業風向球"},
        {"日期": "2027-01-14", "項目": "美國 12 月消費者物價指數 (CPI)", "影響": "跨年通膨收官數據與新年總經定調"},
        {"日期": "2027-01-15", "項目": "台積電 (2330) 法人說明會", "影響": "台股大盤、半導體供應鏈、全球 AI 資本支出風向"}
    ]

    m7_events, m7_failed = get_m7_earnings_events()
    if m7_failed:
        st.warning("⚠️ 以下 M7 財報日抓取失敗，未列入行事曆，也未填入預設日期：" + "、".join(m7_failed))
    all_events = base_events + m7_events

    filtered_events = [ev for ev in all_events if today <= datetime.strptime(ev["日期"], "%Y-%m-%d").date() <= three_months_later]
    filtered_events.sort(key=lambda x: x["日期"])

    if filtered_events:
        st.markdown("### 📌 未來 3 個月重大總經與 M7 財報事件排程")
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
                                    res = client.models.generate_content(
                                        model='gemini-2.5-flash', 
                                        contents=f"身為避險基金經理人，面對即將到來的重大財報或總經事件：【{ev['項目']}】（影響層面：{ev['影響']}），請給我 2 點精煉、具體且直接可執行的資產配置與風控建議。"
                                    )
                                    st.session_state.ai_memo_p3[event_key] = {"text": res.text, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                                except Exception as e:
                                    st.error(f"生成失敗: {e}")
                    with sub_c2:
                        if st.button(f"📲 推播TG", key=f"tg_event_btn_{idx}"):
                            try:
                                bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "")
                                chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "")
                                msg = f"📅 【量化戰情室行事曆提醒】\n事件：{ev['項目']}\n日期：{ev['日期']}\n影響：{ev['影響']}"
                                r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=5)
                                if r.status_code == 200:
                                    st.success("✅ TG 推播成功！")
                                else:
                                    st.error(f"❌ 失敗: {r.status_code}")
                            except Exception as e:
                                st.error(f"❌ 錯誤: {e}")
                
                if event_key in st.session_state.ai_memo_p3:
                    memo = st.session_state.ai_memo_p3[event_key]
                    st.info(f"📌 **最新戰術建議 (生成時間：{memo['time']})**")
                    st.markdown(memo["text"])
                    
                    if st.button(f"📤 推播此事件戰術建議到 Telegram", key=f"btn_tg_p3_{idx}"):
                        send_to_telegram(f"行事曆戰術建議：{ev['項目']}", memo["text"])
                        
            st.markdown("---")
    else:
        st.info("目前未來 3 個月內無登錄的重大排程事件。")

# ==========================================
# 第四頁：部位監控與量化風控
# ==========================================
elif page == "第四頁：部位監控與量化風控":
    @st.cache_data(ttl=300)
    def get_market_beta_and_sector(ticker, is_tw):
        detailed_sector_map = {
            "MUU": "記憶體",
            "SNXX": "記憶體",
            "AMUU": "AI晶片",
            "KORU": "韓國指數",
            "金居": "銅箔材料",
            "2330": "晶圓代工",
            "TSM": "晶圓代工",
            "NVDA": "AI晶片",
            "AMD": "AI晶片",
            "AVGO": "網通與半導體",
            "MSFT": "軟體與雲端",
            "AAPL": "消費電子",
            "GOOGL": "數位廣告與雲端",
            "META": "社群與AI",
            "SOXL": "半導體槓桿ETF",
            "00631L": "台股槓桿ETF",
            "LITX": "光通產業",
            "TX": "臺指期貨",
            "MTX": "小型臺指",
            "FMF": "微型臺指期"
        }
        
        t_upper = str(ticker).upper()
        sector_is_fallback = False
        if t_upper in detailed_sector_map:
            refined_sector = detailed_sector_map[t_upper]
        else:
            try:
                t = yf.Ticker(ticker)
                info = t.info
                yf_industry = info.get('industry', '')
                yf_sector = info.get('sector', '')
                if yf_industry:
                    refined_sector = yf_industry
                elif yf_sector:
                    refined_sector = yf_sector
                else:
                    refined_sector = "其他"
            except Exception:
                logger.exception("抓取 %s 產業分類失敗，改用預設「其他與衍生品」", ticker)
                refined_sector = "其他與衍生品"
                sector_is_fallback = True

        if t_upper in ['TX', 'MTX', 'FMF']:
            return 1.0, str(refined_sector), sector_is_fallback, False

        try:
            benchmark = "^TWII" if is_tw else "^GSPC"
            df_asset = yf.Ticker(ticker).history(period="1y")['Close']
            df_bench = yf.Ticker(benchmark).history(period="1y")['Close']
            
            combined = pd.concat([df_asset, df_bench], axis=1).dropna()
            if len(combined) > 30:
                returns = combined.pct_change().dropna()
                cov = np.cov(returns.iloc[:, 0], returns.iloc[:, 1])[0][1]
                var_bench = np.var(returns.iloc[:, 1])
                beta = cov / var_bench if var_bench != 0 else 1.0
            else:
                beta = 1.0
            return float(round(beta, 2)), str(refined_sector), sector_is_fallback, False
        except Exception:
            logger.exception("計算 %s Beta 失敗，改用預設 Beta 1.0", ticker)
            return 1.0, str(refined_sector), sector_is_fallback, True

    @st.cache_data(ttl=60)
    def load_quant_database():
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        import json
        
        # 在 Streamlit Cloud 上直接讀取 st.secrets
        try:
            key_data = dict(st.secrets["google_key"])
        except Exception as e:
            # 若本地或環境有其他備援
            raw_val = os.environ.get("GCP_KEY_JSON", "") or os.environ.get("GOOGLE_SERVICE_KEY_JSON", "")
            if raw_val.strip():
                raw_val = raw_val.strip()
                if raw_val.startswith('\ufeff'):
                    raw_val = raw_val[1:]
                if (raw_val.startswith("'") and raw_val.endswith("'")) or (raw_val.startswith('"') and raw_val.endswith('"')):
                    raw_val = raw_val[1:-1].strip()
                key_data = json.loads(raw_val)
            else:
                raise Exception(f"無法載入 Google 憑證: {e}")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open("Quant_Database")
        return (
            pd.DataFrame(spreadsheet.worksheet("Active_Positions").get_all_records()),
            pd.DataFrame(spreadsheet.worksheet("Daily_NAV").get_all_records()),
            pd.DataFrame(spreadsheet.worksheet("持倉統計").get_all_records())
        )

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
            try:
                usdtwd_rate = last_valid_close(yf.Ticker("USDTWD=X").history(period="1d"))
            except Exception:
                logger.exception("美元/台幣匯率（USDTWD=X）抓取失敗，改用預設值 32.5")
                usdtwd_rate = 32.5
                st.warning("⚠️ 美元/台幣匯率（USDTWD=X）抓取失敗，已改用預設值 32.50（預設值）。以下台幣換算使用此回退匯率，並非即時匯率。")
            
            df = quant_data.copy()
            df['標的'] = df['標的'].astype(str).str.strip().str.upper()
            df = df[df['標的'] != '']
            df['持有數量'] = pd.to_numeric(df['持有數量'], errors='coerce').fillna(0.0)
            df['平均成本'] = pd.to_numeric(df['平均成本'], errors='coerce').fillna(0.0)
            
            df['名目合約總值(原幣)'] = pd.to_numeric(df['投入總金額'], errors='coerce').fillna(0.0)
            df['最大虧損比例'] = pd.to_numeric(df.get('最大虧損比例', -100), errors='coerce').fillna(-100.0)
            df['標的槓桿'] = pd.to_numeric(df.get('標的槓桿', 1), errors='coerce').fillna(1.0)
            df['名稱'] = df.get('名稱', '').astype(str)
            
            df = df[df['持有數量'] != 0].copy()
            
            acc_market_val_twd, acc_exp_twd, total_net_liq = pd.Series(dtype=float), pd.Series(dtype=float), 0.0
            acc_maint_margin = pd.Series(dtype=float)
            
            if not df.empty:
                df['換算匯率'] = np.where(df['種類'].astype(str).str.contains('美'), usdtwd_rate, 1.0).astype(float)
                df['實質佔用本金(原幣)'] = df['名目合約總值(原幣)'].abs()
                df['維持保證金(原幣)'] = 0.0
                
                for idx, row in df.iterrows():
                    if '期' in str(row['種類']):
                        t_str = str(row['標的']).upper()
                        q_abs = abs(row['持有數量'])
                        if t_str in ['TMF', 'FMF']: 
                            df.at[idx, '實質佔用本金(原幣)'] = 35050.0 * q_abs
                            df.at[idx, '維持保證金(原幣)'] = 26900.0 * q_abs
                        elif t_str == 'MTX': 
                            df.at[idx, '實質佔用本金(原幣)'] = 175250.0 * q_abs
                            df.at[idx, '維持保證金(原幣)'] = 134500.0 * q_abs
                        elif t_str == 'TX': 
                            df.at[idx, '實質佔用本金(原幣)'] = 701000.0 * q_abs
                            df.at[idx, '維持保證金(原幣)'] = 538000.0 * q_abs
                        else: 
                            df.at[idx, '實質佔用本金(原幣)'] = abs(row['名目合約總值(原幣)']) * 0.135
                            df.at[idx, '維持保證金(原幣)'] = abs(row['名目合約總值(原幣)']) * 0.11

                try:
                    twii_price = last_valid_close(yf.Ticker("^TWII").history(period="1d"))
                except Exception:
                    logger.exception("台灣加權指數（^TWII）抓取失敗，改用預設值 23000")
                    twii_price = 23000.0
                    st.warning("⚠️ 台灣加權指數（^TWII）抓取失敗，期貨計價已改用預設值 23000（預設值），並非即時指數。")

                tickers, prices, betas, sectors = df['標的'].unique().tolist(), {}, {}, {}
                beta_fallback_notes = {}
                price_fallback_tickers = []
                quote_failure_tickers = []
                for ticker in tickers:
                    try:
                        t_upper = str(ticker).upper()
                        if t_upper in ['TX', 'MTX', 'FMF', 'TMF']:
                            prices[ticker] = twii_price
                            betas[ticker] = 1.0
                            sectors[ticker] = "微型臺指期" if t_upper == 'FMF' else ("小型臺指" if t_upper == 'MTX' else "臺指期貨")
                            continue

                        is_tw_market = (t_upper in ['TE', 'TF']) or ('.TW' in t_upper) or ('.TWO' in t_upper) or (len(t_upper) == 3 and t_upper.isalpha())
                        b_val, s_val, sector_is_fallback, beta_is_fallback = get_market_beta_and_sector(ticker, is_tw_market)
                        
                        betas[ticker] = b_val
                        sectors[ticker] = s_val
                        if beta_is_fallback or sector_is_fallback:
                            parts = []
                            if beta_is_fallback:
                                parts.append("Beta 1.0（預設值）")
                            if sector_is_fallback:
                                parts.append(f"產業別「{s_val}」（預設值）")
                            beta_fallback_notes[str(ticker)] = f"{ticker}：{'、'.join(parts)}"
                        
                        if is_tw_market:
                            try:
                                headers = {'User-Agent': 'Mozilla/5.0'}
                                res = requests.get(f"https://tw.stock.yahoo.com/quote/{ticker}", headers=headers, timeout=5)
                                match = re.search(r'"regularMarketPrice":(?:{"raw":)?([0-9.]+)', res.text)
                                if match:
                                    prices[ticker] = float(match.group(1))
                                else:
                                    prices[ticker] = float(yf.Ticker(ticker).history(period="1d")['Close'].iloc[-1])
                            except Exception:
                                logger.exception("台股 %s 網頁報價解析失敗，改以 yfinance 取得", ticker)
                                prices[ticker] = float(yf.Ticker(ticker).history(period="1d")['Close'].iloc[-1])
                        else:
                            hist = yf.Ticker(ticker).history(period="5d")
                            prices[ticker] = float(hist['Close'].iloc[-1]) if not hist.empty else np.nan
                        if pd.isna(prices.get(ticker, np.nan)):
                            logger.error("%s 即時報價為空，將回退至平均成本", ticker)
                            price_fallback_tickers.append(str(ticker))
                    except Exception:
                        logger.exception("抓取 %s 即時報價失敗，將回退至平均成本，Beta 暫以 1.0、產業別暫以「其他」", ticker)
                        prices[ticker] = np.nan
                        betas[ticker] = 1.0
                        sectors[ticker] = "其他"
                        quote_failure_tickers.append(str(ticker))

                for failed_ticker in quote_failure_tickers:
                    beta_fallback_notes.pop(failed_ticker, None)
                if beta_fallback_notes:
                    st.warning("⚠️ 以下標的的市場資料抓取失敗，已改用預設值：" + "、".join(beta_fallback_notes.values()))
                if price_fallback_tickers:
                    st.warning("⚠️ 以下標的即時報價抓取失敗或為空，已改用平均成本作為回退價格（預設值，非即時市價）：" + "、".join(price_fallback_tickers))
                if quote_failure_tickers:
                    st.warning("⚠️ 以下標的報價抓取失敗：即時報價回退為平均成本（預設值），Beta 暫以 1.0（預設值）、產業別暫以「其他」（預設值）：" + "、".join(quote_failure_tickers))
                
                df['即時報價'] = df['標的'].map(prices)
                df['即時報價'] = df['即時報價'].fillna(df['平均成本'])
                
                df['Beta值'] = df['標的'].map(betas).fillna(1.0)
                df['產業別'] = df['標的'].map(sectors).fillna("其他")
                
                is_futures_mask = df['種類'].astype(str).str.contains('期', na=False)
                
                def calc_futures_pnl(row):
                    t = str(row['標的']).upper()
                    mult = 10 if t in ['FMF', 'TMF'] else (50 if t == 'MTX' else (200 if t == 'TX' else 2000))
                    return (row['即時報價'] - row['平均成本']) * row['持有數量'] * mult

                df['淨未實現損益(原幣)'] = np.where(
                    is_futures_mask,
                    df.apply(calc_futures_pnl, axis=1),
                    0.0
                )

                stock_mask = ~is_futures_mask
                if stock_mask.any():
                    stock_latest = np.where(df.loc[stock_mask, '平均成本'] != 0, 
                                            df.loc[stock_mask, '名目合約總值(原幣)'] * (df.loc[stock_mask, '即時報價'] / df.loc[stock_mask, '平均成本']), 
                                            df.loc[stock_mask, '名目合約總值(原幣)'])
                    tax_rate = 0.003
                    df.loc[stock_mask, '淨未實現損益(原幣)'] = (stock_latest - df.loc[stock_mask, '名目合約總值(原幣)']) - (np.abs(stock_latest) * tax_rate)

                df['報酬率 (%)'] = np.where(df['實質佔用本金(原幣)'] != 0, (df['淨未實現損益(原幣)'] / df['實質佔用本金(原幣)'].abs()) * 100.0, 0.0).astype(float)
                
                df['顯示_投入總金額(原幣)'] = np.where(is_futures_mask, df['實質佔用本金(原幣)'], df['名目合約總值(原幣)']).astype(float)
                df['顯示_即時市值(原幣)'] = np.where(is_futures_mask, df['實質佔用本金(原幣)'] + df['淨未實現損益(原幣)'], df['即時報價'] * df['持有數量']).astype(float)
                
                df['即時市值(台幣)'] = (df['顯示_即時市值(原幣)'].abs() * df['換算匯率']).astype(float)
                df['實質曝險(台幣)'] = (df['即時市值(台幣)'] * df['標的槓桿']).astype(float)
                
                df['是衍生品'] = df['種類'].astype(str).str.contains('期|選擇權|權', na=False, regex=True)
                
                acc_market_val_twd = df.groupby('帳戶')['即時市值(台幣)'].sum()
                acc_exp_twd = df.groupby('帳戶')['實質曝險(台幣)'].sum()
                acc_maint_margin = df.groupby('帳戶')['維持保證金(原幣)'].sum()

            if not account_data.empty:
                for col in ["現金餘額", "融資/質押借款"]:
                    if col in account_data.columns:
                        account_data[col] = pd.to_numeric(account_data[col], errors='coerce').fillna(0.0).astype(float)
                
                account_data['即時持股市值'] = account_data['帳戶名稱'].map(acc_market_val_twd).fillna(0.0).astype(float)
                account_data['名目曝險'] = account_data['帳戶名稱'].map(acc_exp_twd).fillna(0.0).astype(float)
                account_data['維持保證金總額'] = account_data['帳戶名稱'].map(acc_maint_margin).fillna(0.0).astype(float)
                account_data['帳戶淨值'] = 0.0
                
                for idx, row in account_data.iterrows():
                    acc_name = str(row['帳戶名稱']).strip()
                    if "元大期貨" in acc_name:
                        q_equity_contrib = float(df[df['帳戶'] == acc_name]['淨未實現損益(原幣)'].sum() * df[df['帳戶'] == acc_name]['換算匯率'].iloc[0]) if not df[df['帳戶'] == acc_name].empty else 0.0
                        net_val = float(170422.0 + q_equity_contrib)
                        maint_m = float(row['維持保證金總額'])
                        account_data.at[idx, '帳戶淨值'] = net_val
                        account_data.at[idx, '現金餘額'] = float(max(0.0, net_val - maint_m))
                    else:
                        cash = float(row['現金餘額'])
                        mkt_val = float(row['即時持股市值'])
                        debt = float(row['融資/質押借款'])
                        account_data.at[idx, '帳戶淨值'] = cash + mkt_val - debt

                total_net_liq = float(account_data["帳戶淨值"].sum())

            if not df.empty:
                df['真實風險權重 (%)'] = np.where(total_net_liq > 0, (df['實質曝險(台幣)'] / total_net_liq) * 100.0, 0.0).astype(float)

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
                        try:
                            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "")
                            chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "")
                            alarm_text = "🚨 【量化戰情室風控警報】以下部位已觸及最大虧損限制：\n"
                            for idx, row in alarms.iterrows():
                                name_display = row['名稱'] if row['名稱'] else row['標的']
                                alarm_text += f"• {name_display} ({row['標的']}): {row['報酬率 (%)']:.2f}% (設限: {row['最大虧損比例']:.0f}%)\n"
                            r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": alarm_text}, timeout=5)
                            if r.status_code == 200:
                                st.success("✅ 停損警報已成功推播至 Telegram！")
                            else:
                                st.error(f"❌ 推送失敗: {r.status_code}")
                        except Exception as e:
                            st.error(f"❌ 錯誤: {e}")
                    st.markdown("---")

            # ==========================================
            # 投資組合加權 Beta 與市場中性化判斷
            # ==========================================
            if not df.empty:
                total_exposure_sum = df['實質曝險(台幣)'].sum()
                if total_exposure_sum > 0:
                    df['曝險權重'] = df['實質曝險(台幣)'] / total_exposure_sum
                    portfolio_beta = (df['曝險權重'] * df['Beta值']).sum()
                else:
                    portfolio_beta = 0.0

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
                b_col1.metric("整體資產加權 Beta ($\beta_{\text{portfolio}}$)", f"{portfolio_beta:.2f}", "量化中性化指標")
                b_col2.metric("部位中性化判定", neutrality_status, "區間: -0.2 ~ +0.2 為中性")
                b_col3.metric("總實質曝險部位", f"NT$ {total_exposure_sum:,.0f}", "")
                st.markdown("---")

                st.subheader("🏢 各精細產業別持股曝險與資金佔比分佈")
                sector_grouped = df.groupby('產業別').agg(
                    實質曝險合計=('實質曝險(台幣)', 'sum'),
                    標的數量=('標的', 'count')
                ).reset_index()
                
                sector_grouped['產業曝險佔比 (%)'] = (sector_grouped['實質曝險合計'] / total_exposure_sum) * 100.0
                sector_grouped = sector_grouped.sort_values(by='產業曝險佔比 (%)', ascending=False)

                sec_col1, sec_col2 = st.columns([1.5, 2.5])
                with sec_col1:
                    styled_sec_df = sector_grouped.style.format({
                        "實質曝險合計": "NT$ {:,.0f}",
                        "產業曝險佔比 (%)": "{:.2f}%"
                    }).background_gradient(subset=["產業曝險佔比 (%)"], cmap="Blues")
                    st.dataframe(styled_sec_df, use_container_width=True, hide_index=True)
                
                with sec_col2:
                    import plotly.express as px
                    fig_sec = px.pie(
                        sector_grouped, names='產業別', values='產業曝險佔比 (%)',
                        title="各精細產業別資金曝險佔比圓餅圖",
                        hole=0.4,
                        template="plotly_dark"
                    )
                    fig_sec.update_traces(textinfo='percent+label')
                    fig_sec.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
                    st.plotly_chart(fig_sec, use_container_width=True)

            st.markdown("---")

            st.subheader("🏦 各帳戶動態風險與全局槓桿 (即時市價結算)")
            if not account_data.empty:
                account_data["槓桿率 (倍)"] = np.round(np.where(account_data["帳戶淨值"] != 0, (account_data["名目曝險"] / account_data["帳戶淨值"]), 0.0), 2).astype(float)
                account_data["權益比率%/現金水位%"] = np.round(np.where(account_data["維持保證金總額"] > 0, (account_data["帳戶淨值"] / account_data["維持保證金總額"]) * 100.0, np.where(account_data["帳戶淨值"] != 0, (account_data["現金餘額"] / account_data["帳戶淨值"]) * 100.0, 0.0)), 1).astype(float)

                total_exposure = float(account_data["名目曝險"].sum())
                total_cash = float(account_data["現金餘額"].sum())
                total_debt = float(account_data["融資/質押借款"].sum())
                global_leverage = round(total_exposure / total_net_liq, 2) if total_net_liq > 0 else 0.0
                global_cash_pct = round((total_cash / total_net_liq) * 100.0, 1) if total_net_liq > 0 else 0.0

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("總帳戶淨值/權益數", f"NT$ {total_net_liq:,.0f}", "")
                c2.metric("總實質曝險 (TWD)", f"NT$ {total_exposure:,.0f}", "")
                c3.metric("總借款負債 (TWD)", f"NT$ {total_debt:,.0f}", "")
                c4.metric("🌍 合併總槓桿率", f"{global_leverage}x", "警戒線: 1.5x")
                c5.metric("💰 總現金水位", f"{global_cash_pct}%", "健康區間: >40%")
                
                if global_leverage > 1.5:
                    st.error("🚨 **【全局風控警告】合併總槓桿率已超過 1.5x 警戒線，請注意流動性與融資維持率風險！**")
                    if st.button("⚠️ 推送合併槓桿超標警報至 Telegram"):
                        try:
                            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "")
                            chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "")
                            msg = f"🚨 【量化戰情室槓桿警告】\n目前合併總槓桿率達 {global_leverage}x (已超過 1.5x 警戒線)！\n總淨值：NT$ {total_net_liq:,.0f}\n總曝險：NT$ {total_exposure:,.0f}"
                            r = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=5)
                            if r.status_code == 200:
                                st.success("✅ 槓桿警告已成功推播！")
                            else:
                                st.error("❌ 推送失敗")
                        except Exception as e:
                            st.error(f"❌ 錯誤: {e}")

                display_account = account_data[['帳戶名稱', '現金餘額', '融資/質押借款', '名目曝險', '帳戶淨值', '槓桿率 (倍)', '權益比率%/現金水位%']].copy()
                styled_df = display_account.style.format({
                    "現金餘額": "NT$ {:,.0f}", "融資/質押借款": "NT$ {:,.0f}", "名目曝險": "NT$ {:,.0f}",
                    "帳戶淨值": "NT$ {:,.0f}", "槓桿率 (倍)": "{:.2f}x", "權益比率%/現金水位%": "{:.1f}%"
                }).background_gradient(subset=["槓桿率 (倍)"], cmap="Reds").background_gradient(subset=["權益比率%/現金水位%"], cmap="RdYlGn")
                st.dataframe(styled_df, use_container_width=True, hide_index=True)

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

                styled_quant_df = display_df.style.format({
                    "Beta值": "{:.2f}", "真實風險權重 (%)": "{:.2f}%", "標的槓桿": "{:.1f}x", "持有數量": "{:,.0f}",
                    "平均成本": "{:,.2f}", "即時報價": "{:,.2f}", "投入總金額": "{:,.0f}",
                    "即時市值(原幣)": "{:,.0f}", "淨損益(原幣)": "{:,.0f}", "報酬率 (%)": "{:+.2f}%"
                }, precision=2).background_gradient(subset=["Beta值"], cmap="Purples").background_gradient(subset=["報酬率 (%)"], cmap="RdYlGn") 
                st.dataframe(styled_quant_df, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("🤖 AI 避險基金經理人即時壓力測試與風控審查")
            
            if st.button("🧠 啟動 AI 經理人風控與壓力測試", use_container_width=True):
                with st.spinner("AI 經理人正在分析中..."):
                    try:
                        gemini_key = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY", "")
                        client = genai.Client(api_key=gemini_key)
                        positions_summary = df[['標的', '名稱', '產業別', 'Beta值', '真實風險權重 (%)', '標的槓桿', '報酬率 (%)']].to_string(index=False) if not df.empty else "無持倉"
                        prompt_text = f"總帳戶淨值: NT$ {total_net_liq:,.0f}\n合併總槓桿率: {global_leverage}x\n投資組合加權Beta: {portfolio_beta:.2f}\n總現金水位: {global_cash_pct}%\n持倉明細:\n{positions_summary}\n請給予 3 點風控建議。"
                        response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt_text)
                        st.session_state.ai_memo_p4["text"] = response.text
                        st.session_state.ai_memo_p4["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        st.error(f"AI 呼叫失敗: {e}")
            
            if st.session_state.ai_memo_p4["text"]:
                st.markdown("---")
                st.info(f"📌 **最新風控審查 (生成時間：{st.session_state.ai_memo_p4['time']})**")
                st.markdown(st.session_state.ai_memo_p4["text"])
                
                if st.button("📤 推播此風控審查到 Telegram", key="btn_tg_p4"):
                    send_to_telegram("AI 經理人風控與壓力測試審查", st.session_state.ai_memo_p4["text"])

        except Exception as e:
            st.error(f"連線發生錯誤。系統訊息: {e}")

# ==========================================
# 第五頁：Telegram 戰情推播測試
# ==========================================
elif page == "第五頁：Telegram 戰情推播":
    st.title("📱 第五頁：Telegram 戰情推播與即時通知中心")
    st.markdown("---")
    st.info("💡 系統已成功連結您的專屬機器人 `@Dailyreport4Jim_bot`。您可以在此手動測試推播或發送全方位帳戶戰情總結！")

    test_msg = st.text_area("輸入要發送至 Telegram 的戰情訊息：", "🚀 【量化交易戰鬥艙】系統運作正常，盤勢監控與自動風控連線中！")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🚀 發送 Telegram 測試訊息", type="primary", use_container_width=True):
            try:
                bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "")
                chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "")
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": test_msg
                }
                res = requests.post(url, json=payload, timeout=5)
                if res.status_code == 200:
                    st.success("✅ Telegram 訊息推播成功！請至 Telegram 查看。")
                else:
                    st.error(f"❌ 推送失敗，回應碼：{res.status_code}, 內容：{res.text}")
            except Exception as e:
                st.error(f"❌ 發送發生例外錯誤：{e}")
                
    with col2:
        if st.button("📊 推送目前帳戶總覽與風控摘要", use_container_width=True):
            try:
                bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or st.secrets.get("telegram", {}).get("bot_token", "")
                chat_id = os.environ.get("TELEGRAM_CHAT_ID") or st.secrets.get("telegram", {}).get("chat_id", "")
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                
                summary_text = (
                    f"📊 【量化戰鬥艙戰情總結】\n"
                    f"⏱ 時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"🌐 機器人狀態：@Dailyreport4Jim_bot 運作中\n"
                    f"🛡️ 項目：全自動即時市價結算、M7財報日行事曆與風控鬧鐘已全面啟動！"
                )
                payload = {
                    "chat_id": chat_id,
                    "text": summary_text
                }
                res = requests.post(url, json=payload, timeout=5)
                if res.status_code == 200:
                    st.success("✅ 帳戶戰情總結已成功推播至 Telegram！")
                else:
                    st.error(f"❌ 推送失敗，回應碼：{res.status_code}")
            except Exception as e:
                st.error(f"❌ 發生例外錯誤：{e}")