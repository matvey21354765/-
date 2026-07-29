"""Безопасная конфигурация прокси Авито из существующих AVITO_PROXY_*."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlunsplit


class AvitoProxyConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AvitoProxyConfig:
    host: str
    port: int
    protocol: str
    user: str = ""
    password: str = ""
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "AvitoProxyConfig":
        host = os.getenv("AVITO_PROXY_HOST", "").strip()
        port_raw = os.getenv("AVITO_PROXY_PORT", "").strip()
        protocol = os.getenv("AVITO_PROXY_PROTOCOL", "http").strip().lower()
        user = os.getenv("AVITO_PROXY_USER", "")
        password = os.getenv("AVITO_PROXY_PASS", "")
        if protocol == "socks5":
            protocol = "socks5h"
        if protocol not in {"http", "socks5h"}:
            raise AvitoProxyConfigError(
                "AVITO_PROXY_PROTOCOL должен быть http или socks5"
            )
        if not host or not port_raw:
            raise AvitoProxyConfigError(
                "AVITO_PROXY_HOST/AVITO_PROXY_PORT не заданы"
            )
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise AvitoProxyConfigError("Некорректный AVITO_PROXY_PORT") from exc
        if not 1 <= port <= 65535:
            raise AvitoProxyConfigError("Некорректный AVITO_PROXY_PORT")
        try:
            timeout = float(os.getenv("AVITO_PROXY_TIMEOUT", "30"))
        except ValueError:
            timeout = 30.0
        return cls(
            host=host,
            port=port,
            protocol=protocol,
            user=user,
            password=password,
            timeout=max(1.0, min(30.0, timeout)),
        )

    @property
    def credentials_present(self) -> bool:
        return bool(self.user and self.password)

    @property
    def proxy_url(self) -> str:
        auth = ""
        if self.credentials_present:
            auth = (
                f"{quote(self.user, safe='')}:{quote(self.password, safe='')}@"
            )
        return urlunsplit((
            self.protocol,
            f"{auth}{self.host}:{self.port}",
            "",
            "",
            "",
        ))

    @property
    def proxies(self) -> dict[str, str]:
        url = self.proxy_url
        return {"http": url, "https": url}

    def safe_summary(self) -> dict:
        return {
            "proxy_enabled": True,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "credentials_present": self.credentials_present,
        }

    def __repr__(self) -> str:
        return (
            "AvitoProxyConfig("
            f"host={self.host!r}, port={self.port!r}, "
            f"protocol={self.protocol!r}, "
            f"credentials_present={self.credentials_present!r})"
        )
