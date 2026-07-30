from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass(frozen=True)
class SearchRequest:
    city: str
    region: str | None = None
    brand: str | None = None
    model: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    year_min: int | None = None
    year_max: int | None = None
    seller_type: Literal["private", "company", "any"] = "any"
    sort: Literal["date", "default"] = "date"
    max_pages: int = 2
    max_items: int = 50
    detail_mode: Literal["search_only", "full"] = "full"

    def __post_init__(self) -> None:
        if not self.city.strip():
            raise ValueError("city is required")
        if self.seller_type not in {"private", "company", "any"}:
            raise ValueError("invalid seller_type")
        if self.sort not in {"date", "default"}:
            raise ValueError("invalid sort")
        if self.detail_mode not in {"search_only", "full"}:
            raise ValueError("invalid detail_mode")
        if self.max_pages < 1 or self.max_items < 1:
            raise ValueError("limits must be positive")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchRequest":
        return cls(**{
            key: value[key]
            for key in cls.__dataclass_fields__
            if key in value
        })


@dataclass(frozen=True)
class AvitoListing:
    source_id: str
    url: str
    canonical_url: str
    title: str
    price: int | None = None
    currency: str = "RUB"
    city: str | None = None
    region: str | None = None
    location: str | None = None
    description: str | None = None
    seller_name: str | None = None
    seller_type: Literal["private", "company", "unknown"] = "unknown"
    published_at: datetime | None = None
    collected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    year: int | None = None
    mileage: int | None = None
    brand: str | None = None
    model: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    photo_urls: tuple[str, ...] = ()
    status: Literal["active", "unknown"] = "active"
    source: Literal["avito"] = "avito"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("published_at", "collected_at"):
            if value[key] is not None:
                value[key] = value[key].isoformat()
        value["photo_urls"] = list(self.photo_urls)
        return value


@dataclass
class SearchStatistics:
    pages_processed: int = 0
    cards_found: int = 0
    unique_links_found: int = 0
    listings_opened: int = 0
    listings_parsed: int = 0
    listings_failed: int = 0
    retries: int = 0
    captcha_detected: bool = False
    elapsed_seconds: float = 0.0


@dataclass
class SearchResult:
    items: list[AvitoListing] = field(default_factory=list)
    statistics: SearchStatistics = field(default_factory=SearchStatistics)
    stopped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "statistics": asdict(self.statistics),
            "stopped_reason": self.stopped_reason,
            "warnings": list(self.warnings),
        }
