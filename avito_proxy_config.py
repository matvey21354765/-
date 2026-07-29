"""Безопасная конфигурация прокси Авито из существующих AVITO_PROXY_*."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse, urlunsplit


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


def build_mobile_proxy_config() -> AvitoProxyConfig:
    """Build the single marketplace proxy without ever allowing direct access.

    A complete AVITO_PROXY_* set is authoritative.  PROXY_URL is accepted only
    as a compatibility fallback for older deployments.
    """
    host = os.getenv("AVITO_PROXY_HOST", "").strip()
    port = os.getenv("AVITO_PROXY_PORT", "").strip()
    protocol = os.getenv("AVITO_PROXY_PROTOCOL", "").strip()
    user = os.getenv("AVITO_PROXY_USER", "")
    password = os.getenv("AVITO_PROXY_PASS", "")
    if host and port and protocol and user and password:
        return AvitoProxyConfig.from_env()

    raw = os.getenv("PROXY_URL", "").strip()
    if not raw:
        raise AvitoProxyConfigError(
            "Mobile proxy is not configured; direct marketplace access is disabled"
        )
    parsed = urlparse(raw)
    if not parsed.hostname or parsed.port is None:
        raise AvitoProxyConfigError("PROXY_URL is invalid")
    scheme = parsed.scheme.lower()
    if scheme == "socks5":
        scheme = "socks5h"
    if scheme not in {"http", "socks5h"}:
        raise AvitoProxyConfigError("Unsupported mobile proxy protocol")
    try:
        timeout = float(os.getenv("AVITO_PROXY_TIMEOUT", "30"))
    except ValueError:
        timeout = 30.0
    return AvitoProxyConfig(
        host=parsed.hostname,
        port=parsed.port,
        protocol=scheme,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        timeout=max(1.0, min(30.0, timeout)),
    )
