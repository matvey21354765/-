from __future__ import annotations

import json
from pathlib import Path

from avito_normalizer import normalize_avito_item, normalize_rest_app_items_with_diagnostics, parse_price
from avito_provider_router import AvitoProviderResult, AvitoProviderRouter
from avito_rest_provider import describe_rest_app_payload, extract_rest_app_items


FIXTURE = Path(__file__).parent / "fixtures" / "rest_app_ads_sample.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_extract_rest_app_items_supported_shapes():
    ad = payload()["data"]["items"][0]
    shapes = [[ad], {"ads": [ad]}, {"items": [ad]}, {"data": [ad]},
              {"result": [ad]}, {"data": {"items": [ad]}}]
    assert all(extract_rest_app_items(shape) == [ad] for shape in shapes)
    assert describe_rest_app_payload(shapes[-1])["nested_items_path"] == "data.items"


def test_normalize_price_string_and_nested_location():
    item = normalize_avito_item(payload()["data"]["items"][0], "avito")
    assert parse_price("65 000 ₽") == 65000
    assert item["price"] == 65000
    assert item["city"] == "Омск"
    assert item["region"] == "Омская область"


def test_missing_optional_fields_do_not_drop_listing():
    raw = {"id": "1", "title": "ВАЗ 2107", "price_value": "70 000"}
    item = normalize_avito_item(raw, "avito")
    assert item["phone"] == ""
    assert item["images"] == []


def test_normalization_failure_is_structured_and_does_not_use_fallback():
    normalized, diagnostics = normalize_rest_app_items_with_diagnostics([
        {"id": "1", "title": "No price"}
    ])
    assert normalized == []
    assert diagnostics["failed_count"] == 1
    fallback_calls = []
    router = AvitoProviderRouter(
        lambda: AvitoProviderResult(http=200, raw_count=1, normalized_count=0),
        lambda: fallback_calls.append(True) or AvitoProviderResult(provider="playwright"),
    )
    result = router.search()
    assert result.error == "normalization_failed"
    assert fallback_calls == []


def test_network_error_can_use_fallback():
    router = AvitoProviderRouter(
        lambda: AvitoProviderResult(error="timeout"),
        lambda: AvitoProviderResult(items=[{"id": "fallback"}], provider="playwright"),
    )
    assert router.search().fallback_used is True
