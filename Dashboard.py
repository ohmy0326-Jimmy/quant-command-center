import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.express as pdx
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google import genai
import json

# ==========================================
# 1. 頁面基本設定與密碼驗證防護
# ==========================================
st.set_page_config(
    page_title="量化交易戰鬥艙",
    page_icon="🚀",
    layout="wide"
)

def check_password():
    """傳回 True 代表密碼正確或已通過驗證"""
    def password_entered():
        if st.session_state["password"] == st.secrets["passwords"]["admin"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # 不在記憶體中保留密碼明文
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
        >
        st.error("😕 密碼錯誤，請重新輸入")
        return False
    else:
        return True

# 執行登入檢查
if not check_password():
    st.stop()

# ==========================================
# 2. 初始化核心連線與服務
# ==========================================
st.title("🚀 24小時全自動量化交易戰鬥艙")
st.markdown("---")

# 初始化 Gemini AI
try:
    gemini_client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
except Exception as e:
    st.warning(f"Gemini API 初始化提示: {e}")

# 初始化 Google Sheets 連結
@st.cache_resource
def init_gspread():
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        # 從 Streamlit Secrets 讀取 google_key 字典
        key_dict = dict(st.secrets["google_key"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(key_dict, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        return None

gc = init_gspread()

# ==========================================
# 3. 側邊欄控制與功能選單
# ==========================================
st.sidebar.header("🎛️ 控制中心")
menu = st.sidebar.selectbox(
    "選擇功能模組",
    ["📊 盤勢與部位監控", "🤖 AI 策略參謀", "📱 Telegram 訊息測試"]
)

# ==========================================
# 4. 主畫面模組邏輯
# ==========================================
if menu == "📊 盤勢與部位監控":
    st.header("📊 即時多資產部位與槓桿監控")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(label="帳戶總市值 (NAV)", value="$1,280,500", delta="+2.4%")
    with col2:
        st.metric(label="目前總槓桿率", value="1.45x", delta="-0.05x")
    with col3:
        st.metric(label="今日實現損益", value="+$18,400", delta="+1.4%")
        
    st.markdown("---")
    st.subheader("📋 交易部位清單預覽")
    
    # 模擬資料展示
    df_mock = pd.DataFrame({
        "標的代號": ["2330", "00631L", "SOXL", "TMF"],
        "台股名稱": ["台積電", "富邦台指正二", "半導體三倍做多", "微型台指期"],
        "類型": ["台股", "台股", "美股", "期貨"],
        "持倉股數": [1000, 5000, 800, 2],
        "投入總金額": [650000, 420000, 310000, 150000]
    })
    st.dataframe(df_mock, use_container_width=True)

elif menu == "🤖 AI 策略參謀":
    st.header("🤖 Gemini 量化策略與盤勢諮詢")
    
    user_query = st.text_area("請輸入你想向 AI 參謀詢問的盤勢看法或策略調整：", placeholder="例如：目前大盤融資維持率偏高，我的槓桿部位是否需要進行風控調節？")
    
    if st.button("送出詢問", type="primary"):
        if user_query:
            with st.spinner("AI 參謀正在運算中..."):
                try:
                    response = gemini_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=f"你是一位專業的量化交易操盤手與風控顧問，請以專業、精闢的態度回答以下問題：{user_query}"
                    )
                    st.success("回覆完成：")
                    st.write(response.text)
                except Exception as e:
                    st.error(f"呼叫 Gemini AI 發生錯誤：{e}")
        else:
            st.warning("請先輸入您的問題！")

elif menu == "📱 Telegram 訊息測試":
    st.header("📱 Telegram 戰情推播測試")
    
    st.info("系統已連線至 Telegram Bot，可隨時發送即時成交或風控警報。")
    
    test_msg = st.text_input("輸入要發送的測試訊息：", "🚀 量化交易戰鬥艙：手動測試訊息正常！")
    
    if st.button("發送 Telegram 訊息"):
        try:
            bot_token = st.secrets["telegram"]["bot_token"]
            chat_id = st.secrets["telegram"]["chat_id"]
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": test_msg
            }
            res = requests.post(url, json=payload)
            if res.status_code == 200:
                st.success("✅ Telegram 訊息推播成功！")
            else:
                st.error(f"❌ 推送失敗，回應碼：{res.status_code}")
        except Exception as e:
            st.error(f"發生例外錯誤：{e}")