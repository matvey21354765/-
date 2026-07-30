"""One safe production Auto.ru request with classified diagnostics."""

from __future__ import annotations

import datetime
import json
import os
import sys

from dotenv import load_dotenv
from autoru_transport import (
    autoru_captcha_detected,
    autoru_proxies,
    autoru_timeout,
    get_autoru_transport,
    proxy_host_safe,
)

load_dotenv()


def main() -> int:
    import control_bot
    from curl_cffi import requests as cffi_requests

    transport = get_autoru_transport()
    proxies = autoru_proxies()
    result = {
        "transport": transport["mode"],
        "proxy_configured": transport["mode"] == "proxy",
        "proxy_host_safe": proxy_host_safe(),
        "http_status": None,
        "final_url": "",
        "content_type": "",
        "response_size": 0,
        "captcha_detected": False,
        "raw_items_count": 0,
        "normalized_items_count": 0,
        "error_type": None,
        "error_message_safe": "",
    }

    url = (
        "https://auto.ru/krasnodar/cars/used/"
        "?seller_group=PRIVATE&price_to=100000&sort=fresh_relevance_1-desc"
    )
    session = cffi_requests.Session(
        impersonate="chrome120",
        trust_env=False,
    )
    try:
        response = session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": "https://auto.ru/",
                "Upgrade-Insecure-Requests": "1",
            },
            proxies=proxies,
            timeout=autoru_timeout(),
            allow_redirects=True,
        )
        result["http_status"] = int(response.status_code)
        result["final_url"] = str(response.url)
        result["content_type"] = str(
            response.headers.get("content-type", "")
        )
        result["response_size"] = len(response.content or b"")
        result["captcha_detected"] = autoru_captcha_detected(
            response.status_code,
            str(response.url),
            response.text,
        )
        if response.status_code == 407:
            result["error_type"] = "proxy_auth"
            result["error_message_safe"] = "HTTP 407"
        elif result["captcha_detected"]:
            result["error_type"] = "restriction_captcha"
            result["error_message_safe"] = (
                f"HTTP {response.status_code}; Auto.ru CAPTCHA"
            )
        elif response.status_code != 200:
            result["error_type"] = "network"
            result["error_message_safe"] = f"HTTP {response.status_code}"
        else:
            items = control_bot._autoru_parse_html(
                response.text, datetime.date.today()
            )
            result["raw_items_count"] = len(items)
            identities = {
                control_bot._item_identity(item): item
                for item in items
                if control_bot._item_identity(item)
            }
            normalized = list(identities.values())
            result["normalized_items_count"] = len(normalized)
            if not normalized:
                result["error_type"] = "parse_error"
                result["error_message_safe"] = (
                    "HTTP 200 response contained no recognized listings"
                )
    except Exception as exc:
        text = str(exc).lower()
        result["error_type"] = (
            "proxy_auth"
            if "407" in text or "proxy authentication" in text
            else "network"
        )
        result["error_message_safe"] = type(exc).__name__
    finally:
        session.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["normalized_items_count"]:
        return 0
    if result["error_type"] == "restriction_captcha":
        return 2
    if result["error_type"] == "proxy_auth":
        return 3
    if result["error_type"] == "network":
        return 4
    if result["error_type"] == "parse_error":
        return 5
    return 6


if __name__ == "__main__":
    sys.exit(main())
