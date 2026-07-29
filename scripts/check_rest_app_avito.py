"""Production-safe Rest-App Avito API diagnostic."""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

from rest_app_avito_provider import (
    CAR_CATEGORY_ID,
    RestAppAvitoProvider,
    RestAppError,
)


load_dotenv()


def main() -> int:
    city_name = os.getenv("REST_APP_TEST_CITY", "Новосибирск").strip()
    region_name = os.getenv(
        "REST_APP_TEST_REGION", "Новосибирская область"
    ).strip()
    report = {
        "http": None,
        "api_status": "",
        "info": False,
        "categories": 0,
        "car_category_id": "",
        "regions": 0,
        "region_id": "",
        "cities": 0,
        "city_id": "",
        "items_count": 0,
        "raw_data_type": "",
        "raw_items_count": 0,
        "normalized_items_count": 0,
        "rejection_reasons": [],
        "first_five": [],
        "error": "",
    }
    try:
        provider = RestAppAvitoProvider(cache_ttl=120)
        info = provider.info()
        report["http"] = provider.last_diagnostics.get("http")
        report["api_status"] = str(info.get("status") or "")
        report["info"] = report["api_status"] == "ok"

        categories = provider.categories()
        report["categories"] = len(categories)
        def find_car(rows):
            for row in rows:
                if (
                    str(row.get("id")) == CAR_CATEGORY_ID
                    or str(row.get("name", "")).strip().casefold() == "автомобили"
                ):
                    return row
                children = row.get("children") or row.get("childs") or row.get("data")
                if isinstance(children, list):
                    found = find_car(children)
                    if found:
                        return found
            return None

        car = find_car(categories)
        if not car:
            raise RestAppError("Категория автомобилей не найдена")
        report["car_category_id"] = str(car.get("id"))

        regions = provider.regions()
        report["regions"] = len(regions)
        region_id = provider._find_id(regions, region_name)
        if not region_id:
            raise RestAppError(f"Регион не найден: {region_name}")
        report["region_id"] = region_id

        cities = provider.cities(region_id)
        report["cities"] = len(cities)
        report["city_id"] = provider._find_id(cities, city_name)

        items = provider.search(
            region_name=region_name,
            city_name=city_name,
            price_min=0,
            price_max=99_000_000,
            private_only=True,
            limit=50,
        )
        report["http"] = provider.last_diagnostics.get("http")
        report["api_status"] = str(
            provider.last_diagnostics.get("api_status") or report["api_status"]
        )
        report["items_count"] = len(items)
        report["raw_data_type"] = str(
            provider.last_diagnostics.get("raw_data_type") or ""
        )
        report["raw_items_count"] = int(
            provider.last_diagnostics.get("raw_items") or 0
        )
        report["normalized_items_count"] = len(items)
        if report["raw_items_count"] and not items:
            report["rejection_reasons"] = list(
                provider.last_diagnostics.get("rejection_reasons") or []
            )[:5]
        report["first_five"] = items[:5]
    except RestAppError as exc:
        report["http"] = exc.status_code
        report["error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (
        report["api_status"] == "ok"
        and report["normalized_items_count"] > 0
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
