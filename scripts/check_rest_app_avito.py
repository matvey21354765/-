"""Diagnostic cascade for finding a working Rest-App /api/ads payload."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from rest_app_avito_provider import RestAppAvitoProvider, RestAppError


load_dotenv()
DEBUG_PATH = Path("data/rest_app_empty_debug.json")


def _range(days: int) -> dict[str, str]:
    now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    return {
        "date1": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
        "date2": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _stages(region_id: str, city_id: str) -> list[tuple[str, dict]]:
    day = _range(1)
    return [
        ("A", {
            "category_id": "9", "region_id": region_id, **day,
        }),
        ("B", {
            "category_id": "9", "city_id": city_id, **day,
        }),
        ("C", {
            "category_id": "9", "region_id": region_id,
            "price1": 0, "price2": 100000, **day,
        }),
    ]


def _safe_filters(filters: dict) -> dict:
    return {
        key: value for key, value in filters.items()
        if key not in {"login", "token"}
    }


def main() -> int:
    try:
        provider = RestAppAvitoProvider()
        info = provider.info()
        regions = provider.regions()
        region_id = provider._find_id(regions, "Краснодарский край")
        if not region_id:
            raise RuntimeError("Краснодарский край is absent from /api/region")
        cities = provider.cities(region_id)
        city_id = provider._find_id(cities, "Краснодар")
        if not city_id:
            raise RuntimeError("Краснодар is absent from /api/city")
    except Exception as exc:
        print(json.dumps({
            "api_status": "",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2))
        return 2

    report = {
        "api_status": str(info.get("status") or ""),
        "stages": [],
        "working_stage": "",
        "working_filters": {},
        "raw_items_count": 0,
        "normalized_items_count": 0,
        "first_five": [],
    }
    last_safe_payload: dict = {}
    stages = _stages(region_id, city_id)
    for index, (name, filters) in enumerate(stages):
        payload: dict = {}
        error = ""
        try:
            payload = provider._post("ads", {
                **filters, "sort": "desc", "limit": 50,
            })
        except RestAppError as exc:
            error = f"{type(exc).__name__}: {exc}"
            raw_diag = provider.last_diagnostics.get("raw_payload")
            if isinstance(raw_diag, dict):
                payload = raw_diag
        raw_items, raw_type = provider._extract_raw_items(payload)
        stage_report = {
            "stage": name,
            "filters": _safe_filters(filters),
            "http": provider.last_diagnostics.get("http"),
            "api_status": payload.get("status") if isinstance(payload, dict) else "",
            "raw_data_type": raw_type,
            "raw_items_count": len(raw_items),
            "top_level_keys": (
                sorted(str(key) for key in payload)
                if isinstance(payload, dict) else []
            ),
            "safe_message": (
                error or ("данные получены" if raw_items else "список пуст")
            ),
        }
        report["stages"].append(stage_report)
        last_safe_payload = payload if isinstance(payload, dict) else {}
        print(json.dumps(stage_report, ensure_ascii=False))
        if raw_items and not report["working_stage"]:
            unique: dict[str, dict] = {}
            rejection_reasons: list[str] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    if len(rejection_reasons) < 5:
                        rejection_reasons.append("element is not an object")
                    continue
                item = provider._normalize(raw)
                if not item.get("source_id"):
                    if len(rejection_reasons) < 5:
                        rejection_reasons.append("missing source_id")
                    continue
                unique[item["source_id"]] = item
            normalized = list(unique.values())
            report.update({
                "working_stage": name,
                "working_filters": _safe_filters(filters),
                "raw_items_count": len(raw_items),
                "normalized_items_count": len(normalized),
                "first_five": normalized[:5],
                "rejection_reasons": (
                    rejection_reasons if not normalized else []
                ),
            })
            Path("data/rest_app_working_filters.json").write_text(
                json.dumps({
                    "stage": name,
                    "filters": _safe_filters(filters),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if index < len(stages) - 1:
            time.sleep(2)

    if not report["working_stage"]:
        DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_PATH.write_text(json.dumps({
            "request_stages": report["stages"],
            "last_response": last_safe_payload,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["normalized_items_count"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
