FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# portfolio_risk 會在讀取部位時再匯入這些本地模組；漏複製會讓 /v1/portfolio 回 502。
# 新增本地模組時要一併加進來（tests/test_api_dockerfile.py 會檢查）。
COPY portfolio_risk.py sector_map.py ai_advice.py portfolio_briefing.py \
     taifex_quotes.py beta_rules.py futures_ledger.py sheet_dates.py stock_futures.py stock_futures_margin.py ./
COPY api ./api

EXPOSE 8080

# Cloud Run 會設定 PORT。建置上下文是倉庫根目錄：docker build -f api/Dockerfile .
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
