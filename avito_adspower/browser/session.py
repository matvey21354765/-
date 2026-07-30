from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import (
    AvitoAuthenticationRequiredError,
    AvitoCaptchaError,
)
from ..selectors.auth import AUTHENTICATED_SELECTORS, LOGIN_URL_MARKERS
from ..selectors.captcha import CAPTCHA_TEXT_MARKERS, CAPTCHA_URL_MARKERS


def captcha_detected(url: str, text: str) -> bool:
    low_url, low_text = url.lower(), text.lower()
    return any(x in low_url for x in CAPTCHA_URL_MARKERS) or any(
        x in low_text for x in CAPTCHA_TEXT_MARKERS
    )


@dataclass
class AvitoSession:
    page: object
    require_auth: bool
    navigation_timeout: float

    async def verify(self) -> None:
        await self.page.goto(
            "https://www.avito.ru/",
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout * 1000,
        )
        text = await self.page.locator("body").inner_text()
        if captcha_detected(self.page.url, text):
            raise AvitoCaptchaError("Avito CAPTCHA or restriction detected")
        if not self.require_auth:
            return
        if any(marker in self.page.url.lower() for marker in LOGIN_URL_MARKERS):
            raise AvitoAuthenticationRequiredError("Avito login required")
        authenticated = False
        for selector in AUTHENTICATED_SELECTORS:
            if await self.page.locator(selector).count():
                authenticated = True
                break
        if not authenticated:
            raise AvitoAuthenticationRequiredError(
                "authenticated Avito profile was not detected"
            )
