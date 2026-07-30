from __future__ import annotations

from ..browser.session import captcha_detected
from ..exceptions import AvitoCaptchaError
from ..selectors.search import RESULT_CONTAINER


class SearchNavigator:
    def __init__(
        self,
        page: object,
        navigation_timeout: float,
        locator_timeout: float,
    ) -> None:
        self._page = page
        self._navigation_timeout = navigation_timeout
        self._locator_timeout = locator_timeout

    async def open(self, url: str) -> None:
        await self._page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self._navigation_timeout * 1000,
        )
        text = await self._page.locator("body").inner_text()
        if captcha_detected(self._page.url, text):
            raise AvitoCaptchaError("CAPTCHA on search page")
        await self._page.locator(RESULT_CONTAINER).wait_for(
            state="attached",
            timeout=self._locator_timeout * 1000,
        )
