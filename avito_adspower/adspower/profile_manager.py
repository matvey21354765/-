from __future__ import annotations

from dataclasses import dataclass

from ..config import WorkerConfig
from .client import AdsPowerClient, ProfileConnection


@dataclass
class ManagedProfile:
    connection: ProfileConnection
    started_by_worker: bool


class ProfileManager:
    def __init__(self, client: AdsPowerClient, config: WorkerConfig) -> None:
        self._client = client
        self._config = config
        self._managed: ManagedProfile | None = None

    async def acquire(self) -> ManagedProfile:
        current = await self._client.status()
        if current.active and current.cdp_endpoint:
            self._managed = ManagedProfile(current, False)
        else:
            self._managed = ManagedProfile(await self._client.start(), True)
        return self._managed

    async def release(self) -> None:
        if (
            self._managed
            and self._managed.started_by_worker
            and self._config.stop_profile_on_exit
        ):
            await self._client.stop()
