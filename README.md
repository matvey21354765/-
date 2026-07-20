# Авто-брокер бот

## Production entrypoint

Единственный production entrypoint для деплоя — `start.sh` (он рендерит
секреты из переменных окружения и запускает `control_bot.py`).

* Docker-сборка запускает контейнер командой `CMD ["sh", "start.sh"]`.
* `Procfile`: `worker: sh start.sh`.
* `control_bot.py` — основной модуль бота (запускается через `start.sh`).
* `tg_bot.py` и `avito_bot.py` не являются production entrypoint. Это отдельные локальные/вспомогательные сценарии для Telegram UserBot и Авито-диалогов, их не нужно указывать в Railway Start Command.

> ⚠️ **Секреты только через переменные окружения.** Никаких токенов, UUID
> прокси или ключей в репозитории. VLESS-креды задаются через `XRAY_*`
> (см. `xray_config.example.json`), а не хранятся в `xray_config.json`.

## Railway deploy

Railway настроен на сборку через Dockerfile (`railway.toml` → `builder = "DOCKERFILE"`), поэтому runtime-команда берётся из `Dockerfile`.

### Обязательные Railway Variables

Минимальный набор переменных для production-запуска:

| Variable | Назначение |
| --- | --- |
| `BOT_TOKEN` | Токен production Telegram-бота от BotFather. |
| `ADMIN_ID` или `ADMIN_IDS` | Telegram ID администратора/администраторов для служебных команд, статистики и уведомлений. |

### Рекомендуемые Railway Variables

Эти переменные не меняют production entrypoint, но нужны для устойчивой работы отдельных возможностей:

| Variable | Назначение |
| --- | --- |
| `DATABASE_URL` | PostgreSQL для сохранения состояния между рестартами Railway. |
| `SCRAPER_API_KEY` | Ключ ScraperAPI для резервного получения страниц, если прямой доступ или прокси недоступны. |
| `AVITO_CLIENT_ID` и `AVITO_CLIENT_SECRET` | Официальный API Авито, если используется интеграция с Avito OAuth/API. |
| `PROXY_URL` | Резидентный прокси для Авито, когда Railway IP получает блокировку (формат: `http://user:pass@host:port`). |
| `AVITO_PROXY_HOST`, `AVITO_PROXY_PORT`, `AVITO_PROXY_USER`, `AVITO_PROXY_PASS`, `AVITO_PROXY_PROTOCOL` | Альтернативная настройка прокси отдельными полями (вместо `PROXY_URL`). |
| `RAILWAY_PUBLIC_DOMAIN` | Публичный домен Railway для ссылок на веб-дашборд, если автодетект домена недоступен. |
| `XRAY_UUID`, `XRAY_ADDRESS`, `XRAY_PORT`, `XRAY_PUBLIC_KEY`, `XRAY_SNI`, `XRAY_SHORT_ID`, `XRAY_AUTOSTART` | Параметры VLESS-прокси. Берутся из env, а не из репо. |
| `DISABLE_SSL_VERIFY` | Только `1` для локального SOCKS-прокси (MITM-туннель). По умолчанию — строгая проверка TLS. |

## Disclaimer (важно)

Бот предоставляет **только информацию** об объявлениях и рыночных ценах.
Это **не инвестиционная рекомендация**. Решения о покупке/продаже принимаете
вы самостоятельно и на свой риск. Авторы не несут ответственности за убытки.

При сборе данных с внешних площадок (Авито, Auto.ru, Дром, Юла, Telegram-каналы)
соблюдайте их условия использования и применимое законодательство (включая
обработку персональных данных). Для рассылок в Telegram используйте только
согласие пользователей и не нарушайте Telegram ToS.
