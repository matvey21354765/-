from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..selectors.search import (
    CARD,
    CARD_IMAGE,
    CARD_LINK,
    CARD_PRICE,
    CARD_TITLE,
)


@dataclass(frozen=True)
class SearchCard:
    url: str
    title: str
    price_text: str
    image_url: str | None


def canonicalize_url(url: str) -> str:
    absolute = urljoin("https://www.avito.ru/", url)
    parsed = urlsplit(absolute)
    return urlunsplit(("https", "www.avito.ru", parsed.path, "", ""))


class SearchExtractor:
    async def extract(self, page: object) -> list[SearchCard]:
        cards: list[SearchCard] = []
        locator = page.locator(CARD)
        for index in range(await locator.count()):
            card = locator.nth(index)
            link = card.locator(CARD_LINK).first
            href = await link.get_attribute("href")
            if not href:
                continue
            title = (
                await card.locator(CARD_TITLE).first.inner_text()
                if await card.locator(CARD_TITLE).count()
                else ""
            )
            price = (
                await card.locator(CARD_PRICE).first.inner_text()
                if await card.locator(CARD_PRICE).count()
                else ""
            )
            image = None
            if await card.locator(CARD_IMAGE).count():
                img = card.locator(CARD_IMAGE).first
                image = (
                    await img.get_attribute("src")
                    or await img.get_attribute("data-src")
                )
            cards.append(
                SearchCard(
                    canonicalize_url(href),
                    title.strip(),
                    price.strip(),
                    image,
                )
            )
        return cards
