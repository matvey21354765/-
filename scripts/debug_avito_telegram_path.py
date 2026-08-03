"""Run the real Avito-to-Telegram data path without sending Telegram messages."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


def main() -> int:
    report = {
        "collector_returned": 0, "cached_returned": 0, "adapted": 0,
        "after_common_filters": 0, "counter_value": 0, "error": "",
    }
    try:
        import control_bot as bot
        region = os.getenv("AVITO_DEBUG_REGION", "moscow")
        price_min = int(os.getenv("AVITO_DEBUG_PRICE_MIN", "0"))
        price_max = int(os.getenv("AVITO_DEBUG_PRICE_MAX", "99000000"))
        brand = os.getenv("AVITO_DEBUG_BRAND", "")
        before = dict(
            bot._REST_APP_COLLECTOR.last_diagnostics
            if bot._REST_APP_COLLECTOR is not None else {}
        )
        cached = bot._avito_cached_result(
            region, price_min, price_max, True, brand
        )
        after = dict(
            bot._REST_APP_COLLECTOR.last_diagnostics
            if bot._REST_APP_COLLECTOR is not None else {}
        )
        final = bot._avito_common_pipeline(
            cached, price_min, price_max, "all", brand
        )
        report.update({
            "collector_returned": int(after.get("provider_returned") or len(cached)),
            "cached_returned": len(cached), "adapted": len(cached),
            "after_common_filters": len(final),
            "counter_value": sum(1 for item in final if item.get("source") == "avito"),
            "cache_hit": bool(after.get("cache_hit")),
            "previous_status": before.get("status"),
        })
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    for key, value in report.items():
        print(f"{key}={json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    return 0 if report["counter_value"] > 0 and not report["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
