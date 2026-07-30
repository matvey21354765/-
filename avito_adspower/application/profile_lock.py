from __future__ import annotations

import asyncio
from collections import defaultdict

from ..exceptions import ProfileBusyError

_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class ProfileLock:
    def __init__(self, profile_id: str, wait_seconds: float = 0) -> None:
        self._lock = _LOCKS[profile_id]
        self._wait_seconds = wait_seconds

    async def __aenter__(self) -> "ProfileLock":
        if self._wait_seconds <= 0 and self._lock.locked():
            raise ProfileBusyError("AdsPower profile is busy")
        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=max(0.01, self._wait_seconds),
            )
        except TimeoutError as exc:
            raise ProfileBusyError("AdsPower profile is busy") from exc
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._lock.locked():
            self._lock.release()
