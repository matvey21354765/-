from __future__ import annotations

import re
from urllib.parse import urlencode

from ..models import SearchRequest


class SearchUrlBuilder:
    @staticmethod
    def _slug(value: str) -> str:
        slug = value.strip().lower().replace(" ", "_")
        return re.sub(r"[^a-zа-яё0-9_-]+", "", slug)

    def build(self, request: SearchRequest, page: int = 1) -> str:
        city = self._slug(request.city)
        path = f"https://www.avito.ru/{city}/avtomobili"
        if request.brand:
            path += "/" + self._slug(request.brand)
        if request.model:
            path += "/" + self._slug(request.model)
        query: dict[str, str | int] = {}
        if request.price_min is not None:
            query["pmin"] = max(0, request.price_min)
        if request.price_max is not None:
            query["pmax"] = max(0, request.price_max)
        if request.year_min is not None:
            query["f"] = f"188_0b{request.year_min}"
        if request.seller_type == "private":
            query["user"] = 1
        if request.sort == "date":
            query["s"] = 104
        if page > 1:
            query["p"] = page
        return path + (f"?{urlencode(query)}" if query else "")
