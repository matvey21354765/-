"""Print secret-safe REST-App collector usage diagnostics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
