FROM python:3.11-slim-bookworm

# システム依存パッケージ
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python依存パッケージ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright + Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# アプリコード
COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "120", "app:app"]
