"""One safe diagnostic call through the production Auto.ru search path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from autoru_transport import (
    get_autoru_transport,
    normalize_autoru_result,
    proxy_host_safe,
)

load_dotenv()


def main() -> int:
    import control_bot

    transport = get_autoru_transport()
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
    try:
        autoru_result = normalize_autoru_result(
            control_bot.scrape_autoru(
                "krasnodar", pages=1, price_min=0, price_max=100000
            ),
        )
        items = autoru_result["items"]
        diag = dict(control_bot._AUTORU_LAST_DIAG)
        result.update({
            "http_status": diag.get("http_status"),
            "final_url": diag.get("final_url", ""),
            "content_type": diag.get("content_type", ""),
            "response_size": diag.get("response_size", 0),
            "captcha_detected": bool(diag.get("captcha_detected")),
            "raw_items_count": int(diag.get("raw", 0)),
            "normalized_items_count": len(items),
            "error_type": autoru_result["error"],
            "error_message_safe": (
                autoru_result["error"]
                or diag.get("error_message_safe", "")
            ),
        })
    except Exception as exc:
        result["error_type"] = "network"
        result["error_message_safe"] = type(exc).__name__

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
