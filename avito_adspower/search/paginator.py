from __future__ import annotations

from collections.abc import AsyncIterator

from ..models import SearchRequest
from .extractor import SearchCard, SearchExtractor
from .navigator import SearchNavigator
from .url_builder import SearchUrlBuilder


class SearchPaginator:
    def __init__(
        self,
        navigator: SearchNavigator,
        extractor: SearchExtractor,
        builder: SearchUrlBuilder,
    ) -> None:
        self._navigator = navigator
        self._extractor = extractor
        self._builder = builder

    async def pages(
        self, request: SearchRequest
    ) -> AsyncIterator[tuple[int, list[SearchCard], str]]:
        empty_streak = 0
        previous_urls: set[str] = set()
        for page_no in range(1, request.max_pages + 1):
            url = self._builder.build(request, page_no)
            await self._navigator.open(url)
            cards = await self._extractor.extract(self._navigator._page)
            current = {card.url for card in cards}
            new = current - previous_urls
            if not new:
                empty_streak += 1
            else:
                empty_streak = 0
            yield page_no, cards, url
            if current and current == previous_urls:
                break
            if empty_streak >= 2:
                break
            previous_urls.update(current)
