from __future__ import annotations
import logging
import xml.etree.ElementTree as ET
from datetime import date
import aiohttp

logger = logging.getLogger(__name__)

NEWS_CHANNEL = "@Predict00000"

_RSS_FEEDS = [
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt", "https://decrypt.co/feed"),
]

_CRYPTO_TERMS = [
    ("Фьючерс (Futures)", "Контракт на покупку/продажу актива по фиксированной цене в будущем. Позволяет торговать с плечом."),
    ("Ликвидация (Liquidation)", "Принудительное закрытие позиции биржей, когда убытки достигают суммы залога."),
    ("Фандинг (Funding Rate)", "Периодический платёж между лонгами и шортами на перп-фьючерсах для выравнивания цены."),
    ("Открытый интерес (Open Interest)", "Суммарное количество открытых позиций на рынке фьючерсов. Рост OI = приток денег."),
    ("Соотношение лонг/шорт (L/S Ratio)", "Доля трейдеров в лонг vs шорт. >1 = больше покупателей, <1 = больше продавцов."),
    ("RSI (Relative Strength Index)", "Индикатор перекупленности/перепроданности. >70 = перекуплен, <30 = перепродан."),
    ("MACD", "Индикатор импульса тренда. Пересечение линий сигнализирует о смене направления."),
    ("Полосы Боллинджера (Bollinger Bands)", "Динамические уровни волатильности. Выход за полосы = сильное движение."),
    ("ATR (Average True Range)", "Средний диапазон движения цены. Высокий ATR = высокая волатильность."),
    ("ADX (Average Directional Index)", "Сила тренда. >25 = сильный тренд, <20 = флэт."),
    ("Поддержка и сопротивление", "Ценовые уровни, где покупатели/продавцы наиболее активны. Пробой уровня = сигнал."),
    ("Дивергенция (Divergence)", "Расхождение между ценой и индикатором. Медвежья: цена растёт, RSI падает."),
    ("Хедж (Hedge)", "Открытие позиции в противоположном направлении для защиты от убытков."),
    ("Скальпинг (Scalping)", "Торговая стратегия с множеством сделок на малых таймфреймах для сбора малых прибылей."),
    ("Индекс страха и жадности (Fear & Greed)", "Показатель настроений рынка 0–100. 0 = крайний страх, 100 = крайняя жадность."),
]

_posted_urls: set[str] = set()


async def fetch_news() -> list[dict]:
    items = []
    timeout = aiohttp.ClientTimeout(total=10)
    for source, url in _RSS_FEEDS:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as r:
                    text = await r.text()
            root = ET.fromstring(text)
            for item in root.findall(".//item")[:3]:
                link = (item.findtext("link") or "").strip()
                title = (item.findtext("title") or "").strip()
                if link and link not in _posted_urls and title:
                    items.append({"source": source, "title": title, "link": link})
                    _posted_urls.add(link)
                    if len(_posted_urls) > 500:
                        _posted_urls.pop()
        except Exception as e:
            logger.warning(f"RSS {source} failed: {e}")
    return items


def format_news(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["📰 <b>Крипто-новости</b>\n"]
    for it in items[:5]:
        lines.append(f"• <a href=\"{it['link']}\">{it['title']}</a>  <i>({it['source']})</i>")
    lines.append("\n#крипто #новости #биткоин #BTC #ETH")
    return "\n".join(lines)


def get_daily_term() -> tuple[str, str]:
    idx = date.today().timetuple().tm_yday % len(_CRYPTO_TERMS)
    return _CRYPTO_TERMS[idx]


def format_term(title: str, body: str) -> str:
    return (
        f"📚 <b>Термин дня: {title}</b>\n\n"
        f"{body}\n\n"
        f"#обучение #крипто #термины #трейдинг"
    )


async def post_news_to_channel(bot) -> None:
    items = await fetch_news()
    if not items:
        return
    text = format_news(items)
    if text:
        try:
            await bot.send_message(NEWS_CHANNEL, text, parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"News post failed: {e}")


async def post_term_to_channel(bot) -> None:
    title, body = get_daily_term()
    text = format_term(title, body)
    try:
        await bot.send_message(NEWS_CHANNEL, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Term post failed: {e}")
