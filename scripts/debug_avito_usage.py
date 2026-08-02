"""Print secret-safe REST-App collector usage diagnostics."""

from __future__ import annotations

import json

from dotenv import load_dotenv

from avito_rest_collector import get_rest_app_collector


def main() -> int:
    load_dotenv()
    print(json.dumps(
        get_rest_app_collector().diagnostics(), ensure_ascii=False, indent=2
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
