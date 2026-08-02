"""Local smoke test for the production Auto.ru Telegram search path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from autoru_transport import normalize_autoru_result


def main() -> int:
    load_dotenv()
    import control_bot

    result = control_bot.scrape_autoru(
        "krasnodar",
        pages=1,
        price_min=0,
        price_max=100000,
    )
    normalized = normalize_autoru_result(result)
    items = normalized["items"]
    first = items[0] if items else {}
    output = {
        "result_type": type(result).__name__,
        "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        "items_count": len(items),
        "first_item_title": first.get("title"),
        "first_item_price": first.get("_price_int") or first.get("price"),
        "first_item_url": first.get("url"),
        "error": normalized["error"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
