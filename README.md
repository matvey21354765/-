# Авто-брокер бот

## Production entrypoint

Единственный production entrypoint для деплоя — `control_bot.py`.

* Docker-сборка запускает контейнер командой `CMD ["python", "control_bot.py"]`.
* `Procfile` оставлен согласованным с Dockerfile: `worker: python control_bot.py`.
* `tg_bot.py` и `avito_bot.py` не являются production entrypoint. Это отдельные локальные/вспомогательные сценарии для Telegram UserBot и Авито-диалогов, их не нужно указывать в Railway Start Command.

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
