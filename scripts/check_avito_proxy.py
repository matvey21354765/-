"""Безопасная диагностика proxy и Авито внутри production-контейнера.

Запуск: python -m scripts.check_avito_proxy
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from curl_cffi import requests

from avito_duff_provider import (
    AvitoBlockedError,
    AvitoDuffProvider,
    AvitoParseError,
    AvitoProxyAuthenticationError,
    AvitoProxyConnectionError,
    AvitoRateLimitedError,
)
from avito_production_state import AvitoProductionState
from avito_proxy_config import AvitoProxyConfig, AvitoProxyConfigError


IPIFY_URL = "https://api.ipify.org?format=json"


def _canonical_url() -> str:
    """Берёт последний подтверждённый canonical URL, не фиксируя его в коде."""
    path = Path("data/vless_test.json")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        value = report.get("canonical_avito", {}).get("canonical_url")
        return str(value or "")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""


def _set_fixed_cooldown(http: int) -> None:
    try:
        seconds = max(
            60, int(os.getenv("AVITO_PROXY_COOLDOWN_SECONDS", "3600"))
        )
    except ValueError:
        seconds = 3600
    store = AvitoProductionState()
    state = store.load_state()
    state.update({
        "blocked_until": time.time() + seconds,
        "consecutive_blocks": int(state.get("consecutive_blocks") or 0) + 1,
        "last_http": http,
        "last_failure_at": time.time(),
    })
    store.save_state(state)


def run_diagnostic(
    config: AvitoProxyConfig,
    *,
    session_factory: Callable = requests.Session,
    provider_factory: Callable = AvitoDuffProvider,
    canonical_url: str | None = None,
) -> dict:
    report = {
        **config.safe_summary(),
        "external_ip": "",
        "avito_http": None,
        "avito_title": "",
        "restricted": False,
        "loader_data_found": False,
        "catalog_items_found": False,
        "items_count": 0,
        "first_three": [],
        "result": "",
        "error_type": "",
    }

    session = session_factory(impersonate="chrome120", trust_env=False)
    try:
        response = session.get(
            IPIFY_URL,
            proxies=config.proxies,
            timeout=config.timeout,
        )
        if response.status_code == 407:
            report.update({
                "result": "Неверный логин/пароль прокси.",
                "error_type": "proxy_authentication",
            })
            return report
        if response.status_code != 200:
            report.update({
                "result": f"Проверка proxy вернула HTTP {response.status_code}.",
                "error_type": "proxy_http",
            })
            return report
        report["external_ip"] = str(response.json().get("ip", ""))
        if not report["external_ip"]:
            report.update({
                "result": "Proxy не вернул внешний IP.",
                "error_type": "proxy_response",
            })
            return report
    except Exception:
        report.update({
            "result": "Production-контейнер не может подключиться к прокси.",
            "error_type": "proxy_connection",
        })
        return report
    finally:
        try:
            session.cookies.clear()
        except Exception:
            pass
        session.close()

    url = canonical_url if canonical_url is not None else _canonical_url()
    if not url:
        report.update({
            "result": "Канонический URL диагностики не найден.",
            "error_type": "configuration",
        })
        return report

    provider = provider_factory(
        socks_proxy=config.proxy_url,
        timeout=config.timeout,
        max_internal_redirects=0,
    )
    try:
        items = provider.search(url)
        diagnostics = provider.last_diagnostics
        report.update({
            "avito_http": diagnostics.get("http"),
            "avito_title": diagnostics.get("title", ""),
            "loader_data_found": True,
            "catalog_items_found": "catalog_items" in diagnostics,
            "items_count": len(items),
            "first_three": [
                {
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "url": item.get("url"),
                }
                for item in items[:3]
            ],
            "result": (
                "Прокси и Авито работают."
                if items else
                "Авито вернул пустой catalog.items."
            ),
        })
    except AvitoProxyAuthenticationError:
        report.update({
            "avito_http": 407,
            "result": "Неверный логин/пароль прокси.",
            "error_type": "proxy_authentication",
        })
    except AvitoRateLimitedError:
        _set_fixed_cooldown(429)
        report.update({
            "avito_http": 429,
            "restricted": True,
            "result": "Текущий IP временно ограничен Авито.",
            "error_type": "rate_limited",
        })
    except AvitoBlockedError as exc:
        http = int(exc.status_code or 403)
        _set_fixed_cooldown(http)
        report.update({
            "avito_http": http,
            "restricted": True,
            "result": "Текущий IP отклонён Авито.",
            "error_type": "blocked",
        })
    except AvitoProxyConnectionError:
        report.update({
            "result": "Production-контейнер не может подключиться к прокси.",
            "error_type": "proxy_connection",
        })
    except AvitoParseError:
        report.update({
            "avito_http": provider.last_diagnostics.get("http"),
            "result": "Структура ответа Авито не содержит catalog.items.",
            "error_type": "parse",
        })
    return report


def main() -> int:
    try:
        config = AvitoProxyConfig.from_env()
    except AvitoProxyConfigError as exc:
        print(json.dumps({
            "proxy_enabled": False,
            "credentials_present": False,
            "result": str(exc),
        }, ensure_ascii=False, indent=2))
        return 2
    report = run_diagnostic(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("items_count", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
