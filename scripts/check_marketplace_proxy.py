"""One-shot production diagnostic for the shared marketplace mobile proxy."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from curl_cffi import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avito_duff_provider import AvitoDuffProvider
from avito_proxy_config import build_mobile_proxy_config
from marketplace_result import classify_network_error


load_dotenv(ROOT / ".env")


def _avito_url() -> str:
    report_path = ROOT / "data" / "vless_test.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for key in ("canonical_final_url", "canonical_url", "final_url"):
                value = report.get(key)
                if isinstance(value, str) and value.startswith("https://www.avito.ru/"):
                    return value
        except (OSError, ValueError):
            pass
    return "https://www.avito.ru/moskva/avtomobili?s=104"


def _autoru_count(text: str) -> int:
    ids = set(re.findall(r'data-ftid=["\']bulls-list_bull["\'][^>]*', text))
    if ids:
        return len(ids)
    return len(set(re.findall(r"/cars/used/sale/[^\"'?]+/\d+", text)))


def main() -> int:
    result = {
        "proxy": {},
        "external_ip": "",
        "ipify_http": None,
        "avito_http": None,
        "avito_items": 0,
        "autoru_http": None,
        "autoru_items": 0,
        "error_class": "",
    }
    try:
        config = build_mobile_proxy_config()
        result["proxy"] = {
            "protocol": config.protocol,
            "host": config.host,
            "port": config.port,
            "credentials_present": config.credentials_present,
        }
        with requests.Session(impersonate="chrome120") as session:
            response = session.get(
                "https://api.ipify.org?format=json",
                proxies=config.proxies,
                timeout=config.timeout,
            )
        result["ipify_http"] = response.status_code
        if response.status_code == 407:
            result["error_class"] = "ProxyAuthenticationError"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        response.raise_for_status()
        result["external_ip"] = str(response.json().get("ip", ""))
    except Exception as exc:
        _, result["error_class"] = classify_network_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    try:
        provider = AvitoDuffProvider(
            socks_proxy=config.proxy_url,
            timeout=config.timeout,
            max_internal_redirects=1,
        )
        items = provider.search(_avito_url())
        result["avito_http"] = provider.last_diagnostics.get("http")
        result["avito_items"] = len(items)
    except Exception as exc:
        result["avito_http"] = getattr(exc, "status_code", None)
        _, result["error_class"] = classify_network_error(exc)

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": "https://auto.ru/",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        with requests.Session(impersonate="chrome120") as session:
            response = session.get(
                "https://auto.ru/moskva/cars/used/?seller_group=PRIVATE",
                headers=headers,
                proxies=config.proxies,
                timeout=config.timeout,
            )
        result["autoru_http"] = response.status_code
        if response.status_code == 200:
            result["autoru_items"] = _autoru_count(response.text or "")
        elif response.status_code == 407:
            result["error_class"] = "ProxyAuthenticationError"
        elif response.status_code == 429:
            result["error_class"] = "MarketplaceRateLimitedError"
        elif response.status_code == 403:
            result["error_class"] = "MarketplaceBlockedError"
    except Exception as exc:
        _, result["error_class"] = classify_network_error(exc)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (
        result["ipify_http"] == 200
        and result["avito_http"] == 200
        and result["avito_items"] > 0
        and result["autoru_http"] == 200
        and result["autoru_items"] > 0
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
