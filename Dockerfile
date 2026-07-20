FROM python:3.11-slim

# Системные зависимости для Playwright
RUN apt-get update && apt-get install -y \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libxcb1 \
    libxkbcommon0 libpango-1.0-0 libcairo2 libasound2t64 \
    wget ca-certificates fonts-liberation unzip curl gettext-base \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Устанавливаем xray-core для VLESS прокси
RUN wget -q https://github.com/XTLS/Xray-core/releases/download/v1.8.13/Xray-linux-64.zip \
    -O /tmp/xray.zip && \
    unzip /tmp/xray.zip -d /usr/local/bin/xray-dist && \
    mv /usr/local/bin/xray-dist/xray /usr/local/bin/xray && \
    chmod +x /usr/local/bin/xray && \
    rm -rf /tmp/xray.zip /usr/local/bin/xray-dist

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .

# Healthcheck: бот должен отвечать в Telegram. Проверяем доступность
# веб-дашборда (поднят на :8080 внутри бота) как индикатор живости.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f -s http://localhost:8080/ >/dev/null 2>&1 || exit 1

# Production entrypoint: рендерит секреты из env и запускает бота.
CMD ["sh", "start.sh"]
