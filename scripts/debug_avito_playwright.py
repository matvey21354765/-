"""Диагностический скрипт серверного Playwright-провайдера Avito.

Запускать на Railway/Linux:
    python -m scripts.debug_avito_playwright

Скрипт:
- запускает AvitoBrowserManager;
- проверяет пул прокси / AVITO_PROXY_URL;
- выполняет healthcheck (реальный запуск Chromium);
- выполняет один поиск;
- закрывает browser в finally;
- не запускает polling Telegram;
- не печатает секреты.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avito_playwright import AvitoBrowserManager
from avito_proxy_pool import get_avito_proxy_pool


async def main() -> int:
    headless = os.getenv("AVITO_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"}
    pool = await get_avito_proxy_pool()
    pool_summary = pool.summary()

    print(f"headless={headless}")
    print(f"proxy_pool={pool_summary.get('proxy_pool', False)}")
    print(f"proxy_host_safe={pool_summary.get('proxy_host_safe', '')}")
    print(f"ports_range={pool_summary.get('ports_range') or ''}")
    print(f"proxy_url_configured={bool(os.getenv('AVITO_PROXY_URL', '').strip())}")

    manager = AvitoBrowserManager(headless=headless)
    search_result = None

    try:
        hc = await manager.healthcheck()
        print(f"browser_started={hc.get('chromium_started')}")
        print(f"proxy_configured={hc.get('proxy_configured')}")
        selected_port = hc.get("selected_port") or pool_summary.get("current_port")
        print(f"selected_port={selected_port if selected_port is not None else 'null'}")
        print(f"neutral_check_ok={hc.get('ok')}")
        print(f"http_status={hc.get('status') or ''}")
        print(f"final_url={hc.get('final_url') or ''}")
        print(f"captcha_detected={hc.get('captcha_detected')}")
        print(f"blocked_detected={hc.get('blocked_detected')}")

        if hc.get("browser_error_safe"):
            print(f"browser_error_safe={hc['browser_error_safe']}")

        if not hc.get("ok"):
            print(f"error={hc.get('error') or 'unknown'}")
            print("items_count=0")
            print("elapsed_ms=0")
            return 0

        search_result = await manager.search(
            city=os.getenv("AVITO_DEBUG_CITY", "moskva"),
            price_min=0,
            price_max=1_000_000,
            limit=10,
        )
        meta = search_result.get("meta", {})
        print(f"avito_opened=true")
        print(f"final_url={meta.get('final_url') or ''}")
        print(f"captcha_detected={meta.get('captcha_detected')}")
        print(f"blocked_detected={meta.get('blocked_detected')}")
        print(f"items_count={meta.get('normalized_items_count', 0)}")
        print(f"elapsed_ms={meta.get('elapsed_ms', 0)}")

        for idx, item in enumerate(search_result.get("items", [])[:3], start=1):
            print(
                f"item_{idx}="
                f"{item.get('title', '')[:60]} | "
                f"{item.get('price', '')} | "
                f"{item.get('url', '')[:80]}"
            )
    finally:
        try:
            await manager.stop()
        except Exception as exc:
            print(f"manager_stop_error={type(exc).__name__}")

    print(f"error={search_result.get('error') if search_result else 'unknown'}")
    return 0


if __name__ == "__main__":
    rc = asyncio.run(asyncio.wait_for(main(), timeout=120))
    sys.exit(rc)
