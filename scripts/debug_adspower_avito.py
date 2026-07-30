from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avito_adspower.api import AvitoParser, SearchRequest, WorkerConfig
from avito_adspower.exceptions import (
    AdsPowerProfileNotFoundError,
    AdsPowerUnavailableError,
    AvitoAuthenticationRequiredError,
    AvitoCaptchaError,
    BrowserConnectionError,
    ConfigurationError,
    ParserChangedError,
    SearchDeadlineExceededError,
)
from avito_adspower.search.url_builder import SearchUrlBuilder


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True)
    parser.add_argument("--price-max", type=int)
    parser.add_argument("--max-items", type=int, default=10)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> tuple[dict, int]:
    started = time.monotonic()
    report = {
        "status": "error",
        "adspower_reachable": False,
        "profile_found": False,
        "profile_started_by_worker": False,
        "cdp_connected": False,
        "existing_context_used": False,
        "authorization_detected": False,
        "captcha_detected": False,
        "search_url_safe": "",
        "cards_found": 0,
        "unique_links_found": 0,
        "listings_parsed": 0,
        "items_with_real_url": 0,
        "items_with_price": 0,
        "items_with_photo": 0,
        "elapsed_seconds": 0.0,
        "error_type": None,
        "error_message_safe": None,
    }
    try:
        config = WorkerConfig.from_env()
        request = SearchRequest(
            city=args.city,
            price_max=args.price_max,
            max_pages=config.max_pages,
            max_items=min(args.max_items, config.max_items),
            detail_mode=config.detail_mode,
        )
        report["search_url_safe"] = SearchUrlBuilder().build(request)
        result = await AvitoParser(config).search(request)
        stats = result.statistics
        report.update({
            "status": "ok" if result.items else "error",
            "adspower_reachable": True,
            "profile_found": True,
            "cdp_connected": True,
            "existing_context_used": True,
            "authorization_detected": config.require_auth,
            "captcha_detected": stats.captcha_detected,
            "cards_found": stats.cards_found,
            "unique_links_found": stats.unique_links_found,
            "listings_parsed": stats.listings_parsed,
            "items_with_real_url": sum(
                item.url.startswith("https://www.avito.ru/")
                for item in result.items
            ),
            "items_with_price": sum(
                item.price is not None for item in result.items
            ),
            "items_with_photo": sum(
                bool(item.photo_urls) for item in result.items
            ),
        })
        code = 0 if result.items else 7
    except AvitoCaptchaError as exc:
        report.update({
            "captcha_detected": True,
            "error_type": "captcha",
            "error_message_safe": type(exc).__name__,
        })
        code = 2
    except AdsPowerProfileNotFoundError as exc:
        report.update({
            "adspower_reachable": True,
            "error_type": "profile_not_found",
            "error_message_safe": type(exc).__name__,
        })
        code = 3
    except AdsPowerUnavailableError as exc:
        report.update({
            "error_type": "adspower_unavailable",
            "error_message_safe": type(exc).__name__,
        })
        code = 4
    except AvitoAuthenticationRequiredError as exc:
        report.update({
            "adspower_reachable": True,
            "profile_found": True,
            "error_type": "auth_required",
            "error_message_safe": type(exc).__name__,
        })
        code = 5
    except BrowserConnectionError as exc:
        report.update({
            "adspower_reachable": True,
            "profile_found": True,
            "error_type": "browser_error",
            "error_message_safe": type(exc).__name__,
        })
        code = 6
    except ParserChangedError as exc:
        report.update({
            "error_type": "parser_changed",
            "error_message_safe": type(exc).__name__,
        })
        code = 7
    except ConfigurationError as exc:
        report.update({
            "error_type": "configuration",
            "error_message_safe": type(exc).__name__,
        })
        code = 8
    except (SearchDeadlineExceededError, TimeoutError) as exc:
        report.update({
            "error_type": "timeout",
            "error_message_safe": type(exc).__name__,
        })
        code = 9
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report, code


def main() -> int:
    report, code = asyncio.run(run(_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
