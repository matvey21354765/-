"""Диагностический скрипт серверного Playwright-провайдера Avito.

Запускать на Railway/Linux:
    python -m scripts.debug_avito_playwright

Скрипт:
- запускает AvitoBrowserManager;
- выполняет healthcheck;
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


async def main() -> int:
    proxy_url = os.getenv("AVITO_PROXY_URL", "").strip()
    headless = os.getenv("AVITO_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "on"}

    print(f"headless={headless}")
    print(f"proxy_url_configured={bool(proxy_url)}")

    if not proxy_url:
        print("provider_not_configured: AVITO_PROXY_URL не задан")
        print("error=provider_not_configured")
        print("raw_items_count=0")
        print("normalized_items_count=0")
        print("elapsed_ms=0")
        return 0

    manager = AvitoBrowserManager(proxy_url=proxy_url, headless=headless)
    search_result = None

    try:
        hc = await manager.healthcheck()
        print(f"chromium_started={hc.get('chromium_started')}")
        print(f"proxy_configured={hc.get('proxy_configured')}")
        print(f"proxy_protocol={hc.get('proxy_protocol', '')}")
        print(f"proxy_host_safe={hc.get('proxy_host_safe', '')}")
        print(f"healthcheck_ok={hc.get('ok')}")
        print(f"final_url={hc.get('final_url') or ''}")
        print(f"captcha_detected={hc.get('captcha_detected')}")
        print(f"blocked_detected={hc.get('blocked_detected')}")
        print(f"healthcheck_error={hc.get('error') or ''}")

        if not hc.get("ok"):
            print(f"error={hc.get('error') or 'unknown'}")
            print("raw_items_count=0")
            print("normalized_items_count=0")
            return 0

        search_result = await manager.search(
            city=os.getenv("AVITO_DEBUG_CITY", "moskva"),
            price_min=0,
            price_max=1_000_000,
            limit=10,
        )
        meta = search_result.get("meta", {})
        print(f"search_final_url={meta.get('final_url') or ''}")
        print(f"search_error={search_result.get('error') or ''}")
        print(f"captcha_detected={meta.get('captcha_detected')}")
        print(f"blocked_detected={meta.get('blocked_detected')}")
        print(f"raw_items_count={meta.get('raw_items_count', 0)}")
        print(f"normalized_items_count={meta.get('normalized_items_count', 0)}")
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
