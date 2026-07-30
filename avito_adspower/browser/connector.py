from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config import WorkerConfig
from ..exceptions import BrowserConnectionError


@dataclass
class ConnectedBrowser:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    page_created: bool

    async def close(self) -> None:
        if self.page_created:
            await self.page.close()
        await self.playwright.stop()


class BrowserConnector:
    def __init__(self, config: WorkerConfig) -> None:
        self._config = config

    async def connect(self, cdp_endpoint: str) -> ConnectedBrowser:
        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            browser = await asyncio.wait_for(
                playwright.chromium.connect_over_cdp(cdp_endpoint),
                timeout=self._config.cdp_connect_timeout,
            )
            if not browser.contexts:
                await playwright.stop()
                raise BrowserConnectionError(
                    "AdsPower browser has no existing BrowserContext"
                )
            context = browser.contexts[0]
            page_created = not bool(context.pages)
            page = (
                await context.new_page()
                if page_created
                else context.pages[0]
            )
            return ConnectedBrowser(
                playwright, browser, context, page, page_created
            )
        except BrowserConnectionError:
            raise
        except Exception as exc:
            raise BrowserConnectionError(
                f"CDP connection failed: {type(exc).__name__}"
            ) from exc
