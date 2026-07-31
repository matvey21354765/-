"""Диагностика резидентского пула прокси для Avito.

Запускать на Railway/Linux:
    python -m scripts.debug_avito_proxy_pool

Скрипт проверяет максимум AVITO_PROXY_CHECK_LIMIT портов
и завершается не позднее 90 секунд.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avito_proxy_pool import get_avito_proxy_pool


async def main() -> int:
    started = time.time()
    pool = await get_avito_proxy_pool()
    summary = pool.summary()

    print(f"proxy_pool={summary.get('proxy_pool', False)}")
    print(f"proxy_host_safe={summary.get('proxy_host_safe', '')}")
    print(f"ports_range={summary.get('ports_range') or ''}")
    print(f"credentials_present={summary.get('credentials_present', False)}")

    if not pool.configured:
        print("error=not_configured")
        print("elapsed_ms=0")
        return 0

    port = await pool.select_working_proxy()
    elapsed = int((time.time() - started) * 1000)

    print(f"ports_checked={summary.get('last_attempted_ports_count', 0)}")
    print(f"selected_port={port or 'null'}")
    print(f"error={'proxy_unavailable' if port is None else 'none'}")
    print(f"elapsed_ms={elapsed}")
    return 0


if __name__ == "__main__":
    rc = asyncio.run(asyncio.wait_for(main(), timeout=90))
    sys.exit(rc)
