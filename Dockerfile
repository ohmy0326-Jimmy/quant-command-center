FROM python:3.10-slim

WORKDIR /app

# 安裝必要的系統編譯與網路工具（已移除會報錯的套件）
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 複製並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案所有程式碼
COPY . .

# 設定 Port 與啟動指令
EXPOSE 8080

CMD ["streamlit", "run", "Dashboard.py", "--server.port=8080", "--server.address=0.0.0.0", "--server.headless=true"]