"""Резидентский пул прокси для Avito Playwright.

Переменные окружения:
    AVITO_PROXY_HOST
    AVITO_PROXY_PORT_START
    AVITO_PROXY_PORT_END
    AVITO_PROXY_USERNAME
    AVITO_PROXY_PASSWORD
    AVITO_PROXY_PROTOCOL (default: http)
    AVITO_PROXY_CHECK_LIMIT (default: 10)
    AVITO_PROXY_CONNECT_TIMEOUT_SECONDS (default: 8)
    AVITO_PROXY_STICKY_MINUTES (default: 60)

Формат пула:
    pool.proxys.io:10000:LOGIN:PASSWORD
    pool.proxys.io:10001:LOGIN:PASSWORD
    ...

Не логирует логин/пароль и полный proxy URL.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any


try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None


class AvitoProxyPoolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProxyEndpoint:
    """Безопасное представление выбранного прокси-порта."""

    host: str
    port: int
    protocol: str
    username: str
    password: str

    @property
    def server(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    def as_playwright_config(self) -> dict[str, Any]:
        """Полный proxy-объект для playwright.chromium.launch(...)."""
        cfg: dict[str, Any] = {"server": self.server}
        if self.username:
            cfg["username"] = self.username
        if self.password:
            cfg["password"] = self.password
        return cfg

    def safe_summary(self) -> dict[str, Any]:
        return {
            "proxy_host_safe": f"{self.host}:{self.port}",
            "selected_port": self.port,
            "proxy_protocol": self.protocol,
            "credentials_present": bool(self.username and self.password),
        }


class AvitoProxyPool:
    """Круговой перебор портов резидентского пула с липкостью."""

    def __init__(self) -> None:
        self.host = os.getenv("AVITO_PROXY_HOST", "").strip()
        self.port_start = self._int_env("AVITO_PROXY_PORT_START", 0)
        self.port_end = self._int_env("AVITO_PROXY_PORT_END", 0)
        self.username = os.getenv("AVITO_PROXY_USERNAME", "").strip()
        self.password = os.getenv("AVITO_PROXY_PASSWORD", "").strip()
        self.protocol = (os.getenv("AVITO_PROXY_PROTOCOL", "http").strip().lower() or "http")
        self.check_limit = max(1, self._int_env("AVITO_PROXY_CHECK_LIMIT", 10))
        self.connect_timeout = max(1, self._int_env("AVITO_PROXY_CONNECT_TIMEOUT_SECONDS", 8))
        self.sticky_seconds = max(0, self._int_env("AVITO_PROXY_STICKY_MINUTES", 60)) * 60

        self._current_endpoint: ProxyEndpoint | None = None
        self._current_since: float = 0.0
        self._last_success_port: int | None = None
        self._attempted_count: int = 0
        self._idx: int = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, default) or default)
        except (TypeError, ValueError):
            return default

    @property
    def configured(self) -> bool:
        return bool(
            self.host
            and self.username
            and self.password
            and self.port_start
            and self.port_end
            and self.port_end >= self.port_start
        )

    def _iter_ports(self, start_port: int | None = None):
        """Круговой итератор портов."""
        total = self.port_end - self.port_start + 1
        start = self.port_start
        if start_port and self.port_start <= start_port <= self.port_end:
            start = start_port
        idx = start - self.port_start
        for i in range(total):
            yield self.port_start + ((idx + i) % total)

    def _proxy_url(self, port: int) -> str:
        return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{port}"

    def _safe_error(self, error: str) -> str:
        """Убираем креденшелы из текста ошибки на всякий случай."""
        if self.username and self.username in error:
            error = error.replace(self.username, "***")
        if self.password and self.password in error:
            error = error.replace(self.password, "***")
        return error

    def _endpoint(self, port: int) -> ProxyEndpoint:
        return ProxyEndpoint(
            host=self.host,
            port=port,
            protocol=self.protocol,
            username=self.username,
            password=self.password,
        )

    async def check_proxy(self, port: int) -> dict[str, Any]:
        """Проверяет порт через нейтральный сайт.

        Возвращает:
            {"ok": bool, "ip": str|None, "error": str|None, "status": int|None}
        """
        result: dict[str, Any] = {"ok": False, "ip": None, "error": None, "status": None}
        if not _requests:
            result["error"] = "requests_not_available"
            return result

        proxy_url = self._proxy_url(port)
        url = "https://api.ipify.org/?format=json"

        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _requests.get(
                    url,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=self.connect_timeout,
                ),
            )
            result["status"] = resp.status_code
            text_lower = (resp.text or "").lower()

            if resp.status_code == 503 and any(
                marker in text_lower
                for marker in ("no exit node", "unable to assign node", "err-msg")
            ):
                result["error"] = "no_exit_node"
                return result

            if resp.status_code == 407:
                result["error"] = "proxy_auth"
                return result

            if resp.status_code == 200:
                try:
                    ip = resp.json().get("ip", "")
                except Exception:
                    ip = (resp.text or "").strip()
                if ip:
                    result["ok"] = True
                    result["ip"] = ip
                    return result

            result["error"] = f"http_{resp.status_code}"
        except _requests.exceptions.ProxyError as exc:
            err = self._safe_error(str(exc).lower())
            if "407" in err or "proxy authentication" in err:
                result["error"] = "proxy_auth"
            elif "503" in err and any(
                marker in err
                for marker in ("no exit node", "unable to assign node", "err-msg")
            ):
                result["error"] = "no_exit_node"
            else:
                result["error"] = "proxy_connect"
        except _requests.exceptions.ConnectTimeout:
            result["error"] = "proxy_timeout"
        except _requests.exceptions.ConnectionError as exc:
            err = self._safe_error(str(exc).lower())
            if "no exit node" in err or "unable to assign node" in err:
                result["error"] = "no_exit_node"
            else:
                result["error"] = "proxy_connect"
        except Exception as exc:
            err = self._safe_error(str(exc).lower())
            if "timeout" in err:
                result["error"] = "proxy_timeout"
            elif "getaddrinfo" in err or "name or service not known" in err:
                result["error"] = "proxy_dns"
            else:
                result["error"] = "proxy_error"
        return result

    async def select_working_proxy(self) -> ProxyEndpoint | None:
        """Выбирает рабочий порт, проверяя не больше AVITO_PROXY_CHECK_LIMIT портов."""
        async with self._lock:
            now = time.time()
            ports_to_try: list[int] = []

            # Сначала пытаемся повторно использовать текущий порт (липкость)
            if (
                self._current_endpoint
                and (now - self._current_since) < self.sticky_seconds
            ):
                ports_to_try.append(self._current_endpoint.port)

            # Затем стартуем с последнего успешного порта или с текущего индекса кругового перебора
            start = self._last_success_port
            if start is None and self._current_endpoint:
                start = self._current_endpoint.port
            if start is None:
                start = self.port_start + self._idx
            for p in self._iter_ports(start):
                if p not in ports_to_try:
                    ports_to_try.append(p)
                if len(ports_to_try) >= self.check_limit:
                    break

            # Страховка: если check_limit=1 и текущий порт уже в списке
            if not ports_to_try:
                for p in self._iter_ports():
                    ports_to_try.append(p)
                    if len(ports_to_try) >= self.check_limit:
                        break

            self._attempted_count = 0
            for port in ports_to_try:
                self._attempted_count += 1
                res = await self.check_proxy(port)
                if res["ok"]:
                    self._current_endpoint = self._endpoint(port)
                    self._current_since = now
                    self._last_success_port = port
                    return self._current_endpoint
                # Неверные креденшелы — дальше перебирать бесполезно
                if res["error"] == "proxy_auth":
                    break

            self._current_endpoint = None
            self._current_since = 0.0
            return None

    async def mark_failed(self, port: int | None, error: str) -> None:
        async with self._lock:
            if self._current_endpoint and self._current_endpoint.port == port:
                self._current_endpoint = None
                self._current_since = 0.0
            if self._last_success_port == port and error in (
                "no_exit_node",
                "proxy_connect",
                "proxy_timeout",
                "proxy_dns",
                "blocked",
            ):
                self._last_success_port = None
            total = self.port_end - self.port_start + 1
            if total > 0:
                self._idx = (self._idx + 1) % total

    async def mark_success(self, port: int | None) -> None:
        async with self._lock:
            if port:
                self._last_success_port = port
                if self._current_endpoint is None or self._current_endpoint.port != port:
                    self._current_endpoint = self._endpoint(port)
                self._current_since = time.time()
                total = self.port_end - self.port_start + 1
                if total > 0:
                    self._idx = (port - self.port_start) % total

    def get_current_proxy(self) -> ProxyEndpoint | None:
        return self._current_endpoint

    async def reset_current_proxy(self) -> None:
        async with self._lock:
            self._current_endpoint = None
            self._current_since = 0.0

    def get_playwright_config(self) -> dict[str, Any] | None:
        if not self._current_endpoint:
            return None
        return self._current_endpoint.as_playwright_config()

    def summary(self) -> dict[str, Any]:
        base = {
            "proxy_pool": self.configured,
            "ports_range": f"{self.port_start}-{self.port_end}" if self.configured else None,
            "credentials_present": bool(self.username and self.password),
            "last_attempted_ports_count": self._attempted_count,
        }
        if self._current_endpoint:
            return {
                **base,
                **self._current_endpoint.safe_summary(),
                "proxy_configured": True,
            }
        if self.configured:
            return {
                **base,
                "proxy_host_safe": self.host,
                "selected_port": None,
                "proxy_configured": True,
            }
        return {
            **base,
            "proxy_host_safe": None,
            "selected_port": None,
            "proxy_configured": False,
        }


_pool_instance: AvitoProxyPool | None = None
_pool_lock = asyncio.Lock()


async def get_avito_proxy_pool() -> AvitoProxyPool:
    global _pool_instance
    if _pool_instance is None:
        async with _pool_lock:
            if _pool_instance is None:
                _pool_instance = AvitoProxyPool()
    return _pool_instance
