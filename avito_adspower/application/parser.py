from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import aiohttp

from ..adspower.client import AdsPowerClient
from ..adspower.profile_manager import ProfileManager
from ..browser.connector import BrowserConnector
from ..browser.session import AvitoSession
from ..config import WorkerConfig
from ..exceptions import (
    AvitoCaptchaError,
    ParserChangedError,
    SearchDeadlineExceededError,
)
from ..listing.extractor import ListingExtractor
from ..listing.navigator import ListingNavigator
from ..listing.normalizer import (
    extract_source_id,
    normalize_photo_url,
    parse_int,
)
from ..models import AvitoListing, SearchRequest, SearchResult, SearchStatistics
from ..search.extractor import SearchExtractor
from ..search.navigator import SearchNavigator
from ..search.paginator import SearchPaginator
from ..search.url_builder import SearchUrlBuilder
from .profile_lock import ProfileLock
from .retry import DelayStrategy, RetryPolicy


class AvitoParser:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        delay: DelayStrategy | None = None,
    ) -> None:
        self.config = config
        self.delay = delay or DelayStrategy(
            config.delay_min_seconds, config.delay_max_seconds
        )

    async def stream(
        self, request: SearchRequest
    ) -> AsyncIterator[AvitoListing]:
        result = await self.search(request)
        for item in result.items:
            yield item

    async def search(self, request: SearchRequest) -> SearchResult:
        started = time.monotonic()
        statistics = SearchStatistics()
        result = SearchResult(statistics=statistics)
        timeout = aiohttp.ClientTimeout(
            total=self.config.profile_start_timeout
        )
        async with ProfileLock(self.config.adspower_profile_id):
            async with aiohttp.ClientSession(timeout=timeout) as http:
                client = AdsPowerClient(self.config, http)
                manager = ProfileManager(client, self.config)
                connected = None
                try:
                    profile = await manager.acquire()
                    connected = await BrowserConnector(self.config).connect(
                        profile.connection.cdp_endpoint or ""
                    )
                    await AvitoSession(
                        connected.page,
                        self.config.require_auth,
                        self.config.navigation_timeout,
                    ).verify()
                    result.items = await asyncio.wait_for(
                        self._collect(
                            request,
                            connected.page,
                            connected.context,
                            statistics,
                            started,
                        ),
                        timeout=self.config.search_deadline,
                    )
                except TimeoutError as exc:
                    raise SearchDeadlineExceededError(
                        "Avito search deadline exceeded"
                    ) from exc
                except AvitoCaptchaError:
                    statistics.captcha_detected = True
                    raise
                finally:
                    statistics.elapsed_seconds = round(
                        time.monotonic() - started, 3
                    )
                    if connected is not None:
                        await connected.close()
                    await manager.release()
        return result

    async def _collect(
        self,
        request: SearchRequest,
        page: object,
        context: object,
        statistics: SearchStatistics,
        started: float,
    ) -> list[AvitoListing]:
        request = SearchRequest(
            **{
                **request.__dict__,
                "max_pages": min(request.max_pages, self.config.max_pages),
                "max_items": min(request.max_items, self.config.max_items),
            }
        )
        paginator = SearchPaginator(
            SearchNavigator(
                page,
                self.config.navigation_timeout,
                self.config.locator_timeout,
            ),
            SearchExtractor(),
            SearchUrlBuilder(),
        )
        retry = RetryPolicy(self.config.retry_attempts)
        seen: set[str] = set()
        output: list[AvitoListing] = []
        async for page_no, cards, _ in paginator.pages(request):
            statistics.pages_processed = page_no
            statistics.cards_found += len(cards)
            for card in cards:
                source_id = extract_source_id(card.url)
                if source_id in seen:
                    continue
                seen.add(source_id)
                statistics.unique_links_found += 1
                if request.detail_mode == "search_only":
                    image = normalize_photo_url(card.image_url)
                    output.append(
                        AvitoListing(
                            source_id=source_id,
                            url=card.url,
                            canonical_url=card.url,
                            title=card.title,
                            price=parse_int(card.price_text),
                            city=request.city,
                            region=request.region,
                            location=", ".join(
                                x for x in (request.region, request.city) if x
                            ),
                            brand=request.brand,
                            model=request.model,
                            photo_urls=(image,) if image else (),
                        )
                    )
                    statistics.listings_parsed += 1
                else:
                    statistics.listings_opened += 1
                    navigator = ListingNavigator(
                        context, self.config.listing_timeout
                    )
                    detail_page = None
                    try:
                        async def operation() -> object:
                            return await navigator.open(card.url)

                        detail_page = await retry.run(
                            operation,
                            lambda: setattr(
                                statistics,
                                "retries",
                                statistics.retries + 1,
                            ),
                        )
                        data = await ListingExtractor().extract(detail_page)
                        canonical = data["canonical_url"]
                        attributes = data["attributes"]
                        output.append(
                            AvitoListing(
                                source_id=extract_source_id(canonical),
                                url=canonical,
                                canonical_url=canonical,
                                title=data["title"] or card.title,
                                price=data["price"] or parse_int(card.price_text),
                                city=request.city,
                                region=request.region,
                                location=", ".join(
                                    x
                                    for x in (request.region, request.city)
                                    if x
                                ),
                                description=data["description"],
                                seller_name=data["seller_name"],
                                year=parse_int(
                                    attributes.get("Год выпуска")
                                ),
                                mileage=parse_int(
                                    attributes.get("Пробег")
                                ),
                                brand=request.brand,
                                model=request.model,
                                attributes=attributes,
                                photo_urls=data["photo_urls"],
                            )
                        )
                        statistics.listings_parsed += 1
                    except AvitoCaptchaError:
                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        statistics.listings_failed += 1
                    finally:
                        if detail_page is not None:
                            await detail_page.close()
                    await self.delay.wait()
                if len(output) >= request.max_items:
                    return output
                if time.monotonic() - started >= self.config.search_deadline:
                    raise SearchDeadlineExceededError("deadline exceeded")
        if not output and statistics.cards_found == 0:
            raise ParserChangedError("Avito search cards were not found")
        return output
