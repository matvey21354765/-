from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..exceptions import (
    AvitoAuthenticationRequiredError,
    AvitoCaptchaError,
    AvitoRestrictionError,
)

T = TypeVar("T")


class RetryPolicy:
    def __init__(self, attempts: int) -> None:
        self.attempts = max(1, attempts)

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        retry_counter: Callable[[], None] | None = None,
    ) -> T:
        for attempt in range(self.attempts):
            try:
                return await operation()
            except (
                AvitoCaptchaError,
                AvitoAuthenticationRequiredError,
                AvitoRestrictionError,
                asyncio.CancelledError,
            ):
                raise
            except Exception:
                if attempt + 1 >= self.attempts:
                    raise
                if retry_counter:
                    retry_counter()
                await asyncio.sleep(
                    min(5.0, (2**attempt) + random.uniform(0, 0.5))
                )
        raise RuntimeError("unreachable")


class DelayStrategy:
    def __init__(self, minimum: float, maximum: float) -> None:
        self.minimum = minimum
        self.maximum = maximum

    async def wait(self) -> None:
        await asyncio.sleep(random.uniform(self.minimum, self.maximum))


class NoDelayStrategy(DelayStrategy):
    def __init__(self) -> None:
        super().__init__(0, 0)

    async def wait(self) -> None:
        return None
