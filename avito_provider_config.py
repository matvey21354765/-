"""Single source of truth for selecting the Avito provider."""
from __future__ import annotations

import os

SUPPORTED_AVITO_PROVIDERS = {"disabled", "rest_app", "playwright", "adspower_worker", "webjson"}


#: рабочий провайдер по умолчанию (см. WORKING_CONFIG.md)
DEFAULT_AVITO_PROVIDER = "webjson"


def get_avito_provider() -> str:
    explicit = os.getenv("AVITO_PROVIDER", "").strip().lower()
    legacy = os.getenv("AVITO_SOURCE", "").strip().lower()
    selected = explicit or legacy or DEFAULT_AVITO_PROVIDER
    if selected not in SUPPORTED_AVITO_PROVIDERS:
        return DEFAULT_AVITO_PROVIDER
    # rest_app отдаёт ленту «новое по всей РФ»: после фильтра по городу остаётся
    # ноль объявлений. Переключаемся на рабочий webjson, если не форсировано явно.
    if selected == "rest_app" and os.getenv("AVITO_FORCE_PROVIDER", "").strip() not in ("1", "true", "yes"):
        return DEFAULT_AVITO_PROVIDER
    return selected
