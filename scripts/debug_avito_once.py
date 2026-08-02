"""Detailed one-shot Avito transport/parser diagnostic.

Per run this performs one IP check and exactly one Avito request through the
same configured proxy. It never retries, rotates the IP, or falls back direct.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curl_cffi import requests
from dotenv import load_dotenv

from avito_duff_provider import _StateParser
from avito_proxy_config import build_mobile_proxy_config
from scripts.check_marketplace_proxy import _avito_url


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "avito_debug.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
load_dotenv(ROOT / ".env")


def _nested(value, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _inspect(document: str) -> dict:
    parser = _StateParser()
    parser.feed(document)
    payload = parser.get_state_payload()
    loader_data = None
    payload_error = ""
    if payload:
        try:
            state = json.loads(html.unescape(payload))
            loader_data = _nested(state, "loaderData", "data")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            payload_error = f"{type(exc).__name__}: {exc}"
    catalog = loader_data.get("catalog") if isinstance(loader_data, dict) else None
    items = catalog.get("items") if isinstance(catalog, dict) else None
    lower = document.lower()
    return {
        "title": parser.title,
        "restricted_text": "доступ ограничен" in lower,
        "captcha": "captcha" in lower or "капча" in lower,
        "loader_data": isinstance(loader_data, dict),
        "catalog_items": isinstance(items, list),
        "items_count": len(items) if isinstance(items, list) else 0,
        "payload_error": payload_error,
    }


def _classification(http: int | None, inspection: dict, error: str = "") -> str:
    if error:
        return f"network_error: {error}"
    if http == 407:
        return "proxy_auth: proxy returned HTTP 407"
    if http == 429:
        return "rate_limited: Avito returned HTTP 429"
    if http == 403:
        return "blocked: Avito returned HTTP 403"
    if inspection["restricted_text"]:
        return "blocked: page contains 'Доступ ограничен'"
    if inspection["captcha"]:
        return "blocked: page contains CAPTCHA marker"
    if http != 200:
        return f"network/http_error: unexpected HTTP {http}"
    if not inspection["loader_data"]:
        detail = inspection["payload_error"] or "loaderData.data is absent"
        return f"parse_error: {detail}"
    if not inspection["catalog_items"]:
        return "parse_error: catalog.items is absent"
    if inspection["items_count"] == 0:
        return "empty: catalog.items is present but empty"
    return "success"


def main() -> int:
    config = build_mobile_proxy_config()
    external_ip = ""
    ipify_error = ""
    try:
        with requests.Session(impersonate="chrome120", trust_env=False) as session:
            ip_response = session.get(
                "https://api.ipify.org?format=json",
                proxies=config.proxies,
                timeout=config.timeout,
            )
        if ip_response.status_code == 200:
            external_ip = str(ip_response.json().get("ip", ""))
        else:
            ipify_error = f"HTTP {ip_response.status_code}"
    except Exception as exc:
        ipify_error = f"{type(exc).__name__}: {exc}"

    if not external_ip:
        print(json.dumps({
            "http_status": None,
            "final_url": "",
            "external_ip": "",
            "user_agent": USER_AGENT,
            "error": f"proxy IP check failed: {ipify_error}",
            "classification_reason": "proxy unavailable; Avito was not requested",
        }, ensure_ascii=False, indent=2))
        return 2

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Referer": "https://www.avito.ru/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    response = None
    request_error = ""
    try:
        with requests.Session(impersonate="chrome120", trust_env=False) as session:
            response = session.get(
                _avito_url(),
                headers=headers,
                proxies=config.proxies,
                timeout=config.timeout,
                allow_redirects=True,
            )
    except Exception as exc:
        request_error = f"{type(exc).__name__}: {exc}"

    document = response.text or "" if response is not None else ""
    inspection = _inspect(document)
    http = int(response.status_code) if response is not None else None
    reason = _classification(http, inspection, request_error)
    report = {
        "http_status": http,
        "full_http_code": http,
        "final_url": str(response.url) if response is not None else "",
        "external_ip": external_ip,
        "user_agent": USER_AGENT,
        "html_first_500": re.sub(r"\s+", " ", document[:500]).strip(),
        "title": inspection["title"],
        "restricted_text": inspection["restricted_text"],
        "captcha": inspection["captcha"],
        "loaderData.data": inspection["loader_data"],
        "catalog.items": inspection["catalog_items"],
        "catalog_items_count": inspection["items_count"],
        "html_size": len((response.content or b"")) if response is not None else 0,
        "classification_reason": reason,
        "parser_empty_reason": (
            "" if inspection["items_count"] > 0 else reason
        ),
        "error": request_error,
    }
    if http == 200:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(document, encoding="utf-8")
        report["saved_html"] = str(OUTPUT.relative_to(ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if reason == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
