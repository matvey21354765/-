"""Постоянный поиск PerekupDrive: один активный поиск на пользователя,
общий пул объявлений, разделы по возрасту, рыночная цена Авито,
сохранённые машины и персональные отчёты.

Модуль надстраивается над perekup_tracking (история цен, наблюдения,
антидубли уведомлений, дневная статистика) и использует ту же SQLite-базу.
Все миграции идемпотентны: существующие таблицы и данные не трогаются.

Здесь нет сети: источники наполняют пул через ingest_listing(), а пользователь
получает выдачу локальными фильтрами по пулу.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

import perekup_tracking as _pt

MSK = timezone(timedelta(hours=3))

# Границы разделов (в часах)
FRESH_HOURS = 2
TODAY_HOURS = 24
DAYS3_HOURS = 72

CATEGORIES = ("fresh", "today", "days3", "bargain", "price_drop", "saved")

CATEGORY_TITLES = {
    "fresh": "🚨 Кто быстрее",
    "today": "🔥 Новые сегодня",
    "days3": "📅 До 3 дней",
    "bargain": "🤝 Простор для торга",
    "price_drop": "📉 Снизили цену",
    "saved": "⭐ Сохранённые",
}

PAGE_SIZE = 10

CONDITION_LABELS = {
    "running": "на ходу",
    "any": "можно с вложениями",
}

# Соседние регионы для оценки рынка (slug'и как в control_bot.REGIONS).
NEIGHBOUR_REGIONS: dict[str, tuple[str, ...]] = {
    "perm": ("udmurtia", "sverdlovsk", "bashkortostan", "kirov"),
    "sverdlovsk": ("perm", "chelyabinsk", "tyumen", "kurgan"),
    "chelyabinsk": ("sverdlovsk", "bashkortostan", "kurgan"),
    "bashkortostan": ("perm", "chelyabinsk", "tatarstan", "udmurtia"),
    "tatarstan": ("bashkortostan", "udmurtia", "samara", "chuvashia"),
    "udmurtia": ("perm", "tatarstan", "kirov", "bashkortostan"),
    "moskva": ("moskovskaya_oblast", "tver", "kaluga", "vladimir"),
    "moskovskaya_oblast": ("moskva", "tver", "kaluga", "vladimir", "ryazan"),
    "sankt-peterburg": ("leningradskaya_oblast", "novgorod", "pskov"),
    "leningradskaya_oblast": ("sankt-peterburg", "novgorod", "pskov"),
    "novosibirsk": ("omsk", "kemerovo", "altai", "tomsk"),
    "krasnodar": ("rostov", "stavropol", "adygea"),
    "rostov": ("krasnodar", "volgograd", "stavropol"),
    "samara": ("tatarstan", "saratov", "ulyanovsk", "orenburg"),
}

# Мусорные объявления, которые нельзя брать в расчёт рынка.
_JUNK_RE = re.compile(
    r"(запчаст|разбор|на\s*зап|под\s*восстанов|распил|конструктор|"
    r"без\s*документ|без\s*птс|дубликат\s*птс|утилизац|битый\s*в\s*хлам|"
    r"аренда|выкуп|в\s*аренду|сдам|обмен\s*на\s*квартир)",
    re.IGNORECASE,
)
_RUNNING_RE = re.compile(r"(на\s*ходу|отличное\s*состоян|не\s*требует\s*вложен)", re.IGNORECASE)
_NOT_RUNNING_RE = re.compile(
    r"(не\s*на\s*ходу|не\s*заводится|не\s*ездит|треб\w*\s*ремонт|"
    r"после\s*дтп|битая|битый|нужны\s*вложен|треб\w*\s*вложен|на\s*восстановлен)",
    re.IGNORECASE,
)
_BARGAIN_RE = re.compile(r"(торг|уступ|срочно|обмен)", re.IGNORECASE)

# «Без ограничения» по цене (та же константа, что и в control_bot.NO_PRICE_LIMIT)
NO_PRICE_LIMIT = 99_000_000

MIN_MARKET_SAMPLE = 5          # меньше — оценка «предварительная»
MARKET_YEAR_TOLERANCE = 1
MARKET_OUTLIER_LOW = 0.45      # отсечь аномально дешёвые (битые/запчасти)
MARKET_OUTLIER_HIGH = 2.2


# ──────────────────────────────────────────────────────────────────────
# Схема
# ──────────────────────────────────────────────────────────────────────
def init_db() -> None:
    """Идемпотентно создаёт таблицы постоянного поиска."""
    _pt.init_db()
    with _pt._db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                region TEXT,
                regions TEXT,
                price_min INTEGER DEFAULT 0,
                price_max INTEGER DEFAULT 0,
                brands TEXT,
                model TEXT,
                year_min INTEGER,
                year_max INTEGER,
                condition TEXT DEFAULT 'any',
                sources TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT DEFAULT 'active'
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_us_user ON user_searches(user_id, status)")

        # Общий пул объявлений: наполняется источниками централизованно.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS listing_pool (
                listing_key TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                url TEXT,
                price INTEGER,
                region TEXT,
                brand TEXT,
                model TEXT,
                year INTEGER,
                mileage INTEGER,
                condition TEXT,
                description TEXT,
                photo TEXT,
                published_at REAL,
                first_seen_at REAL,
                last_seen_at REAL,
                market_price INTEGER,
                market_sample INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        """)
        # Чем считался рынок: 'avito' или 'all' (когда объявлений Авито нет).
        _pool_cols = {r[1] for r in cur.execute("PRAGMA table_info(listing_pool)")}
        if "market_basis" not in _pool_cols:
            cur.execute("ALTER TABLE listing_pool ADD COLUMN market_basis TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pool_seen ON listing_pool(last_seen_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pool_model ON listing_pool(brand, model, year)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pool_region ON listing_pool(region, price)")

        # Сохранённые машины пользователя (сохранение = наблюдение).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS saved_cars (
                user_id INTEGER NOT NULL,
                listing_key TEXT NOT NULL,
                saved_at REAL NOT NULL,
                saved_price INTEGER,
                status TEXT DEFAULT 'active',
                PRIMARY KEY (user_id, listing_key)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_cars(user_id, status)")

        # Скрытые пользователем объявления.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hidden_listings (
                user_id INTEGER NOT NULL,
                listing_key TEXT NOT NULL,
                hidden_at REAL NOT NULL,
                PRIMARY KEY (user_id, listing_key)
            )
        """)

        # Короткие id объявлений для callback_data.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS listing_ids (
                sid TEXT PRIMARY KEY,
                listing_key TEXT
            )
        """)

        # Персональные настройки отчётов и тихих часов.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                quiet_from INTEGER DEFAULT 23,
                quiet_to INTEGER DEFAULT 8,
                morning_report INTEGER DEFAULT 1,
                evening_report INTEGER DEFAULT 1,
                instant_notify INTEGER DEFAULT 1
            )
        """)


def _conn():
    return _pt._db()


# ──────────────────────────────────────────────────────────────────────
# Короткий стабильный id объявления для callback_data (лимит Telegram — 64 байта)
# ──────────────────────────────────────────────────────────────────────
def short_id(key: str) -> str:
    """Детерминированный короткий id. Переживает рестарт: старые кнопки живут."""
    import hashlib
    sid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    with _conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listing_ids (sid TEXT PRIMARY KEY, listing_key TEXT)")
        conn.execute("INSERT OR IGNORE INTO listing_ids (sid, listing_key) VALUES (?,?)",
                     (sid, key))
    return sid


def key_by_short_id(sid: str) -> str:
    with _conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listing_ids (sid TEXT PRIMARY KEY, listing_key TEXT)")
        row = conn.execute("SELECT listing_key FROM listing_ids WHERE sid=?", (sid,)).fetchone()
    return row["listing_key"] if row else ""


# ──────────────────────────────────────────────────────────────────────
# Разбор объявления
# ──────────────────────────────────────────────────────────────────────
_BRAND_ALIASES = {
    "ваз": "vaz", "lada": "vaz", "лада": "vaz", "vaz": "vaz",
    "уаз": "uaz", "газ": "gaz",
    "тойота": "toyota", "toyota": "toyota",
    "хендай": "hyundai", "хёндай": "hyundai", "hyundai": "hyundai",
    "киа": "kia", "kia": "kia",
    "ниссан": "nissan", "nissan": "nissan",
    "форд": "ford", "ford": "ford",
    "рено": "renault", "renault": "renault",
    "шевроле": "chevrolet", "chevrolet": "chevrolet",
    "мазда": "mazda", "mazda": "mazda",
    "хонда": "honda", "honda": "honda",
    "фольксваген": "volkswagen", "volkswagen": "volkswagen", "vw": "volkswagen",
    "бмв": "bmw", "bmw": "bmw",
    "мерседес": "mercedes", "mercedes": "mercedes", "mercedes-benz": "mercedes",
    "ауди": "audi", "audi": "audi",
    "шкода": "skoda", "skoda": "skoda",
    "митсубиси": "mitsubishi", "мицубиси": "mitsubishi", "mitsubishi": "mitsubishi",
    "опель": "opel", "opel": "opel",
    "субару": "subaru", "subaru": "subaru",
    "лексус": "lexus", "lexus": "lexus",
    "дэу": "daewoo", "daewoo": "daewoo",
    "пежо": "peugeot", "peugeot": "peugeot",
    "ситроен": "citroen", "citroen": "citroen",
    "сузуки": "suzuki", "suzuki": "suzuki",
    "инфинити": "infiniti", "infiniti": "infiniti",
    "датсун": "datsun", "datsun": "datsun",
    "равон": "ravon", "ravon": "ravon",
    "чери": "chery", "chery": "chery",
    "хавал": "haval", "haval": "haval",
    "джили": "geely", "geely": "geely",
}

_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")


def parse_brand(title: str) -> str:
    """Нормализованная марка из заголовка ('' если не распознана)."""
    t = (title or "").lower().replace("-", " ")
    for token in re.split(r"[\s,./]+", t):
        tok = token.strip()
        if tok in _BRAND_ALIASES:
            return _BRAND_ALIASES[tok]
    for alias, norm in _BRAND_ALIASES.items():
        if alias in t:
            return norm
    return ""


def parse_model(title: str) -> str:
    """Грубая модель: марка + первое следующее слово/число (для группировки)."""
    t = re.sub(r"[,].*$", "", (title or "")).lower().strip()
    t = re.sub(r"\b(19[89]\d|20[0-2]\d)\b", " ", t)
    t = re.sub(r"[^a-zа-я0-9 \-]", " ", t)
    parts = [p for p in re.split(r"\s+", t) if p]
    brand = parse_brand(title)
    if not parts:
        return ""
    # ВАЗ-2114 → vaz 2114
    if brand:
        aliases = sorted((a for a, n in _BRAND_ALIASES.items() if n == brand),
                         key=len, reverse=True)
        for i, p in enumerate(parts):
            flat = p.replace("-", "")
            hit = next((a for a in aliases if flat.startswith(a)), "")
            if not hit:
                continue
            # «ваз-2114» → индекс модели прямо в слове
            m = re.search(r"(\d{3,4})$", flat)
            if m:
                return f"{brand} {m.group(1)}"
            rest = parts[i + 1:i + 2]
            return f"{brand} {rest[0]}" if rest else brand
        return brand
    return " ".join(parts[:2])


def parse_year(item: dict) -> int:
    y = item.get("_year")
    try:
        y = int(y or 0)
    except (TypeError, ValueError):
        y = 0
    if 1980 <= y <= 2100:
        return y
    m = _YEAR_RE.search(str(item.get("title") or ""))
    return int(m.group(1)) if m else 0


def detect_condition(item: dict) -> str:
    """'running' — на ходу, 'repair' — нужны вложения, '' — неизвестно."""
    text = f"{item.get('title','')} {item.get('description','')}"
    if _NOT_RUNNING_RE.search(text):
        return "repair"
    if _RUNNING_RE.search(text):
        return "running"
    return ""


def condition_text(cond: str) -> str:
    return {"running": "на ходу", "repair": "на ходу, нужны вложения"}.get(cond, "не указано")


def is_junk(item: dict) -> bool:
    return bool(_JUNK_RE.search(f"{item.get('title','')} {item.get('description','')}"))


_MONTHS_RU = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_REL_RE = re.compile(
    r"(\d+)\s*(секунд|минут|час|сут|дн|день|недел|месяц)[а-яё]*\s*назад", re.I)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
# «27 июня» и «27 июня 2025»: на странице объявления год пишут у прошлогодних,
# и без него дата уезжала на год вперёд.
_DAY_MONTH_RE = re.compile(r"(\d{1,2})\s+([а-яё]{3,})(?:\s+(20\d{2}))?", re.I)


def parse_published_ts(value: Any, now: float | None = None) -> float | None:
    """Момент публикации из значения любого вида (ts, ISO-строка, «вчера в 20:29»).

    Площадки отдают время публикации кто как: Авито — текстом («Сегодня 12:30»,
    «5 августа 12:30», «2 часа назад»), REST-провайдеры — ISO-строкой или
    миллисекундами. Раньше принимались только числа, поэтому у большинства
    объявлений published_at оставался пустым и в карточке показывалось время,
    когда объявление увидел бот.
    """
    now = time.time() if now is None else float(now)
    if value is None or isinstance(value, bool):
        return None

    def _ok(ts: float) -> float | None:
        # Отсекаем мусор: будущее дальше суток и всё старше 10 лет.
        if ts > now + 86400 or ts < now - 10 * 365 * 86400:
            return None
        return min(ts, now)

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 100_000_000_000:      # миллисекунды
            ts /= 1000.0
        return _ok(ts) if ts > 1_000_000_000 else None

    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    # Чистое число строкой
    try:
        return parse_published_ts(float(s), now)
    except ValueError:
        pass

    # ISO / «YYYY-MM-DD HH:MM:SS»
    iso = s.replace("Z", "+00:00").replace("/", "-")
    if len(iso) >= 8 and iso[:4].isdigit():
        try:
            dt = datetime.fromisoformat(iso.replace(" ", "T", 1) if " " in iso[:19] else iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MSK)
            return _ok(dt.timestamp())
        except ValueError:
            pass

    low = s.lower()

    m = _REL_RE.search(low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"секунд": 1, "минут": 60, "час": 3600, "сут": 86400, "дн": 86400,
                "день": 86400, "недел": 604800, "месяц": 2592000}[unit]
        return _ok(now - n * mult)

    tm = _TIME_RE.search(low)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    if hh > 23 or mm > 59:
        hh, mm = 0, 0
    today = datetime.fromtimestamp(now, MSK).date()

    def _at(d) -> float | None:
        return _ok(datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK).timestamp())

    if "сегодня" in low:
        return _at(today)
    if "вчера" in low:
        return _at(today - timedelta(days=1))

    dm = _DAY_MONTH_RE.search(low)
    if dm:
        day = int(dm.group(1))
        mon = _MONTHS_RU.get(dm.group(2)[:3])
        if mon and 1 <= day <= 31:
            explicit_year = int(dm.group(3)) if dm.group(3) else 0
            year = explicit_year or today.year
            try:
                d = datetime(year, mon, day, hh, mm, tzinfo=MSK)
            except ValueError:
                return None
            # Декабрьские даты в январе относятся к прошлому году. Явный год
            # с площадки правкам не подлежит.
            if not explicit_year and d.timestamp() > now + 86400:
                d = d.replace(year=year - 1)
            return _ok(d.timestamp())
    return None


def published_at(item: dict) -> float | None:
    """Момент публикации (ts) или None, если площадка его не отдала."""
    for k in ("_published_ts", "published_at", "_published_at", "sortTimeStamp",
              "sort_time", "publishedAt", "time", "_time", "date_published",
              "datePublished", "creation_date", "creationDate", "created_at",
              "createdAt", "publish_date", "publishDate", "_date_published"):
        ts = parse_published_ts(item.get(k))
        if ts:
            return ts
    days = item.get("_days_on_site")
    if days is not None:
        try:
            days = float(days)
        except (TypeError, ValueError):
            return None
        if days > 0:
            return time.time() - days * 86400.0
    return None


# ──────────────────────────────────────────────────────────────────────
# Пул объявлений
# ──────────────────────────────────────────────────────────────────────
def photo_of(item: dict) -> str:
    """Фото объявления. Скраперы пишут его в разные ключи — проверяем все."""
    for k in ("_photo_url", "photo_url", "photo", "image", "image_url", "img"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for k in ("photos", "images", "_photos_urls"):
        v = item.get(k)
        if isinstance(v, (list, tuple)):
            for el in v:
                if isinstance(el, str) and el.startswith("http"):
                    return el
                if isinstance(el, dict):
                    for vv in el.values():
                        if isinstance(vv, str) and vv.startswith("http"):
                            return vv
    return ""


def ingest_listing(item: dict, now: float | None = None) -> dict:
    """Кладёт объявление в общий пул и фиксирует событие цены.

    Возвращает событие perekup_tracking.record_listing (для уведомлений).
    """
    key = _pt.listing_key(item)
    price = int(item.get("_price_int") or 0)
    if not key or price <= 0:
        return {"event": "none"}
    now = time.time() if now is None else float(now)
    event = _pt.record_listing(item, now=now)
    pub = published_at(item)
    with _conn() as conn:
        row = conn.execute(
            "SELECT first_seen_at, published_at FROM listing_pool WHERE listing_key=?", (key,)
        ).fetchone()
        title = str(item.get("title") or "")[:300]
        vals = (
            str(item.get("source") or ""), title, str(item.get("url") or ""), price,
            normalize_region(_region_of_item(item)),
            parse_brand(title), parse_model(title),
            parse_year(item), int(item.get("mileage") or 0), detect_condition(item),
            str(item.get("description") or "")[:1000],
            photo_of(item),
        )
        if row is None:
            conn.execute(
                """INSERT INTO listing_pool (listing_key, source, title, url, price, region,
                       brand, model, year, mileage, condition, description, photo,
                       published_at, first_seen_at, last_seen_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
                (key,) + vals + (pub, now, now),
            )
        else:
            conn.execute(
                # Пустое фото/описание при повторном обходе НЕ должно затирать
                # уже сохранённое: иначе у части карточек пропадает картинка.
                """UPDATE listing_pool SET source=?, title=?, url=?, price=?, region=?,
                       brand=?, model=?, year=?, mileage=?, condition=?,
                       description=COALESCE(NULLIF(?,''), description),
                       photo=COALESCE(NULLIF(?,''), photo),
                       published_at=COALESCE(?, published_at),
                       last_seen_at=?, status='active'
                 WHERE listing_key=?""",
                vals + (pub, now, key),
            )
    return event


def set_pool_photo(key: str, photo: str) -> None:
    """Запоминает фото объявления (добор по ссылке — один раз на объявление)."""
    if not key or not photo:
        return
    with _conn() as conn:
        conn.execute("UPDATE listing_pool SET photo=? WHERE listing_key=?", (photo, key))


def set_pool_details(key: str, *, photo: str = "", description: str = "",
                     published_at: float | None = None) -> None:
    """Сохраняет данные, добранные со страницы объявления.

    Пустые значения не затирают уже сохранённые: добор идёт по одному
    объявлению за раз и не должен обнулять то, что площадка отдала раньше.
    """
    if not key:
        return
    sets, args = [], []
    if photo:
        sets.append("photo=COALESCE(NULLIF(photo,''), ?)")
        args.append(photo)
    if description:
        sets.append("description=COALESCE(NULLIF(description,''), ?)")
        args.append(str(description)[:1000])
    if published_at:
        sets.append("published_at=COALESCE(published_at, ?)")
        args.append(float(published_at))
    if not sets:
        return
    with _conn() as conn:
        conn.execute(f"UPDATE listing_pool SET {', '.join(sets)} WHERE listing_key=?",
                     (*args, key))


def get_pool_listing(key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM listing_pool WHERE listing_key=?", (key,)).fetchone()
    return dict(row) if row else None


def mark_pool_removed(keys: Iterable[str], now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    keys = [k for k in keys if k]
    if not keys:
        return 0
    with _conn() as conn:
        conn.executemany(
            "UPDATE listing_pool SET status='removed' WHERE listing_key=?", [(k,) for k in keys]
        )
    _pt.mark_removed(keys, now=now)
    return len(keys)


# ──────────────────────────────────────────────────────────────────────
# Рыночная цена Авито
# ──────────────────────────────────────────────────────────────────────
def _region_scope(region: str) -> list[str]:
    region = (region or "").strip().lower()
    if not region:
        return []
    return [region] + list(NEIGHBOUR_REGIONS.get(region, ()))


def avito_market_price(brand: str, model: str, year: int, region: str = "",
                       condition: str = "", sources: Iterable[str] = ("avito",)) -> dict:
    """Медиана очищенных похожих объявлений.

    sources — площадки-эталоны. По умолчанию Авито; market_price() переходит на
    все площадки, когда объявлений Авито в пуле нет.

    Возвращает {"price": int, "sample": int, "preliminary": bool}.
    """
    brand = (brand or "").strip().lower()
    model = (model or "").strip().lower()
    if not brand:
        return {"price": 0, "sample": 0, "preliminary": True}
    y_lo = year - MARKET_YEAR_TOLERANCE if year else 0
    y_hi = year + MARKET_YEAR_TOLERANCE if year else 9999
    scope = _region_scope(region)
    sql = ["SELECT listing_key, title, description, price, region, condition",
           "FROM listing_pool WHERE price > 0 AND brand=?"]
    args: list[Any] = [brand]
    src_list = [s for s in (sources or ()) if s]
    if src_list:
        sql.append("AND source IN (%s)" % ",".join("?" * len(src_list)))
        args += src_list
    if model:
        sql.append("AND model=?")
        args.append(model)
    if year:
        sql.append("AND year BETWEEN ? AND ?")
        args += [y_lo, y_hi]
    if scope:
        sql.append("AND region IN (%s)" % ",".join("?" * len(scope)))
        args += scope
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(" ".join(sql), args).fetchall()]

    prices: list[int] = []
    seen_titles: set[tuple] = set()
    for r in rows:
        if _JUNK_RE.search(f"{r.get('title','')} {r.get('description','')}"):
            continue
        if condition and r.get("condition") and r["condition"] != condition:
            # Сопоставимое состояние: «на ходу» не сравниваем с «под восстановление».
            continue
        dedup = (str(r.get("title", "")).strip().lower(), int(r["price"]))
        if dedup in seen_titles:
            continue
        seen_titles.add(dedup)
        prices.append(int(r["price"]))
    if not prices:
        return {"price": 0, "sample": 0, "preliminary": True}

    rough = median(prices)
    cleaned = [p for p in prices
               if rough * MARKET_OUTLIER_LOW <= p <= rough * MARKET_OUTLIER_HIGH]
    if not cleaned:
        cleaned = prices
    return {
        "price": int(median(cleaned)),
        "sample": len(cleaned),
        "preliminary": len(cleaned) < MIN_MARKET_SAMPLE,
    }


def market_price(brand: str, model: str, year: int, region: str = "",
                 condition: str = "") -> dict:
    """Рыночная цена: сперва по Авито, при пустой выборке — по всем площадкам.

    Раньше рынок считался только по Авито. Когда Авито ничего не отдаёт (а это
    штатная ситуация — блокировки, лимиты), рынок оставался неизвестным у всей
    выдачи, и «ниже рынка» было не с чем сравнивать. Дром/Auto.ru/Юла в пуле
    уже есть — по ним и считаем, честно помечая основу оценки.
    """
    res = avito_market_price(brand, model, year, region, condition, ("avito",))
    if int(res.get("sample") or 0) > 0:
        return {**res, "basis": "avito"}
    res = avito_market_price(brand, model, year, region, condition, ())
    return {**res, "basis": "all" if int(res.get("sample") or 0) else ""}


def refresh_market_price(key: str) -> dict:
    """Пересчитывает и сохраняет рыночную цену для объявления из пула."""
    lst = get_pool_listing(key)
    if not lst:
        return {"price": 0, "sample": 0, "preliminary": True, "basis": ""}
    res = market_price(lst.get("brand", ""), lst.get("model", ""),
                       int(lst.get("year") or 0), lst.get("region", ""),
                       lst.get("condition", ""))
    with _conn() as conn:
        conn.execute(
            "UPDATE listing_pool SET market_price=?, market_sample=?, market_basis=? "
            "WHERE listing_key=?",
            (int(res["price"]), int(res["sample"]), res.get("basis") or "", key),
        )
    return res


# ──────────────────────────────────────────────────────────────────────
# Активный поиск
# ──────────────────────────────────────────────────────────────────────
def save_search(user_id: int, *, region: str = "", regions: Iterable[str] = (),
                price_min: int = 0, price_max: int = 0, brands: Iterable[str] = (),
                model: str = "", year_min: int = 0, year_max: int = 0,
                condition: str = "any", sources: Iterable[str] = (),
                now: float | None = None) -> int:
    """Сохраняет ЕДИНСТВЕННЫЙ активный поиск пользователя (старый архивируется)."""
    now = time.time() if now is None else float(now)
    with _conn() as conn:
        conn.execute(
            "UPDATE user_searches SET status='archived' WHERE user_id=? AND status='active'",
            (int(user_id),),
        )
        cur = conn.execute(
            """INSERT INTO user_searches (user_id, region, regions, price_min, price_max,
                   brands, model, year_min, year_max, condition, sources,
                   created_at, updated_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')""",
            (int(user_id), region, json.dumps(list(regions), ensure_ascii=False),
             int(price_min or 0), int(price_max or 0),
             json.dumps([b.lower() for b in brands], ensure_ascii=False),
             (model or "").lower(), int(year_min or 0), int(year_max or 0),
             condition or "any", json.dumps(list(sources), ensure_ascii=False),
             now, now),
        )
        return int(cur.lastrowid)


def get_active_search(user_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM user_searches WHERE user_id=? AND status='active'
                ORDER BY updated_at DESC LIMIT 1""", (int(user_id),)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["regions"] = json.loads(d.get("regions") or "[]")
    d["brands"] = json.loads(d.get("brands") or "[]")
    d["sources"] = json.loads(d.get("sources") or "[]")
    return d


def all_active_searches(limit: int = 5000) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_searches WHERE status='active' ORDER BY updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["regions"] = json.loads(d.get("regions") or "[]")
        d["brands"] = json.loads(d.get("brands") or "[]")
        d["sources"] = json.loads(d.get("sources") or "[]")
        out.append(d)
    return out


def deactivate_search(user_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE user_searches SET status='archived' WHERE user_id=? AND status='active'",
            (int(user_id),),
        )


def matches_search(listing: dict, search: dict) -> bool:
    """Локальный фильтр: подходит ли объявление из пула под поиск."""
    if not search:
        return False
    price = int(listing.get("price") or 0)
    if price <= 0:
        return False
    if search.get("price_min") and price < int(search["price_min"]):
        return False
    _pmax = int(search.get("price_max") or 0)
    if 0 < _pmax < NO_PRICE_LIMIT and price > _pmax:
        return False

    regions = [normalize_region(r)
               for r in ([search.get("region")] + list(search.get("regions") or [])) if r]
    lst_region = normalize_region(listing.get("region") or "")
    if regions:
        if not lst_region:
            return False          # регион неизвестен — не подсовываем чужой город
        if lst_region not in regions and not _same_oblast(lst_region, regions):
            return False

    brands = [b for b in (search.get("brands") or []) if b and b != "all"]
    if brands and (listing.get("brand") or "") not in brands:
        return False

    model = (search.get("model") or "").strip().lower()
    if model and model not in (listing.get("model") or "") and \
            model not in (listing.get("title") or "").lower():
        return False

    year = int(listing.get("year") or 0)
    if search.get("year_min") and year and year < int(search["year_min"]):
        return False
    if search.get("year_max") and year and year > int(search["year_max"]):
        return False

    if (search.get("condition") or "any") == "running":
        if (listing.get("condition") or "") == "repair":
            return False

    sources = [s for s in (search.get("sources") or []) if s]
    if sources and (listing.get("source") or "") not in sources:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Разделы по возрасту
# ──────────────────────────────────────────────────────────────────────
def listing_age_hours(listing: dict, now: float | None = None) -> tuple[float, bool]:
    """(возраст в часах, точен_ли). Неточный = считали по first_seen_at."""
    now = time.time() if now is None else float(now)
    pub = listing.get("published_at")
    if pub:
        return max(0.0, (now - float(pub)) / 3600.0), True
    fs = listing.get("first_seen_at") or now
    return max(0.0, (now - float(fs)) / 3600.0), False


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 минуту, 2 минуты, 5 минут."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def age_label(listing: dict, now: float | None = None) -> str:
    hours, exact = listing_age_hours(listing, now)
    if hours < 1:
        m = max(1, int(hours * 60))
        base = f"{m} {_plural(m, 'минуту', 'минуты', 'минут')} назад"
    elif hours < 24:
        h = int(hours)
        base = f"{h} {_plural(h, 'час', 'часа', 'часов')} назад"
    else:
        d = int(hours // 24)
        base = f"{d} {_plural(d, 'день', 'дня', 'дней')} назад"
    if exact:
        return base
    # Точное время публикации площадка не отдала — говорим об этом честно,
    # но коротко, чтобы не путать с реальным возрастом объявления.
    return f"{base} (бот впервые увидел)"


def category_of(listing: dict, now: float | None = None) -> str:
    """Раздел объявления: fresh / today / days3 / bargain / '' (не показываем).

    «Кто быстрее» — только объявления с ИЗВЕСТНЫМ временем публикации: если
    площадка его не отдала, возраст считается от момента, когда бот впервые
    увидел объявление, и все свежесобранные машины валились в «Кто быстрее»
    («2 минут назад (бот впервые увидел)»), а «Новые сегодня» оставался пустым.
    Такие объявления идут в «Новые сегодня» / «До 3 дней».
    """
    hours, exact = listing_age_hours(listing, now)
    if hours < FRESH_HOURS:
        return "fresh" if exact else "today"
    if hours < TODAY_HOURS:
        return "today"
    if hours < DAYS3_HOURS:
        return "days3"
    return "bargain" if bargain_reasons(listing) else ""


def bargain_reasons(listing: dict) -> list[str]:
    """Причины попадания в «Простор для торга» (пусто → раздел не показывает)."""
    reasons: list[str] = []
    st = _pt.get_listing_state(listing.get("listing_key") or "") or {}
    drops = int(st.get("drops_count") or 0)
    if drops >= 2:
        reasons.append(f"цена снижалась {drops} раза")
    elif drops == 1:
        reasons.append("цена уже снижалась")
    if str(st.get("status") or "") == "active" and int(st.get("first_price") or 0) > int(
            st.get("current_price") or 0):
        if not drops:
            reasons.append("цена ниже стартовой")
    text = f"{listing.get('title','')} {listing.get('description','')}"
    if _BARGAIN_RE.search(text):
        reasons.append("продавец указал торг")
    hist = _pt.price_history(listing.get("listing_key") or "", limit=50)
    if any(h.get("event_type") == "relisted" for h in hist):
        reasons.append("объявление размещено повторно")
    # Срок продажи считаем от публикации, если площадка её отдала: объявление
    # может висеть годами, а бот увидел его вчера. Долгая продажа — сама по
    # себе повод торговаться, и такие машины не должны пропадать из выдачи.
    since = listing.get("published_at") or listing.get("first_seen_at")
    if since:
        try:
            days = (time.time() - float(since)) / 86400.0
        except (TypeError, ValueError):
            days = 0.0
        if days >= 14:
            reasons.append(f"в продаже уже {int(days)} дней")
    return reasons


def _hidden_keys(user_id: int) -> set[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT listing_key FROM hidden_listings WHERE user_id=?", (int(user_id),)
        ).fetchall()
    return {r["listing_key"] for r in rows}


def hide_listing(user_id: int, key: str, now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO hidden_listings (user_id, listing_key, hidden_at) VALUES (?,?,?)",
            (int(user_id), key, now),
        )


# ──────────────────────────────────────────────────────────────────────
# Отбор «только лучшие»
# ──────────────────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: Показывать только объявления с фото, описанием и ценой ниже рынка.
ONLY_BEST = _env_flag("PS_ONLY_BEST", True)
#: Короче этого описание считается отсутствующим («ВАЗ 2106, 1996» — не описание).
MIN_DESCRIPTION_CHARS = 25
#: Сколько рыночных цен досчитывать за один проход (SQL по локальному пулу).
MARKET_CALC_BUDGET = 300


def has_photo(listing: dict) -> bool:
    return bool(photo_of(listing) or (listing.get("photo") or ""))


def has_description(listing: dict) -> bool:
    desc = " ".join(str(listing.get("description") or "").split())
    return len(desc) >= MIN_DESCRIPTION_CHARS


def not_above_market(listing: dict) -> bool:
    """Цена не выше известного рынка. Неизвестный рынок не считается минусом.

    Требовать «строго ниже рынка» на этапе отбора нельзя: рынок известен не по
    каждой модели, и жёсткое условие выкидывало из выдачи почти всё — из 134
    подходящих объявлений до пользователя доходило 17.
    """
    market = int(listing.get("market_price") or 0)
    price = int(listing.get("price") or 0)
    if not market or not price:
        return True
    return price < market


def listing_is_good(listing: dict) -> bool:
    """Полностью годная карточка: фото, описание и цена не выше рынка.

    Применяется на выдаче — после того, как бот сходил на страницу объявления
    и добрал фото, описание и дату публикации.
    """
    return has_photo(listing) and has_description(listing) and not_above_market(listing)


def listing_is_worth_showing(listing: dict) -> bool:
    """Стоит ли вообще брать объявление в кандидаты.

    Фото и описание к этому моменту могут быть ещё не добраны со страницы,
    поэтому здесь отсекается только заведомо пустое (ни фото, ни описания и
    даже ссылки нет, по которой их можно достать) и дороже рынка.
    """
    if not not_above_market(listing):
        return False
    if has_photo(listing) or has_description(listing):
        return True
    return bool(listing.get("url"))


def _ensure_market(listing: dict, budget: list[int]) -> dict:
    """Досчитывает рыночную цену объявления, если её ещё не считали."""
    if listing.get("market_price") is not None and int(listing.get("market_price") or 0):
        return listing
    if int(listing.get("market_sample") or 0) > 0 or budget[0] <= 0:
        return listing
    budget[0] -= 1
    try:
        res = refresh_market_price(listing.get("listing_key") or "")
    except Exception:
        return listing
    listing["market_price"] = int(res.get("price") or 0)
    listing["market_sample"] = int(res.get("sample") or 0)
    listing["market_basis"] = res.get("basis") or ""
    return listing


def _pool_candidates(user_id: int, category: str, now: float,
                     *, only_best: bool | None = None) -> list[dict]:
    """Объявления пула, подходящие под активный поиск и раздел."""
    search = get_active_search(user_id)
    if not search:
        return []
    strict = ONLY_BEST if only_best is None else bool(only_best)
    hidden = _hidden_keys(user_id)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM listing_pool WHERE status='active' AND price > 0
                ORDER BY COALESCE(published_at, first_seen_at) DESC LIMIT 4000"""
        ).fetchall()
    budget = [MARKET_CALC_BUDGET]
    out = []
    for r in rows:
        d = dict(r)
        if d["listing_key"] in hidden:
            continue
        if not matches_search(d, search):
            continue
        if category_of(d, now) != category:
            continue
        if strict:
            # Рынок считаем только для уже отобранных — это локальный SQL,
            # но на 4000 строк он всё равно был бы лишней работой.
            _ensure_market(d, budget)
            if not listing_is_worth_showing(d):
                continue
        out.append(d)
    return out


def search_listings(user_id: int, category: str, *, offset: int = 0,
                    limit: int = PAGE_SIZE, now: float | None = None,
                    only_best: bool | None = None) -> list[dict]:
    """Локальная выдача по активному поиску пользователя (без сети)."""
    now = time.time() if now is None else float(now)
    if category == "saved":
        return saved_cars(user_id)[offset:offset + limit]
    if category == "price_drop":
        return price_drop_feed(user_id, limit=limit, offset=offset, now=now)
    out = _pool_candidates(user_id, category, now, only_best=only_best)
    out.sort(key=listing_rank)
    return out[offset:offset + limit]


def listing_rank(listing: dict) -> tuple:
    """Порядок выдачи: полные карточки ниже рынка — первыми.

    Сортируем так, чтобы наверх поднимались объявления с фото, описанием и
    наибольшей разницей с рынком, а неполные не терялись совсем, а уходили
    вниз списка.
    """
    market = int(listing.get("market_price") or 0)
    price = int(listing.get("price") or 0)
    gain = (market - price) if (market and price and market > price) else 0
    return (
        0 if listing_is_good(listing) else 1,
        0 if has_photo(listing) else 1,
        -gain,
        -float(listing.get("published_at") or listing.get("first_seen_at") or 0),
    )


# ──────────────────────────────────────────────────────────────────────
# Рекомендации: что открыть прямо сейчас
# ──────────────────────────────────────────────────────────────────────
#: Насколько дешевле рынка, чтобы это стоило называть выгодой.
RECOMMEND_MIN_GAIN = 20_000
#: Свежесть, при которой объявление стоит открыть первым (часы).
RECOMMEND_FRESH_HOURS = 6


def recommendation_reason(listing: dict, now: float | None = None) -> str:
    """Одна строка: почему именно эту машину стоит открыть сейчас."""
    now = time.time() if now is None else float(now)
    market = int(listing.get("market_price") or 0)
    price = int(listing.get("price") or 0)
    gain = market - price if (market and price and market > price) else 0
    hours, exact = listing_age_hours(listing, now)
    drops = int(listing.get("drops_count") or 0)

    if drops >= 2:
        return f"Цена снижена {drops}-й раз — продавец торопится."
    if drops == 1:
        return "Цена снижена — продавец готов торговаться."
    if exact and hours < 1:
        base = f"Опубликована {max(1, int(hours * 60))} минут назад."
    elif exact and hours < RECOMMEND_FRESH_HOURS:
        base = f"Опубликована {int(hours)} ч назад."
    else:
        base = ""
    if gain >= RECOMMEND_MIN_GAIN:
        gain_text = f"Ниже рынка на ~{fmt_money(gain)}."
        return f"{base} {gain_text}".strip()
    if base:
        return base
    if gain > 0:
        return f"Ниже рынка на ~{fmt_money(gain)}."
    if _BARGAIN_RE.search(f"{listing.get('title','')} {listing.get('description','')}"):
        return "Продавец указал торг."
    return "Подходит под ваш поиск."


def _recommend_score(listing: dict, now: float) -> float:
    """Чем выше, тем раньше показываем. Свежесть + выгода + снижения цены."""
    market = int(listing.get("market_price") or 0)
    price = int(listing.get("price") or 0)
    gain = market - price if (market and price and market > price) else 0
    hours, exact = listing_age_hours(listing, now)
    score = 0.0
    if market and price:
        score += min(60.0, gain * 100.0 / market)       # доля выгоды, до 60
    if exact:
        if hours < 1:
            score += 40
        elif hours < RECOMMEND_FRESH_HOURS:
            score += 30
        elif hours < TODAY_HOURS:
            score += 15
    score += min(30, int(listing.get("drops_count") or 0) * 15)
    if has_photo(listing):
        score += 10
    if has_description(listing):
        score += 5
    return score


def recommendations(user_id: int, *, limit: int = 3,
                    now: float | None = None) -> list[dict]:
    """Что открыть прямо сейчас: лучшее из всех разделов одним списком.

    Пользователю не нужно самому решать, в какой раздел заглянуть: бот сам
    поднимает наверх свежие машины ниже рынка и те, где продавец снизил цену.
    К каждой возвращается готовая строка-объяснение в поле _reason.
    """
    now = time.time() if now is None else float(now)
    pool: dict[str, dict] = {}
    for cat in ("fresh", "today", "days3", "bargain"):
        for d in _pool_candidates(user_id, cat, now):
            pool.setdefault(d["listing_key"], d)
    # Снижения цены — отдельный сигнал: их в разделах по возрасту может не быть.
    for d in price_drop_feed(user_id, limit=50, now=now):
        row = pool.get(d["listing_key"]) or d
        row["drops_count"] = max(int(row.get("drops_count") or 0), 1)
        drop = int(d.get("drop") or 0)
        if drop:
            row.setdefault("drop", drop)
        pool[d["listing_key"]] = row
    if not pool:
        return []
    ranked = sorted(pool.values(), key=lambda d: -_recommend_score(d, now))
    out = []
    for d in ranked[:max(1, int(limit))]:
        d["_reason"] = recommendation_reason(d, now)
        out.append(d)
    return out


def format_recommendations(items: Iterable[dict], now: float | None = None) -> str:
    """Экран «что открыть прямо сейчас»: номер, машина, цена, одна причина."""
    now = time.time() if now is None else float(now)
    items = list(items)
    if not items:
        return ("🎯 Пока нечего рекомендовать\n\n"
                "Бот собирает объявления постоянно — загляните чуть позже.")
    lines = ["🎯 Что рекомендую открыть прямо сейчас", ""]
    for i, d in enumerate(items, 1):
        title = d.get("title") or "Автомобиль"
        year = int(d.get("year") or 0)
        if year and str(year) not in title:
            title = f"{title}, {year}"
        lines.append(f"{i}. {title}")
        lines.append(f"   {fmt_money(int(d.get('price') or 0))}")
        lines.append(f"   {d.get('_reason') or recommendation_reason(d, now)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def category_total(user_id: int, category: str, now: float | None = None) -> int:
    """Сколько всего машин в разделе — нужно для кнопок «Ещё N» / «Назад»."""
    now = time.time() if now is None else float(now)
    if category == "saved":
        return len(saved_cars(user_id))
    if category == "price_drop":
        return len(price_drop_feed(user_id, limit=4000, now=now))
    return int(category_counts(user_id, now).get(category, 0))


def category_counts(user_id: int, now: float | None = None) -> dict:
    """Счётчики для главного экрана поиска.

    Считаются по тем же правилам, что и выдача: иначе в разделе значилось бы
    87 машин, а открывалось три.
    """
    now = time.time() if now is None else float(now)
    counts = {c: 0 for c in CATEGORIES}
    for cat in ("fresh", "today", "days3", "bargain"):
        if cat in counts:
            counts[cat] = len(_pool_candidates(user_id, cat, now))
    counts["price_drop"] = len(price_drop_feed(user_id, limit=500, now=now))
    counts["saved"] = len(saved_cars(user_id))
    return counts


# ──────────────────────────────────────────────────────────────────────
# Снизили цену
# ──────────────────────────────────────────────────────────────────────
def price_drop_feed(user_id: int, *, limit: int = PAGE_SIZE, offset: int = 0,
                    hours: int = 72, now: float | None = None) -> list[dict]:
    """События снижения цены по объявлениям, подходящим под поиск пользователя,
    и по сохранённым машинам."""
    now = time.time() if now is None else float(now)
    since = now - hours * 3600
    search = get_active_search(user_id)
    saved_keys = {c["listing_key"] for c in saved_cars(user_id)}
    hidden = _hidden_keys(user_id)
    with _conn() as conn:
        rows = conn.execute(
            """SELECT h.listing_key, h.price, h.prev_price, h.detected_at
                 FROM listing_price_history h
                WHERE h.event_type='price_drop' AND h.detected_at >= ?
                ORDER BY h.detected_at DESC LIMIT 1000""",
            (since,),
        ).fetchall()
    out = []
    seen = set()
    for r in rows:
        key = r["listing_key"]
        if key in seen or key in hidden:
            continue
        lst = get_pool_listing(key)
        if not lst or lst.get("status") != "active":
            continue
        if key not in saved_keys and not (search and matches_search(lst, search)):
            continue
        seen.add(key)
        d = dict(lst)
        d["drop_from"] = int(r["prev_price"] or 0)
        d["drop_to"] = int(r["price"] or 0)
        d["drop"] = max(0, d["drop_from"] - d["drop_to"])
        d["drop_at"] = float(r["detected_at"])
        out.append(d)
    return out[offset:offset + limit]


# ──────────────────────────────────────────────────────────────────────
# Сохранённые машины (сохранение = наблюдение)
# ──────────────────────────────────────────────────────────────────────
def save_car(user_id: int, key: str, now: float | None = None) -> bool:
    """Сохраняет машину и автоматически включает наблюдение. True — впервые."""
    now = time.time() if now is None else float(now)
    lst = get_pool_listing(key)
    if not lst:
        return False
    with _conn() as conn:
        existing = conn.execute(
            "SELECT status FROM saved_cars WHERE user_id=? AND listing_key=?",
            (int(user_id), key),
        ).fetchone()
        conn.execute(
            """INSERT INTO saved_cars (user_id, listing_key, saved_at, saved_price, status)
               VALUES (?,?,?,?, 'active')
               ON CONFLICT(user_id, listing_key)
               DO UPDATE SET status='active', saved_at=excluded.saved_at""",
            (int(user_id), key, now, int(lst.get("price") or 0)),
        )
        is_new = existing is None or str(existing["status"]) != "active"
    # Наблюдение — бессрочное, отдельной кнопки «Следить» нет.
    _pt.add_watch(user_id, {"url": lst.get("url"), "source": lst.get("source"),
                            "title": lst.get("title"),
                            "_price_int": int(lst.get("price") or 0)}, days=0, now=now)
    if is_new:
        _pt.bump_stat(user_id, "saved", now=now)
    return is_new


def unsave_car(user_id: int, key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE saved_cars SET status='removed' WHERE user_id=? AND listing_key=?",
            (int(user_id), key),
        )
    _pt.stop_watch(user_id, key)


def saved_cars(user_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT s.listing_key, s.saved_at, s.saved_price, p.*
                 FROM saved_cars s LEFT JOIN listing_pool p ON p.listing_key = s.listing_key
                WHERE s.user_id=? AND s.status='active'
                ORDER BY s.saved_at DESC""",
            (int(user_id),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not d.get("title"):
            continue
        out.append(d)
    return out


def is_saved(user_id: int, key: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM saved_cars WHERE user_id=? AND listing_key=? AND status='active'",
            (int(user_id), key),
        ).fetchone()
    return bool(row)


def record_interest(user_id: int, key: str, now: float | None = None) -> None:
    """Факт интереса: пользователь нажал «Открыть»."""
    lst = get_pool_listing(key) or {}
    _pt.record_view(user_id, {"url": lst.get("url", ""), "source": lst.get("source", ""),
                              "_source_id": None}, now=now)


def cheaper_similar(user_id: int, key: str, min_gap_pct: float = 5.0) -> dict | None:
    """Похожая машина заметно дешевле сохранённой."""
    lst = get_pool_listing(key)
    if not lst or not lst.get("brand"):
        return None
    price = int(lst.get("price") or 0)
    if price <= 0:
        return None
    year = int(lst.get("year") or 0)
    scope = _region_scope(lst.get("region") or "")
    sql = ["SELECT * FROM listing_pool WHERE status='active' AND price > 0",
           "AND listing_key != ? AND brand = ?"]
    args: list[Any] = [key, lst["brand"]]
    if lst.get("model"):
        sql.append("AND model = ?")
        args.append(lst["model"])
    if year:
        sql.append("AND year BETWEEN ? AND ?")
        args += [year - MARKET_YEAR_TOLERANCE, year + MARKET_YEAR_TOLERANCE]
    if scope:
        sql.append("AND region IN (%s)" % ",".join("?" * len(scope)))
        args += scope
    sql.append("ORDER BY price ASC LIMIT 1")
    with _conn() as conn:
        row = conn.execute(" ".join(sql), args).fetchone()
    if not row:
        return None
    cand = dict(row)
    if is_junk(cand):
        return None
    gap = price - int(cand["price"])
    if gap <= 0 or gap / price * 100 < min_gap_pct:
        return None
    cand["gap"] = gap
    cand["saved_price"] = price
    cand["saved_title"] = lst.get("title")
    return cand


# ──────────────────────────────────────────────────────────────────────
# Настройки пользователя (тихие часы, отчёты)
# ──────────────────────────────────────────────────────────────────────
def get_prefs(user_id: int) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM user_prefs WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None:
            conn.execute("INSERT OR IGNORE INTO user_prefs (user_id) VALUES (?)", (int(user_id),))
            row = conn.execute("SELECT * FROM user_prefs WHERE user_id=?",
                               (int(user_id),)).fetchone()
    return dict(row)


def set_pref(user_id: int, field: str, value: int) -> None:
    if field not in {"quiet_from", "quiet_to", "morning_report", "evening_report",
                     "instant_notify"}:
        return
    get_prefs(user_id)
    with _conn() as conn:
        conn.execute(f"UPDATE user_prefs SET {field}=? WHERE user_id=?",
                     (int(value), int(user_id)))


def in_quiet_hours(user_id: int, now: float | None = None) -> bool:
    p = get_prefs(user_id)
    q_from, q_to = int(p["quiet_from"]), int(p["quiet_to"])
    if q_from == q_to:
        return False
    hour = datetime.fromtimestamp(time.time() if now is None else float(now), MSK).hour
    if q_from < q_to:
        return q_from <= hour < q_to
    return hour >= q_from or hour < q_to


# ──────────────────────────────────────────────────────────────────────
# Антиспам поверх notification_history
# ──────────────────────────────────────────────────────────────────────
def notify_once(user_id: int, event_type: str, key: str, signature: str,
                now: float | None = None) -> bool:
    """Одно событие — одно сообщение. Подпись включает user+listing+тип+событие."""
    if not signature:
        return False
    return _pt.should_notify(user_id, event_type, f"{event_type}:{key}:{signature}", key, now=now)


def new_listing_signature(key: str, price: int) -> str:
    return f"new:{int(price)}"


# ──────────────────────────────────────────────────────────────────────
# Форматирование
# ──────────────────────────────────────────────────────────────────────
def fmt_money(v) -> str:
    try:
        return f"{int(v):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return "—"


def _category_icon(cat: str) -> str:
    return {"fresh": "🚨", "today": "🔥", "days3": "📅", "bargain": "🤝",
            "price_drop": "📉"}.get(cat, "🚗")


def _published_label(listing: dict, now: float | None = None) -> str:
    """«вчера в 20:29» / «4 августа в 13:05» — когда объявление выложено."""
    ts = listing.get("published_at") or 0
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=3))          # МСК
    dt = datetime.fromtimestamp(ts, tz)
    today = datetime.fromtimestamp(
        time.time() if now is None else float(now), tz).date()
    _months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря")
    if dt.date() == today:
        return f"сегодня в {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"вчера в {dt:%H:%M}"
    return f"{dt.day} {_months[dt.month - 1]} в {dt:%H:%M}"


def published_head(listing: dict, now: float | None = None) -> str:
    """Первая строка карточки: когда объявление реально выложено на площадке.

    Раньше в шапке стоял возраст, посчитанный от момента, когда объявление
    увидел бот («8 дн назад»), а точная дата публикации пряталась ниже. Для
    перекупа важна именно дата публикации, поэтому она идёт первой, а возраст
    остаётся рядом в скобках. Если площадка дату не отдала — показываем
    прежний возраст с пометкой.
    """
    pub = _published_label(listing, now)
    if not pub:
        return age_label(listing, now)
    return f"Опубликовано: {pub} ({age_label(listing, now)})"


def format_card(listing: dict, *, category: str = "", now: float | None = None,
                reasons: Iterable[str] | None = None) -> str:
    """Карточка машины в формате ТЗ (без DealScore)."""
    now = time.time() if now is None else float(now)
    cat = category or category_of(listing, now) or "today"
    price = int(listing.get("price") or 0)
    market = int(listing.get("market_price") or 0)
    _src = str(listing.get("source") or "").lower()
    _src_label = {"avito": "🔴 Avito", "drom": "🔵 Дром", "autoru": "🟠 Auto.ru",
                  "youla": "🟡 Юла", "vk": "📘 ВКонтакте",
                  "tg": "✈️ Telegram", "tg_channel": "📢 TG-канал"}.get(_src, "")
    _head = f"{_category_icon(cat)} {published_head(listing, now)}"
    if _src_label:
        _head += f"  ·  {_src_label}"
    lines = [_head, ""]
    year = int(listing.get("year") or 0)
    title = listing.get("title") or "Автомобиль"
    lines.append(f"{title}" + (f", {year}" if year and str(year) not in title else ""))
    lines.append(f"Цена: {fmt_money(price)}")
    if market:
        approx = "≈" if int(listing.get("market_sample") or 0) >= MIN_MARKET_SAMPLE else "≈~"
        # Основа оценки называется честно: Авито или все площадки сразу.
        _basis = "Рынок Авито" if (listing.get("market_basis") or "avito") == "avito" \
            else "Рынок площадок"
        lines.append(f"{_basis}: {approx}{fmt_money(market)}")
        diff = market - price
        if diff > 0:
            lines.append(f"Разница: ≈{fmt_money(diff)}")
            if price:
                lines.append(f"Дешевле рынка на {int(round(diff * 100 / market))}%")
        if int(listing.get("market_sample") or 0) < MIN_MARKET_SAMPLE:
            lines.append("Оценка предварительная — мало похожих объявлений")
    lines.append("")
    region = region_label(listing.get("region") or "")
    if region:
        lines.append(f"📍 {region}")
    # Дата публикации уже стоит в шапке — здесь она нужна только тогда,
    # когда площадка её не отдала: иначе клиент не поймёт, что «N дн назад» —
    # это возраст по данным бота, а не время выкладки объявления.
    if not _published_label(listing, now):
        lines.append("🕒 Опубликовано: площадка не указала дату")
    lines.append(f"🔧 По описанию: {condition_text(listing.get('condition') or '')}")
    _desc = (listing.get("description") or "").strip()
    if _desc:
        _desc = " ".join(_desc.split())
        lines.append("")
        lines.append(f"📝 {_desc[:300]}" + ("…" if len(_desc) > 300 else ""))
    # Блок «Почему показали» убран: он повторял то, что и так видно по шапке
    # и цене, и занимал место в карточке. reasons оставлен для вызовов, где
    # причина действительно нужна (рекомендации, уведомления).
    why = list(reasons or ())
    if why:
        lines.append("")
        lines += [f"• {r}" for r in why]
    return "\n".join(lines)


def why_shown(listing: dict, *, category: str = "", now: float | None = None) -> list[str]:
    now = time.time() if now is None else float(now)
    cat = category or category_of(listing, now)
    out: list[str] = []
    if cat == "fresh":
        out.append("только появилась")
    elif cat == "today":
        out.append("свежее объявление")
    elif cat == "bargain":
        out += bargain_reasons(listing)[:2]
    market = int(listing.get("market_price") or 0)
    price = int(listing.get("price") or 0)
    if market and price and market > price:
        out.append("цена ниже похожих предложений")
    out.append("подходит под поиск пользователя")
    return out


REGION_NAMES: dict[str, str] = {}


def region_label(slug: str) -> str:
    """Человеческое имя региона (заполняется ботом из REGIONS)."""
    return REGION_NAMES.get((slug or "").strip().lower(), slug or "")


# Регион из ссылки. Второй вариант — путь без домена («/omsk/avtomobili/…»):
# часть парсеров отдаёт относительные ссылки, и без него объявление
# оставалось без региона и не проходило фильтр поиска.
_URL_REGION_RE = re.compile(
    r"(?:(?:avito\.ru|drom\.ru|youla\.ru|auto\.ru)/|^/)([a-z0-9_\-]+)/", re.IGNORECASE)


# Города, относящиеся к области поиска: заполняется из control_bot
# (OBLAST_CITY_SLUGS[<область>] = {города}). Пока пусто — работает точное
# совпадение, что безопаснее, чем пропускать чужие регионы.
OBLAST_CITIES: dict[str, set] = {}


def _same_oblast(city: str, wanted: list) -> bool:
    """True, если город входит в область одного из искомых регионов."""
    for w in wanted:
        if city in OBLAST_CITIES.get(w, ()):  # noqa: SIM118
            return True
    return False


def _region_of_item(item: dict) -> str:
    """Регион объявления: явное поле → город из URL → регион поиска.

    Без последнего шага объявления из городов области (Невьянск в Свердловской)
    оставались без региона и проходили ЛЮБОЙ фильтр, из-за чего в выдаче
    Екатеринбурга появлялись машины из других областей.
    """
    for k in ("region", "_search_region", "_region"):
        v = str(item.get(k) or "").strip()
        if v:
            return v
    m = _URL_REGION_RE.search(str(item.get("url") or ""))
    if m:
        slug = m.group(1).lower()
        if slug not in ("all", "rossiya", "moskva_i_mo"):
            return slug
    return ""


def normalize_region(value: str) -> str:
    """Приводит регион к slug'у.

    Источники отдают регион по-разному: одни — slug ('ekaterinburg'), другие —
    человеческое имя ('Екатеринбург'). Без приведения к одному виду фильтр
    поиска молча отсекал половину объявлений.
    """
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower()
    if low in REGION_NAMES:
        return low
    for slug, name in REGION_NAMES.items():
        if name.strip().lower() == low:
            return slug
    return low


# Города, чей субъект — край (остальные считаем областью). Республики и
# города федерального значения обрабатываются отдельно.
_KRAI_CITIES = {
    "perm", "krasnodar", "krasnoyarsk", "stavropol", "barnaul", "habarovsk",
    "vladivostok", "chita", "petropavlovsk_kamchatskiy", "birobidzhan",
}
_REPUBLIC_CITIES = {
    "kazan", "ufa", "mahachkala", "grozny", "vladikavkaz", "nalchik",
    "cheboksary", "izhevsk", "saransk", "yoshkar_ola", "syktyvkar",
    "petrozavodsk", "elista", "abakan", "kyzyl", "gorno_altaysk",
    "ulan_ude", "yakutsk", "maykop", "cherkessk",
}


def _region_suffix(slug: str) -> str:
    """Подпись «и край / и область / и республика» к городу поиска."""
    low = (slug or "").strip().lower()
    if not low:
        return ""
    if low in ("moscow", "spb", "sankt-peterburg", "saint_petersburg"):
        return " и область"
    if low in _KRAI_CITIES:
        return " и край"
    if low in _REPUBLIC_CITIES:
        return " и республика"
    return " и область"


def format_summary(user_id: int, now: float | None = None) -> str:
    """Главный экран поиска — сводка вместо десятков карточек."""
    s = get_active_search(user_id)
    counts = category_counts(user_id, now)
    if not s:
        return "🎯 Активного поиска нет. Нажмите «🔍 Найти авто», чтобы создать."
    pmax = int(s.get("price_max") or 0)
    budget = f"до {fmt_money(pmax)}" if 0 < pmax < NO_PRICE_LIMIT else "без ограничения по цене"
    where = region_label(s.get("region") or "") or "не указан"
    if s.get("regions"):
        where += " и " + ", ".join(region_label(r) for r in s["regions"])
    else:
        # Поиск идёт по всему субъекту, а не только по городу, — так и пишем.
        where += _region_suffix(s.get("region") or "")
    brands = s.get("brands") or []
    what = "Все машины" if not brands else ", ".join(brands).title()
    if s.get("model"):
        what = s["model"].title()
    return (
        f"🎯 {what} {budget}\n"
        f"📍 {where}\n\n"
        f"🚨 Кто быстрее — {counts['fresh']}\n"
        f"🔥 Новые сегодня — {counts['today']}\n"
        f"📅 До 3 дней — {counts['days3']}\n"
        f"📉 Снизили цену — {counts['price_drop']}\n"
        f"🤝 Простор для торга — {counts['bargain']}\n"
        f"⭐ Сохранённые — {counts['saved']}"
    )


# ──────────────────────────────────────────────────────────────────────
# Уведомления: тексты
# ──────────────────────────────────────────────────────────────────────
def format_new_listing_notification(listing: dict, now: float | None = None) -> str:
    price = int(listing.get("price") or 0)
    market = int(listing.get("market_price") or 0)
    lines = ["🚨 Новое подходящее объявление", "",
             listing.get("title") or "Автомобиль",
             f"Цена: {fmt_money(price)}"]
    if market:
        lines.append(f"Рынок Авито: ≈{fmt_money(market)}")
        if market > price:
            lines.append(f"Разница: ≈{fmt_money(market - price)}")
    lines.append(f"🕒 {published_head(listing, now)}")
    region = region_label(listing.get("region") or "")
    if region:
        lines.append(f"📍 {region}")
    if listing.get("url"):
        lines.append(str(listing["url"]))
    lines += ["", "Почему прислали:", "• только что появилось на площадке;"]
    if market and market > price:
        lines.append("• цена ниже похожих вариантов;")
    lines.append("• подходит под ваши фильтры.")
    return "\n".join(lines)


def format_price_drop_notification(listing: dict, old_price: int, new_price: int,
                                   saved_at: float | None = None,
                                   drops_count: int = 0, now: float | None = None) -> str:
    now = time.time() if now is None else float(now)
    lines = ["📉 Сохранённая машина подешевела", "",
             listing.get("title") or "Автомобиль", "",
             f"Было: {fmt_money(old_price)}",
             f"Стало: {fmt_money(new_price)}", "",
             f"Снижение: {fmt_money(max(0, old_price - new_price))}"]
    if saved_at:
        days = int((now - float(saved_at)) / 86400)
        lines.append("")
        lines.append("Вы сохранили её сегодня." if days <= 0
                     else f"Вы сохранили её {days} дней назад.")
    if drops_count > 1:
        lines.append(f"Цена снижалась уже {drops_count} раза.")
    return "\n".join(lines)


def format_relisted_notification(listing: dict, diff: int = 0) -> str:
    lines = ["♻️ Машина снова появилась", "",
             listing.get("title") or "Автомобиль", "",
             "Ранее объявление было снято."]
    if diff > 0:
        lines.append(f"Теперь цена ниже на {fmt_money(diff)}.")
    return "\n".join(lines)


def format_cheaper_similar_notification(cand: dict) -> str:
    return "\n".join([
        "🔥 Найден похожий вариант дешевле", "",
        f"Вы сохранили {cand.get('saved_title') or 'машину'} за {fmt_money(cand.get('saved_price'))}.",
        f"Новая похожая — {fmt_money(cand.get('price'))}.", "",
        f"Разница: {fmt_money(cand.get('gap'))}.",
    ])


# ──────────────────────────────────────────────────────────────────────
# Утренний и вечерний отчёты
# ──────────────────────────────────────────────────────────────────────
def morning_report(user_id: int, now: float | None = None) -> dict | None:
    """Итоги ночи. None — событий не было, сообщение не отправляем."""
    now = time.time() if now is None else float(now)
    search = get_active_search(user_id)
    hidden = _hidden_keys(user_id)
    checked = matched = new = 0
    since = now - 12 * 3600
    if search:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM listing_pool WHERE last_seen_at >= ? LIMIT 5000", (since,)
            ).fetchall()
        for r in rows:
            d = dict(r)
            checked += 1
            if d["listing_key"] in hidden or not matches_search(d, search):
                continue
            matched += 1
            fs = float(d.get("published_at") or d.get("first_seen_at") or 0)
            if fs >= since:
                new += 1
    drops = len(price_drop_feed(user_id, limit=500, hours=12, now=now))
    saved_keys = {c["listing_key"] for c in saved_cars(user_id)}
    saved_changed = 0
    for d in price_drop_feed(user_id, limit=500, hours=12, now=now):
        if d["listing_key"] in saved_keys:
            saved_changed += 1
    if not (new or drops or saved_changed):
        return None
    text = ("☀️ За ночь по вашим поискам\n\n"
            f"Проверено: {checked}\n"
            f"Подошло: {matched}\n"
            f"Новых: {new}\n"
            f"Снизили цену: {drops}\n"
            f"Из сохранённых изменились: {saved_changed}")
    return {"text": text, "checked": checked, "matched": matched, "new": new,
            "drops": drops, "saved_changed": saved_changed}


def evening_report(user_id: int, now: float | None = None) -> dict | None:
    """Итоги дня из реальных персональных событий."""
    now = time.time() if now is None else float(now)
    stats = _pt.get_stats(user_id, day=datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d"))
    search = get_active_search(user_id)
    hidden = _hidden_keys(user_id)
    since = now - 24 * 3600
    checked = matched = below = 0
    unseen = 0
    if search:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT * FROM listing_pool WHERE last_seen_at >= ? LIMIT 5000", (since,)
            ).fetchall()
        for r in rows:
            d = dict(r)
            checked += 1
            if d["listing_key"] in hidden or not matches_search(d, search):
                continue
            matched += 1
            mk = int(d.get("market_price") or 0)
            if mk and mk > int(d.get("price") or 0):
                below += 1
            if not _pt.has_viewed(user_id, d["listing_key"]):
                unseen += 1
    opened = int(stats.get("opened") or 0)
    saved = int(stats.get("saved") or 0)
    if not (matched or opened or saved):
        return None
    text = ("🌙 Итоги дня\n\n"
            f"Проверено: {checked}\n"
            f"Подошло: {matched}\n"
            f"Ниже рынка: {below}\n"
            f"Вы открыли: {opened}\n"
            f"Сохранили: {saved}\n"
            f"Непросмотрено: {unseen}")
    return {"text": text, "checked": checked, "matched": matched, "below_market": below,
            "opened": opened, "saved": saved, "unseen": unseen}
