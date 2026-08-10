# AURA — landing

Одностраничный лендинг для AURA: Telegram-бот для торговли на рынках предсказаний Polymarket.

Один статический файл `index.html` — без сборки, зависимостей и внешних запросов.
Шрифты системные, все иконки и графики — инлайн-SVG.

## Локально

Открыть `index.html` в браузере. Либо:

    python3 -m http.server 8000

## Деплой

- **Netlify Drop** — перетащить папку на https://app.netlify.com/drop
- **Vercel** — импорт репозитория, framework preset: Other
- **GitHub Pages** — Settings → Pages → Deploy from branch → `/` (root)

## Структура

    index.html      вся страница: разметка, стили, графика
    netlify.toml    настройки публикации

## Блоки

1. Шапка и hero — заголовок, CTA, продуктовая полоса со скриншотом бота
2. Бегущая строка активных рынков
3. Powerful tools — шесть возможностей продукта
4. All connected — схема экосистемы
5. CTA и футер с контактами (Telegram, X, Discord, почта)
