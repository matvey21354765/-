# Авито через VLESS Reality на production VPS

## Где должен работать Xray

Xray должен работать на том же VPS, где запущен Telegram-бот. Happ VPN на
домашнем Windows-компьютере меняет маршрут только этого компьютера и никак не
влияет на удалённый процесс бота.

## Безопасное размещение конфигурации

Не копируйте реальный VLESS-конфиг в репозиторий. На VPS:

```bash
sudo install -d -o xray -g xray -m 0750 /etc/xray
sudo install -o xray -g xray -m 0640 /secure/upload/config.json /etc/xray/config.json
sudo -u xray /usr/local/bin/xray run -test -config /etc/xray/config.json
```

Можно использовать иной путь через серверную переменную `XRAY_CONFIG_PATH`,
но systemd unit следует согласовать с этим путём. В Git остаётся только
`infra/xray/config.example.json`.

## Systemd

```bash
sudo install -o root -g root -m 0644 infra/systemd/xray.service.example /etc/systemd/system/xray.service
sudo systemctl daemon-reload
sudo systemctl enable --now xray.service
sudo systemctl status xray.service --no-pager
```

Inbound должен слушать исключительно `127.0.0.1:10808`. Не открывайте этот
порт в firewall и не публикуйте его в интернет.

## Проверка маршрута

```bash
python3 scripts/check_xray_route.py
```

Проверка считается успешной, только если порт слушается, SOCKS отвечает и
прямой IP отличается от SOCKS IP. Скрипт не обращается к Авито.

## Настройки бота

Добавьте в серверное окружение, не коммитьте `.env`:

```env
AVITO_ENABLED=true
AVITO_PROVIDER=duff_vless
AVITO_SOCKS_PROXY=socks5h://127.0.0.1:10808
AVITO_MIN_INTERVAL_SECONDS=900
AVITO_BLOCK_COOLDOWN_SECONDS=7200
AVITO_MAX_INTERNAL_REDIRECTS=1
AVITO_CACHE_TTL_SECONDS=1800
AVITO_STALE_CACHE_TTL_SECONDS=86400
AVITO_MANUAL_WAIT_SECONDS=20
```

Сначала запускается Xray, затем бот:

```bash
sudo systemctl restart xray.service
python3 scripts/check_xray_route.py
sudo systemctl restart perekupdrive.service
```

Healthcheck из Python:

```bash
python3 -c "import json, control_bot; print(json.dumps(control_bot.check_avito_transport_health(), ensure_ascii=False, indent=2))"
```

Он проверяет только локальный порт и ipify, состояние cooldown и возраст кэша.

## Docker

В текущем production-варианте для обычного VPS рекомендуется запускать бот и
Xray как systemd services на одном host. Если бот фактически запускается из
Docker, `127.0.0.1` внутри контейнера не ведёт на host Xray.

Безопасный вариант — отдельный Xray-контейнер с именем `xray` в общей private
Docker network без публикации порта 10808. Для бота задайте:

```env
AVITO_SOCKS_PROXY=socks5h://xray:10808
```

Добавьте healthcheck Xray и `depends_on: condition: service_healthy`. Не
используйте `network_mode: host` без отдельного обоснования безопасности.

## Поведение при сбоях

- Один логический поиск состоит из initial GET и максимум одного canonical GET.
- Глобальный интервал — минимум 900 секунд.
- Retry, Playwright, Selenium, вторая страница и direct fallback отсутствуют.
- Первый 403/429 блокирует сеть на 2 часа, второй — на 6 часов, следующие —
  на 24 часа.
- Последний успешный результат атомарно хранится в
  `data/avito_last_success.json` и не стирается ошибкой.
- Состояние cooldown атомарно хранится в `data/avito_provider_state.json`.
- При недоступном VPN или временной блокировке используется допустимый stale
  cache, но новые сетевые запросы во время `blocked_until` не выполняются.

## Отключение и откат

Отключить Авито:

```env
AVITO_ENABLED=false
```

Явно переключить сохранённый legacy provider:

```env
AVITO_PROVIDER=legacy
```

Legacy не включается автоматически. Никогда не публикуйте UUID, publicKey,
shortId, serverName, cookies или реальный `config.json`.
