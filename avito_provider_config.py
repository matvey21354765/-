"""Single source of truth for selecting the Avito provider."""
from __future__ import annotations

import os

SUPPORTED_AVITO_PROVIDERS = {"disabled", "rest_app", "playwright", "adspower_worker", "webjson"}


def get_avito_provider() -> str:
    explicit = os.getenv("AVITO_PROVIDER", "").strip().lower()
    legacy = os.getenv("AVITO_SOURCE", "").strip().lower()
    selected = explicit or legacy or "disabled"
    return selected if selected in SUPPORTED_AVITO_PROVIDERS else "disabled"
