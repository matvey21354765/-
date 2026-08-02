"""One-request, secret-safe REST-App Avito pipeline doctor."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avito_normalizer import normalize_rest_app_items_with_diagnostics
from avito_rest_provider import describe_rest_app_payload, extract_rest_app_items, save_safe_rest_app_sample
from rest_app_avito_provider import CAR_CATEGORY_ID, RestAppAvitoProvider, RestAppError


def main() -> int:
    report = {
        "status": None, "payload_type": "", "payload_keys": [],
        "raw_items_count": 0, "first_item_keys": [],
        "normalized_count": 0, "failed_count": 0, "filtered_count": 0,
        "first_normalized_title": "", "first_normalized_price": None,
        "first_normalized_city": "", "error": "",
    }
    try:
        provider = RestAppAvitoProvider()
        now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
        payload = provider._post("ads", {
            "category_id": CAR_CATEGORY_ID, "sort": "desc", "limit": 50,
            "date1": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "date2": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        shape = describe_rest_app_payload(payload)
        raw = extract_rest_app_items(payload)
        normalized, diagnostics = normalize_rest_app_items_with_diagnostics(raw)
        save_safe_rest_app_sample(payload)
        report.update({
            "status": provider.last_diagnostics.get("http") or 200,
            "payload_type": shape["top_level_type"],
            "payload_keys": shape["top_level_keys"],
            "raw_items_count": len(raw),
            "first_item_keys": shape["first_item_keys"],
            "normalized_count": len(normalized),
            "failed_count": diagnostics["failed_count"],
            "filtered_count": len(normalized),
        })
        if raw and not normalized:
            report["error"] = "normalization_failed"
        elif not shape["nested_items_path"]:
            report["error"] = "unexpected_payload_shape"
        if normalized:
            first = normalized[0]
            report.update({
                "first_normalized_title": first["title"],
                "first_normalized_price": first["price"],
                "first_normalized_city": first["city"],
            })
    except RestAppError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
