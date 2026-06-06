from __future__ import annotations
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

NEWS_CHANNEL = "@Predict00000"

_RSS_FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt",       "https://decrypt.co/feed"),
]

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_posted_urls: set[str] = set()

_CRYPTO_TERMS = [
    ("📖 Термин дня: Фьючерс",
     "Фьючерс — контракт на покупку/продажу актива в будущем по заранее оговорённой цене. "
     "В крипте используется для хеджирования и спекуляции без владения самим активом."),
    ("📖 Термин дня: Ликвидация",
     "Ликвидация — принудительное закрытие позиции биржей, когда убыток достигает порога маржи. "
     "При x10 плече достаточно движения в -10% чтобы потерять весь депозит."),
    ("📖 Термин дня: Funding Rate",
     "Ставка финансирования — регулярный платёж между лонгами и шортами на фьючерсном рынке. "
     "Положительный rate = лонги платят шортам (рынок перегрет вверх). Обновляется каждые 8 часов."),
    ("📖 Термин дня: Open Interest (OI)",
     "Открытый интерес — суммарное количество открытых фьючерсных контрактов. "
     "Рост OI + рост цены = сильный тренд. Падение OI = закрытие позиций, ослабление тренда."),
    ("📖 Термин дня: Long/Short Ratio",
     "Соотношение лонг/шорт позиций на бирже. Если >1 — большинство ставит на рост. "
     "Экстремальные значения часто предшествуют развороту — рынок любит ликвидировать толпу."),
    ("📖 Термин дня: RSI",
     "Relative Strength Index — индикатор силы тренда (0–100). "
     "RSI > 70 = перекупленность, возможна коррекция. RSI < 30 = перепроданность, возможен отскок. "
     "Нейтральная зона: 40–60."),
    ("📖 Термин дня: MACD",
     "Moving Average Convergence Divergence — индикатор momentum. "
     "Когда MACD пересекает сигнальную линию снизу вверх — бычий сигнал. "
     "Сверху вниз — медвежий. Гистограмма показывает силу импульса."),
    ("📖 Термин дня: Bollinger Bands",
     "Полосы Боллинджера — канал из трёх линий вокруг цены (SMA ± 2σ). "
     "Сужение полос (squeeze) = скоро сильное движение. "
     "Цена у верхней полосы = перекупленность. У нижней = перепроданность."),
    ("📖 Термин дня: ATR",
     "Average True Range — средний диапазон свечи за N периодов. "
     "Измеряет волатильность рынка. Высокий ATR = крупные движения, сложнее торговать с узким стопом."),
    ("📖 Термин дня: ADX",
     "Average Directional Index — сила тренда (0–100). "
     "ADX < 25 = флэт, сигналы ненадёжны. ADX > 25 = тренд. ADX > 40 = очень сильный тренд."),
    ("📖 Термин дня: Поддержка и сопротивление",
     "Поддержка — уровень, где цена исторически отскакивала вверх. "
     "Сопротивление — уровень, где цена разворачивалась вниз. "
     "Пробой уровня с объёмом = подтверждённый сигнал."),
    ("📖 Термин дня: Дивергенция",
     "Расхождение между ценой и индикатором. "
     "Бычья дивергенция: цена делает новый минимум, RSI — нет → разворот вверх. "
     "Медвежья дивергенция: цена делает новый максимум, RSI падает → разворот вниз."),
    ("📖 Термин дня: Хедж",
     "Хеджирование — открытие противоположной позиции для защиты от убытков. "
     "Пример: держишь BTC spot, открываешь SHORT на фьючерсе → нейтрализуешь риск падения."),
    ("📖 Термин дня: Скальпинг",
     "Стратегия торговли: множество коротких сделок с маленькой прибылью. "
     "Типичный таймфрейм — 1–5 минут. Требует низких комиссий и быстрого исполнения."),
    ("📖 Термин дня: Fear & Greed Index",
     "Индекс страха и жадности (0–100). "
     "0–25 = крайний страх (хорошо для покупки). 75–100 = крайняя жадность (осторожно, возможна коррекция). "
     "Обновляется ежедневно."),
]


async def _fetch_rss(url: str) -> list[dict]:
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return []
                text = await r.text()
        root = ET.fromstring(text)
        ns = ""
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        result = []
        for item in items[:5]:
            title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or ""
            link  = item.findtext("link")  or item.findtext("{http://www.w3.org/2005/Atom}link")  or ""
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "")
            desc  = item.findtext("description") or item.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            # Strip HTML tags from description
            import re
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:300]
            title = title.strip()
            link  = link.strip()
            if title and link:
                result.append({"title": title, "link": link, "desc": desc})
        return result
    except Exception as e:
        logger.warning(f"RSS fetch error {url}: {e}")
        return []


async def fetch_news() -> list[dict]:
    """Fetch fresh news from all RSS sources, skip already posted."""
    all_items = []
    for source, url in _RSS_FEEDS:
        items = await _fetch_rss(url)
        for it in items:
            if it["link"] not in _posted_urls:
                it["source"] = source
                all_items.append(it)
        await asyncio.sleep(0.5)
    return all_items[:5]


def format_news(items: list[dict]) -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    lines = [f"📰 <b>Крипто-новости</b>  <i>{now}</i>\n"]
    for i, it in enumerate(items, 1):
        src = it.get("source", "")
        lines.append(
            f"{i}. 🔹 <a href=\"{it['link']}\">{it['title']}</a>"
            + (f"  <i>({src})</i>" if src else "")
        )
        if it.get("desc"):
            lines.append(f"   <i>{it['desc'][:200]}…</i>")
        lines.append("")
    lines.append("#крипто #новости #BTC #ETH #SOL")
    return "\n".join(lines)


def get_daily_term() -> tuple[str, str]:
    day = datetime.now(timezone.utc).timetuple().tm_yday
    title, body = _CRYPTO_TERMS[day % len(_CRYPTO_TERMS)]
    return title, body


def format_term(title: str, body: str) -> str:
    return (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{body}\n\n"
        f"#термин #обучение #крипта #фьючерсы"
    )


async def post_news_to_channel(bot) -> int:
    """Fetch and post news to the channel. Returns count of posted items."""
    items = await fetch_news()
    if not items:
        logger.info("No new news to post")
        return 0
    text = format_news(items)
    try:
        await bot.send_message(NEWS_CHANNEL, text, parse_mode="HTML",
                               disable_web_page_preview=True)
        for it in items:
            _posted_urls.add(it["link"])
        # Keep cache size bounded
        if len(_posted_urls) > 500:
            _posted_urls.clear()
        logger.info(f"Posted {len(items)} news to {NEWS_CHANNEL}")
        return len(items)
    except Exception as e:
        logger.error(f"Failed to post news: {e}")
        return 0


async def post_term_to_channel(bot) -> None:
    """Post daily crypto term to the channel."""
    title, body = get_daily_term()
    text = format_term(title, body)
    try:
        await bot.send_message(NEWS_CHANNEL, text, parse_mode="HTML")
        logger.info(f"Posted daily term to {NEWS_CHANNEL}")
    except Exception as e:
        logger.error(f"Failed to post term: {e}")
