#!/usr/bin/env python3
"""Сквозная проверка пути пользователя за день (без секретов в выводе).

объявление найдено → пользователь открыл → включил наблюдение → цена снизилась
→ создано событие → уведомление разрешено → повтор заблокирован
→ событие попало в дневную статистику.

Запуск:  python scripts/debug_user_daily_flow.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_tmp = tempfile.TemporaryDirectory()
os.environ["PEREKUP_DB_PATH"] = str(Path(_tmp.name) / "flow.db")

import perekup_tracking as track  # noqa: E402

USER = 424242
ITEM = {
    "title": "Toyota Camry 2016",
    "url": "https://www.avito.ru/perm/avtomobili/toyota_camry_2016_123456",
    "source": "avito",
    "_price_int": 1_450_000,
    "_days_on_site": 0,
}

out = {
    "listing_created": 0, "view_recorded": 0, "watch_created": 0,
    "price_drop_detected": 0, "notification_created": 0, "duplicate_blocked": 0,
    "daily_stats_updated": 0, "morning_digest_items": 0,
    "evening_digest_items": 0, "error": "",
}

try:
    track.init_db()
    key = track.listing_key(ITEM)

    # 1. Объявление найдено
    ev = track.record_listing(ITEM)
    out["listing_created"] = int(ev["event"] == "first_seen")

    # 2. Пользователь открыл карточку
    track.record_view(USER, ITEM)
    out["view_recorded"] = int(track.has_viewed(USER, key) is not None)

    # 3. Включил наблюдение
    out["watch_created"] = int(track.add_watch(USER, ITEM, days=14))
    # повторное нажатие не создаёт дубль
    track.add_watch(USER, ITEM, days=14)
    assert len(track.user_watches(USER)) == 1, "дубль наблюдения"

    # 4. Цена снизилась
    dropped = dict(ITEM, _price_int=1_380_000)
    ev2 = track.record_listing(dropped)
    out["price_drop_detected"] = int(
        ev2["event"] == "price_drop"
        and track.is_significant_drop(ev2["old_price"], ev2["new_price"])
    )

    # 5. Уведомление создаётся один раз
    sig = track.drop_signature(key, ev2["old_price"], ev2["new_price"])
    out["notification_created"] = int(track.should_notify(USER, "price_drop", sig, key))
    out["duplicate_blocked"] = int(not track.should_notify(USER, "price_drop", sig, key))

    # 6. Дневная статистика
    track.bump_stat(USER, "price_drops")
    track.bump_stat(USER, "matched", 3)
    stats = track.get_stats(USER)
    out["daily_stats_updated"] = int(stats["opened"] >= 1 and stats["price_drops"] >= 1)

    # 7. Материал для утреннего/вечернего отчёта
    out["morning_digest_items"] = len(track.user_watches(USER))
    out["evening_digest_items"] = stats["matched"] + stats["price_drops"]

except Exception as exc:  # без утечки секретов
    out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

for k, v in out.items():
    print(f"{k}={v}")

_tmp.cleanup()
sys.exit(1 if out["error"] else 0)
