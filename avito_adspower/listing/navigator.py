from __future__ import annotations

from ..browser.session import captcha_detected
from ..exceptions import AvitoCaptchaError


class ListingNavigator:
    def __init__(self, context: object, timeout: float) -> None:
        self._context = context
        self._timeout = timeout

    async def open(self, url: str) -> object:
        page = await self._context.new_page()
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._timeout * 1000,
            )
            text = await page.locator("body").inner_text()
            if captcha_detected(page.url, text):
                raise AvitoCaptchaError("CAPTCHA on listing page")
            return page
        except BaseException:
            await page.close()
            raise
