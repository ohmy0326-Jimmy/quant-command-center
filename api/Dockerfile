FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY portfolio_risk.py ai_advice.py portfolio_briefing.py ./
COPY api ./api

EXPOSE 8080

# Cloud Run 會設定 PORT。建置上下文是倉庫根目錄：docker build -f api/Dockerfile .
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
