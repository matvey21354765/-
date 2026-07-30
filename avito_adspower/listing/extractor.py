from __future__ import annotations

from typing import Any

from ..selectors import listing as selectors
from .normalizer import normalize_photo_url, parse_int


async def _first_text(page: object, choices: tuple[str, ...]) -> str | None:
    for selector in choices:
        locator = page.locator(selector)
        if await locator.count():
            value = (await locator.first.inner_text()).strip()
            if value:
                return value
    return None


class ListingExtractor:
    async def extract(self, page: object) -> dict[str, Any]:
        canonical = await page.locator(selectors.CANONICAL).get_attribute(
            "href"
        )
        title = await _first_text(page, selectors.TITLE) or ""
        price_text = await _first_text(page, selectors.PRICE)
        attributes: dict[str, str] = {}
        for selector in selectors.ATTRIBUTES:
            items = page.locator(selector)
            for index in range(await items.count()):
                text = (await items.nth(index).inner_text()).strip()
                if ":" in text:
                    key, value = text.split(":", 1)
                    attributes[key.strip()] = value.strip()
        photos: list[str] = []
        for selector in selectors.PHOTOS:
            items = page.locator(selector)
            for index in range(await items.count()):
                url = normalize_photo_url(
                    await items.nth(index).get_attribute("src")
                )
                if url and url not in photos:
                    photos.append(url)
        return {
            "canonical_url": canonical or page.url,
            "title": title,
            "price": parse_int(price_text),
            "description": await _first_text(page, selectors.DESCRIPTION),
            "seller_name": await _first_text(page, selectors.SELLER),
            "attributes": attributes,
            "photo_urls": tuple(photos),
        }
