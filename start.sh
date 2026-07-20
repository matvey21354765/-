#!/bin/sh
# Стартовый скрипт: рендерит xray_config.json из env (если заданы),
# затем запускает бота. Секреты НЕ зашиты в репо — берутся из переменных окружения.
set -e

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
