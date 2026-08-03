"""One-request Avito pipeline doctor; never imports or starts Telegram."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from avito_normalizer import normalize_rest_app_items_with_diagnostics
from avito_provider_config import get_avito_provider
from avito_rest_provider import describe_rest_app_payload, extract_rest_app_items
from rest_app_avito_provider import RestAppAvitoProvider


def _base_report() -> dict:
    return {
        "provider_selected": get_avito_provider(), "provider_configured": False,
        "request_started": False, "http_status": None, "final_url": "",
        "payload_type": "", "payload_keys": [], "items_path": "",
        "raw_items_count": 0, "normalized_count": 0,
        "normalization_failed_count": 0, "after_city_filter": 0,
        "after_price_filter": 0, "after_brand_filter": 0,
        "after_user_filters": 0, "after_dedupe": 0,
        "already_seen_count": 0, "returned_count": 0,
        "captcha_detected": False, "blocked_detected": False,
        "error_type": "none", "error_message_safe": "", "elapsed_ms": 0,
    }


def _safe_message(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    for name in ("REST_APP_LOGIN", "REST_APP_TOKEN", "BOT_TOKEN"):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "***")
    return message[:300]


def main() -> int:
    report = _base_report()
    started = time.monotonic()
    try:
        if report["provider_selected"] != "rest_app":
            report["error_type"] = "provider_not_supported_by_safe_doctor"
        else:
            report["provider_configured"] = bool(
                os.getenv("REST_APP_LOGIN", "").strip()
                and os.getenv("REST_APP_TOKEN", "").strip()
            )
            if not report["provider_configured"]:
                report["error_type"] = "provider_not_configured"
            else:
                provider = RestAppAvitoProvider()
                report["request_started"] = True
                params = {"category_id": "9", "sort": "desc", "limit": 50}
                params.update(provider._date_range(1))
                payload = provider._post("ads", params)
                shape = describe_rest_app_payload(payload)
                raw = extract_rest_app_items(payload)
                normalized, diag = normalize_rest_app_items_with_diagnostics(raw)
                unique = {
                    f"avito:{item.get('source_id') or item.get('url')}": item
                    for item in normalized
                }
                count = len(normalized)
                report.update({
                    "http_status": provider.last_diagnostics.get("http"),
                    "final_url": "https://rest-app.net/api/ads",
                    "payload_type": shape["top_level_type"],
                    "payload_keys": shape["top_level_keys"],
                    "items_path": shape["nested_items_path"],
                    "raw_items_count": len(raw), "normalized_count": count,
                    "normalization_failed_count": diag["failed_count"],
                    "after_city_filter": count, "after_price_filter": count,
                    "after_brand_filter": count, "after_user_filters": count,
                    "after_dedupe": len(unique),
                    "already_seen_count": count - len(unique),
                    "returned_count": len(unique),
                })
                if raw and not normalized:
                    report["error_type"] = "normalization_failed"
                elif not shape["nested_items_path"] and not isinstance(payload, list):
                    report["error_type"] = "unexpected_payload_shape"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error_message_safe"] = _safe_message(exc)
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    for key, value in report.items():
        encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        print(f"{key}={encoded}")
    return 0 if report["error_type"] == "none" else 1


if __name__ == "__main__":
    raise SystemExit(main())
