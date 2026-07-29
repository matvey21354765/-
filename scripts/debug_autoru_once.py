"""One safe production Auto.ru request with classified diagnostics."""

from __future__ import annotations

import datetime
import json
import os
import sys

from dotenv import load_dotenv
from autoru_transport import autoru_proxies, get_autoru_transport

load_dotenv()


def main() -> int:
    import control_bot
    from curl_cffi import requests as cffi_requests

    transport = get_autoru_transport()
    proxies = autoru_proxies()
    result = {
        "transport": transport["mode"],
        "AUTORU_PROXY_URL_present": bool(
            os.getenv("AUTORU_PROXY_URL", "").strip()
        ),
        "PROXY_URL_ignored": True,
        "http_status": None,
        "final_url": "",
        "raw_items_count": 0,
        "normalized_items_count": 0,
        "filtered_items_count": 0,
        "error_type": "",
        "error": "",
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
            timeout=30,
            allow_redirects=True,
        )
        result["http_status"] = int(response.status_code)
        result["final_url"] = str(response.url)
        if response.status_code in (403, 407, 429):
            result["error_type"] = (
                "proxy_auth"
                if response.status_code == 407
                else f"http_{response.status_code}"
            )
            result["error"] = f"HTTP {response.status_code}"
        elif response.status_code != 200:
            result["error_type"] = "network"
            result["error"] = f"HTTP {response.status_code}"
        elif control_bot._autoru_is_captcha(response.text):
            result["error_type"] = "http_403"
            result["error"] = "Auto.ru restriction/captcha page"
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
            result["filtered_items_count"] = sum(
                1
                for item in normalized
                if control_bot.in_price_range(item, 0, 100000)
            )
            if not normalized and len(response.text) > 3000:
                result["error_type"] = "parse_error"
                result["error"] = "HTTP 200 response contained no recognized listings"
    except Exception as exc:
        text = str(exc).lower()
        result["error_type"] = (
            "proxy_auth"
            if "407" in text or "proxy authentication" in text
            else "network"
        )
        result["error"] = type(exc).__name__
    finally:
        session.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["normalized_items_count"] else 1


if __name__ == "__main__":
    sys.exit(main())
