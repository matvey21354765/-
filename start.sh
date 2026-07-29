#!/bin/sh
# Стартовый скрипт: настраивает единый актуальный мобильный прокси,
# при необходимости рендерит Xray-конфиг и запускает бота.
# Секреты берутся только из переменных окружения и не выводятся в лог.
set -e

# AVITO_PROXY_* — источник истины для нового мобильного прокси.
# Из них на каждом старте формируются обе переменные, которые читает старый и
# новый сетевой код: PROXY_URL для Auto.ru/общего транспорта и
# AVITO_SOCKS_PROXY для AvitoDuffProvider. Сохранённый старый PROXY_URL больше
# не может незаметно направить запросы на прежний порт.
if [ -n "$AVITO_PROXY_HOST" ] && [ -n "$AVITO_PROXY_PORT" ]; then
  AVITO_PROXY_PROTOCOL="${AVITO_PROXY_PROTOCOL:-http}"
  export AVITO_PROXY_PROTOCOL

  CURRENT_PROXY_URL="$(python - <<'PY'
import os
from urllib.parse import quote

scheme = os.getenv("AVITO_PROXY_PROTOCOL", "http").strip().lower()
if scheme == "socks5":
    scheme = "socks5h"
host = os.getenv("AVITO_PROXY_HOST", "").strip()
port = os.getenv("AVITO_PROXY_PORT", "").strip()
user = os.getenv("AVITO_PROXY_USER", "")
password = os.getenv("AVITO_PROXY_PASS", "")
auth = ""
if user and password:
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
print(f"{scheme}://{auth}{host}:{port}")
PY
)"

  export PROXY_URL="$CURRENT_PROXY_URL"
  export AVITO_SOCKS_PROXY="$CURRENT_PROXY_URL"
  echo "[start] единый mobile proxy: ${AVITO_PROXY_PROTOCOL}://${AVITO_PROXY_HOST}:${AVITO_PROXY_PORT}"
else
  echo "[start] AVITO_PROXY_HOST/PORT не заданы — обязательный mobile proxy не настроен."
fi

if [ -n "$XRAY_UUID" ]; then
  echo "[start] рендер xray_config.json из env..."
  envsubst < /app/xray_config.example.json > /app/xray_config.json
  if [ "$XRAY_AUTOSTART" = "1" ] && command -v xray >/dev/null 2>&1; then
    echo "[start] запуск xray..."
    xray run -c /app/xray_config.json >/tmp/xray.log 2>&1 &
  fi
else
  echo "[start] XRAY_UUID не задан — xray не настраивается."
fi

echo "[start] запуск бота..."
exec python control_bot.py
