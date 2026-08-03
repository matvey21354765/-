"""Single source of truth for selecting the Avito provider.

REST-App is intentionally retired.  Old Railway variables that still select
``rest_app`` are migrated to the supported browser provider so a stale
configuration cannot silently re-enable the removed integration.
"""
from __future__ import annotations

import os

SUPPORTED_AVITO_PROVIDERS = {"disabled", "playwright", "adspower_worker"}
_RETIRED_PROVIDER_ALIASES = {"rest_app": "playwright"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def get_avito_provider() -> str:
    explicit = os.getenv("AVITO_PROVIDER", "").strip().lower()
    legacy = os.getenv("AVITO_SOURCE", "").strip().lower()
    selected = explicit or legacy

    # REST-App is no longer a runtime option.  Keep stale production
    # environments operational by moving them to Playwright and disabling all
    # REST-App imports before control_bot evaluates REST_APP_ENABLED.
    selected = _RETIRED_PROVIDER_ALIASES.get(selected, selected)
    os.environ["REST_APP_ENABLED"] = "false"

    if selected:
        return selected if selected in SUPPORTED_AVITO_PROVIDERS else "disabled"

    enabled = os.getenv("AVITO_ENABLED", "").strip().lower() in _TRUE_VALUES
    return "playwright" if enabled else "disabled"
