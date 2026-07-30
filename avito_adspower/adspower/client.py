from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

from ..config import WorkerConfig
from ..exceptions import (
    AdsPowerInvalidResponseError,
    AdsPowerProfileNotFoundError,
    AdsPowerProfileStartError,
    AdsPowerUnavailableError,
)


@dataclass(frozen=True)
class ProfileConnection:
    profile_id: str
    active: bool
    cdp_endpoint: str | None


class AdsPowerClient:
    """Isolated adapter for the documented AdsPower Local API v2."""

    def __init__(
        self,
        config: WorkerConfig,
        session: aiohttp.ClientSession,
    ) -> None:
        self._config = config
        self._session = session

    @property
    def _headers(self) -> dict[str, str]:
        if not self._config.adspower_api_key:
            return {}
        return {
            "Authorization": f"Bearer {self._config.adspower_api_key}"
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(
            total=self._config.profile_start_timeout
        )
        try:
            async with self._session.request(
                method,
                f"{self._config.adspower_api_url}{path}",
                params=params,
                json=json_body,
                headers=self._headers,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    raise AdsPowerUnavailableError(
                        f"AdsPower Local API HTTP {response.status}"
                    )
                payload = await response.json(content_type=None)
        except AdsPowerUnavailableError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise AdsPowerUnavailableError(
                f"AdsPower Local API unavailable: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise AdsPowerInvalidResponseError("AdsPower returned non-object")
        if payload.get("code") != 0:
            message = str(payload.get("msg") or "AdsPower request failed")
            if "not exist" in message.lower() or "not found" in message.lower():
                raise AdsPowerProfileNotFoundError(message)
            raise AdsPowerInvalidResponseError(message)
        return payload

    async def health(self) -> bool:
        await self.status()
        return True

    async def status(self) -> ProfileConnection:
        payload = await self._request(
            "GET",
            "/api/v2/browser-profile/active",
            params={"profile_id": self._config.adspower_profile_id},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AdsPowerInvalidResponseError("missing status data")
        status = str(data.get("status") or "").lower()
        ws = data.get("ws") if isinstance(data.get("ws"), dict) else {}
        endpoint = ws.get("puppeteer")
        return ProfileConnection(
            profile_id=self._config.adspower_profile_id,
            active=status == "active",
            cdp_endpoint=str(endpoint) if endpoint else None,
        )

    async def start(self) -> ProfileConnection:
        payload = await self._request(
            "POST",
            "/api/v2/browser-profile/start",
            json_body={
                "profile_id": self._config.adspower_profile_id,
                "last_opened_tabs": "1",
                "proxy_detection": "0",
            },
        )
        data = payload.get("data")
        ws = data.get("ws") if isinstance(data, dict) else None
        endpoint = ws.get("puppeteer") if isinstance(ws, dict) else None
        if not endpoint:
            raise AdsPowerProfileStartError(
                "AdsPower start response has no CDP endpoint"
            )
        return ProfileConnection(
            self._config.adspower_profile_id, True, str(endpoint)
        )

    async def stop(self) -> None:
        await self._request(
            "POST",
            "/api/v2/browser-profile/stop",
            json_body={"profile_id": self._config.adspower_profile_id},
        )
