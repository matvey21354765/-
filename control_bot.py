"""
Авто-брокер бот — публичная версия.
Каждый пользователь выбирает регион и бюджет, бот ищет частников ниже рынка.
"""

import asyncio
import json
import logging
import random
import re
import time
import datetime
import subprocess
import hashlib
from pathlib import Path
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import analytics

# ── Админы (для /stats) ─────────────────────────────────────────
def _parse_admin_ids() -> set[int]:
    ids: set[int] = set()
    import re as _re
    for raw in (os.getenv("ADMIN_ID", "749256529"), os.getenv("ADMIN_IDS", "")):
        for part in _re.findall(r"\d+", str(raw)):
            ids.add(int(part))
    return ids

ADMIN_IDS = _parse_admin_ids()

# ── Токен ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8923014188:AAHvNW2B5fin2XCmbVhlaLNjWhLwI3JhZ90")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "b317ae63b4d847805e2f91a1dc073b40")
# Официальный API Авито (бесплатно): зарегистрируй приложение на https://developers.avito.ru/
# и добавь переменные окружения AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в Railway
AVITO_CLIENT_ID     = os.getenv("AVITO_CLIENT_ID", "")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET", "")
_avito_oauth_token: "dict | None" = None  # {"token": "...", "expires_at": timestamp}
_free_proxy_cache: list[str] = []
_free_proxy_cache_time: float = 0.0
# Прокси, проверенные и реально дающие доступ к Авито (обновляются при старте и каждые 15 мин)
_working_free_proxies: list[str] = []
_working_free_proxies_time: float = 0.0

# ── Резидентный прокси для запросов к Авито (опционально) ────────
# Поддерживает HTTP и SOCKS5. AVITO_PROXY_AUTH=ip — авторизация по IP (без логина).
# Можно задать через PROXY_URL=http://user:pass@host:port (удобнее для большинства провайдеров)
AVITO_PROXY_HOST = os.getenv("AVITO_PROXY_HOST", "")
AVITO_PROXY_PORT = os.getenv("AVITO_PROXY_PORT", "")
AVITO_PROXY_USER = os.getenv("AVITO_PROXY_USER", "")
AVITO_PROXY_PASS = os.getenv("AVITO_PROXY_PASS", "")
AVITO_PROXY_PROTOCOL = os.getenv("AVITO_PROXY_PROTOCOL", "http").lower()
AVITO_PROXY_AUTH = os.getenv("AVITO_PROXY_AUTH", "login").lower()  # "login" или "ip"
# Диапазон портов для ротации IP (напр. pool.proxys.world:10000-10999 = 1000 IP).
AVITO_PROXY_PORT_MIN = os.getenv("AVITO_PROXY_PORT_MIN", "")
AVITO_PROXY_PORT_MAX = os.getenv("AVITO_PROXY_PORT_MAX", "")

_AVITO_PROXY_PORTS: list[int] = []
if AVITO_PROXY_PORT_MIN and AVITO_PROXY_PORT_MAX:
    try:
        _AVITO_PROXY_PORTS = list(range(int(AVITO_PROXY_PORT_MIN), int(AVITO_PROXY_PORT_MAX) + 1))
    except Exception:
        _AVITO_PROXY_PORTS = []

# Альтернативный способ задать прокси — одна переменная PROXY_URL
# Форматы: http://user:pass@host:port  /  socks5://user:pass@host:port  /  host:port
_PROXY_URL_RAW = (
    os.getenv("PROXY_URL", "") or
    os.getenv("HTTPS_PROXY", "") or
    os.getenv("HTTP_PROXY", "") or
    ""
)
# Не берём Railway-системный прокси (он не является резидентным)
if _PROXY_URL_RAW and "__agentproxy" in _PROXY_URL_RAW:
    _PROXY_URL_RAW = ""

if _PROXY_URL_RAW and not AVITO_PROXY_HOST:
    import urllib.parse as _up
    try:
        _pu = _up.urlparse(_PROXY_URL_RAW if "://" in _PROXY_URL_RAW else "http://" + _PROXY_URL_RAW)
        if _pu.hostname:
            AVITO_PROXY_HOST = _pu.hostname
            AVITO_PROXY_PORT = str(_pu.port or 80)
            AVITO_PROXY_USER = _pu.username or ""
            AVITO_PROXY_PASS = _pu.password or ""
            AVITO_PROXY_PROTOCOL = (_pu.scheme or "http").lower()
    except Exception:
        pass


# Флаг: прокси вернул 407 (неверная авторизация) — автоматически отключаем
_proxy_auth_failed: bool = False


def _avito_proxies() -> "dict[str, str] | None":
    """Возвращает прокси-словарь со СЛУЧАЙНЫМ портом из пула (ротация IP).
    Если пул портов не задан — возвращает статический AVITO_PROXIES.
    Если прокси вернул 407 — возвращает None (работаем напрямую)."""
    global _proxy_auth_failed
    if _proxy_auth_failed:
        return None
    if AVITO_PROXY_HOST and _AVITO_PROXY_PORTS:
        port = random.choice(_AVITO_PROXY_PORTS)
        use_auth = AVITO_PROXY_AUTH != "ip" and AVITO_PROXY_USER
        auth = f"{AVITO_PROXY_USER}:{AVITO_PROXY_PASS}@" if use_auth else ""
        url = f"{AVITO_PROXY_PROTOCOL}://{auth}{AVITO_PROXY_HOST}:{port}"
        return {"http": url, "https": url}
    return AVITO_PROXIES


def _mark_proxy_failed(err: str) -> None:
    """Помечаем прокси как сломанный при ошибке 407."""
    global _proxy_auth_failed
    if "407" in err or "Proxy Authentication Required" in err or "Tunnel connection failed" in err:
        if not _proxy_auth_failed:
            _proxy_auth_failed = True
            print("[прокси] ⚠️ Прокси вернул 407 — переключаемся на прямое соединение")


AVITO_PROXIES: "dict[str, str] | None" = None
if AVITO_PROXY_HOST and (AVITO_PROXY_PORT or _AVITO_PROXY_PORTS):
    _use_auth = AVITO_PROXY_AUTH != "ip" and AVITO_PROXY_USER
    _auth = f"{AVITO_PROXY_USER}:{AVITO_PROXY_PASS}@" if _use_auth else ""
    _repr_port = AVITO_PROXY_PORT or (str(_AVITO_PROXY_PORTS[0]) if _AVITO_PROXY_PORTS else "")
    _avito_proxy_url = f"{AVITO_PROXY_PROTOCOL}://{_auth}{AVITO_PROXY_HOST}:{_repr_port}"
    AVITO_PROXIES = {"http": _avito_proxy_url, "https": _avito_proxy_url}

_proxy_display = f"{AVITO_PROXY_PROTOCOL}://{AVITO_PROXY_HOST}:{_repr_port}" if AVITO_PROXIES else None
print(f"[прокси] {'✅ ' + _proxy_display if _proxy_display else '❌ не настроен — Авито/Auto.ru могут не работать'}")

# ── Регионы ─────────────────────────────────────────────────────
REGIONS = {
    "ekaterinburg": "Екатеринбург",
    "moscow":       "Москва",
    "spb":          "Санкт-Петербург",
    "novosibirsk":  "Новосибирск",
    "kazan":        "Казань",
    "chelyabinsk":  "Челябинск",
    "ufa":          "Уфа",
    "krasnodar":    "Краснодар",
    "omsk":         "Омск",
    "tyumen":       "Тюмень",
    "perm":         "Пермь",
    "krasnoyarsk":  "Красноярск",
    "voronezh":     "Воронеж",
    "samara":       "Самара",
    "rostov":       "Ростов-на-Дону",
}

# Слаги для Auto.ru — используем область целиком, не только город
AUTORU_SLUGS = {
    "ekaterinburg": "sverdlovskaya_oblast",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirskaya_oblast",
    "kazan":        "tatarstan",
    "chelyabinsk":  "chelyabinskaya_oblast",
    "ufa":          "bashkortostan",
    "krasnodar":    "krasnodarskiy_kray",
    "omsk":         "omskaya_oblast",
    "tyumen":       "tyumenskaya_oblast",
    "perm":         "permskiy_kray",
    "krasnoyarsk":  "krasnoyarskiy_kray",
    "voronezh":     "voronezhskaya_oblast",
    "samara":       "samarskaya_oblast",
    "rostov":       "rostovskaya_oblast",
}

# Слаги для Авито — область целиком
AVITO_REGION_SLUGS = {
    "ekaterinburg": "sverdlovskaya_oblast",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirskaya_oblast",
    "kazan":        "tatarstan",
    "chelyabinsk":  "chelyabinskaya_oblast",
    "ufa":          "bashkortostan",
    "krasnodar":    "krasnodarskiy_kray",
    "omsk":         "omskaya_oblast",
    "tyumen":       "tyumenskaya_oblast",
    "perm":         "permskiy_kray",
    "krasnoyarsk":  "krasnoyarskiy_kray",
    "voronezh":     "voronezhskaya_oblast",
    "samara":       "samarskaya_oblast",
    "rostov":       "rostovskaya_oblast",
}

# Слаги для Дрома — область (geo-параметр)
DROM_GEO = {
    "ekaterinburg": 12,    # Свердловская область
    "moscow":       1,     # Москва и МО
    "spb":          2,     # СПб и ЛО
    "novosibirsk":  15,    # Новосибирская обл.
    "kazan":        23,    # Татарстан
    "chelyabinsk":  13,    # Челябинская обл.
    "ufa":          3,     # Башкортостан
    "krasnodar":    18,    # Краснодарский кр.
    "omsk":         16,    # Омская обл.
    "tyumen":       27,    # Тюменская обл.
    "perm":         8,     # Пермский кр.
    "krasnoyarsk":  24,    # Красноярский кр.
    "voronezh":     36,    # Воронежская обл.
    "samara":       26,    # Самарская обл.
    "rostov":       20,    # Ростовская обл.
}

# ── Дилерские признаки ──────────────────────────────────────────
DEALER_KEYWORDS = [
    "ооо ", "зао ", "пао ", " ип,", " ип.", "официальный дилер", "автодилер",
    "автосалон", "автоцентр", "автохолдинг", "автогруп", "автогрупп",
    "trade-in", "трейд-ин",
    "рольф", "major", "колёса даром", "автопланета", "автоград",
    "июль авто", "автоленд", "favorit", "фаворит",
    "fresh auto", "автобан", "автосфера", "арконт", "ключавто", "авилон",
    "петровский", "прагматика", "бизнес кар", "максимум авто",
    "genser", "генсер", "ац урал", "восток авто", "сити авто",
    "наш автосалон", "тест-драйв",
    "гарантия завода", "официальная гарантия",
    "car dealer", "автосупермаркет",
    "в наличии и под заказ", "отдел продаж",
    # Рекламные вставки Auto.ru (не реальные объявления)
    "самый недорогой способ продвижения",
    "поможет быстрее найти покупателя",
    "оказаться наверху списка объявлений",
    "отсортированного по актуальности",
    "продвижение объявления",
]

HOT_WORDS = re.compile(
    r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево|продам быстро|срочная продажа"
    r"|срочно продам|срочно продаю|нужны деньги|уезжаю|переезжаю|не торгуюсь нет"
    r"|ниже рынка|ниже рыночной|выгодно|хорошая цена|торг при осмотре)",
    re.IGNORECASE,
)

MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
    "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
}

# ── Пользователи ────────────────────────────────────────────────
USERS_DIR = Path("users")
USERS_DIR.mkdir(exist_ok=True)

# Лимит частоты поиска на пользователя — защита от спама запросами,
# чтобы один человек не нагружал площадки слишком часто.
SEARCH_COOLDOWN_SEC = 45
_last_search_at: dict[int, float] = {}

# Мониторинг новых объявлений
MONITOR_INTERVAL = 15 * 60   # проверять каждые 15 минут
MONITOR_MIN_SAVINGS_PCT = 10  # показывать только если скидка от рынка ≥ 10%
_monitor_tasks: dict[int, asyncio.Task] = {}   # uid → Task


def user_dir(uid: int) -> Path:
    d = USERS_DIR / str(uid)
    d.mkdir(exist_ok=True)
    return d


def load_settings(uid: int) -> dict:
    f = user_dir(uid) / "settings.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(uid: int, s: dict):
    f = user_dir(uid) / "settings.json"
    f.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


_URL_NORM_RE = re.compile(r'(\d{7,})')
_URL_DOMAIN_RE = re.compile(r'https?://(?:www\.|m\.)?([^/]+)')

def _norm_url(u: str) -> str:
    """Нормализует URL для дедупликации: убирает query-params и мобильный поддомен."""
    if not u:
        return u
    u = u.split("?")[0].split("#")[0].rstrip("/")
    u = u.replace("//m.avito.ru/", "//www.avito.ru/")
    u = u.replace("//m.vk.com/", "//vk.com/")
    return u


def load_seen(uid: int) -> set:
    f = user_dir(uid) / "seen.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data[-300:])  # keep only last 300
    except Exception:
        pass
    return set()


def save_seen(uid: int, seen: set):
    f = user_dir(uid) / "seen.json"
    f.write_text(json.dumps(list(seen), ensure_ascii=False), encoding="utf-8")


# ── Реферальная система ──────────────────────────────────────────
REFERRALS_FILE = Path("data/referrals.json")

def _load_referrals() -> dict:
    try:
        REFERRALS_FILE.parent.mkdir(exist_ok=True)
        if REFERRALS_FILE.exists():
            return json.loads(REFERRALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_referrals(data: dict):
    try:
        REFERRALS_FILE.parent.mkdir(exist_ok=True)
        REFERRALS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def get_referral_bonus_days(uid: int) -> int:
    """Returns total bonus days accumulated by this user."""
    data = _load_referrals()
    entry = data.get(str(uid), {})
    return entry.get("bonus_days", 0)

def _get_or_create_referral(uid: int) -> dict:
    """Gets or creates referral entry for user."""
    data = _load_referrals()
    key = str(uid)
    if key not in data:
        import random as _random
        import string as _string
        code = "".join(_random.choices(_string.ascii_uppercase + _string.digits, k=6))
        data[key] = {"code": code, "invited": [], "bonus_days": 0}
        _save_referrals(data)
    return data[key]

def _record_referral(new_uid: int, inviter_uid: int):
    """Records that new_uid was invited by inviter_uid."""
    data = _load_referrals()
    inviter_key = str(inviter_uid)
    new_key = str(new_uid)
    
    # Don't record if already has an inviter
    if data.get(new_key, {}).get("inviter"):
        return
    
    # Ensure inviter exists
    if inviter_key not in data:
        _get_or_create_referral(inviter_uid)
        data = _load_referrals()
    
    # Ensure new user exists
    if new_key not in data:
        import random as _random
        import string as _string
        code = "".join(_random.choices(_string.ascii_uppercase + _string.digits, k=6))
        data[new_key] = {"code": code, "invited": [], "bonus_days": 0}
    
    # Record inviter for new user
    data[new_key]["inviter"] = inviter_uid
    
    # Add to inviter's invited list
    invited_list = data[inviter_key].get("invited", [])
    if new_uid not in invited_list:
        invited_list.append(new_uid)
        data[inviter_key]["invited"] = invited_list
        
        # Give +3 days bonus per invited friend
        data[inviter_key]["bonus_days"] = data[inviter_key].get("bonus_days", 0) + 3
        
        # Milestone: 10 friends = +30 extra days
        if len(invited_list) == 10:
            data[inviter_key]["bonus_days"] = data[inviter_key].get("bonus_days", 0) + 30
    
    _save_referrals(data)


def load_skipped(uid: int) -> set:
    f = user_dir(uid) / "skipped.json"
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_skipped(uid: int, skipped: set):
    f = user_dir(uid) / "skipped.json"
    f.write_text(json.dumps(list(skipped), ensure_ascii=False), encoding="utf-8")


# ── Фильтры ─────────────────────────────────────────────────────

def parse_price(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else None


def is_dealer(item: dict) -> bool:
    text = (
        item.get("title", "") + " " +
        item.get("description", "") + " " +
        item.get("seller", "")
    ).lower()
    return any(k in text for k in DEALER_KEYWORDS)


def in_price_range(item: dict, price_min: int, price_max: int) -> bool:
    p = item.get("_price_int") or parse_price(item.get("price", ""))
    if p:
        return price_min <= p <= price_max
    # Цена неизвестна. Пытаемся исключить заведомо дорогие машины по году.
    # Новые авто (2022+) стоят от ~1 млн ₽. Если бюджет до 800k — не показываем.
    year = item.get("year") or item.get("_year") or 0
    try:
        year = int(str(year)[:4])
    except Exception:
        year = 0
    if year >= 2022 and price_max < 800_000:
        return False
    if year >= 2020 and price_max < 400_000:
        return False
    # Пропускаем как кандидата: реальная цена нужна, но лучше показать
    # объявление с "—", чем потерять реальную выгодную машину.
    return True


def hot_score(item: dict) -> float:
    """Базовая оценка срочности продажи."""
    title = item.get("title", "") + " " + item.get("description", "")
    score = 0.0
    if HOT_WORDS.search(title):
        score += 20.0
    return round(score, 2)


def _car_group_key(title: str) -> str:
    """Извлекает марку+модель+год для группировки (напр. 'toyota camry 2018').

    Корректно обрабатывает заголовки с префиксами площадок:
      'Дром Volkswagen Golf 2012' → 'volkswagen golf 2012'
      'Авито Mazda 3 1.6 AT, 2008' → 'mazda 3 2008'
    """
    t = title.lower()
    # Удаляем префиксы площадок
    for _pfx in ("авито", "дром", "auto.ru", "autoru", "вконтакте", "tg", "telegram"):
        t = re.sub(rf'^\s*{re.escape(_pfx)}\s*', '', t)
    # Убираем технические характеристики: 1.6 МТ, 156 000 км и т.п.
    t = re.sub(r'\d+[\.,]\d+\s*(л|at|mt|акп|мкп|амт)', '', t)
    t = re.sub(r'\d[\d\s]{2,}км', '', t)
    # Год выпуска
    year_m = re.search(r'\b(20\d{2}|19\d{2})\b', t)
    year = year_m.group(1) if year_m else ""
    if year_m:
        t = t[:year_m.start()] + t[year_m.end():]  # убираем год из строки
    # Берём первые 2 смысловых слова — марка + модель
    # Оставляем числа-части модели: Mazda 3, BMW 5, ВАЗ 2114 и т.п.
    # Исключаем числа > 2100 (могут быть годами, уже обработаны выше)
    def _is_model_word(w: str) -> bool:
        if w.isalpha():
            return True
        if w.isdigit():
            n = int(w)
            return n < 2100  # модели: 3, 5, 2114, 320 и т.п. (не годы)
        return False
    words = [w for w in re.sub(r'[^а-яёa-z0-9\s]', ' ', t).split() if w and _is_model_word(w)]
    brand_model = " ".join(words[:2]) if len(words) >= 2 else " ".join(words)
    return f"{brand_model} {year}".strip()


def rank_by_market_price(items: list[dict], ref_items: list[dict] | None = None,
                          avito_only_median: bool = False) -> list[dict]:
    """
    Вычисляет рыночную цену по медиане Авито-данных (ref_items).
    Если avito_only_median=True — медиана строится ТОЛЬКО по ref_items (Авито),
    а не смешивается с ценами других площадок.
    """
    from statistics import median

    # Для медианы используем либо только Авито-данные, либо всё вместе
    # Если Авито-эталон пустой (заблокирован) — используем все доступные данные
    if avito_only_median and ref_items and len(ref_items) >= 5:
        all_for_median = list(ref_items)
    elif ref_items:
        all_for_median = list(ref_items) + list(items)
    else:
        all_for_median = list(items)
    groups: dict[str, list[int]] = {}
    # Промежуточный уровень: марка+модель+2-летний диапазон (2015→1007, 2017→1008, 2019→1009)
    # Разделяет 2015 Solaris от 2017 Solaris → медиана не искажается новыми моделями
    groups_year_bracket: dict[str, list[int]] = {}
    # Широкие группы: только марка+модель (без года) — запасной уровень
    groups_broad: dict[str, list[int]] = {}
    # Ещё шире: только первое слово (марка) — для совсем маленьких выборок
    groups_brand: dict[str, list[int]] = {}
    for it in all_for_median:
        p = it.get("_price_int", 0)
        if p > 0:
            key = _car_group_key(it.get("title", ""))
            groups.setdefault(key, []).append(p)
            # Year-bracket ключ: марка+модель+диапазон4
            parts = key.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
                broad_key = parts[0]
                try:
                    bracket = int(parts[1]) // 2  # 2015→1007, 2016-2017→1008, 2018-2019→1009
                    groups_year_bracket.setdefault(f"{broad_key}_{bracket}", []).append(p)
                except Exception:
                    pass
            else:
                broad_key = key
            groups_broad.setdefault(broad_key, []).append(p)
            # Ключ марки: только первое слово
            brand_key_m = key.split(" ", 1)[0]
            groups_brand.setdefault(brand_key_m, []).append(p)

    market: dict[str, float] = {k: median(v) for k, v in groups.items() if len(v) >= 1}
    market_year_bracket: dict[str, float] = {k: median(v) for k, v in groups_year_bracket.items() if len(v) >= 1}
    market_broad: dict[str, float] = {k: median(v) for k, v in groups_broad.items() if len(v) >= 2}
    market_brand: dict[str, float] = {k: median(v) for k, v in groups_brand.items() if len(v) >= 3}

    for it in items:
        p = it.get("_price_int", 0)
        deal_score = 0.0
        savings_pct = 0.0

        if p > 0:
            key = _car_group_key(it.get("title", ""))
            parts = key.rsplit(" ", 1)
            broad_key = parts[0] if (len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4) else key
            # Уровень 1: точный (марка+модель+год)
            med = market.get(key, 0)
            # Уровень 2: 2-летний диапазон года (2012→1006, 2013→1006, 2014→1007...)
            if not med:
                try:
                    bracket = int(parts[1]) // 2 if (len(parts) == 2 and parts[1].isdigit()) else 0
                    if bracket:
                        med = market_year_bracket.get(f"{broad_key}_{bracket}", 0)
                except Exception:
                    pass
            # Уровень 3: марка+модель (все годы) — только если цена близка к медиане.
            # Узкие границы: 0.55–1.8 — исключает сравнение старого Golf 2012 с новым Golf 2022
            if not med:
                med_all = market_broad.get(broad_key, 0)
                if med_all and 0.55 < (p / med_all) < 1.8:
                    med = med_all
            # Уровень 4: только марка — ещё уже: 0.6–1.5
            if not med:
                brand_key_m = key.split(" ", 1)[0]
                med_brand = market_brand.get(brand_key_m, 0)
                if med_brand and 0.6 < (p / med_brand) < 1.5:
                    med = med_brand
            if med > 0:
                savings_pct = round((1 - p / med) * 100, 1)
                # Кап: скидка > 50% почти всегда означает неверное сравнение.
                # Golf 2012 за 820к vs медиана всех Golf 2.5M = 67% → отклоняем.
                # Реальные скидки >50% бывают, но редки — лучше не показывать,
                # чем вводить в заблуждение.
                # Динамический кап: для бюджетных авто (<400к) разрешаем до 60%,
                # иначе 50%. Golf 2012 за 820к vs медиана 2.5M = 67% → отклоняем.
                _savings_cap = 60 if p < 400_000 else 50
                if savings_pct > _savings_cap:
                    med = 0
                    savings_pct = 0.0
            if med > 0:
                it["_savings_pct"] = savings_pct
                it["_market_price"] = int(med)
                it["_below_market"] = savings_pct > 0

                deal_score += savings_pct * 3.0

        # Бонус за возраст: объявление давно висит → продавец готов к торгу
        # Новые (0-1 дней) — нейтрально. За каждый день после 2-го +1.5 балла, cap 45
        days = it.get("_days_on_site", 0)
        if days >= 2:
            deal_score += min((days - 1) * 1.5, 45.0)

        # Бонус за срочность в тексте: "срочно", "торг", "уступлю" и т.п.
        text_full = it.get("title", "") + " " + it.get("description", "")
        if HOT_WORDS.search(text_full):
            deal_score += 15.0

        # Небольшой бонус за наличие фото — реальное объявление
        if it.get("_photo_url") or it.get("photo_url") or it.get("_photos", 0) > 0:
            deal_score += 3.0

        # Штраф если цена выше рынка (не интересно)
        if savings_pct < -5:
            deal_score -= 20.0

        it["_deal_score"] = round(deal_score, 2)
        it["_hot_score"] = round(it.get("_hot_score", 0) + max(0, deal_score), 2)

    return items


def _sort_by_deal(items: list[dict]) -> list[dict]:
    """
    Идеальная сортировка: сначала самые выгодные + висящие дольше.

    Логика:
      Tier 0 — ниже рынка (savings_pct > 0):
        Ключ: -(savings_pct * 2 + age_bonus)
        age_bonus = min(days, 90) * 0.5   → макс 45 очков за 90 дней
        savings   = pct * 2               → -30% даёт 60 очков
        Смысл: среди одинакового % скидки тот, кто висит дольше, идёт первым.
        Пример: -25% 0 дней = 50 очков, -25% 30 дней = 65 очков → 30-дневный первый.
                -40% 0 дней = 80 очков → всё равно выше -25%, правильно.

      Tier 1 — по рынку или выше, но цена известна:
        Сортировка: дешевле → выше (покупатель ищет минимум)

      Tier 2 — цена неизвестна: в конец

      Tier 10 — уже просмотрено: самый конец
    """
    def _score(x) -> float:
        pct  = x.get("_savings_pct", 0) or 0
        days = x.get("_days_on_site", 0) or 0
        # Бонус за срочность/торг в тексте (уже посчитан в _deal_score)
        hot  = 10.0 if (x.get("_deal_score", 0) - pct * 3) > 10 else 0.0
        age_bonus = min(days, 90) * 0.5
        return pct * 2.0 + age_bonus + hot

    def _tier(x) -> int:
        if x.get("_already_seen"):
            return 10
        pct = x.get("_savings_pct", 0) or 0
        if pct > 0:
            return 0
        if x.get("_price_int", 0) > 0:
            return 1
        return 2

    items.sort(key=lambda x: (
        _tier(x),
        -_score(x) if _tier(x) == 0 else x.get("_price_int", 999_999_999),
    ))
    return items


# ── Парсер Дрома ────────────────────────────────────────────────

def parse_ru_date(text: str):
    if not text:
        return None
    text = text.strip()
    today = datetime.date.today()
    low = text.lower()
    if "сегодня" in low: return today
    if "вчера" in low: return today - datetime.timedelta(days=1)
    m = re.search(r"(\d+)\s+дн", low)
    if m: return today - datetime.timedelta(days=int(m.group(1)))
    if re.search(r"\d+\s+(час|мин)", low): return today
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", text, re.IGNORECASE)
    if m:
        day, mon_str = int(m.group(1)), MONTHS.get(m.group(2)[:3].lower())
        if mon_str:
            try: return datetime.date(today.year, mon_str, day)
            except ValueError: pass
    return None


def scrape_drom(region: str, pages: int = 15, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        import cloudscraper as _cs
        session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
    except ImportError:
        try:
            import requests as _req
            from bs4 import BeautifulSoup as _BS
            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
        except ImportError:
            return []

    results = []
    today = datetime.date.today()

    # Используем субдомен города — Дром автоматически показывает всю область
    base = f"https://{region}.drom.ru"

    for p in range(1, pages + 1):
        url = f"{base}/auto/all/" if p == 1 else f"{base}/auto/all/page{p}/"
        params = {}
        if price_min > 0:
            params["minprice"] = price_min
        if price_max < 99_000_000:
            params["maxprice"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            soup = _BS(r.text, "lxml")
            cards = soup.select("div[data-ftid='bulls-list_bull']")
            if not cards:
                break

            for card in cards:
                try:
                    link = card.select_one("a[data-ftid='bull_title']") or card.select_one("h3 a")
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else (base + href)

                    price_el = (
                        card.select_one("span[data-ftid='bull_price']")
                        or card.select_one("[class*='price']")
                        or card.select_one("span[class*='Price']")
                        or card.select_one("div[class*='price']")
                    )
                    price = price_el.get_text(strip=True) if price_el else ""
                    # Если цена пустая — ищем по regex в HTML карточки
                    if not price:
                        _card_html = str(card)
                        _pm = re.search(r'(\d[\d\s]{3,8})(?:\s*₽|\s*руб)', _card_html)
                        if _pm:
                            price = _pm.group(0).strip()

                    seller_el = (
                        card.select_one("[data-ftid='bull_seller']")
                        or card.select_one("a[class*='seller']")
                        or card.select_one("span[class*='seller']")
                    )
                    seller = seller_el.get_text(strip=True) if seller_el else ""

                    desc_el = card.select_one("[data-ftid='bull_description']")
                    desc = desc_el.get_text(strip=True) if desc_el else ""

                    date_el = (
                        card.select_one("[data-ftid='bull_date']")
                        or card.select_one("time")
                    )
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    photo_el = card.select_one("span[data-ftid='bull_images-count']")
                    photos_str = photo_el.get_text() if photo_el else ""
                    pm = re.search(r"\d+", photos_str)
                    photos = int(pm.group()) if pm else 0

                    photo_url = ""
                    _DROM_CDN = ("auto.drom.ru", "static.drom.ru", "st.drom.ru",
                                 "storage.drom.ru", "photo.drom.ru", "img.drom.ru")
                    # 1. picture > source (Drom lazy-load)
                    for src_el in card.find_all("source"):
                        for attr in ("data-srcset", "srcset", "data-src"):
                            val = src_el.get(attr, "")
                            if val:
                                url_part = val.split(",")[0].split(" ")[0].strip()
                                if url_part.startswith("http") and any(d in url_part for d in _DROM_CDN):
                                    photo_url = url_part
                                    break
                        if photo_url:
                            break
                    # 2. img tags — проверяем src, srcset, data-src
                    if not photo_url:
                        for img_el in card.find_all("img"):
                            # Берём первый непустой URL из всех атрибутов
                            for attr in ("src", "data-src", "data-lazy-src", "data-original", "srcset"):
                                raw = img_el.get(attr, "")
                                if raw:
                                    # srcset может быть "url 1x, url 2x"
                                    src = raw.split(",")[0].split(" ")[0].strip()
                                    if src and src.startswith("http") and len(src) > 20:
                                        if any(d in src for d in _DROM_CDN) or "drom" in src:
                                            photo_url = src
                                            break
                            if photo_url:
                                break
                    # 3. Regex fallback — любой URL на Drom CDN
                    if not photo_url:
                        card_str = str(card)
                        img_m = re.search(
                            r'https?://[^"\'<\s]*\.drom\.ru/[^"\'\s\\]{10,}\.(?:jpg|jpeg|webp|png)',
                            card_str
                        )
                        if not img_m:
                            img_m = re.search(
                                r'https?://[^"\']+(?:static|photo)[^"\']+\.(?:jpg|jpeg|webp)', card_str
                            )
                        if img_m:
                            photo_url = img_m.group(0).replace("\\/", "/")
                    # 4. noscript — Drom SSR кладёт реальный img в <noscript>
                    if not photo_url:
                        for ns in card.find_all("noscript"):
                            ns_html = str(ns)
                            _nm = re.search(
                                r'https?://[^"\'<\s\\]{10,}\.(?:jpg|jpeg|webp|png)',
                                ns_html
                            )
                            if _nm:
                                _cand = _nm.group(0).replace("\\/", "/")
                                if any(d in _cand for d in _DROM_CDN) or "drom" in _cand:
                                    photo_url = _cand
                                    break
                    # 5. JSON в script-тегах карточки (React hydration data)
                    if not photo_url:
                        for script_el in card.find_all("script"):
                            sc = script_el.string or ""
                            _sm = re.search(
                                r'https?://[^"\'<\s]*\.drom\.ru/[^"\'\s\\]{10,}\.(?:jpg|jpeg|webp|png)',
                                sc
                            )
                            if _sm:
                                photo_url = _sm.group(0).replace("\\/", "/")
                                break

                    if title and item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "drom",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": photos,
                            "_days_on_site": days,
                            "description": desc,
                            "seller": seller,
                            "_photo_url": photo_url,
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.05)
        except Exception as e:
            print(f"  [Дром {region}] стр.{p}: {e}")
            break

    return results


# ── Auto.ru geo IDs для API ──────────────────────────────────────
AUTORU_GEO_IDS = {
    "ekaterinburg": [56],    # Свердловская обл.
    "moscow":       [1],     # Москва
    "spb":          [10174], # Санкт-Петербург
    "novosibirsk":  [65],    # Новосибирская обл.
    "kazan":        [11119], # Татарстан
    "chelyabinsk":  [56088], # Челябинская обл.
    "ufa":          [102],   # Башкортостан
    "krasnodar":    [35],    # Краснодарский кр.
    "omsk":         [66],    # Омская обл.
    "tyumen":       [61],    # Тюменская обл.
    "perm":         [51],    # Пермский кр.
    "krasnoyarsk":  [54],    # Красноярский кр.
    "voronezh":     [193],   # Воронежская обл.
    "samara":       [11162], # Самарская обл.
    "rostov":       [39],    # Ростовская обл.
}

# ── Парсер Auto.ru ──────────────────────────────────────────────

def _autoru_parse_offers(data: dict, today) -> list[dict]:
    """Парсит список объявлений из JSON Auto.ru."""
    results = []
    listing = (
        data.get("listing", {}).get("data", {}).get("offers", [])
        or data.get("search", {}).get("offers", {}).get("offers", [])
        or data.get("offers", [])
    )
    for offer in listing:
        try:
            vehicle = offer.get("vehicle_info", {})
            mark = vehicle.get("mark_info", {}).get("name", "")
            model = vehicle.get("model_info", {}).get("name", "")
            year = offer.get("documents", {}).get("year", "")
            title = f"{mark} {model} {year}".strip()
            price_val = offer.get("price_info", {}).get("price", "")
            price_str = f"{int(price_val):,} ₽".replace(",", " ") if price_val else ""
            item_url = offer.get("url", "") or f"https://auto.ru/cars/used/sale/{offer.get('id', '')}"
            if offer.get("seller_type") == "COMMERCIAL":
                continue
            photos_list = offer.get("photos", [])
            photo_url = ""
            if photos_list:
                sizes = photos_list[0].get("sizes", {})
                photo_url = sizes.get("1200x900") or sizes.get("832x624") or sizes.get("456x342") or ""
            days = 0
            date_str = offer.get("additional_info", {}).get("creation_date", "")
            if date_str:
                try:
                    dt = datetime.datetime.fromisoformat(date_str[:10]).date()
                    days = max(0, (today - dt).days)
                except Exception:
                    pass
            desc = offer.get("description", "")[:300]
            tech = vehicle.get("tech_param", {})
            if tech and not desc:
                parts = [x for x in [tech.get("engine_type",""), f"{tech.get('power','')} л.с." if tech.get("power") else "", tech.get("transmission","")] if x]
                desc = ", ".join(parts)
            if title and item_url:
                price_int = int(price_val) if price_val else 0
                item = {
                    "source": "autoru", "title": title, "price": price_str,
                    "url": item_url, "date": str(today - datetime.timedelta(days=days)),
                    "_photos": len(photos_list), "_days_on_site": days,
                    "description": desc, "seller": "", "_photo_url": photo_url,
                    "_price_int": price_int,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass
    return results


def _autoru_parse_html(text: str, today) -> list[dict]:
    """Извлекает объявления из HTML Auto.ru (__INITIAL_STATE__ или regex)."""
    results = []

    # Метод 1: window.__INITIAL_STATE__
    for marker in ("window.__INITIAL_STATE__=", "window.__INITIAL_STATE__ ="):
        idx = text.find(marker)
        if idx == -1:
            continue
        brace_start = text.find("{", idx)
        if brace_start == -1:
            continue
        script_end = text.find("</script>", brace_start)
        json_str = text[brace_start:script_end].rstrip("; \n\r") if script_end != -1 else text[brace_start:brace_start + 800_000]
        try:
            data = json.loads(json_str)
            found = _autoru_parse_offers(data, today)
            if found:
                print(f"  [Auto.ru] __INITIAL_STATE__: {len(found)} объявлений")
                return found
        except Exception as e:
            print(f"  [Auto.ru] __INITIAL_STATE__ json error: {e}")

    # Метод 2: regex по паттернам Auto.ru в сыром HTML/JSON
    # Auto.ru URLs: https://auto.ru/cars/used/sale/brand/model/id/
    seen_urls: set[str] = set()
    for m in re.finditer(
        r'"url"\s*:\s*"(https://auto\.ru/cars/[^"]{10,120})"'
        r'.*?"price"\s*:\s*(\d{4,9})',
        text, re.DOTALL
    ):
        item_url = m.group(1)
        price_val = int(m.group(2))
        if item_url in seen_urls or price_val < 10_000:
            continue
        seen_urls.add(item_url)
        # Ищем марку/модель/год в блоке вокруг этого матча
        ctx_start = max(0, m.start() - 800)
        ctx = text[ctx_start:m.end() + 3000]
        mark_m = re.search(r'"mark_info"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', ctx)
        model_m = re.search(r'"model_info"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', ctx)
        year_m = re.search(r'"year"\s*:\s*(\d{4})', ctx)
        mark = mark_m.group(1) if mark_m else ""
        model = model_m.group(1) if model_m else ""
        year = year_m.group(1) if year_m else ""
        title = f"{mark} {model} {year}".strip() or "Авто на Auto.ru"
        price_str = f"{price_val:,} ₽".replace(",", " ")
        # Фото: ищем сначала 1200x900, потом любой размер из CDN Яндекса
        photo_m = re.search(r'"1200x900"\s*:\s*"([^"]+)"', ctx)
        if not photo_m:
            photo_m = re.search(r'"(?:832x624|456x342|320x240)"\s*:\s*"([^"]+)"', ctx)
        if not photo_m:
            photo_m = re.search(r'"((?:https?:)?//avatars\.mds\.yandex\.net/[^"]{10,})"', ctx)
        photo_url = photo_m.group(1).replace("\\/", "/") if photo_m else ""
        if photo_url.startswith("//"):
            photo_url = "https:" + photo_url
        desc_m = re.search(r'"description"\s*:\s*"([^"]{10,400})"', text[max(0,m.start()-2000):m.end()+3000])
        desc = desc_m.group(1).replace("\\n", " ").strip() if desc_m else ""
        item = {
            "source": "autoru", "title": title, "price": price_str,
            "url": item_url, "date": str(today),
            "_photos": 1 if photo_url else 0, "_days_on_site": 0,
            "description": desc, "seller": "", "_photo_url": photo_url,
            "_price_int": price_val,
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)

    if results:
        print(f"  [Auto.ru] regex HTML: {len(results)} объявлений")
    return results


def scrape_autoru(region: str, pages: int = 10, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AUTORU_SLUGS.get(region, region)
    geo_ids = AUTORU_GEO_IDS.get(region, [])
    try:
        import requests as _req
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    # Метод 1: AJAX API Auto.ru (наиболее надёжный, возвращает JSON)
    headers_ajax = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://auto.ru",
        "Referer": f"https://auto.ru/{slug}/cars/used/",
        "x-client-app": "autoru-frontend-application",
        "x-page-request-id": "ajax",
    }

    for p in range(1, pages + 1):
        body: dict = {
            "category": "cars", "section": "used",
            "seller_type": ["PRIVATE"], "page": p, "page_size": 37,
            "sort": "fresh_relevance_1-desc",
            "output_type": "list",
        }
        if geo_ids:
            body["geo_id"] = geo_ids
        if price_min > 0:
            body["price_from"] = price_min
        if price_max < 99_000_000:
            body["price_to"] = price_max

        batch = []
        html_url = f"https://auto.ru/{slug}/cars/used/?seller_group=PRIVATE&page={p}&sort=fresh_relevance_1-desc"
        if price_min > 0:
            html_url += f"&price_from={price_min}"
        if price_max < 99_000_000:
            html_url += f"&price_to={price_max}"

        # Метод 0а: Прямой AJAX API с прокси (наиболее надёжный при наличии РФ IP)
        if not batch and AVITO_PROXIES:
            try:
                r_ajax = _req.post(
                    "https://auto.ru/-/ajax/desktop/listing/",
                    json=body,
                    headers={**headers_ajax, "x-requested-with": "fetch"},
                    proxies=_avito_proxies(),
                    timeout=20,
                )
                print(f"  [Auto.ru] прокси AJAX стр.{p}: HTTP {r_ajax.status_code}, {len(r_ajax.text):,}б")
                if r_ajax.status_code == 200:
                    try:
                        batch = _autoru_parse_offers(r_ajax.json(), today)
                    except Exception:
                        batch = _autoru_parse_html(r_ajax.text, today)
            except Exception as e:
                print(f"  [Auto.ru] прокси AJAX: {str(e)[:80]}")

        # Метод 0b: Прямой HTML через прокси (РФ IP, обходит гео-блок)
        if not batch and AVITO_PROXIES:
            try:
                r0 = _req.get(html_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": f"https://auto.ru/{slug}/cars/used/",
                }, timeout=25, proxies=_avito_proxies())
                print(f"  [Auto.ru] прокси HTML стр.{p}: HTTP {r0.status_code}, {len(r0.text):,}б")
                if r0.status_code == 200 and len(r0.text) > 50_000:
                    batch = _autoru_parse_html(r0.text, today)
            except Exception as e:
                print(f"  [Auto.ru] прокси HTML: {str(e)[:50]}")

        # Метод 0c: curl_cffi — Chrome TLS fingerprint, без зависимости от прокси
        if not batch:
            try:
                from curl_cffi import requests as _cffi
                _cffi_hdrs0 = {
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": f"https://auto.ru/{slug}/cars/used/",
                }
                rc0 = _cffi.get(html_url, impersonate="chrome124", timeout=15, headers=_cffi_hdrs0,
                                proxies=_avito_proxies())
                print(f"  [Auto.ru] curl_cffi стр.{p}: HTTP {rc0.status_code}, {len(rc0.text):,}б")
                if rc0.status_code == 200 and len(rc0.text) > 30_000:
                    batch = _autoru_parse_html(rc0.text, today)
            except Exception as e:
                print(f"  [Auto.ru] curl_cffi: {str(e)[:80]}")

        # Метод 1: ScraperAPI render=true — JS выполняется, __INITIAL_STATE__ заполняется
        if not batch and SCRAPER_API_KEY:
            try:
                r3 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": html_url,
                    "country_code": "ru", "render": "true", "wait": "1500",
                }, timeout=40)
                print(f"  [Auto.ru] ScraperAPI render стр.{p}: HTTP {r3.status_code}, {len(r3.text):,}б")
                if r3.status_code == 200 and len(r3.text) > 100_000:
                    batch = _autoru_parse_html(r3.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI render: {e}")

        # Метод 2: ScraperAPI без render (быстрее)
        if not batch and SCRAPER_API_KEY:
            try:
                r4 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": html_url,
                    "country_code": "ru", "premium": "true",
                }, timeout=60)
                print(f"  [Auto.ru] ScraperAPI HTML стр.{p}: HTTP {r4.status_code}, {len(r4.text):,}б")
                if r4.status_code == 200 and len(r4.text) > 50_000:
                    batch = _autoru_parse_html(r4.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI HTML: {e}")

        # Метод 3: ScraperAPI → AJAX POST
        if not batch and SCRAPER_API_KEY:
            try:
                r = _req.post(
                    "http://api.scraperapi.com/",
                    params={"api_key": SCRAPER_API_KEY, "url": "https://auto.ru/-/ajax/desktop/listing/", "country_code": "ru"},
                    data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=40
                )
                print(f"  [Auto.ru] ScraperAPI AJAX стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200:
                    try:
                        batch = _autoru_parse_offers(r.json(), today)
                    except Exception:
                        batch = _autoru_parse_html(r.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI AJAX: {e}")

        print(f"  [Auto.ru] стр.{p}: итого {len(batch)} объявлений")
        if not batch:
            break
        results.extend(batch)
        time.sleep(0.05)

    print(f"  [Auto.ru] итого {len(results)} объявлений")
    return results


# ── Парсер Kolesa.ru ────────────────────────────────────────────

KOLESA_SLUGS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov-na-donu",
}


def scrape_kolesa(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = KOLESA_SLUGS.get(region, region)
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    today = datetime.date.today()
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*",
    })

    for p in range(1, pages + 1):
        # Kolesa.ru: частники = seller=1, регион через city[]
        url = f"https://kolesa.ru/cars/"
        params: dict = {"city[]": slug, "seller": "1", "page": p}
        if price_min > 0:
            params["price[from]"] = price_min
        if price_max < 99_000_000:
            params["price[to]"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                break
            soup = _BS(r.text, "lxml")

            # Kolesa использует article.a-card или div.a-list__item
            cards = (
                soup.select("article.a-card")
                or soup.select("div.a-list__item")
                or soup.select("div[class*='listing-item']")
                or soup.select("li[data-id]")
            )
            if not cards:
                print(f"  [Kolesa {region}] стр.{p}: нет карточек (HTTP {r.status_code}, {len(r.text)} байт)")
                break

            for card in cards:
                try:
                    link = (
                        card.select_one("a.a-card__title")
                        or card.select_one("a[class*='title']")
                        or card.select_one("h5 a")
                        or card.select_one("h2 a")
                        or card.select_one("a[href*='/cars/']")
                    )
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://kolesa.ru" + href)

                    price_el = (
                        card.select_one(".a-card__price")
                        or card.select_one("[class*='price']")
                    )
                    price = price_el.get_text(strip=True) if price_el else ""

                    date_el = card.select_one("[class*='date']") or card.select_one("time")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date_obj = parse_ru_date(date_text)
                    days = max(0, (today - date_obj).days) if date_obj else 0

                    desc_el = (
                        card.select_one(".a-card__description")
                        or card.select_one("[class*='descr']")
                        or card.select_one("[class*='description']")
                    )
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    img_el = card.select_one("img[data-src]") or card.select_one("img[src]")
                    photo_url = ""
                    if img_el:
                        src = img_el.get("data-src") or img_el.get("src", "")
                        if src and src.startswith("http"):
                            photo_url = src

                    if title and item_url and "kolesa.ru" in item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "kolesa", "title": title, "price": price,
                            "url": item_url, "date": str(date_obj) if date_obj else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": photo_url,
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.2)
        except Exception as e:
            print(f"  [Kolesa {region}] стр.{p}: {e}")
            break

    print(f"  [Kolesa] {len(results)} объявлений")
    return results


# ── Парсер Bibika.ru ─────────────────────────────────────────────

BIBIKA_REGIONS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moscow",
    "spb":          "spb",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov",
}


def scrape_bibika(region: str, pages: int = 3, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = BIBIKA_REGIONS.get(region, region)
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    today = datetime.date.today()
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })

    for p in range(1, pages + 1):
        # bibika.ru: частные объявления
        url = f"https://bibika.ru/auto/{slug}/"
        params: dict = {"page": p, "private": "1"}
        if price_min > 0:
            params["price_from"] = price_min
        if price_max < 99_000_000:
            params["price_to"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  [Bibika {region}] HTTP {r.status_code}")
                break
            soup = _BS(r.text, "lxml")

            cards = (
                soup.select("div.bull-item")
                or soup.select(".auto-item")
                or soup.select("article.car-item")
                or soup.select("div[itemtype*='Product']")
                or soup.select("[class*='auto-item']")
            )
            if not cards:
                print(f"  [Bibika {region}] стр.{p}: нет карточек")
                break

            for card in cards:
                try:
                    link = (
                        card.select_one("a[href*='/auto/']")
                        or card.select_one("h2 a")
                        or card.select_one("h3 a")
                    )
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://bibika.ru" + href)

                    price_el = card.select_one("[class*='price']") or card.select_one("[itemprop='price']")
                    price = price_el.get("content") or price_el.get_text(strip=True) if price_el else ""

                    date_el = card.select_one("[class*='date']") or card.select_one("time")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    desc_el = (
                        card.select_one("[class*='descr']")
                        or card.select_one("[class*='description']")
                        or card.select_one("[itemprop='description']")
                        or card.select_one("p")
                    )
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    if title and item_url and "bibika.ru" in item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "bibika", "title": title, "price": price,
                            "url": item_url, "date": str(date) if date else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": "",
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.2)
        except Exception as e:
            print(f"  [Bibika {region}] стр.{p}: {e}")
            break

    print(f"  [Bibika] {len(results)} объявлений")
    return results


# ── ВКонтакте: паблики авто-барахолок по городам ─────────────────
VK_AUTO_GROUPS = {
    "ekaterinburg": [
        # Проверенные реальные паблики
        "kareta96", "podjopnik96",
        # Авто барахолки Екб
        "avtobaraholka96", "prodamavto96", "avto_ekb96", "ekbauto", "club_avto_ekb",
        "autobazar_ekb", "avtomobileekb", "club196avto", "avto_yekb", "prodamavtoe96",
        "avtodo100_ekb", "avto_do300_ekb", "avto_do500_ekb", "srochno_avto96",
        "avtobazar96", "kupit_avto_ekb", "avto96_prodazha", "ekb_auto_sell",
        "avto_ur", "avto_sverdlovsk", "avtorynok_ekb", "deshevo_avto96",
        # Дополнительные паттерны
        "avto_ekaterinburg", "ekaterinburg_avto", "prodazha_avto_ekb",
        "avto_eburg", "sverdlovsk_avto", "eburg_avto96", "avto_market96",
        "avto_do200_ekb", "kuplu_avto_ekb", "bazar_avto_ekb", "avto_ekb_cheap",
        "avtosalon_ekb", "carbazar96", "ekb_carbuy", "avto_sverd96",
    ],
    "moskva": [
        "kareta77", "podjopnik77",
        "avtobaraholkamsk", "prodamavtomsk", "autobazar_msk", "avtoclub77",
        "moscowcars", "kupit_avto_msk", "avto_moskva77", "avtomoskva77",
        "avtodo100_msk", "avto_do300_msk", "srochno_avto77", "avto_bazar_msk",
        "avtorynok_msk", "deshevo_avto77", "avto_msk_prodazha", "buy_car_msk",
        "avto_moskva_prodazha", "moskva_avto_sale", "avto_market77",
        "prodazha_avto_msk", "carbazar77", "avto_cheap_msk", "avtotorg77",
    ],
    "spb": [
        "kareta78", "podjopnik78",
        "avtobaraholkaspb", "prodamavtospb", "autobazar_spb", "avto78spb",
        "avtoclub78", "spbavto78", "prodamavtopiter",
        "avtodo100_spb", "avto_do300_spb", "srochno_avto78", "avto78_prodazha",
        "avtorynok_spb", "deshevo_avto78", "spb_auto_sell",
        "avto_spb_prodazha", "spb_avto_sale", "carbazar78", "avtotorg78",
        "prodazha_avto_spb", "piter_avto78",
    ],
    "novosibirsk": [
        "kareta54", "podjopnik54",
        "avtobaraholka54", "prodamavto54", "autobazar_nsk", "avtonsk54",
        "avto_novosibirsk", "club54avto", "nsk_avto54",
        "avtodo100_nsk", "avto_do300_nsk", "srochno_avto54", "nsk_auto_sell",
        "avtorynok_nsk", "deshevo_avto54",
        "avto_nsk_prodazha", "nsk_avto_sale", "carbazar54", "avtotorg54",
        "prodazha_avto_nsk", "novosibirsk_avto_sale",
    ],
    "kazan": [
        "kareta16", "podjopnik16",
        "avtobaraholkakazan", "prodamavtokazan", "avtoclub16", "kazan_avto16",
        "autobazar_kazan", "avto_kazan16",
        "avtodo100_kazan", "avto_do300_kazan", "srochno_avto16", "kazan_auto_sell",
        "avtorynok_kazan", "deshevo_avto16",
        "avto_kazan_prodazha", "kazan_avto_sale", "carbazar16", "avtotorg16",
    ],
    "chelyabinsk": [
        "kareta74", "podjopnik74",
        "avtobaraholka74", "prodamavto74", "avto74chel", "autobazar_chel",
        "club74avto", "avtoclub74",
        "avtodo100_chel", "avto_do300_74", "srochno_avto74", "chel_auto_sell",
        "avtorynok_chel", "deshevo_avto74",
        "avto_chel_prodazha", "chel_avto_sale", "carbazar74", "avtotorg74",
        "prodazha_avto_chel",
    ],
    "ufa": [
        "kareta02", "podjopnik02",
        "avtobaraholkaufa", "prodamavtoufa", "avto02ufa", "autobazar_ufa",
        "avtoclub02", "ufa_avto02",
        "avtodo100_ufa", "avto_do300_ufa", "srochno_avto02", "ufa_auto_sell",
        "avtorynok_ufa", "deshevo_avto02",
        "avto_ufa_prodazha", "ufa_avto_sale", "carbazar02", "avtotorg02",
    ],
    "krasnodar": [
        "kareta23", "podjopnik23",
        "avtobaraholkakrd", "prodamavtokrd", "avto23krd", "autobazar_krasnodar",
        "avtoclub23", "krasnodar_avto23", "kuban_avto",
        "avtodo100_krd", "avto_do300_krd", "srochno_avto23", "krd_auto_sell",
        "avtorynok_krd", "deshevo_avto23", "kuban_avto_sell",
        "avto_krd_prodazha", "krd_avto_sale", "carbazar23", "avtotorg23",
        "kuban_carbazar", "avto_kuban_sale",
    ],
    "omsk": [
        "kareta55", "podjopnik55",
        "avtobaraholkaomsk", "prodamavtoomsk", "avto55omsk", "autobazar_omsk",
        "club55avto", "omsk_avto55",
        "avtodo100_omsk", "avto_do300_omsk", "srochno_avto55", "omsk_auto_sell",
        "avtorynok_omsk", "deshevo_avto55",
        "avto_omsk_prodazha", "omsk_avto_sale", "carbazar55", "avtotorg55",
    ],
    "rostov": [
        "kareta61", "podjopnik61",
        "avtobaraholkarostov", "prodamavtorostov", "avto61rostov", "autobazar_rostov",
        "avtoclub61", "rostov_avto61",
        "avtodo100_rostov", "avto_do300_61", "srochno_avto61", "rostov_auto_sell",
        "avtorynok_rostov", "deshevo_avto61",
        "avto_rostov_prodazha", "rostov_avto_sale", "carbazar61", "avtotorg61",
    ],
    "tyumen": [
        "kareta72", "podjopnik72",
        "avtobaraholkatyumen", "prodamavtotmn", "avto72tyumen", "autobazar_tyumen",
        "tyumen_avto72",
        "avtodo100_tmn", "avto_do300_tmn", "srochno_avto72", "tmn_auto_sell",
        "avtorynok_tmn", "deshevo_avto72", "tyumen_auto_sell",
        "avto_tmn_prodazha", "tyumen_avto_sale", "carbazar72", "avtotorg72",
    ],
    "samara": [
        "kareta63", "podjopnik63",
        "avtobaraholkasamara", "prodamavtosmr", "avto63samara", "autobazar_samara",
        "samara_avto63",
        "avtodo100_samara", "avto_do300_63", "srochno_avto63", "samara_auto_sell",
        "avtorynok_samara", "deshevo_avto63",
        "avto_samara_prodazha", "samara_avto_sale", "carbazar63", "avtotorg63",
    ],
    "krasnoyarsk": [
        "kareta24", "podjopnik24",
        "avtobaraholkakrs", "prodamavtokrs", "avto24krsk", "autobazar_krs",
        "krasnoyarsk_avto24",
        "avtodo100_krs", "avto_do300_24", "srochno_avto24", "krs_auto_sell",
        "avtorynok_krs", "deshevo_avto24",
        "avto_krs_prodazha", "krs_avto_sale", "carbazar24", "avtotorg24",
    ],
    "nn": [
        "kareta52", "podjopnik52",
        "avtobaraholkann", "prodamavtonn", "avto52nn", "autobazar_nn", "nn_avto52",
        "avtodo100_nn", "avto_do300_nn", "srochno_avto52", "nn_auto_sell",
        "avtorynok_nn", "deshevo_avto52",
        "avto_nn_prodazha", "nn_avto_sale", "carbazar52", "avtotorg52",
    ],
    "perm": [
        "kareta59", "podjopnik59",
        "avtobaraholkaperm", "prodamavtoperm", "avto59perm", "autobazar_perm",
        "avtodo100_perm", "avto_do300_perm", "srochno_avto59", "perm_auto_sell",
        "avtorynok_perm", "deshevo_avto59",
        "avto_perm_prodazha", "perm_avto_sale", "carbazar59", "avtotorg59",
    ],
    "voronezh": [
        "kareta36", "podjopnik36",
        "avtobaraholkavrn", "prodamavtovrn", "avto36voronezh", "autobazar_vrn",
        "avtodo100_vrn", "avto_do300_vrn", "srochno_avto36", "vrn_auto_sell",
        "avtorynok_vrn", "deshevo_avto36",
    ],
    "irkutsk": [
        "kareta38", "podjopnik38",
        "avtobaraholka38", "prodamavto38", "avto38irkutsk", "autobazar_irkutsk",
        "avtodo100_irk", "irkutsk_auto_sell", "avtorynok_irkutsk",
    ],
    "volgograd": [
        "kareta34", "podjopnik34",
        "avtobaraholka34", "prodamavto34", "avto34vgd", "autobazar_vgd",
        "avtodo100_vgd", "volgograd_auto_sell", "avtorynok_vgd",
    ],
}

# ── Парсер Telegram-каналов автопродаж ──────────────────────────

TG_AUTO_CHANNELS = {
    "ekaterinburg": [
        "avto_ekb", "prodamavto_ekb", "avtoekb", "kupit_avto_ekb", "avto96ekb",
        "baraholka_avto_ekb", "avtobazar_ekb", "ekb_avto96", "avto_yekaterinburg",
        "prodamauto96", "avto_baraholka96", "ekbauto2024",
        "avtodo100_ekb", "avto_do300ekb", "srochno_avto96", "avto_ur96",
        "avtomobile96", "avto_sverdl", "car_ekb", "avto_ekb_96",
    ],
    "moskva": [
        "avto_msk", "prodamavto_msk", "avtomoskva", "kupit_avto_msk", "avto_moskva",
        "cars_msk", "avtobazar_msk", "prodamauto_msk", "avto_baraholka_msk",
        "moscowcars2024", "avto77msk", "moskva_avto77",
        "avtodo100_msk", "avto_do300msk", "srochno_avto77", "avtomsk77",
        "car_moscow", "avto_msk77", "avtorynok77",
    ],
    "spb": [
        "avto_spb", "prodamavto_spb", "avtospb", "avto78spb", "cars_spb",
        "avtobazar_spb", "prodamauto_spb", "avto_baraholka_spb", "spb_avto78",
        "avto78_prodazha", "spbauto2024",
        "avtodo100_spb", "avto_do300spb", "srochno_avto78", "car_spb78",
    ],
    "novosibirsk": [
        "avto_nsk", "prodamavto_nsk", "avtonsk54", "cars_nsk", "avtobazar_nsk",
        "prodamauto_nsk", "avto_baraholka54", "nsk_avto54", "novosibirsk_avto",
        "avtodo100_nsk", "avto_do300nsk", "srochno_avto54", "car_nsk54",
        "avtorynok_nsk",
    ],
    "kazan": [
        "avto_kazan", "prodamavto_kazan", "avtokazan16", "avtobazar_kazan",
        "kazan_avto16", "prodamauto_kazan", "avto_baraholka_kazan",
        "avtodo100_kazan", "avto_do300kazan", "srochno_avto16", "car_kazan16",
    ],
    "krasnodar": [
        "avto_krd", "prodamavto_krd", "avto23krd", "avtobazar_krasnodar",
        "krasnodar_avto23", "prodamauto_krd", "avto_baraholka_krd", "kuban_cars",
        "avtodo100_krd", "avto_do300krd", "srochno_avto23", "car_krd23",
        "avto_kuban", "kuban_avto_sell",
    ],
    "chelyabinsk": [
        "avto_chel", "prodamavto_chel", "avto74chel", "avtobazar_chel",
        "chel_avto74", "prodamauto_chel", "avto_baraholka74",
        "avtodo100_chel", "avto_do300chel", "srochno_avto74", "car_chel74",
    ],
    "ufa": [
        "avto_ufa", "prodamavto_ufa", "avto02ufa", "avtobazar_ufa",
        "ufa_avto02", "prodamauto_ufa", "avto_baraholka_ufa",
        "avtodo100_ufa", "avto_do300ufa", "srochno_avto02", "car_ufa02",
    ],
    "omsk": [
        "avto_omsk", "prodamavto_omsk", "avto55omsk", "avtobazar_omsk",
        "omsk_avto55", "prodamauto_omsk", "avto_baraholka55",
        "avtodo100_omsk", "avto_do300omsk", "srochno_avto55", "car_omsk55",
    ],
    "rostov": [
        "avto_rostov", "prodamavto_rostov", "avto61rostov", "avtobazar_rostov",
        "rostov_avto61", "prodamauto_rostov", "avto_baraholka_rostov",
        "avtodo100_rostov", "avto_do300rostov", "srochno_avto61", "car_rostov61",
    ],
    "tyumen": [
        "avto_tyumen", "prodamavto72", "avto72tyumen", "tyumen_avto", "cars_tyumen",
        "avtobazar_tyumen", "prodamauto72", "avto_baraholka72",
        "avtodo100_tmn", "avto_do300tmn", "srochno_avto72", "car_tyumen72",
        "avto_tmn72", "tyumen_car_sell",
    ],
    "samara": [
        "avto_samara", "prodamavto63", "avto63samara", "avtobazar_samara",
        "samara_avto63", "prodamauto63", "avto_baraholka63",
        "avtodo100_samara", "avto_do300samara", "srochno_avto63", "car_samara63",
    ],
    "volgograd": [
        "avto_volgograd", "prodamavto34", "avto34vlg", "avtobazar_volgograd",
        "volgograd_avto34", "prodamauto34",
        "avtodo100_vgd", "avto_do300vgd", "srochno_avto34", "car_vgd34",
    ],
    "perm": [
        "avto_perm", "prodamavto59", "avto59perm", "avtobazar_perm",
        "perm_avto59", "prodamauto59", "avto_baraholka59",
        "avtodo100_perm", "avto_do300perm", "srochno_avto59", "car_perm59",
    ],
    "voronezh": [
        "avto_voronezh", "prodamavto36", "avto36vrn", "avtobazar_voronezh",
        "voronezh_avto36", "prodamauto36",
        "avtodo100_vrn", "avto_do300vrn", "srochno_avto36", "car_vrn36",
    ],
    "saratov": [
        "avto_saratov", "prodamavto64", "avto64sar", "avtobazar_saratov",
        "saratov_avto64", "avtodo100_sar", "car_saratov64",
    ],
    "krasnoyarsk": [
        "avto_krsk", "prodamavto24", "avto24krsk", "avtobazar_krs",
        "krasnoyarsk_avto24", "prodamauto24",
        "avtodo100_krs", "avto_do300krs", "srochno_avto24", "car_krs24",
    ],
    "irkutsk": [
        "avto_irkutsk", "prodamavto38", "avto38irk", "avtobazar_irkutsk",
        "irkutsk_avto38", "avtodo100_irk", "avto_do300irk", "car_irkutsk38",
    ],
    "vladivostok": [
        "avto_vladivostok", "prodamavto25", "avto25vlad", "avtobazar_vlad",
        "vladivostok_avto25", "avtodo100_vlad", "car_vlad25",
    ],
    "habarovsk": [
        "avto_habarovsk", "prodamavto27", "avto27hab", "avtobazar_hab",
        "habarovsk_avto27", "avtodo100_hab", "car_hab27",
    ],
    "nn": [
        "avto_nn", "prodamavto52", "avto52nn", "avtobazar_nn",
        "nn_avto52", "prodamauto52", "avto_baraholka52",
        "avtodo100_nn", "avto_do300nn", "srochno_avto52", "car_nn52",
    ],
}

# Маппинг слагов регионов бота → ключи TG_AUTO_CHANNELS
_TG_REGION_MAP = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moskva",
    "spb":          "spb",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "rostov":       "rostov",
    "tyumen":       "tyumen",
    "samara":       "samara",
    "volgograd":    "volgograd",
    "perm":         "perm",
    "voronezh":     "voronezh",
    "saratov":      "saratov",
    "krasnoyarsk":  "krasnoyarsk",
    "irkutsk":      "irkutsk",
    "vladivostok":  "vladivostok",
    "habarovsk":    "habarovsk",
    "nn":           "nn",
}

_TG_PRICE_RE = re.compile(
    r"(\d[\d\s]{2,10})\s*(?:₽|тыс\.?\s*р(?:уб)?|руб|р\.)",
    re.IGNORECASE,
)
_TG_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")
_TG_MILEAGE_RE = re.compile(r"(\d[\d\s]{2,6})\s*(?:тыс\.?\s*км|км)", re.IGNORECASE)


def _tg_parse_price(text: str) -> int:
    """Извлекает цену из текста Telegram-объявления."""
    for m in _TG_PRICE_RE.finditer(text):
        raw = re.sub(r"\D", "", m.group(1))
        if not raw:
            continue
        val = int(raw)
        # «тыс.» суффикс — умножаем
        suffix = m.group(0)[len(m.group(1)):].strip().lower()
        if "тыс" in suffix:
            val = val * 1000
        if 50_000 <= val <= 50_000_000:
            return val
    return 0


_SOCIAL_SALE_KEYWORDS = [
    "продам", "продаю", "продаётся", "продается", "куплю", "в продаже",
    "выставил на продажу", "выставляю", "меняю", "обмен", "отдам за",
    "уступлю", "торг уместен", "срочно продам", "срочная продажа",
]

# Конкретные идентификаторы автомобиля — "авто" и "машин" сюда НЕ входят (слишком общие)
_SOCIAL_CAR_STRONG = [
    "автомобил", "пробег", "двигател", "кузов", "тыс.км", "тыс км", "т.км",
    "год выпуска", "г.в.", "г/в", "год вып", "объём", "об.", "литр",
    "toyota", "honda", "kia", "hyundai", "nissan", "mazda", "bmw", "audi", "mercedes",
    "lada", "ваз", "vaz", "haval", "geely", "chery", "skoda", "volkswagen", "vw",
    "renault", "peugeot", "ford", "opel", "chevrolet", "mitsubishi", "subaru",
    "lexus", "infiniti", "volvo", "land rover", "jeep", "suzuki", "datsun",
    "changan", "exeed", "omoda", "tank", "jaecoo", "byd", "lixiang", "москвич",
    "нива", "приора", "гранта", "калина", "largus", "vesta", "xray",
    # Русские названия марок
    "митсубиши", "митсубиси", "лексус", "инфинити", "субару", "сузуки",
    "пежо", "ситроен", "вольво", "шевроле", "опель", "форд", "рено",
    "шкода", "хонда", "мазда", "ниссан", "тойота", "киа", "хёндай",
    "хендай", "бмв", "ауди", "фольксваген", "мерседес", "уаз", "газ",
]
_SOCIAL_REJECT_KEYWORDS = [
    # Недвижимость
    "квартир", "комнат", "сдаётся", "сдается", "сдам", "аренд", "съём", "съем",
    "недвижимост", "студи", "апартамент",
    # Услуги
    "перевозк", "пассажирск", "грузоперевозк", "рейс", "маршрут", "такси",
    # Не машина: госномера, запчасти, автозвук, резина, детали
    "госномер", "гос. номер", "номерной знак", "красивый номер", "продам номер",
    "эксклюзив. номер", "эксклюзивный номер", "регистрационный номер",
    "номер на гелик", "номер на мерс", "номер на авто", "идеальный номер",
    "подчеркнет статус", "номер авт", "автономер",
    "запчаст", "автозапчаст", "разбор", "на разбор", "на запчаст",
    "шин", "резин", "покрышк", "колес", "колёс", "диски", "диск р", "диск на",
    " шт.", "шт,", " шт\n",                        # «4 шт.» — детали поштучно
    "на ваз ", "на lada ", "запчасти на",           # «на ВАЗ 4 шт» — для другого авто
    "сабвуфер", "сабвуф", "автозвук", "усилитель", "магнитол", "колонки", "автоакустик",
    "бампер", "фара", "крыло", "капот", "зеркало", "стекло лобов",
    "масло моторн", "антифриз", "автохимия", "тормозн",
    # Спам и нерелевант
    "лайфхак", "новост", "зафиксировал", "камер зафиксир", "нарушени",
    "штраф", "гибдд фиксир", "корги", "собак", "животн",
    "реклам", "подпишись", "заработ", "казино", "ставк",
    "пресс-релиз", "подписчик",
    # Правила/описание каналов и групп — не объявления
    "правила группы", "правила канала", "правила чата", "платформа размещения",
    "регистрация в ркн", "администратор", "@tut_admin", "другие города",
    "не проходят ссылк", "поддержку, развитие", "поддержку развитие",
    "доска объявлений", "барахолка", "обратная связь бота", "бот поддержки",
    "вступить в группу", "вступить в чат",
    # Новости о ценах на топливо/бензин — не продажа авто
    "цены на бензин", "стоимость бензина", "цена бензина", "бензин подорожа",
    "бензин подешев", "цены на топливо", "цены на азс", "заправка дорожает",
    "лихорадит цены", "горожане обсуждают", "цены на нефть",
    "аи-92", "аи-95", "аи-98", "дизельное топлив",
    # Другие новости и нерелевантный контент
    "читайте также", "подробнее на сайте", "источник:", "по данным",
    "сообщает корреспондент", "по информации", "как сообщает",
]

def _is_car_sale_social(text: str) -> bool:
    """Возвращает True только если текст — объявление о продаже/покупке именно автомобиля."""
    tl = text.lower()
    if any(rk in tl for rk in _SOCIAL_REJECT_KEYWORDS):
        return False
    has_sale = any(sk in tl for sk in _SOCIAL_SALE_KEYWORDS)
    # Нужен «сильный» идентификатор (марка/модель/пробег/год) — "авто" в контексте "на вашем авто" не считается
    has_car = any(ck in tl for ck in _SOCIAL_CAR_STRONG)
    return has_sale and has_car

_SOCIAL_TITLE_RE = re.compile(
    r"(toyota|honda|kia|hyundai|nissan|mazda|bmw|audi|mercedes|lada|ваз|haval|geely|chery|skoda|volkswagen|vw|renault|peugeot|ford|opel|chevrolet|mitsubishi|subaru|lexus|infiniti|volvo|jeep|suzuki|datsun|changan|exeed|omoda|tank|jaecoo|byd|нива|приора|гранта|калина|vesta|largus|xray|москвич)",
    re.IGNORECASE,
)
_SOCIAL_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")

def _social_make_title(text: str) -> str:
    """Строит краткий title для VK/TG поста: марка + год + первые слова."""
    first_line = text.split("\n")[0].strip()
    # Если первая строка содержит марку — используем её
    if _SOCIAL_TITLE_RE.search(first_line):
        return first_line[:100]
    # Иначе ищем марку в тексте и строим: "Марка, год — ..."
    brand_m = _SOCIAL_TITLE_RE.search(text)
    year_m = _SOCIAL_YEAR_RE.search(text)
    if brand_m:
        brand = brand_m.group(0).upper() if len(brand_m.group(0)) <= 3 else brand_m.group(0).title()
        year = f" {year_m.group(1)}" if year_m else ""
        return f"{brand}{year} — {first_line[:60]}"
    return first_line[:100] or text[:100]


def scrape_tg_channels(region: str, price_min: int, price_max: int) -> list[dict]:
    """
    Ищет объявления о продаже авто в Telegram-каналах города.
    Стратегия: пробуем реальные публичные каналы через t.me/s/,
    если не находим — ищем через Yandex.
    """
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    city_key = _TG_REGION_MAP.get(region, "")

    # Создаём сессию с русским прокси (тот же что и для Авито)
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    _tg_proxy_url = (
        os.getenv("PROXY_URL") or
        os.getenv("AVITO_PROXY_URL") or
        (
            f"http://{AVITO_PROXY_USER}:{AVITO_PROXY_PASS}@{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}"
            if AVITO_PROXY_HOST and AVITO_PROXY_USER and not _proxy_auth_failed
            else ""
        )
    )
    if _tg_proxy_url and "__agentproxy" not in _tg_proxy_url:
        session.proxies.update({"http": _tg_proxy_url, "https": _tg_proxy_url})

    # Реальные публичные TG каналы продажи авто по городам
    # Источник: t.me/s/ — только каналы с открытым веб-просмотром
    TG_REAL_CHANNELS: dict[str, list[str]] = {
        "ekaterinburg": [
            "avto_ekb", "ekb_auto", "auto_ekb96", "prodau96",
            "ekb_avto", "avtomobili_ekb", "prodamavto_ekb",
            "avto96", "ekbauto96", "auto_eburg", "avtoekb",
        ],
        "moskva": [
            "avto_msk", "moscowavto", "auto_moskva", "avto77msk",
            "prodamavto_msk", "avtomobili_msk", "avto_moscow",
            "autoru_msk", "prodauto_msk", "avtorynok_msk",
        ],
        "spb": [
            "avto_spb", "spb_avto", "auto_spb78", "prodamavto_spb",
            "avtomobili_spb", "avto78spb", "autospb", "avto_piter",
        ],
        "novosibirsk": [
            "avto_nsk", "nsk_avto", "auto_novosibirsk",
            "prodamavto_nsk", "avtomobili_nsk", "avto54nsk",
        ],
        "kazan": [
            "avto_kazan", "kazan_avto", "auto_kzn",
            "prodamavto_kzn", "avtomobili_kazan",
        ],
        "chelyabinsk": [
            "avto_chel", "chel_avto", "auto74chel",
            "prodamavto_chel", "avtomobili74",
        ],
        "ufa": [
            "avto_ufa", "ufa_avto", "auto_ufa02",
            "prodamavto_ufa", "avtomobili_ufa",
        ],
        "krasnodar": [
            "avto_krasnodar", "krasnodar_avto", "auto_krd",
            "prodamavto_krd", "kubanavto", "avto23krd",
        ],
        "omsk": [
            "avto_omsk", "omsk_avto", "auto55omsk",
            "prodamavto_omsk", "avtomobili_omsk",
        ],
        "rostov": [
            "avto_rostov", "rostov_avto", "auto61rostov",
            "prodamavto_rost", "avtomobili_rostov",
        ],
        "tyumen": [
            "avto_tyumen", "tyumen_avto", "auto72tmn",
            "prodamavto_tmn", "avtomobili_tyumen",
        ],
        "samara": [
            "avto_samara", "samara_avto", "auto63smr",
            "prodamavto_samara", "avtomobili_samara",
        ],
        "krasnoyarsk": [
            "avto_krasnoyarsk", "krs_avto", "auto24krs",
            "prodamavto_krs", "avtomobili_krs",
        ],
        "nn": [
            "avto_nn", "nn_avto", "auto52nn",
            "prodamavto_nn", "avtomobili_nn",
        ],
        "perm": [
            "avto_perm", "perm_avto", "auto59perm",
            "prodamavto_perm", "avtomobili_perm",
        ],
        "voronezh": [
            "avto_voronezh", "vrn_avto", "auto36vrn",
            "prodamavto_vrn",
        ],
        "volgograd": [
            "avto_volgograd", "vgd_avto", "auto34vgd",
            "prodamavto_vgd",
        ],
    }

    # ── Шаг 1: Discovery реальных каналов через tgstat/Yandex/DDG ───
    import urllib.parse as _upq_tg
    from concurrent.futures import ThreadPoolExecutor as _TPE_TG, as_completed as _ac_TG

    _tme_re = re.compile(r'(?:t\.me|telegram\.me)/([a-zA-Z][a-zA-Z0-9_]{3,31})(?![/\d])')
    _skip_tg = {"telegram","durov","tgstat","tlgrm","joinchat","share","addstickers",
                "robocop","BotFather","gif","stickers","contest","c","bot","notifications",
                "SpamBot","vote","channel","group"}

    # Федеральные автоканалы которые точно существуют в Telegram
    _TG_FEDERAL_CHANNELS = [
        "avtomarket_rf", "avtorynok_ru", "prodamauto_rf", "avtobaraholka_rf",
        "kupit_avto_rossiya", "avto_rossiya", "avtomobili_rossii",
        "avtomarket", "avto_market_russia", "avtobazar_ru",
        "prodamavto_rf", "auto_baraxolka", "avto_sale_russia",
        "carprice_ru", "avtorynok", "avtomarket_online",
    ]
    # Уже известные каналы из справочника (могут быть фейками — проверим t.me/s/)
    _seed_channels = list(dict.fromkeys(
        TG_REAL_CHANNELS.get(city_key, []) + TG_AUTO_CHANNELS.get(city_key, []) + _TG_FEDERAL_CHANNELS
    ))

    def _discover_channels() -> list[str]:
        found: list[str] = []

        # 1. tgstat.ru — лучший каталог TG каналов России
        _tgstat_city_map = {
            "ekaterinburg": "ekaterinburg", "moskva": "moskva", "spb": "spb",
            "novosibirsk": "novosibirsk", "kazan": "kazan", "chelyabinsk": "chelyabinsk",
            "ufa": "ufa", "krasnodar": "krasnodar", "omsk": "omsk",
            "rostov": "rostov-na-donu", "tyumen": "tyumen", "samara": "samara",
            "krasnoyarsk": "krasnoyarsk", "nn": "nizhni-novgorod",
            "perm": "perm", "voronezh": "voronezh", "irkutsk": "irkutsk",
            "volgograd": "volgograd", "habarovsk": "khabarovsk", "vladivostok": "vladivostok",
        }
        _tc = _tgstat_city_map.get(city_key, "")
        _tgstat_urls = [
            f"https://tgstat.ru/search?q={_upq_tg.quote('авто ' + region_name_ru)}&cat=business",
            f"https://tgstat.ru/search?q={_upq_tg.quote('автобарахолка ' + region_name_ru)}",
            f"https://tgstat.ru/search?q={_upq_tg.quote('авто ' + oblast_name_ru)}&cat=business",
        ]
        if _tc:
            _tgstat_urls.insert(0, f"https://tgstat.ru/city/{_tc}/cars")
        for _url in _tgstat_urls:
            try:
                r = session.get(_url, timeout=10,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                if r.status_code == 200:
                    found += _tme_re.findall(_upq_tg.unquote(r.text))
            except Exception:
                pass

        # 2. tlgrm.ru — ещё один каталог
        for _loc in search_locations[:2]:
            try:
                r = session.get(
                    f"https://tlgrm.ru/channels?q={_upq_tg.quote('авто ' + _loc)}",
                    timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    found += _tme_re.findall(_upq_tg.unquote(r.text))
            except Exception:
                pass

        # 3. Yandex — по городу и области
        for _loc in search_locations:
            for _q in [
                f"site:t.me автобарахолка {_loc}",
                f"site:t.me продам авто {_loc}",
                f"telegram канал авто {_loc} продажа",
            ]:
                try:
                    r = session.get("https://yandex.ru/search/",
                        params={"text": _q}, timeout=9,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                    if r.status_code == 200:
                        found += _tme_re.findall(_upq_tg.unquote(r.text))
                except Exception:
                    pass

        # 4. DDG
        for _loc in search_locations[:1]:
            for _q in [f"site:t.me автобарахолка {_loc}", f"site:t.me продажа авто {_loc}"]:
                try:
                    r = session.get("https://html.duckduckgo.com/html/",
                        params={"q": _q, "kl": "ru-ru"}, timeout=9)
                    if r.status_code == 200:
                        found += _tme_re.findall(_upq_tg.unquote(r.text))
                except Exception:
                    pass

        unique = [s for s in dict.fromkeys(found)
                  if s.lower() not in {x.lower() for x in _skip_tg} and len(s) >= 4]
        print(f"  [TG discover] найдено {len(unique)} каналов")
        return unique

    # ── Шаг 2: Парсим публичные каналы через t.me/s/ ─────────────────
    _tg_post_url_re2 = re.compile(r'https?://t\.me/([a-zA-Z0-9_]+)/(\d+)')
    _tg_phone_re2 = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')

    def _parse_channel(channel: str) -> list[dict]:
        """Парсит публичный TG канал через t.me/s/ — до 3 страниц."""
        try:
            batch = []
            seen_urls_ch: set[str] = set()
            today_d = datetime.date.today()
            before_id = None
            for _page in range(3):
                url_t = f"https://t.me/s/{channel}" if before_id is None else f"https://t.me/s/{channel}?before={before_id}"
                try:
                    r = session.get(url_t, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code != 200 or "tgme_widget_message" not in r.text:
                        break
                    from bs4 import BeautifulSoup as _BS2
                    soup = _BS2(r.text, "lxml")
                    messages = soup.select("div.tgme_widget_message_wrap") or soup.select(".tgme_widget_message")
                    if not messages:
                        break
                    found_new = False
                    min_id = None
                    for msg_el in messages:
                        text_el = msg_el.select_one(".tgme_widget_message_text")
                        if not text_el:
                            continue
                        import html as _html_mod2
                        text = _html_mod2.unescape(text_el.get_text(" ", strip=True))
                        text = re.sub(r'\s+', ' ', text).strip()
                        # Track ID for pagination
                        link_el2 = msg_el.select_one("a.tgme_widget_message_date")
                        if link_el2:
                            id_m2 = re.search(r'/(\d+)$', link_el2.get("href", ""))
                            if id_m2:
                                mid2 = int(id_m2.group(1))
                                if min_id is None or mid2 < min_id:
                                    min_id = mid2
                        if len(text) < 20 or not _is_car_sale_social(text) or _is_moto(text[:200]):
                            continue
                        price = _tg_parse_price(text)
                        if price > 0 and not (price_min <= price <= price_max):
                            continue
                        link_el = msg_el.select_one("a.tgme_widget_message_date") or msg_el.select_one("a[href*='t.me']")
                        msg_url = link_el.get("href", f"https://t.me/{channel}") if link_el else f"https://t.me/{channel}"
                        if msg_url in seen_urls_ch:
                            continue
                        seen_urls_ch.add(msg_url)
                        found_new = True
                        # Дата
                        days = 0
                        time_el = msg_el.select_one("time[datetime]")
                        if time_el:
                            try:
                                from datetime import datetime as _dt2
                                post_date = _dt2.fromisoformat(time_el.get("datetime", "")[:10]).date()
                                days = max(0, (today_d - post_date).days)
                            except Exception:
                                pass
                        # Фото
                        photo_url = ""
                        img_wrap = msg_el.select_one("a.tgme_widget_message_photo_wrap")
                        if img_wrap:
                            pm = re.search(r"url\('([^']+)'\)", img_wrap.get("style", ""))
                            if pm:
                                photo_url = pm.group(1)
                        # Телефон
                        phone_m = _tg_phone_re2.search(text)
                        phone = phone_m.group(0).strip() if phone_m else ""
                        year_m = _TG_YEAR_RE.search(text)
                        batch.append({
                            "title": _social_make_title(text) or f"Авто {region_name_ru}",
                            "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
                            "_price_int": price,
                            "url": msg_url,
                            "_photo_url": photo_url,
                            "description": text[:500],
                            "source": "tg",
                            "seller": f"@{channel}" + (f" · {phone}" if phone else ""),
                            "_seller_url": f"https://t.me/{channel}",
                            "_year": int(year_m.group(1)) if year_m else 0,
                            "_days_on_site": days,
                            "_no_price": price == 0,
                        })
                    before_id = min_id
                    if not found_new or not before_id:
                        break
                except Exception:
                    break
            return batch
        except Exception:
            return []

    # ── Шаг 3: DDG поиск постов из TG напрямую ───────────────────────
    def _ddg_tg_posts() -> list[dict]:
        import urllib.parse as _upq2
        _tg_post_re = re.compile(r'https?://t\.me/[a-zA-Z0-9_]+/\d+', re.I)
        all_locs = list(dict.fromkeys([region_name_ru, oblast_name_ru]))
        queries = [f"site:t.me продам авто {_loc}" for _loc in all_locs]
        queries += [f"site:t.me автобарахолка {_loc}" for _loc in all_locs[:1]]
        batch = []
        seen_u: set[str] = set()
        for q in queries:
            try:
                r = session.get("https://html.duckduckgo.com/html/",
                    params={"q": q, "kl": "ru-ru"}, timeout=9)
                if r.status_code != 200:
                    continue
                html = _upq2.unquote(r.text)
                for m in _tg_post_re.finditer(html):
                    href = m.group(0).rstrip(".,)")
                    if href in seen_u:
                        continue
                    seen_u.add(href)
                    pos = html.find(m.group(0))
                    ctx = html[max(0, pos-300):pos+500]
                    import html as _html_mod3
                    ctx = _html_mod3.unescape(re.sub(r"<[^>]+>", " ", ctx))
                    ctx = re.sub(r"\s+", " ", ctx).strip()
                    if not _is_car_sale_social(ctx):
                        continue
                    price = _tg_parse_price(ctx)
                    if price > 0 and not (price_min <= price <= price_max):
                        continue
                    ch_m = re.match(r'https?://t\.me/([a-zA-Z0-9_]+)/', href)
                    ch_name = ch_m.group(1) if ch_m else "tg"
                    year_m = _TG_YEAR_RE.search(ctx)
                    batch.append({
                        "title": _social_make_title(ctx) or f"Авто {region_name_ru}",
                        "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
                        "_price_int": price,
                        "url": href,
                        "_photo_url": "",
                        "description": ctx[:400],
                        "source": "tg",
                        "seller": f"@{ch_name}",
                        "_seller_url": f"https://t.me/{ch_name}",
                        "_year": int(year_m.group(1)) if year_m else 0,
                        "_days_on_site": 0,
                        "_no_price": price == 0,
                    })
            except Exception:
                pass
        return batch

    # ── Запускаем всё параллельно ─────────────────────────────────────
    with _TPE_TG(max_workers=2) as _dis_ex:
        _disc_fut = _dis_ex.submit(_discover_channels)
        _ddg_fut = _dis_ex.submit(_ddg_tg_posts)
        # Параллельно парсим seed-каналы
        with _TPE_TG(max_workers=10) as _ch_ex:
            for ch_batch in _ch_ex.map(_parse_channel, _seed_channels, timeout=30):
                if ch_batch:
                    results.extend(ch_batch)
                    print(f"  [TG seed] {len(ch_batch)} объявлений")

        try:
            discovered = _disc_fut.result(timeout=10)
        except Exception:
            discovered = []
        try:
            ddg_batch = _ddg_fut.result(timeout=5)
            if ddg_batch:
                results.extend(ddg_batch)
                print(f"  [TG DDG] {len(ddg_batch)} постов")
        except Exception:
            pass

    # Парсим обнаруженные каналы
    new_chs = [c for c in discovered if c not in _seed_channels and c not in _skip_tg][:25]
    if new_chs:
        with _TPE_TG(max_workers=10) as _ch_ex2:
            for ch_batch in _ch_ex2.map(_parse_channel, new_chs, timeout=30):
                if ch_batch:
                    results.extend(ch_batch)

    # Дедупликация
    seen_norm_tg: set[str] = set()
    deduped_tg: list[dict] = []
    for it in results:
        u = it.get("url", "").split("?")[0].rstrip("/")
        if not u or u in seen_norm_tg:
            continue
        seen_norm_tg.add(u)
        deduped_tg.append(it)
    return deduped_tg


def scrape_vk_groups(region: str, price_min: int, price_max: int) -> list[dict]:
    """
    Ищет объявления о продаже авто в пабликах ВКонтакте.
    Стратегия:
    1. VK API (если есть VK_TOKEN) - надёжно
    2. Поиск через Яндекс по site:vk.com - без токена
    3. Прямой парсинг известных групп через m.vk.com
    """
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    vk_token = os.getenv("VK_TOKEN", "4e23362e4e23362e4e23362e4c4d62ef5f44e234e23362e241bdc8082449c578e8eace8")
    city_key = _TG_REGION_MAP.get(region, "")

    _vk_region_names = {
        "ekaterinburg": "Екатеринбург", "moskva": "Москва", "spb": "Петербург",
        "novosibirsk": "Новосибирск", "kazan": "Казань", "chelyabinsk": "Челябинск",
        "ufa": "Уфа", "krasnodar": "Краснодар", "omsk": "Омск", "rostov": "Ростов",
        "tyumen": "Тюмень", "samara": "Самара", "volgograd": "Волгоград",
        "perm": "Пермь", "voronezh": "Воронеж", "saratov": "Саратов",
        "krasnoyarsk": "Красноярск", "irkutsk": "Иркутск",
        "vladivostok": "Владивосток", "habarovsk": "Хабаровск", "nn": "Нижний Новгород",
    }
    _vk_oblast_names = {
        "ekaterinburg": "Свердловская область",
        "moskva": "Московская область",
        "spb": "Ленинградская область",
        "novosibirsk": "Новосибирская область",
        "kazan": "Татарстан",
        "chelyabinsk": "Челябинская область",
        "ufa": "Башкортостан",
        "krasnodar": "Краснодарский край",
        "omsk": "Омская область",
        "rostov": "Ростовская область",
        "tyumen": "Тюменская область",
        "samara": "Самарская область",
        "perm": "Пермский край",
        "voronezh": "Воронежская область",
        "krasnoyarsk": "Красноярский край",
        "nn": "Нижегородская область",
        "irkutsk": "Иркутская область",
    }
    region_name_ru = _vk_region_names.get(city_key, city_key)
    oblast_name_ru = _vk_oblast_names.get(city_key, region_name_ru)
    vk_search_locations = list(dict.fromkeys([region_name_ru, oblast_name_ru]))

    results: list[dict] = []
    today = datetime.date.today()

    _vk_price_re = re.compile(
        r"(\d[\d\s]{1,8})\s*(?:₽|тыс\.?\s*(?:р(?:уб(?:лей?|ля)?)?\.?)?|руб(?:лей?|ля)?\.?|р\.|тр\.?|к\b)",
        re.IGNORECASE,
    )
    # Число рядом с ценовым словом: "цена 150000", "прошу 95 000", "стоимость 80тыс"
    _vk_price_ctx_re = re.compile(
        r"(?:цен[аеу]|стоимост[ьи]|прошу|продам за|отдам за)\s*[:\-]?\s*(\d[\d\s]{2,7})(?:\s*(?:тыс|т\.р|т\.\s*р))?",
        re.IGNORECASE,
    )
    _vk_year_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")


    def _parse_price(text: str) -> int:
        # Сначала ищем с явным символом валюты
        for m in _vk_price_re.finditer(text):
            raw = re.sub(r"\D", "", m.group(1))
            if not raw:
                continue
            val = int(raw)
            suffix = m.group(0)[len(m.group(1)):].strip().lower()
            if "тыс" in suffix or "тр" in suffix or (suffix.startswith("к") and not "кузов" in suffix):
                if val < 1000:
                    val *= 1000
            if 50_000 <= val <= 50_000_000:
                return val
        # Затем ищем число рядом с ценовым словом
        for m in _vk_price_ctx_re.finditer(text):
            raw = re.sub(r"\D", "", m.group(1))
            if not raw:
                continue
            val = int(raw)
            full = m.group(0).lower()
            if any(s in full for s in ("тыс", "т.р")):
                val *= 1000
            if val < 1000:  # вероятно тысячи без суффикса: "цена 95" → 95000
                val *= 1000
            if 50_000 <= val <= 50_000_000:
                return val
        # Третий проход: голое число в диапазоне цен авто
        _bare_num_re = re.compile(r'\b(\d{5,7})\b')
        for m in _bare_num_re.finditer(text):
            val = int(m.group(1))
            if 50_000 <= val <= 9_999_999:
                # Проверяем что рядом нет слов как "пробег", "год", "км"
                ctx = text[max(0,m.start()-30):m.end()+30].lower()
                if any(skip in ctx for skip in ('пробег','км','год','г.в','г/в','тыс.км')):
                    continue
                return val
        return 0

    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    # Используем тот же русский прокси что и для Авито — лучше обходит блокировки VK/Yandex
    _vk_proxy_url = (
        os.getenv("PROXY_URL") or
        os.getenv("AVITO_PROXY_URL") or
        (
            f"http://{AVITO_PROXY_USER}:{AVITO_PROXY_PASS}@{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}"
            if AVITO_PROXY_HOST and AVITO_PROXY_USER and not _proxy_auth_failed
            else ""
        )
    )
    if _vk_proxy_url and "__agentproxy" not in _vk_proxy_url:
        session.proxies.update({"http": _vk_proxy_url, "https": _vk_proxy_url})

    def _vk_make_item(post: dict, source_label: str = "") -> "dict | None":
        """Превращает VK API post dict в item для бота."""
        text = post.get("text", "")
        if len(text) < 30:
            return None
        if not _is_car_sale_social(text):
            return None
        if _is_moto(text[:200]):
            return None
        price = _parse_price(text)
        if price > 0 and not (price_min <= price <= price_max):
            return None
        owner_id = post.get("owner_id") or post.get("from_id", 0)
        post_id = post.get("id", "")
        url = f"https://vk.com/wall{owner_id}_{post_id}"
        year_m = _vk_year_re.search(text)
        photo_url = ""
        for att in post.get("attachments", []):
            if att.get("type") == "photo":
                sizes = att["photo"].get("sizes", [])
                if sizes:
                    photo_url = max(sizes, key=lambda s: s.get("width", 0)).get("url", "")
                    break
        # Дата
        import datetime as _dt
        days = 0
        if post.get("date"):
            try:
                post_date = _dt.datetime.fromtimestamp(post["date"]).date()
                days = max(0, (_dt.date.today() - post_date).days)
            except Exception:
                pass
        seller = source_label or f"vk.com/wall{owner_id}"
        return {
            "title": _social_make_title(text),
            "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
            "_price_int": price,
            "url": url,
            "_photo_url": photo_url,
            "description": text[:500],
            "source": "vk",
            "seller": seller,
            "_seller_url": f"https://vk.com/wall{owner_id}",
            "_year": int(year_m.group(1)) if year_m else 0,
            "_days_on_site": days,
            "_no_price": price == 0,
        }

    from concurrent.futures import ThreadPoolExecutor as _TPE_VK, as_completed as _ac_VK
    import urllib.parse as _upq_vk

    # ── Шаг 1: Находим реальные VK-группы через Yandex и DDG ───────
    # Yandex хорошо индексирует vk.com/club{id} и vk.com/public{id}
    _found_group_ids: dict[int, str] = {}   # group_id → name/slug
    _found_slugs: list[str] = []
    _vk_group_id_re = re.compile(r'vk\.com/(?:club|public)(\d+)', re.I)
    _vk_slug_re2 = re.compile(r'vk\.com/([a-zA-Z][a-zA-Z0-9_.]{3,40})(?=["\s\?/]|$)', re.I)
    _skip_vk = {"wall","photo","video","music","feed","im","messages","login",
                "join","share","away","l","id","app","market","faq","support",
                "dev","blog","about","terms","privacy","advertising","vk",
                "vkontakte","catalog","events","groups","people","search","write",
                "albums","audios","docs","friends","notifications","settings","bookmark"}

    def _parse_vk_group_refs(html: str):
        html_d = _upq_vk.unquote(html)
        for m in _vk_group_id_re.finditer(html_d):
            gid = int(m.group(1))
            if gid not in _found_group_ids:
                _found_group_ids[gid] = f"club{gid}"
        for m in _vk_slug_re2.finditer(html_d):
            sl = m.group(1).lower().rstrip(".,)")
            if (sl not in _skip_vk and not re.match(r'^\d+$', sl)
                    and sl not in _found_slugs and len(sl) >= 4):
                _found_slugs.append(sl)

    def _yandex_vk_groups_search(loc: str):
        for q in [
            f"site:vk.com/club автобарахолка {loc}",
            f"site:vk.com/public продажа авто {loc}",
            f"site:vk.com авторынок {loc} продам",
            f"site:vk.com автомобили барахолка {loc}",
        ]:
            try:
                r = session.get("https://yandex.ru/search/",
                    params={"text": q}, timeout=9,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                if r.status_code == 200:
                    _parse_vk_group_refs(r.text)
            except Exception:
                pass

    def _ddg_vk_groups_search(loc: str):
        for q in [
            f"site:vk.com автобарахолка {loc}",
            f"site:vk.com продам авто {loc}",
        ]:
            try:
                r = session.get("https://html.duckduckgo.com/html/",
                    params={"q": q, "kl": "ru-ru"}, timeout=9)
                if r.status_code == 200:
                    _parse_vk_group_refs(r.text)
            except Exception:
                pass

    # Параллельно ищем по городу и области
    _disc_tasks = [(_yandex_vk_groups_search, loc) for loc in vk_search_locations]
    _disc_tasks += [(_ddg_vk_groups_search, loc) for loc in vk_search_locations[:1]]
    with _TPE_VK(max_workers=8) as _ex_disc:
        futs_disc = [_ex_disc.submit(fn, loc) for fn, loc in _disc_tasks]
        for _ in _ac_VK(futs_disc, timeout=15):
            pass
    print(f"  [VK discover] ID групп: {len(_found_group_ids)}, slugs: {len(_found_slugs)}")

    # ── Шаг 2: VK API groups.search (если есть токен) ───────────────
    VK_API_URL = "https://api.vk.com/method"
    _api_params = {"access_token": vk_token, "v": "5.199"} if vk_token else {}

    if vk_token:
        def _gs_one(q_type: tuple) -> list:
            gq, gtype = q_type
            try:
                r = session.get(f"{VK_API_URL}/groups.search",
                    params={"q": gq, "type": gtype, "count": 20, **_api_params}, timeout=8)
                resp = r.json()
                if resp.get("error"):
                    return []
                return resp.get("response", {}).get("items", [])
            except Exception:
                return []

        _api_gs_tasks = [(q, t)
            for loc in vk_search_locations
            for q in [f"автобарахолка {loc}", f"авто {loc}", f"продажа авто {loc}"]
            for t in ("page", "group")]
        with _TPE_VK(max_workers=8) as _gsex:
            for res in _gsex.map(_gs_one, _api_gs_tasks[:16], timeout=15):
                for g in (res or []):
                    gid = g.get("id")
                    if gid and gid not in _found_group_ids:
                        _found_group_ids[gid] = g.get("name", f"club{gid}")

        # newsfeed.search — работает только с user-token
        _nf_seen: set[str] = set()
        for q in [f"продам авто {loc}" for loc in vk_search_locations] + ["срочно авто торг"]:
            try:
                r = session.get(f"{VK_API_URL}/newsfeed.search",
                    params={"q": q, "count": 100, "extended": 1, **_api_params}, timeout=8)
                resp = r.json()
                if resp.get("error"):
                    break
                for post in resp.get("response", {}).get("items", []):
                    item = _vk_make_item(post, "")
                    if item and item["url"] not in _nf_seen:
                        _nf_seen.add(item["url"])
                        results.append(item)
            except Exception:
                break
        if _nf_seen:
            print(f"  [VK newsfeed] {len(_nf_seen)} постов")

    # ── Шаг 3: Скрейпим стены найденных групп через VK API ──────────
    # wall.get на публичных группах работает БЕЗ токена
    def _scrape_group_wall(gid_name: tuple) -> list:
        gid, gname = gid_name
        local = []
        base = {"owner_id": f"-{gid}", "count": 100, "filter": "owner", "v": "5.199"}
        if vk_token:
            base["access_token"] = vk_token
        try:
            rg = session.get(f"{VK_API_URL}/wall.get", params=base, timeout=8)
            for post in rg.json().get("response", {}).get("items", []):
                item = _vk_make_item(post, gname)
                if item:
                    local.append(item)
        except Exception:
            pass
        ws = {"owner_id": f"-{gid}", "query": "продам", "count": 100, "v": "5.199"}
        if vk_token:
            ws["access_token"] = vk_token
        try:
            rw = session.get(f"{VK_API_URL}/wall.search", params=ws, timeout=8)
            for post in rw.json().get("response", {}).get("items", []):
                item = _vk_make_item(post, gname)
                if item:
                    local.append(item)
        except Exception:
            pass
        return local

    wall_groups = list(_found_group_ids.items())[:30]
    _wall_seen: set[str] = set()
    with _TPE_VK(max_workers=12) as _wex:
        for posts in _wex.map(_scrape_group_wall, wall_groups, timeout=25):
            for item in (posts or []):
                if item["url"] not in _wall_seen:
                    _wall_seen.add(item["url"])
                    results.append(item)
    print(f"  [VK walls] {len(_wall_seen)} постов из {len(wall_groups)} групп")

    # ── Шаг 4: Парсим найденные slug-группы через m.vk.com ──────────
    # Эти slug реально найдены через Yandex/DDG — не фиктивные
    def _try_vk_community(slug: str) -> list[dict]:
        """Парсит стену паблика ВКонтакте — до 3 страниц с реальными датами."""
        try:
            batch = []
            seen_post_urls: set[str] = set()
            today_d = datetime.date.today()

            _mob_ua = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
            for offset in (0, 20, 40):
                try:
                    url_c = f"https://m.vk.com/{slug}" if offset == 0 else f"https://m.vk.com/{slug}?offset={offset}"
                    r = session.get(url_c, timeout=8, headers={"User-Agent": _mob_ua})
                    if r.status_code in (302, 403, 404):
                        break
                    if r.status_code != 200:
                        break
                    rtext = r.text
                    _page_404 = any(x in rtext for x in (
                        "This community was removed", "Страница не найдена",
                        "сообщество недоступно", "private group",
                        "id=\"not_found\"", "class=\"not_found\""
                    ))
                    if _page_404:
                        break
                    _has_posts = (
                        "data-post-id" in rtext or "wi_body" in rtext or
                        "wall-item__text" in rtext or "post__text" in rtext or
                        ("wall_item" in rtext and "продам" in rtext.lower())
                    )
                    if not _has_posts:
                        break
                    soup = _BS(rtext, "lxml")
                    posts = (soup.select("div._post") or soup.select("div.wall_item") or
                             soup.select("div.wi") or soup.select("article.post") or
                             soup.select("div[class*='wall-item']") or
                             soup.select("[data-post-id]") or soup.select("div[id^='post']"))
                    if not posts:
                        break
                    found_new = False
                    _our_city_names = {n.lower() for n in vk_search_locations}
                    _all_city_names = {"москва","московск","питер","петербург","спб",
                        "новосибирск","казань","екатеринбург","нижний новгород",
                        "челябинск","самара","омск","ростов","уфа","красноярск",
                        "пермь","воронеж","тюмень","краснодар","саратов","иркутск"}
                    for post in posts:
                        text_el = (post.select_one(".wall_post_text") or
                                   post.select_one("._post_content") or
                                   post.select_one(".post__text") or
                                   post.select_one(".wi_body") or
                                   post.select_one(".wall-item__text") or
                                   post.select_one("[class*='post_text']"))
                        if not text_el:
                            text_el = post
                        text = text_el.get_text(" ", strip=True)
                        if len(text) < 20 or not _is_car_sale_social(text) or _is_moto(text[:200]):
                            continue
                        tl = text.lower()
                        _other = next((c for c in _all_city_names
                                       if c in tl and not any(oc in tl for oc in _our_city_names)), None)
                        if _other and not any(oc in tl for oc in _our_city_names):
                            continue
                        price = _parse_price(text)
                        if price > 0 and not (price_min <= price <= price_max):
                            continue
                        link_el = post.select_one("a[href*='/wall']") or post.select_one("a.post__date")
                        post_url = ""
                        if link_el:
                            h = link_el.get("href", "")
                            post_url = f"https://vk.com{h}" if h.startswith("/") else h
                        if not post_url or post_url in seen_post_urls:
                            continue
                        seen_post_urls.add(post_url)
                        found_new = True
                        days = 0
                        time_el = post.select_one("time[datetime]")
                        if time_el:
                            try:
                                from datetime import datetime as _dt
                                post_date = _dt.fromisoformat(time_el.get("datetime", "")[:10]).date()
                                days = max(0, (today_d - post_date).days)
                            except Exception:
                                pass
                        photo_url = ""
                        for img in post.select("img"):
                            src = img.get("src", "")
                            if src and ("userapi.com" in src or "vk.com" in src) and "sticker" not in src:
                                photo_url = src
                                break
                        year_m = _vk_year_re.search(text)
                        phone_m = re.search(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', text)
                        phone = phone_m.group(0).strip() if phone_m else ""
                        batch.append({
                            "title": _social_make_title(text),
                            "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
                            "_price_int": price,
                            "url": post_url,
                            "_photo_url": photo_url,
                            "description": text[:500],
                            "source": "vk",
                            "seller": f"vk.com/{slug}" + (f" · {phone}" if phone else ""),
                            "_seller_url": f"https://vk.com/{slug}",
                            "_year": int(year_m.group(1)) if year_m else 0,
                            "_days_on_site": days,
                            "_no_price": price == 0,
                        })
                        if len(batch) >= 60:
                            break
                    if not found_new or len(batch) >= 60:
                        break
                    time.sleep(0.1)
                except Exception:
                    break
            return batch
        except Exception as e:
            print(f"  [VK {slug}] {e}")
            return []

    # ── Хардкодные VK slug-группы по городам (fallback если discovery=0) ──
    _VK_SEED_SLUGS: dict[str, list[str]] = {
        "ekaterinburg": [
            "avto_ekb", "avtoekb", "avto_ekb96", "ekb_avto96", "baraholka_avto96",
            "avtobazar_ekb", "avto_eburg", "avto_sverdl", "ekbauto", "prodamavto_ekb",
            "avtorynok_ekb", "avto96", "avto_yekaterinburg", "auto_ekb",
        ],
        "moskva": [
            "avto_msk", "avtomoskva", "avto77msk", "avtobazar_msk", "prodamavto_msk",
            "cars_msk", "avto_moscow", "moscowcars", "avtorynok_msk", "kupit_avto_msk",
            "prodauto_msk", "avtomarket_msk", "auto_moskva",
        ],
        "spb": [
            "avto_spb", "avtospb", "avto78spb", "prodamavto_spb", "avtobazar_spb",
            "spb_avto78", "cars_spb", "avto_piter", "avto78_prodazha",
            "kupit_avto_spb", "prodauto_spb",
        ],
        "novosibirsk": [
            "avto_nsk", "avtonsk54", "nsk_avto54", "prodamavto_nsk", "avtobazar_nsk",
            "novosibirsk_avto", "avto54", "cars_nsk", "avto_novosibirsk",
        ],
        "kazan": [
            "avto_kazan", "avtokazan16", "kazan_avto16", "prodamavto_kazan",
            "avtobazar_kazan", "cars_kazan", "avto_kzn", "auto_kazan",
        ],
        "chelyabinsk": [
            "avto_chel", "avto74chel", "chel_avto74", "prodamavto_chel",
            "avtobazar_chel", "cars_chel", "avto74", "avto_chelyabinsk",
        ],
        "ufa": [
            "avto_ufa", "avto02ufa", "ufa_avto02", "prodamavto_ufa",
            "avtobazar_ufa", "cars_ufa", "avto_bashkortostan",
        ],
        "krasnodar": [
            "avto_krd", "avto23krd", "krasnodar_avto23", "prodamavto_krd",
            "avtobazar_krasnodar", "kuban_avto", "avto_kuban", "cars_krd",
            "avto_krasnodar", "auto_krd",
        ],
        "omsk": [
            "avto_omsk", "avto55omsk", "omsk_avto55", "prodamavto_omsk",
            "avtobazar_omsk", "cars_omsk", "avto55",
        ],
        "rostov": [
            "avto_rostov", "avto61rostov", "rostov_avto61", "prodamavto_rostov",
            "avtobazar_rostov", "cars_rostov", "avto61", "avto_don",
            "avto_rostovnadon", "don_avto",
        ],
        "tyumen": [
            "avto_tyumen", "avto72tyumen", "tyumen_avto72", "prodamavto72",
            "avtobazar_tyumen", "cars_tyumen", "avto72", "avto_tmn",
        ],
        "samara": [
            "avto_samara", "avto63samara", "samara_avto63", "prodamavto63",
            "avtobazar_samara", "cars_samara", "avto63", "avto_samarskaya",
        ],
        "perm": [
            "avto_perm", "avto59perm", "perm_avto59", "prodamavto59",
            "avtobazar_perm", "cars_perm", "avto59",
        ],
        "voronezh": [
            "avto_voronezh", "avto36vrn", "voronezh_avto36", "prodamavto36",
            "avtobazar_voronezh", "cars_vrn", "avto36", "avto_vrn",
            "vrn_avto", "auto_voronezh", "avtovrn", "avtomobili_vrn",
        ],
        "volgograd": [
            "avto_volgograd", "avto34vlg", "volgograd_avto34", "prodamavto34",
            "avtobazar_volgograd", "cars_vgd", "avto34",
        ],
        "krasnoyarsk": [
            "avto_krsk", "avto24krsk", "krasnoyarsk_avto24", "prodamavto24",
            "avtobazar_krs", "cars_krs", "avto24",
        ],
        "nn": [
            "avto_nn", "avto52nn", "nn_avto52", "prodamavto52", "avtobazar_nn",
            "cars_nn", "avto52", "avto_nnov", "avto_nizhny",
        ],
        "saratov": [
            "avto_saratov", "avto64sar", "saratov_avto64", "prodamavto64",
            "avtobazar_saratov", "cars_sar", "avto64",
        ],
        "irkutsk": [
            "avto_irkutsk", "avto38irk", "irkutsk_avto38", "prodamavto38",
            "avtobazar_irkutsk", "cars_irk", "avto38",
        ],
        "vladivostok": [
            "avto_vladivostok", "avto25vlad", "vladivostok_avto25", "prodamavto25",
            "avtobazar_vlad", "cars_vlad", "avto25", "japancars_vlad",
        ],
        "habarovsk": [
            "avto_habarovsk", "avto27hab", "habarovsk_avto27", "prodamavto27",
            "avtobazar_hab", "cars_hab", "avto27",
        ],
    }
    # Добавляем хардкодные slugs если discovery ничего не нашёл
    _seed_vk = _VK_SEED_SLUGS.get(city_key, [])
    for _s in _seed_vk:
        if _s not in _found_slugs and _s not in _skip_vk:
            _found_slugs.append(_s)

    # Конвертируем найденные slugs в group_id через VK API groups.getById (без токена)
    _all_slugs_to_resolve = [s for s in _found_slugs if s not in _skip_vk
                              and not re.match(r'^\d+$', s)][:30]
    if _all_slugs_to_resolve:
        def _resolve_slug_to_id(slug: str) -> "tuple[int,str]|None":
            try:
                params = {"group_ids": slug, "v": "5.199"}
                if vk_token:
                    params["access_token"] = vk_token
                r = session.get(f"{VK_API_URL}/groups.getById",
                    params=params, timeout=6)
                resp = r.json()
                items = resp.get("response", {}).get("groups") or resp.get("response", [])
                if isinstance(items, list) and items:
                    g = items[0]
                    gid = g.get("id")
                    if gid and gid not in _found_group_ids:
                        return (gid, g.get("name", slug))
            except Exception:
                pass
            return None

        with _TPE_VK(max_workers=15) as _res_ex:
            for res in _res_ex.map(_resolve_slug_to_id, _all_slugs_to_resolve, timeout=20):
                if res:
                    _found_group_ids[res[0]] = res[1]
        print(f"  [VK resolve] {len(_found_group_ids)} групп после resolve slugs")

    # Скрейпим стены дополнительно найденных через resolve
    _extra_wall_groups = [(gid, name) for gid, name in _found_group_ids.items()
                           if (gid, name) not in [(g[0], g[1]) for g in wall_groups]][:20]
    if _extra_wall_groups:
        with _TPE_VK(max_workers=10) as _ewex:
            for posts in _ewex.map(_scrape_group_wall, _extra_wall_groups, timeout=25):
                for item in (posts or []):
                    if item["url"] not in _wall_seen:
                        _wall_seen.add(item["url"])
                        results.append(item)
        print(f"  [VK extra walls] {len(_extra_wall_groups)} доп. групп")

    # Берём только slug-ги найденные через поиск (реальные)
    real_slugs = [s for s in _found_slugs if s not in _skip_vk][:20]
    if real_slugs:
        with _TPE_VK(max_workers=10) as _sex:
            for batch_s in _sex.map(_try_vk_community, real_slugs, timeout=25):
                results.extend(batch_s or [])

    # Также Yandex/DDG прямые посты
    _try_yandex_vk_results = _try_yandex_vk()
    if _try_yandex_vk_results:
        results.extend(_try_yandex_vk_results)
    _try_ddg_vk_results = _try_ddg_vk()
    if _try_ddg_vk_results:
        results.extend(_try_ddg_vk_results)

    # Дедупликация
    def _norm_vk_url(u: str) -> str:
        return u.replace("//m.vk.com/", "//vk.com/").split("?")[0].rstrip("/")

    seen_norm: set[str] = set()
    deduped_results: list[dict] = []
    for it in results:
        u = it.get("url", "")
        norm = _norm_vk_url(u) if u else ""
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        it["url"] = norm
        deduped_results.append(it)
    return deduped_results


# ── Парсер Авито ────────────────────────────────────────────────

# Слаги для Авито — городской слаг для URL
AVITO_SLUGS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov-na-donu",
}

# ID локаций для Авито API
AVITO_LOCATION_IDS = {
    "ekaterinburg": 621940,
    "moscow":       637640,
    "spb":          638582,
    "novosibirsk":  661122,
    "kazan":        621133,
    "chelyabinsk":  1282,
    "ufa":          1281,
    "krasnodar":    13579,
    "omsk":         665066,
    "tyumen":       641900,
    "perm":         656049,
    "krasnoyarsk":  641901,
    "voronezh":     621890,
    "samara":       621540,
    "rostov":       621900,
}



import threading as _threading
import asyncio as _aio

# Playwright живёт на одном выделенном asyncio event loop в отдельном потоке
# (объекты Playwright привязаны к loop, на котором были созданы), но благодаря
# async API внутри этого loop можно держать НЕСКОЛЬКО страниц одновременно —
# поэтому параллельность не теряется, в отличие от sync API под общим локом.
_AVITO_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".avito_cookies.json")
_avito_loop: "_aio.AbstractEventLoop | None" = None
_avito_loop_thread: "_threading.Thread | None" = None
_avito_loop_lock = _threading.Lock()
_avito_ready = _threading.Event()
_avito_async_context = None  # type: ignore
_avito_async_sem: "_aio.Semaphore | None" = None  # ограничивает кол-во одновр. вкладок


async def _avito_async_init():
    global _avito_async_context, _avito_async_sem
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    # Playwright Chromium не поддерживает SOCKS5 с логином/паролем.
    # Прокси в браузере используем только если это HTTP или SOCKS5 без авторизации (IP whitelist).
    if AVITO_PROXY_HOST and AVITO_PROXY_PORT:
        _use_browser_proxy = not (AVITO_PROXY_PROTOCOL == "socks5" and AVITO_PROXY_USER)
        if _use_browser_proxy:
            _proxy = {"server": f"{AVITO_PROXY_PROTOCOL}://{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}"}
            if AVITO_PROXY_USER:
                _proxy["username"] = AVITO_PROXY_USER
                _proxy["password"] = AVITO_PROXY_PASS
            _launch_kwargs["proxy"] = _proxy
    browser = await pw.chromium.launch(**_launch_kwargs)
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="ru-RU",
        viewport={"width": 1366, "height": 900},
        extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        ignore_https_errors=True,
    )
    if os.path.exists(_AVITO_COOKIE_FILE):
        try:
            with open(_AVITO_COOKIE_FILE, "r", encoding="utf-8") as f:
                await context.add_cookies(json.load(f))
        except Exception:
            pass
    _avito_async_context = context
    _avito_async_sem = _aio.Semaphore(3)  # компромисс скорость/незаметность


def _avito_loop_main():
    global _avito_loop
    loop = _aio.new_event_loop()
    _avito_loop = loop
    _aio.set_event_loop(loop)
    loop.run_until_complete(_avito_async_init())
    _avito_ready.set()
    loop.run_forever()


def _avito_ensure_loop():
    global _avito_loop_thread
    with _avito_loop_lock:
        if _avito_loop_thread is None or not _avito_loop_thread.is_alive():
            _avito_ready.clear()
            _avito_loop_thread = _threading.Thread(target=_avito_loop_main, daemon=True)
            _avito_loop_thread.start()
    _avito_ready.wait(timeout=30)


async def _avito_async_fetch(url: str, wait_ms: int, timeout_ms: int) -> str:
    async with _avito_async_sem:
        # небольшая случайная пауза перед навигацией — снижает шанс рейт-лимита (429)
        await _aio.sleep(random.uniform(0.5, 1.5))
        page = await _avito_async_context.new_page()
        try:
            try:
                from playwright_stealth import stealth_async
                await stealth_async(page)
            except Exception:
                pass
            # Load saved cookies if available to warm up the session
            try:
                if os.path.exists(_AVITO_COOKIE_FILE):
                    with open(_AVITO_COOKIE_FILE, "r", encoding="utf-8") as f:
                        saved_cookies = json.load(f)
                    await _avito_async_context.add_cookies(saved_cookies)
            except Exception:
                pass
            # First visit the homepage to warm up cookies and look like a real browser
            try:
                await page.goto("https://www.avito.ru/", timeout=timeout_ms, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
            except Exception:
                pass
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            await page.wait_for_timeout(max(wait_ms, 3000))
            # Simulate human: random scroll and mouse movements
            try:
                await page.evaluate("window.scrollBy(0, Math.random() * 300)")
                await page.wait_for_timeout(random.randint(300, 700))
                vp = page.viewport_size or {"width": 1280, "height": 720}
                await page.mouse.move(
                    random.randint(100, vp["width"] - 100),
                    random.randint(100, vp["height"] - 100),
                )
                await page.wait_for_timeout(random.randint(200, 500))
                await page.mouse.move(
                    random.randint(100, vp["width"] - 100),
                    random.randint(100, vp["height"] - 100),
                )
            except Exception:
                pass
            html = await page.content()
            try:
                cookies = await _avito_async_context.cookies()
                with open(_AVITO_COOKIE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cookies, f)
            except Exception:
                pass
            return html
        finally:
            await page.close()


def _avito_fetch_html(url: str, wait_ms: int = 4000, timeout_ms: int = 30000) -> str:
    """Бесплатно получает HTML страницы Авито через headless-браузер (Playwright + stealth)."""
    try:
        _avito_ensure_loop()
        fut = _aio.run_coroutine_threadsafe(_avito_async_fetch(url, wait_ms, timeout_ms), _avito_loop)
        # +20s: warmup главной страницы + скролл/мышь
        return fut.result(timeout=(timeout_ms * 2 + wait_ms) / 1000 + 20)
    except Exception as e:
        print(f"  [Авито][браузер] ошибка: {e}")
        return ""


def _avito_scraperapi(url: str) -> "requests.Response | None":
    """Запрашивает страницу через ScraperAPI с JS-рендером и ждёт 5 секунд."""
    try:
        import requests as _req
        r = _req.get("http://api.scraperapi.com", params={
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "render": "true",
            "wait": "5000",
            "country_code": "ru",
            "ultra_premium": "true",
        }, timeout=120)
        return r
    except Exception as e:
        print(f"  [ScraperAPI] ошибка: {e}")
        return None


def _avito_extract_links(text: str, slug: str) -> list[str]:
    """Извлекает URL объявлений Авито прямо из HTML текста по паттерну href."""
    pattern = rf'href="(/{re.escape(slug)}/[a-z0-9_/-]+-\d{{5,}})"'
    hrefs = re.findall(pattern, text)
    seen = set()
    result = []
    for h in hrefs:
        if h not in seen:
            seen.add(h)
            result.append("https://www.avito.ru" + h)
    return result


def _avito_price_from_item(it: dict) -> tuple[str, int]:
    """Извлекает цену из объекта Авито. Возвращает (строка, число)."""
    def _find_price_in_obj(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return "", 0
        # Текстовое значение цены — проверяем ПЕРВЫМ (сохраняем форматирование)
        for text_key in ("valueText", "text", "label", "displayValue"):
            t = obj.get(text_key)
            if t and isinstance(t, str):
                digits = re.sub(r"[^\d]", "", t)
                if digits and 10_000 < int(digits) < 99_000_000:
                    return t, int(digits)
        # Прямое числовое значение
        for val_key in ("value", "number", "amount", "price", "sum"):
            v = obj.get(val_key)
            if v and isinstance(v, (int, float)) and 10_000 < v < 99_000_000:
                text_v = obj.get("valueText") or obj.get("text") or f"{int(v):,} ₽".replace(",", " ")
                return str(text_v), int(v)
        # Рекурсия в под-объекты
        for k, v in obj.items():
            if isinstance(v, dict):
                r_str, r_int = _find_price_in_obj(v, depth + 1)
                if r_int:
                    return r_str, r_int
        return "", 0

    for key in ("priceDetailed", "price", "priceInfo", "priceMicro", "priceValue",
                "salePrice", "discountedPrice", "finalPrice", "currentPrice"):
        info = it.get(key)
        if not info:
            continue
        if isinstance(info, (int, float)) and 10_000 < info < 99_000_000:
            return f"{int(info):,} ₽".replace(",", " "), int(info)
        if isinstance(info, dict):
            # Сначала ищем valueText — самый надёжный источник цены
            vt = info.get("valueText") or info.get("text") or info.get("displayValue") or ""
            if vt and isinstance(vt, str):
                digits = re.sub(r"[^\d]", "", vt)
                if digits and 10_000 < int(digits) < 99_000_000:
                    return vt, int(digits)
            r_str, r_int = _find_price_in_obj(info)
            if r_int:
                return r_str, r_int
    # Deep search: некоторые форматы хранят цену не в стандартных ключах
    def _deep_price_search(obj, depth=0):
        if depth > 8 or not isinstance(obj, dict):
            return "", 0
        for k, v in obj.items():
            lk = k.lower()
            if any(x in lk for x in ("price", "cost", "amount", "sum", "стоим", "цен")):
                if isinstance(v, (int, float)) and 10_000 < v < 99_000_000:
                    return f"{int(v):,} ₽".replace(",", " "), int(v)
                if isinstance(v, str):
                    d = re.sub(r"[^\d]", "", v)
                    if d and 10_000 < int(d) < 99_000_000:
                        return v, int(d)
                if isinstance(v, dict):
                    rs, ri = _find_price_in_obj(v, 0)
                    if ri:
                        return rs, ri
            elif isinstance(v, dict):
                rs, ri = _deep_price_search(v, depth + 1)
                if ri:
                    return rs, ri
        return "", 0
    r_str, r_int = _deep_price_search(it)
    if r_int:
        return r_str, r_int
    return "", 0


def _avito_desc_from_title(title: str, mileage: int = 0) -> str:
    """Синтезирует описание из структурированного заголовка Авито.

    Заголовок объявления Авито имеет вид:
        "ВАЗ (LADA) 2109 1.3 MT, 1988, 1 000 000 км"
    Из него можно вытащить: модель, объём двигателя, КПП, год, пробег.
    Карточка никогда не должна выглядеть пустой — это гарантия descriptions.
    """
    if not title:
        return ""
    parts = []
    # Год выпуска
    ym = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    if ym:
        parts.append(f"{ym.group(1)} г.")
    # Объём двигателя (1.3, 2.0 и т.п.)
    em = re.search(r"\b(\d\.\d)\b", title)
    if em:
        parts.append(f"{em.group(1)} л")
    # Коробка передач
    tl = title.upper()
    if re.search(r"\bAT\b|АКПП|АКП|\bАТ\b", tl):
        parts.append("АКПП")
    elif re.search(r"\bMT\b|МКПП|МКП|\bМТ\b", tl):
        parts.append("МКПП")
    elif re.search(r"\bCVT\b|вариатор", tl, re.I):
        parts.append("вариатор")
    elif re.search(r"\bAMT\b|робот", tl, re.I):
        parts.append("робот")
    # Пробег: из параметра или из заголовка
    if mileage and mileage > 0:
        parts.append(f"{mileage:,} км".replace(",", " "))
    else:
        mm = re.search(r"([\d][\d\s ]{2,})\s*км", title)
        if mm:
            km = re.sub(r"[^\d]", "", mm.group(1))
            if km:
                parts.append(f"{int(km):,} км".replace(",", " "))
    return " · ".join(parts)


def _avito_item_from_json(it: dict, today) -> dict | None:
    """Преобразует объект Авито JSON в dict объявления. Возвращает None для дилеров."""
    try:
        title = it.get("title", "")
        url_path = it.get("urlPath") or it.get("url", "")
        if not url_path:
            return None
        item_url = ("https://www.avito.ru" + url_path) if url_path.startswith("/") else url_path
        if not title or "avito.ru" not in item_url:
            return None
        print(f"  [item] title={title[:30]!r} url={url_path[:40]!r}")

        # Фильтр дилеров по типу продавца в JSON
        seller_obj = it.get("seller") or it.get("user") or {}
        seller_name = ""
        if isinstance(seller_obj, dict):
            seller_type = (
                seller_obj.get("type") or
                seller_obj.get("accountType") or
                seller_obj.get("sellerType") or
                seller_obj.get("userType") or ""
            ).lower()
            # company, shop, dealer, business, commercial — дилеры (type="1" — это частник, НЕ фильтруем!)
            # ВАЖНО: используем точное совпадение или разграниченные подстроки
            # чтобы не отфильтровать частников с типом "private", "1" и т.п.
            _DEALER_TYPES = {"company", "shop", "dealer", "business", "commercial"}
            if seller_type in _DEALER_TYPES or any(
                seller_type == t or seller_type.startswith(t + "_") or seller_type.endswith("_" + t)
                for t in _DEALER_TYPES
            ):
                print(f"  [item] DROPPED (dealer type): seller_type={seller_type!r} title={title[:30]!r}")
                return None
            seller_name = seller_obj.get("name") or seller_obj.get("title") or ""
        # Дополнительная проверка только по НАЗВАНИЮ ПРОДАВЦА (не заголовку объявления)
        if seller_name and any(k in seller_name.lower() for k in ("автосалон", "автоцентр", "официальный", "ооо", "зао", "ип ", "дилер", "моторс", "авто групп", "автопрестиж")):
            print(f"  [item] DROPPED (dealer name): seller_name={seller_name!r}")
            return None

        price_str, price_int = _avito_price_from_item(it)

        # Объявление без цены — почти всегда дилерский шоурум-листинг ("цена по запросу"),
        # частники на Авито всегда указывают цену. Отбрасываем сразу, чтобы не показывать
        # карточки с "—" вместо цены.
        if not price_int:
            print(f"  [item] DROPPED (no price): {title[:30]!r}")
            return None

        mileage = 0
        for param in (it.get("params") or it.get("parameters") or []):
            if isinstance(param, dict):
                if param.get("type") == "mileage" or "пробег" in str(param.get("title","")).lower():
                    try: mileage = int(re.sub(r"[^\d]", "", str(param.get("value","") or param.get("valueText",""))))
                    except: pass
        # Если пробег не нашли в параметрах — вытаскиваем из заголовка ("..., 150 000 км")
        if not mileage:
            mm = re.search(r"([\d][\d\s ]{2,})\s*км", title)
            if mm:
                try: mileage = int(re.sub(r"[^\d]", "", mm.group(1)))
                except: pass

        def _find_avito_photo_in_obj(obj, depth=0) -> str:
            """Рекурсивно ищет первый URL фото Авито в любом месте JSON объекта."""
            if depth > 12 or obj is None:
                return ""
            if isinstance(obj, str):
                low = obj.lower()
                if len(obj) > 15 and "avito.st" in low and (obj.startswith("//") or obj.startswith("http")):
                    raw = obj.replace("\\/", "/")
                    url_c = ("https:" + raw) if raw.startswith("//") else raw
                    url_lo = url_c.lower()
                    # Реальные фото объявлений всегда на img.avito.st/image/
                    # Логотипы/заглушки Авито лежат на других путях (avito.st/s/, avito.st/static/ и т.п.)
                    if "img.avito.st" not in url_lo and "images.avito.st" not in url_lo:
                        return ""
                    if not any(x in url_lo for x in ("/stub", "noimage", "placeholder", "/ava/", "/avatar/", "/userava", "/user_ava", "/logo", "/icon", "favicon", "/brand", "/promo")):
                        return url_c
                return ""
            if isinstance(obj, list):
                for el in obj:
                    r = _find_avito_photo_in_obj(el, depth + 1)
                    if r:
                        return r
                return ""
            if isinstance(obj, dict):
                def _avito_url_ok(u: str) -> bool:
                    lo = u.lower()
                    # Только реальный CDN фотографий объявлений
                    if "img.avito.st" not in lo and "images.avito.st" not in lo and "cdn.avito.st" not in lo and "s.avito.st" not in lo:
                        return False
                    return not any(x in lo for x in (
                        "/stub", "noimage", "placeholder", "/ava/", "/avatar/",
                        "/userava", "/user_ava", "/logo", "/icon", "favicon",
                        "/brand", "/promo", "/static/", "default",
                    ))

                # Ищем по всем известным ключам размеров фото Авито
                for size in ("1280x960", "1208x906", "864x648", "640x480",
                             "432x324", "320x240", "100x75", "originalSize", "big", "small"):
                    v = obj.get(size)
                    if isinstance(v, str) and "avito.st" in v.lower():
                        raw = v.replace("\\/", "/")
                        url_c = ("https:" + raw) if raw.startswith("//") else raw
                        if _avito_url_ok(url_c):
                            return url_c
                # Прямые ключи-превью (часто содержат готовый URL фото)
                for k in ("url", "thumb", "thumbnail", "coverImage", "firstImage", "src"):
                    v = obj.get(k)
                    if isinstance(v, str) and "avito.st" in v.lower():
                        raw = v.replace("\\/", "/")
                        url_c = ("https:" + raw) if raw.startswith("//") else raw
                        if _avito_url_ok(url_c):
                            return url_c
                    elif isinstance(v, (dict, list)):
                        r = _find_avito_photo_in_obj(v, depth + 1)
                        if r:
                            return r
                # Рекурсия в приоритетные ключи (включая "sizes" — Авито иногда прячет URLs туда)
                for k in ("sizes", "images", "photos", "gallery", "media", "image", "photo", "preview", "data"):
                    if k in obj:
                        r = _find_avito_photo_in_obj(obj[k], depth + 1)
                        if r:
                            return r
                # Полный обход всех значений (включая строки!)
                for v in obj.values():
                    if isinstance(v, str):
                        r = _find_avito_photo_in_obj(v, depth + 1)
                        if r:
                            return r
                    elif isinstance(v, (dict, list)):
                        r = _find_avito_photo_in_obj(v, depth + 1)
                        if r:
                            return r
            return ""

        photo_url = _find_avito_photo_in_obj(it)
        if not photo_url:
            # Резервный поиск: regex по сериализованному JSON объявления
            _it_str = json.dumps(it, ensure_ascii=False)
            _img_m = re.search(
                r'(https?://(?:img|images)\.avito\.st/[^\s"\'\\]{10,}\.(?:jpg|jpeg|webp|png))',
                _it_str
            )
            if _img_m:
                _cand = _img_m.group(1).replace("\\/", "/")
                if not any(x in _cand.lower() for x in ("/stub", "noimage", "/logo", "/icon", "/brand", "/ava/")):
                    photo_url = _cand
        if not photo_url:
            photo_url = ""

        # Всегда формируем текст цены из числа — страховка от пустого valueText
        if price_int and not price_str:
            price_str = f"{price_int:,} ₽".replace(",", " ")

        # РЕАЛЬНОЕ описание объявления продавца из JSON поисковой выдачи.
        def _find_desc_in_obj(obj, depth=0) -> str:
            """Рекурсивно ищет описание в любом месте JSON-объекта."""
            if depth > 6 or not isinstance(obj, dict):
                return ""
            for k in ("description", "descriptionFull", "shortDescription",
                      "text", "body", "content", "fullDescription", "advertDescription"):
                v = obj.get(k)
                if isinstance(v, str) and len(v) > 30:
                    return v.strip()
            for k in ("item", "advert", "data", "offer"):
                v = obj.get(k)
                if isinstance(v, dict):
                    r = _find_desc_in_obj(v, depth + 1)
                    if r:
                        return r
            return ""

        _desc_real = _find_desc_in_obj(it)
        if not _desc_real:
            _desc_real = ""
        # _desc_synthetic=True означает, что описание собрано нами из заголовка/
        # параметров, а НЕ взято из текста объявления. В этом случае _ensure_photo
        # дозагрузит настоящее описание со страницы объявления.
        _desc_synthetic = False
        _desc_raw = _desc_real
        # Если реального описания нет — составляем из параметров (год, пробег, КПП…)
        if not _desc_raw:
            params = it.get("params") or it.get("parameters") or []
            desc_parts = []
            for p in params:
                if isinstance(p, dict):
                    pname = p.get("title") or p.get("name") or ""
                    pval  = p.get("valueText") or p.get("value") or ""
                    if pname and pval and str(pval) not in ("0", ""):
                        desc_parts.append(f"{pname}: {pval}")
            if desc_parts:
                _desc_raw = " · ".join(desc_parts[:8])
                _desc_synthetic = True
        # Гарантия: если описания всё ещё нет — синтезируем из заголовка,
        # чтобы карточка никогда не была пустой (год · объём · КПП · пробег).
        if not _desc_raw:
            _desc_raw = _avito_desc_from_title(title, mileage)
            _desc_synthetic = True

        # Реальная дата объявления из sortTimeStamp (мс). Если её нет —
        # считаем «сегодня». Так не показываем ложное «сегодня» на старых.
        _days = 0
        _date_known = False
        _ts = (it.get("sortTimeStamp") or it.get("time") or
               it.get("addDate") or it.get("closingDate") or
               it.get("statsUpdateDate") or 0)
        try:
            if _ts:
                _ts_sec = int(_ts) / 1000 if int(_ts) > 10_000_000_000 else int(_ts)
                _posted = datetime.datetime.fromtimestamp(_ts_sec).date()
                _days = max(0, (today - _posted).days)
                _date_known = True
        except Exception:
            _days = 0

        _images_list = it.get("images") or it.get("photos") or it.get("gallery") or []
        item = {
            "source": "avito", "title": title,
            "price": price_str, "url": item_url,
            "date": str(today - datetime.timedelta(days=_days)),
            "_photos": len(_images_list) if isinstance(_images_list, list) else 0,
            "_days_on_site": _days,
            "_date_known": _date_known,
            "description": _desc_raw[:400],
            "_desc_synthetic": _desc_synthetic,
            "seller": seller_name, "_photo_url": photo_url,
            "_price_int": price_int,
            "mileage": mileage,
            # Явный мусор: пробег >= 900 000 км (заглушки "1 000 000 км"),
            # такие объявления не должны доминировать в выдаче.
            "_junk": 1 if mileage >= 900_000 else 0,
        }
        item["_hot_score"] = hot_score(item)
        return item
    except Exception:
        return None


def _deep_get(d, path):
    """Получить значение по пути вида 'a.b.c' из вложенного dict."""
    for key in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _avito_find_images_map(obj, depth=0) -> dict:
    """Рекурсивно ищет карту картинок Авито: {str(item_id): images}.

    Признак карты: dict, у которого хотя бы половина ключей — числовые id (строки
    из цифр), а значения содержат где-то внутри URL на avito.st. Авито меняет путь
    к этой карте между версиями, поэтому ищем её по структуре, а не по фикс. пути.
    """
    if depth > 8 or not isinstance(obj, (dict, list)):
        return {}
    if isinstance(obj, dict):
        keys = list(obj.keys())
        numeric = [k for k in keys if isinstance(k, str) and k.isdigit() and len(k) >= 6]
        if numeric and len(numeric) >= max(1, len(keys) // 2):
            # Проверяем, что под числовыми ключами действительно лежат фото
            sample_val = obj.get(numeric[0])
            try:
                if "avito.st" in json.dumps(sample_val).lower():
                    return obj
            except Exception:
                pass
        for v in obj.values():
            r = _avito_find_images_map(v, depth + 1)
            if r:
                return r
        return {}
    for v in obj:
        r = _avito_find_images_map(v, depth + 1)
        if r:
            return r
    return {}


def _avito_find_items_in_json(obj, depth=0) -> list:
    """Рекурсивно ищет массив объявлений в JSON Авито."""
    if depth > 15 or not isinstance(obj, (dict, list)):
        return []
    if isinstance(obj, list):
        if len(obj) >= 1 and isinstance(obj[0], dict):
            sample = obj[0]
            # urlPath (начинается с /) + обязательный признак листинга (цена/фото/id)
            # Проверяем на РЕАЛЬНОЕ объявление, а не навигационный пункт
            url_path = sample.get("urlPath", "")
            if isinstance(url_path, str) and url_path.startswith("/") and (
                any(k in sample for k in ("priceDetailed", "price", "images", "gallery", "photos"))
                or ("id" in sample and "title" in sample and ("avto" in url_path or "auto" in url_path or "avtomobili" in url_path))
            ):
                print(f"  [findItems] найден массив len={len(obj)}, sample_url={url_path!r}")
                return obj
            # Альтернатива: url + priceDetailed/images (точные признаки листинга)
            if "url" in sample and any(k in sample for k in ("priceDetailed", "images", "gallery")):
                url_val = sample.get("url", "")
                if isinstance(url_val, str) and ("avito.ru" in url_val or url_val.startswith("/")):
                    print(f"  [findItems] найден массив (url+price/images) len={len(obj)}")
                    return obj
        for x in obj:
            r = _avito_find_items_in_json(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for key in ("items", "catalog", "listing", "offers", "ads", "cars",
                    "search", "results", "snippets", "adverts", "data", "list"):
            val = obj.get(key)
            if isinstance(val, list) and len(val) >= 1 and isinstance(val[0], dict):
                sample = val[0]
                url_path = sample.get("urlPath", "")
                if isinstance(url_path, str) and url_path.startswith("/") and (
                    any(k in sample for k in ("priceDetailed", "price", "images", "gallery", "photos"))
                    or ("id" in sample and "title" in sample and ("avto" in url_path or "auto" in url_path or "avtomobili" in url_path))
                ):
                    print(f"  [findItems] найден массив [{key}] len={len(val)}, sample_url={url_path!r}")
                    return val
                if any(k in sample for k in ("priceDetailed", "images", "gallery")):
                    print(f"  [findItems] найден массив [{key}] (price/images) len={len(val)}")
                    return val
        for v in obj.values():
            r = _avito_find_items_in_json(v, depth + 1)
            if r:
                return r
    return []


def _parse_avito_html(text: str, slug: str, today) -> list[dict]:
    """Парсит HTML страницы Авито: __NEXT_DATA__, HTML-карточки, ссылки."""
    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    try:
        soup = _BS(text, "lxml")
    except Exception:
        soup = _BS(text, "html.parser")

    # 0. ОСНОВНОЙ МЕТОД: __NEXT_DATA__ JSON (Next.js SSR).
    #    Авито — React/Next.js приложение: <img> в HTML отдают серые
    #    placeholder-квадраты, а реальные фото/описания/цены лежат в JSON-блоке
    #    __NEXT_DATA__ на КАЖДОЙ странице. Это главный источник данных.
    nd_match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S
    )
    nd = {}
    if nd_match:
        try:
            nd = json.loads(nd_match.group(1))
        except Exception:
            nd = {}

    _di1 = _deep_get(nd, "props.initialState.catalog.items")
    _di2 = _deep_get(nd, "props.pageProps.initialState.catalog.items")
    _di3 = _deep_get(nd, "initialState.catalog.items")
    _di4 = _deep_get(nd, "props.initialState.listing.catalog.items")
    _di5 = _deep_get(nd, "props.pageProps.catalog.items")
    print(f"  [parse] nd found={bool(nd)}, deep_get paths: {len(_di1) if _di1 else 0}/{len(_di2) if _di2 else 0}/{len(_di3) if _di3 else 0}/{len(_di4) if _di4 else 0}/{len(_di5) if _di5 else 0}")
    items_raw = _di1 or _di2 or _di3 or _di4 or _di5 or _avito_find_items_in_json(nd)
    print(f"  [parse] items_raw count={len(items_raw) if items_raw else 0}")
    # Авито хранит фото отдельно: catalog.itemsImages = {str(id): [{size: url}]}
    items_images_map: dict = (
        _deep_get(nd, "props.initialState.catalog.itemsImages") or
        _deep_get(nd, "props.pageProps.initialState.catalog.itemsImages") or
        _deep_get(nd, "initialState.catalog.itemsImages") or
        _deep_get(nd, "props.pageProps.catalog.itemsImages") or
        _deep_get(nd, "props.initialState.listing.catalog.itemsImages") or
        _deep_get(nd, "props.initialState.catalog.images") or
        {}
    )
    # Если по известным путям карты картинок нет — ищем её рекурсивно по структуре:
    # это dict, где ключ = числовой id объявления (строкой), значение = список/словарь
    # с avito.st URL. Авито периодически меняет путь, поэтому это надёжная страховка.
    if not items_images_map:
        items_images_map = _avito_find_images_map(nd) or {}
    # Regex-карта: item_id -> первый avito.st URL (запасной метод)
    _nd_text = nd_match.group(1) if nd_match else ""
    _cdn_re = re.compile(
        r'((?:https?:)?//(?:\d+\.)?(?:img|images)\.avito\.st/[^"\'\\<>\s]{5,})'
    )

    # Пути, однозначно указывающие на аватар продавца или системный значок — не фото машины
    _BAD_PHOTO_PATHS = ("/stub", "noimage", "placeholder", "/ava/", "/avatar/",
                        "/userAva/", "/user_ava", "/logo", "/icon", "favicon")

    if items_raw:
        print(f"  [Авито] __NEXT_DATA__ items={len(items_raw)}, images_map={len(items_images_map)}")
        for item_data in items_raw:
            if not isinstance(item_data, dict):
                continue
            # Всегда берём фото из itemsImages (карта точнее, чем item.images).
            # Ключи карты в JSON — строки, но id объявления может быть int/str —
            # пробуем оба варианта ключа.
            if items_images_map:
                raw_id = item_data.get("id")
                img_entry = None
                if raw_id is not None:
                    img_entry = (
                        items_images_map.get(str(raw_id))
                        or (items_images_map.get(raw_id) if not isinstance(raw_id, str) else None)
                    )
                if img_entry:
                    item_data = dict(item_data)
                    item_data["images"] = img_entry
            item = _avito_item_from_json(item_data, today)
            if item:
                # Если фото не нашли через JSON — ищем через regex в __NEXT_DATA__
                if not item.get("_photo_url") and _nd_text:
                    item_path = item["url"].replace("https://www.avito.ru", "")
                    esc_path = item_path.replace("/", "\\/")
                    for search_path in (esc_path, item_path):
                        idx = _nd_text.find(search_path)
                        if idx > -1:
                            chunk = _nd_text[max(0, idx - 200):idx + 3000]
                            m = _cdn_re.search(chunk)
                            if m:
                                raw = m.group(1).replace("\\/", "/")
                                url_c = ("https:" + raw) if raw.startswith("//") else raw
                                if not any(x in url_c.lower() for x in _BAD_PHOTO_PATHS):
                                    item["_photo_url"] = url_c
                                    break
                results.append(item)
                # Proximity-парсинг даты из HTML для объявлений с _days_on_site == 0
                if item.get("_days_on_site", 0) == 0:
                    _item_path = item["url"].replace("https://www.avito.ru", "")
                    _idx = text.find(_item_path.replace("/", "\\/"))
                    if _idx < 0:
                        _idx = text.find(_item_path)
                    if _idx >= 0:
                        _chunk = text[max(0, _idx - 500): _idx + 2000]
                        _days_found = None
                        _dm = re.search(r'(\d+)\s*дн[яей\.]+\s*назад', _chunk, re.I)
                        if _dm:
                            _days_found = int(_dm.group(1))
                        elif re.search(r'вчера', _chunk, re.I):
                            _days_found = 1
                        elif re.search(r'сегодня|час[а-я]*\s*назад|\d+\s*мин[уть]*\s*назад', _chunk, re.I):
                            _days_found = 0
                        if _days_found is None:
                            _ru_months = {"янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
                                          "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12}
                            _dm2 = re.search(r'(\d{1,2})\s+([а-яё]{3})', _chunk, re.I)
                            if _dm2:
                                try:
                                    _d, _m_str = int(_dm2.group(1)), _dm2.group(2)[:3].lower()
                                    _m = _ru_months.get(_m_str)
                                    if _m:
                                        _posted_dt = datetime.date(today.year, _m, _d)
                                        if _posted_dt > today:
                                            _posted_dt = datetime.date(today.year - 1, _m, _d)
                                        _days_found = max(0, (today - _posted_dt).days)
                                except Exception:
                                    pass
                        if _days_found is not None and _days_found > 0:
                            item["_days_on_site"] = _days_found
                            item["date"] = str(today - datetime.timedelta(days=_days_found))
        print(f"  [parse] после цикла: results={len(results)} из items_raw={len(items_raw)}")
        # Нельзя путать аватары продавцов с фото машины
        _BAD_PHOTO_PATHS = ("/stub", "noimage", "placeholder", "/ava/", "/avatar/",
                            "/userAva/", "/user_ava", "/logo", "/icon", "favicon")

        if results:
            # Proximity-fallback только если itemsImages не пришёл вообще.
            # Если карта есть, но для объявления пусто — реально нет фото, не берём чужое.
            _no_photo = [r for r in results if not r.get("_photo_url")]
            if _no_photo:
                _all_cdn: list[tuple[int, str]] = []
                for _im in re.finditer(
                    r'((?:https?:)?(?:\\?/){2}(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st'
                    r'/[^"\'<\s\\]{10,})',
                    text
                ):
                    _raw = _im.group(1).replace("\\/", "/").replace("\\u002F", "/")
                    _url = ("https:" + _raw) if _raw.startswith("//") else _raw
                    if not any(x in _url.lower() for x in _BAD_PHOTO_PATHS):
                        _all_cdn.append((_im.start(), _url))
                if _all_cdn:
                    for item in _no_photo:
                        _path = item["url"].replace("https://www.avito.ru", "")
                        _pos = text.find(_path.replace("/", "\\/"))
                        if _pos < 0:
                            _pos = text.find(_path)
                        if _pos < 0:
                            continue
                        _best_url = ""
                        _best_dist = 3000
                        for (_cdn_pos, _cdn_url) in _all_cdn:
                            _d = abs(_cdn_pos - _pos)
                            if _d < _best_dist:
                                _best_dist = _d
                                _best_url = _cdn_url
                        if _best_url:
                            item["_photo_url"] = _best_url
            with_photo = sum(1 for r in results if r.get("_photo_url"))
            with_desc  = sum(1 for r in results if r.get("description"))
            print(f"  [Авито] __NEXT_DATA__ итого: {len(results)} объявлений, "
                  f"с фото: {with_photo}, с описанием: {with_desc}")
            return results
        print("  [Авито] __NEXT_DATA__ дал 0 объявлений — fallback на BS4")

    # 0b. FALLBACK (как Дром): BeautifulSoup + CSS-селекторы прямо по
    #    карточкам поисковой выдачи. Используется только если __NEXT_DATA__
    #    отсутствует/пуст. ВНИМАНИЕ: <img> здесь часто placeholder'ы.
    cards = soup.select('[data-marker="item"]')
    print(f"  [Авито] BS4 cards (data-marker=item)={len(cards)}")
    for card in cards:
        try:
            # --- Ссылка ---
            link = (
                card.select_one("a[itemprop='url']")
                or card.select_one("a[data-marker='item-title']")
                or card.select_one(f"a[href*='/{slug}/']")
                or card.select_one("a[href*='/avtomobili/']")
                or card.select_one("a[href]")
            )
            href = (link.get("href", "") if link else "").split("?")[0]
            item_url = ("https://www.avito.ru" + href) if href.startswith("/") else href
            if not item_url or "avito.ru" not in item_url:
                continue

            # --- Заголовок ---
            title_el = (
                card.select_one("[itemprop='name']")
                or card.select_one("[data-marker='item-title']")
                or card.select_one("h3")
                or card.select_one("h2")
            )
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            card_str = str(card)

            # --- Цена ---
            price = ""
            price_int = 0
            meta_price = card.select_one("meta[itemprop='price']")
            if meta_price and meta_price.get("content"):
                price_int = parse_price(meta_price.get("content"))
            if not price_int:
                price_el = (
                    card.select_one("[data-marker='item-price']")
                    or card.select_one("[itemprop='price']")
                    or card.select_one("[class*='price']")
                    or card.select_one("[class*='Price']")
                )
                if price_el:
                    price = price_el.get("content") or price_el.get_text(strip=True)
                    price_int = parse_price(price)
            if not price_int:
                cm = re.search(r'content="(\d{5,8})"', card_str)
                if cm and 10_000 < int(cm.group(1)) < 99_000_000:
                    price_int = int(cm.group(1))
            if not price_int:
                tm = re.search(r'(\d[\d\s ]{4,12})\s*(?:₽|руб)', card_str)
                if tm:
                    price_int = parse_price(tm.group(1))
            if price_int and not price:
                price = f"{price_int:,} ₽".replace(",", " ")
            if not price_int:
                continue  # без цены — не показываем

            # --- Описание (из карточки) ---
            desc_el = (
                card.select_one("[data-marker='item-description']")
                or card.select_one("p[class*='description']")
                or card.select_one("div[class*='description']")
                or card.select_one("[class*='iva-item-text']")
            )
            description = desc_el.get_text(" ", strip=True)[:300] if desc_el else ""

            # --- Дата ---
            date_el = card.select_one("[data-marker='item-date']")
            date_text = date_el.get_text(strip=True) if date_el else ""

            # --- Фото (как Дром): первый <img> с реальным фото Авито ---
            photo_url = ""
            for img_el in card.find_all("img"):
                src = (img_el.get("src") or img_el.get("data-src") or
                       img_el.get("data-lazy-src") or img_el.get("data-original") or "")
                if not src and img_el.get("srcset"):
                    src = img_el.get("srcset").split()[0]
                if src.startswith("//"):
                    src = "https:" + src
                _sl = src.lower()
                if ("img.avito.st" in _sl or "images.avito.st" in _sl) and src.startswith("http"):
                    if any(x in _sl for x in ("placeholder", "logo", "stub", "noimage", "/icon", "/brand", "/static/")):
                        continue
                    photo_url = src
                    break
            if not photo_url:
                img_m = re.search(
                    r'((?:https?:)?//(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st/[^"\']+\.(?:jpg|jpeg|webp|png))',
                    card_str
                )
                if img_m:
                    raw = img_m.group(1)
                    photo_url = ("https:" + raw) if raw.startswith("//") else raw

            item = {
                "source": "avito", "title": title, "price": price,
                "url": item_url, "date": str(today),
                "_photos": 1 if photo_url else 0, "_days_on_site": 0,
                "description": description, "seller": "", "_photo_url": photo_url,
                "_price_int": price_int, "_date_text": date_text,
            }
            item["_hot_score"] = hot_score(item)
            results.append(item)
        except Exception:
            pass

    if results:
        # Полная страница: ищем все CDN-URL и назначаем фото объявлениям без фото
        _no_photo_bs4 = [r for r in results if not r.get("_photo_url")]
        if _no_photo_bs4:
            _all_cdn_bs4: list[tuple[int, str]] = []
            for _im in re.finditer(
                r'((?:https?:)?(?:\\?/){2}(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st'
                r'/[^"\'<\s\\]{10,})',
                text
            ):
                _raw = _im.group(1).replace("\\/", "/").replace("\\u002F", "/")
                _url = ("https:" + _raw) if _raw.startswith("//") else _raw
                if not any(x in _url.lower() for x in ("/stub", "noimage", "placeholder")):
                    _all_cdn_bs4.append((_im.start(), _url))
            if _all_cdn_bs4:
                for item in _no_photo_bs4:
                    _path = item["url"].replace("https://www.avito.ru", "")
                    _pos = text.find(_path.replace("/", "\\/"))
                    if _pos < 0:
                        _pos = text.find(_path)
                    if _pos < 0:
                        continue
                    _best_url = ""
                    _best_dist = 5000
                    for (_cdn_pos, _cdn_url) in _all_cdn_bs4:
                        _d = abs(_cdn_pos - _pos)
                        if _d < _best_dist:
                            _best_dist = _d
                            _best_url = _cdn_url
                    if _best_url:
                        item["_photo_url"] = _best_url
        print(f"  [Авито] BS4 итого: {len(results)} объявлений")
        return results

    # === FALLBACK (страница заблокирована / другой формат) ===
    # 1. __NEXT_DATA__ (Next.js SSR)
    nd = soup.find("script", {"id": "__NEXT_DATA__"})
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            items_raw = _avito_find_items_in_json(data)
            print(f"  [Авито] __NEXT_DATA__ найден, items_raw={len(items_raw)}")
            for it in items_raw:
                item = _avito_item_from_json(it, today)
                if item:
                    results.append(item)
            if results:
                return results
        except Exception as e:
            print(f"  [Авито] __NEXT_DATA__ ошибка: {e}")

    # 1b. Любой <script> тег с "items":[ или "catalog":[
    if not results:
        for sc in soup.find_all("script"):
            sc_text = sc.string or ""
            if len(sc_text) < 500:
                continue
            for marker in ('"items":[{', '"catalog":[{', '"listing":[{', '"offers":[{'):
                if marker not in sc_text:
                    continue
                idx = sc_text.find(marker) + len(marker) - 2  # позиция [
                chunk = sc_text[idx:]
                depth = 0
                end = 0
                in_str = False
                esc = False
                for i, ch in enumerate(chunk):
                    if esc:
                        esc = False
                        continue
                    if ch == '\\' and in_str:
                        esc = True
                        continue
                    if ch == '"':
                        in_str = not in_str
                        continue
                    if not in_str:
                        if ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                end = i + 1
                                break
                if end:
                    try:
                        arr = json.loads(chunk[:end])
                        if isinstance(arr, list) and len(arr) >= 2:
                            for it in arr:
                                item = _avito_item_from_json(it, today)
                                if item:
                                    results.append(item)
                    except Exception:
                        pass
            if results:
                print(f"  [Авито] script-JSON: {len(results)} объявлений")
                return results

    # 2. HTML карточки с data-marker="item"
    cards = soup.select("[data-marker='item']")
    print(f"  [Авито] HTML cards={len(cards)}")
    for card in cards:
        try:
            link = (
                card.select_one("a[data-marker='item-title']")
                or card.select_one(f"a[href*='/{slug}/']")
                or card.select_one("a[href*='/avtomobili/']")
                or card.select_one("a[href]")
            )
            href = link.get("href", "") if link else ""
            href = href.split("?")[0]  # убираем tracking-параметры (иначе повторы)
            item_url = ("https://www.avito.ru" + href) if href.startswith("/") else href
            if not item_url or "avito.ru" not in item_url:
                continue

            title_el = (
                card.select_one("[data-marker='item-title']")
                or card.select_one("[itemprop='name']")
                or card.select_one("h3")
                or card.select_one("h2")
            )
            title = title_el.get_text(strip=True) if title_el else ""

            card_str = str(card)

            # Цена — несколько стратегий, т.к. Авито меняет классы:
            # 1) meta itemprop=price content="850000"
            # 2) data-marker="item-price"
            # 3) любой элемент с itemprop/class price
            # 4) regex по сырому HTML карточки ("850 000 ₽" / content="850000")
            price = ""
            price_int = 0
            meta_price = card.select_one("meta[itemprop='price']")
            if meta_price and meta_price.get("content"):
                price_int = parse_price(meta_price.get("content"))
            if not price_int:
                price_el = (
                    card.select_one("[data-marker='item-price']")
                    or card.select_one("[itemprop='price']")
                    or card.select_one("[class*='price']")
                    or card.select_one("[class*='Price']")
                )
                if price_el:
                    price = price_el.get("content") or price_el.get_text(strip=True)
                    price_int = parse_price(price)
            if not price_int:
                # content="850000" где-то в карточке
                cm = re.search(r'content="(\d{5,8})"', card_str)
                if cm and 10_000 < int(cm.group(1)) < 99_000_000:
                    price_int = int(cm.group(1))
            if not price_int:
                # "850 000 ₽" или "850000 ₽" в тексте
                tm = re.search(r'(\d[\d\s ]{4,12})\s*(?:₽|руб)', card_str)
                if tm:
                    price_int = parse_price(tm.group(1))
            if price_int and not price:
                price = f"{price_int:,} ₽".replace(",", " ")

            # Фото — ТОЛЬКО реальные фото объявлений Авито: .img.avito.st/image/...
            # (иначе ловятся промо-баннеры и иконки, напр. мультяшный ноутбук)
            photo_url = ""
            for img_el in card.find_all("img"):
                src = (img_el.get("src") or img_el.get("data-src") or
                       img_el.get("data-lazy-src") or img_el.get("data-original") or "")
                if not src and img_el.get("srcset"):
                    src = img_el.get("srcset").split()[0]
                if src.startswith("//"):
                    src = "https:" + src
                _sl = src.lower()
                if ("img.avito.st/" in _sl or "images.avito.st/" in _sl) and src.startswith("http"):
                    photo_url = src
                    break
            if not photo_url:
                img_m = re.search(r'((?:https?:)?//(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st/[^"\']+\.(?:jpg|jpeg|webp|png))', card_str)
                if img_m:
                    raw = img_m.group(1)
                    photo_url = ("https:" + raw) if raw.startswith("//") else raw

            if title:
                item = {
                    "source": "avito", "title": title, "price": price,
                    "url": item_url, "date": str(today),
                    "_photos": 1 if photo_url else 0, "_days_on_site": 0,
                    "description": "", "seller": "", "_photo_url": photo_url,
                    "_price_int": price_int,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass

    if results:
        return results

    # 2.5 НАДЁЖНЫЙ МЕТОД: извлекаем полный JSON-объект КАЖДОГО объявления методом
    #     балансировки скобок и парсим его через _avito_item_from_json. Работает
    #     даже когда Авито убрал __NEXT_DATA__ — данные всё равно лежат как JSON
    #     где-то в HTML (видно по наличию "urlPath"). Так получаем правильную
    #     цену/фото/описание/продавца ИЗ ОБЪЕКТА КАЖДОГО объявления, а не из окна.
    if '"urlPath"' in text and not results:
        def _find_enclosing_object(s: str, pos: int) -> "str | None":
            """От позиции внутри объекта идём НАЗАД до открывающей { этого объекта,
            затем ВПЕРЁД (с учётом строк/экранирования) до парной }. Возвращает
            валидный JSON-объект объявления целиком."""
            depth = 0
            i = pos
            start = None
            low = max(0, pos - 60000)
            while i >= low:
                c = s[i]
                if c == '}':
                    depth += 1
                elif c == '{':
                    if depth == 0:
                        start = i; break
                    depth -= 1
                i -= 1
            if start is None:
                return None
            d = 0; in_str = False; esc = False
            for j in range(start, min(len(s), start + 60000)):
                c = s[j]
                if esc:
                    esc = False; continue
                if c == '\\':
                    esc = True; continue
                if c == '"':
                    in_str = not in_str; continue
                if in_str:
                    continue
                if c == '{':
                    d += 1
                elif c == '}':
                    d -= 1
                    if d == 0:
                        return s[start:j + 1]
            return None

        _seen_b: set = set()
        _price_re_blob = re.compile(
            r'"(?:priceDetailed|price|priceInfo|priceMicro)"\s*:\s*\{[^}]{0,300}"(?:valueText|value)"\s*:\s*"?(\d[\d\s]{3,10})"?'
            r'|"(?:valueText|displayValue)"\s*:\s*"([\d\s]{4,12}\s*(?:₽|руб))"'
            r'|"value"\s*:\s*(\d{5,8})\b'
        )
        for m in re.finditer(r'"urlPath"\s*:\s*"(/[^"]*avtomobili/[^"]+)"', text):
            blob = _find_enclosing_object(text, m.start())
            if not blob or '"urlPath"' not in blob:
                continue
            # Получаем URL и title прямо из blob regex — не зависим от полного json.loads
            _up_m = re.search(r'"urlPath"\s*:\s*"(/[^"]+)"', blob)
            _ti_m = re.search(r'"title"\s*:\s*"([^"]{5,120})"', blob)
            if not _up_m:
                continue
            _raw_url = "https://www.avito.ru" + _up_m.group(1)
            if _raw_url in _seen_b:
                continue
            it = None
            try:
                obj = json.loads(blob)
                it = _avito_item_from_json(obj, today)
            except Exception:
                pass
            # Если json.loads провалился или _avito_item_from_json не нашёл цену —
            # пробуем вытащить цену напрямую регулярным выражением из blob-текста.
            if it is None and _ti_m:
                _price_int_b = 0
                for _pm in _price_re_blob.finditer(blob):
                    _raw_p = next((g for g in _pm.groups() if g), "")
                    _digits = re.sub(r"[^\d]", "", _raw_p)
                    if _digits and 10_000 < int(_digits) < 99_000_000:
                        _price_int_b = int(_digits)
                        break
                if _price_int_b:
                    _title_b = _ti_m.group(1)
                    it = {
                        "source": "avito", "title": _title_b,
                        "price": f"{_price_int_b:,} ₽".replace(",", " "),
                        "url": _raw_url, "date": str(today),
                        "_photos": 0, "_days_on_site": 0,
                        "description": "", "seller": "", "_photo_url": "",
                        "_price_int": _price_int_b, "mileage": 0,
                        "_avito_price_filtered": True,
                    }
                    it["_hot_score"] = hot_score(it)
            if it and it.get("url") and it["url"] not in _seen_b:
                _seen_b.add(it["url"])
                results.append(it)
        print(f"  [Авито] brace-JSON (urlPath) извлёк {len(results)} объявлений с ценой/фото")
        if results:
            return results

    # 3. Regex по "urlPath" + "title" прямо в тексте скриптов
    has_urlpath = '"urlPath"' in text
    print(f"  [Авито] в тексте: urlPath={has_urlpath}, размер={len(text):,}")
    # Извлекаем urlPath + title (не пересекаем границу объекта [^}])
    url_title_pairs = re.findall(
        r'"urlPath"\s*:\s*"(/[^"]{10,})"[^}]{0,600}"title"\s*:\s*"([^"]{5,100})"',
        text
    )
    if not url_title_pairs:
        # Попробуем title → urlPath (порядок может быть обратным)
        url_title_pairs = [
            (u, t) for t, u in re.findall(
                r'"title"\s*:\s*"([^"]{5,100})"[^}]{0,600}"urlPath"\s*:\s*"(/[^"]{10,})"',
                text
            )
        ]
    print(f"  [Авито] url+title пар: {len(url_title_pairs)}")

    # Строим карту urlPath → цена: ищем ценовые поля в широком окне вокруг urlPath
    price_map: dict[str, int] = {}

    def _find_price_in_window(window: str) -> int:
        """Пробует все известные форматы цены Авито. Возвращает 0 если не нашёл."""
        patterns = [
            r'"priceDetailed"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"price"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"priceInfo"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"price"\s*:\s*(\d{5,8})',       # цена как прямое число
            r'"valueText"\s*:\s*"([\d \s]+)\s*[₽р]"',  # "1 200 000 ₽"
            r'"valueText"\s*:\s*"(\d[\d\s]+)"',
        ]
        for pat in patterns:
            pm = re.search(pat, window)
            if pm:
                raw = re.sub(r'\D', '', pm.group(1))
                if raw:
                    v = int(raw)
                    if 10_000 < v < 99_000_000:
                        return v
        return 0

    # Карта urlPath → фото (ищем CDN-ссылки рядом с urlPath)
    photo_map: dict[str, str] = {}
    for m in re.finditer(r'"urlPath"\s*:\s*"(/[^"]{10,})"', text):
        upath = m.group(1).split("?")[0]
        window_start = max(0, m.start() - 1000)
        window_end = min(len(text), m.end() + 4000)
        window = text[window_start:window_end]
        v = _find_price_in_window(window)
        if v:
            price_map[upath] = v
        # Ищем фото CDN Авито — любой хост *.avito.st с картинкой. Учитываем
        # экранированные слэши (\/) и спецсимволы (~) в JSON-ответе.
        img_m = re.search(
            r'((?:https?:)?(?:\\?/){2}[a-z0-9.\-]*avito\.st(?:(?:\\?/)[\w.~\-]+)+\.(?:jpg|jpeg|webp|png|avif))',
            window, re.I,
        )
        if img_m:
            raw_url = img_m.group(1).replace("\\/", "/")
            photo_map[upath] = ("https:" + raw_url) if raw_url.startswith("//") else raw_url

    sample_prices = list(price_map.values())[:5]
    print(f"  [Авито] цен найдено: {len(price_map)}, фото: {len(photo_map)}, примеры: {sample_prices}")
    seen_urls: set = set()
    for url_path, title in url_title_pairs[:80]:
        if not url_path.startswith("/") or len(url_path) < 10:
            continue
        if any(skip in url_path for skip in ("/profile/", "/user/", "/search?", "/avtomobili?", "/category/")):
            continue
        url_path = url_path.split("?")[0]
        item_url = "https://www.avito.ru" + url_path
        if item_url in seen_urls:
            continue
        seen_urls.add(item_url)
        price_int = price_map.get(url_path, 0)
        price = f"{price_int:,} ₽".replace(",", " ") if price_int else ""
        photo_url = photo_map.get(url_path, "")
        item = {
            "source": "avito", "title": title, "price": price,
            "url": item_url, "date": str(today),
            "_photos": 1 if photo_url else 0, "_days_on_site": 0,
            "description": "", "seller": "", "_photo_url": photo_url,
            "_price_int": price_int,
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)

    print(f"  [Авито] regex итого: {len(results)}")
    return results


def _avito_get_oauth_token() -> str:
    """Получает OAuth-токен Авито через client_credentials (бесплатный официальный API)."""
    global _avito_oauth_token
    import time as _time
    import requests as _rq
    now = _time.time()
    if _avito_oauth_token and _avito_oauth_token.get("expires_at", 0) > now + 60:
        return _avito_oauth_token["token"]
    if not AVITO_CLIENT_ID or not AVITO_CLIENT_SECRET:
        return ""
    try:
        r = _rq.post("https://api.avito.ru/token", data={
            "client_id": AVITO_CLIENT_ID,
            "client_secret": AVITO_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            token = data.get("access_token", "")
            expires_in = data.get("expires_in", 3600)
            _avito_oauth_token = {"token": token, "expires_at": now + expires_in}
            print(f"  [Авито OAuth] токен получен, expires_in={expires_in}s")
            return token
        else:
            print(f"  [Авито OAuth] ошибка {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  [Авито OAuth] {e}")
    return ""


def _avito_api_fetch(region: str, pages: int, price_min: int, price_max: int, today) -> list[dict]:
    """
    Использует внутренний JSON API Авито (как мобильное приложение).
    Пробует несколько эндпоинтов с разными заголовками — мобильный сайт,
    cloudscraper с Android UA, публичный API. Эти каналы имеют менее
    агрессивную антибот-защиту, чем десктопный веб-скрейпинг.
    """
    try:
        import requests as _req
    except ImportError:
        return []

    slug = AVITO_SLUGS.get(region, region)
    location_id = AVITO_LOCATION_IDS.get(region, 637640)
    results: list[dict] = []

    session = _req.Session()

    # Заголовки мобильного браузера Android
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }

    # cloudscraper с мобильным UA — обходит CF/JS-challenge без браузера
    try:
        import cloudscraper as _cs
        cs_session = _cs.create_scraper(
            browser={"browser": "chrome", "platform": "android", "mobile": True},
            delay=2,
        )
    except ImportError:
        cs_session = None

    def _items_from_json_response(data) -> list[dict]:
        """Извлекает объявления из любого JSON-ответа Авито."""
        items_raw = _avito_find_items_in_json(data)
        out = []
        for it in items_raw:
            item = _avito_item_from_json(it, today)
            if item:
                out.append(item)
        return out

    def _try_mobile_site(p: int) -> list[dict]:
        """m.avito.ru — мобильный сайт, отдельная антибот-цепочка от десктопа.
        Пробует несколько URL-вариантов: с фильтром частников, без фильтра,
        и через корневой домен — чтобы найти хоть один незаблокированный эндпоинт."""
        # Параметры с фильтром частников
        params_private: dict = {"seller_type": "1"}
        if p > 1:
            params_private["p"] = p
        if price_min > 0:
            params_private["pmin"] = price_min
        if price_max < 99_000_000:
            params_private["pmax"] = price_max

        # Параметры без фильтра частников — фильтруем дилеров в коде
        params_no_filter: dict = {}
        if p > 1:
            params_no_filter["p"] = p

        # s=1 — сортировка по цене (дешёвые первыми) — находит больше вариантов ниже рынка
        params_price_sort = {**params_private, "s": "1"}
        urls_to_try = [
            (f"https://m.avito.ru/{slug}/avtomobili", params_price_sort),
            (f"https://m.avito.ru/{slug}/avtomobili", params_private),
            # Без фильтра seller_type — меньше параметров, иногда не триггерит капчу
            (f"https://m.avito.ru/{slug}/avtomobili", params_no_filter),
            # Корневой домен без субдомена
            (f"https://avito.ru/{slug}/avtomobili", {}),
        ]
        for url, params in urls_to_try:
            try:
                r = session.get(url, params=params, headers=mobile_headers, timeout=20, proxies=_avito_proxies())
                print(f"  [Авито m.] {url} стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200 and ('"urlPath"' in r.text or 'data-marker="item"' in r.text or '__NEXT_DATA__' in r.text):
                    result = _parse_avito_html(r.text, slug, today)
                    if result:
                        return result
            except Exception as e:
                print(f"  [Авито m.] {url} стр.{p}: {e}")
        return []

    def _try_cffi_web(p: int) -> list[dict]:
        """curl_cffi Chrome impersonation — обходит TLS fingerprinting Авито без прокси."""
        try:
            from curl_cffi import requests as _cffi
            _brand_path = f"/{brand}" if brand and brand != "any" else ""
            url = f"https://www.avito.ru/{slug}/avtomobili{_brand_path}"
            params: dict = {"seller_type": "1"}
            if p > 1:
                params["p"] = p
            if price_min > 0:
                params["pmin"] = price_min
            if price_max < 99_000_000:
                params["pmax"] = price_max
            r = _cffi.get(url, params=params, impersonate="chrome124", timeout=18, headers={
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": "https://www.avito.ru/",
            })
            print(f"  [Авито cffi] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
            if r.status_code == 200 and ('"urlPath"' in r.text or 'data-marker="item"' in r.text or '__NEXT_DATA__' in r.text):
                return _parse_avito_html(r.text, slug, today)
        except Exception as e:
            print(f"  [Авито cffi] стр.{p}: {str(e)[:80]}")
        return []

    def _try_cs_web(p: int) -> list[dict]:
        """cloudscraper + Android UA — обходит JS-challenge без headless-браузера."""
        if not cs_session:
            return []
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params: dict = {"seller_type": "1"}
        if p > 1:
            params["p"] = p
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        try:
            r = cs_session.get(url, params=params, timeout=20, proxies=_avito_proxies())
            print(f"  [Авито cs] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
            if r.status_code == 200 and ('"urlPath"' in r.text or 'data-marker="item"' in r.text):
                return _parse_avito_html(r.text, slug, today)
        except Exception as e:
            print(f"  [Авито cs] стр.{p}: {e}")
        return []

    def _try_avito_public_api(p: int) -> list[dict]:
        """
        api.avito.ru/core/v1/items — публичный REST API Авито.
        Используется официальным мобильным приложением, отдельная инфраструктура.
        category_id=9 «Транспорт», params[109]=106 «Легковые автомобили».
        Требует OAuth-токен (env AVITO_API_TOKEN) — без него Авито отдаёт 401,
        поэтому без токена метод просто пропускается (не тратим запрос впустую).
        """
        # Сначала пробуем AVITO_API_TOKEN, потом автоматически получаем через client_credentials
        token = os.environ.get("AVITO_API_TOKEN", "").strip()
        if not token and AVITO_CLIENT_ID and AVITO_CLIENT_SECRET:
            token = _avito_get_oauth_token()
        if not token:
            return []
        params: dict = {
            "locationId": location_id,
            "categoryId": 9,
            "params[109]": 106,
            "privateOnly": 1,
            "page": p,
            "limit": 30,
        }
        if price_min > 0:
            params["priceMin"] = price_min
        if price_max < 99_000_000:
            params["priceMax"] = price_max

        try:
            r = session.get(
                "https://api.avito.ru/core/v1/items",
                params=params,
                headers={
                    "User-Agent": "ru.avito.avitomobile/12 (Android 13; ru_RU)",
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU",
                    "Authorization": f"Bearer {token}",
                    "x-device-id": f"avito-{random.randint(10**9, 10**10 - 1)}",
                },
                timeout=20,
                proxies=_avito_proxies(),
            )
            print(f"  [Авито pubAPI] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
            if r.status_code == 200:
                try:
                    return _items_from_json_response(r.json())
                except Exception as e:
                    print(f"  [Авито pubAPI] json: {e}")
        except Exception as e:
            print(f"  [Авито pubAPI] стр.{p}: {e}")
        return []

    def _try_web_html(p: int) -> list[dict]:
        """
        Десктопная страница каталога www.avito.ru/<slug>/avtomobili.
        Прежний эндпоинт www.avito.ru/web/1/main/items НЕ существует (всегда 404),
        а api.avito.ru/core/v1/items требует OAuth-токен (401 без авторизации) —
        оба гарантированно давали 0. Здесь тянем обычную HTML-страницу каталога
        в той же сессии и парсим __NEXT_DATA__/карточки — это реально отдаёт
        объявления, когда IP не заблокирован.
        """
        # Если задана марка — добавляем её в путь URL, чтобы Авито сразу отдавал
        # только эту марку (точнее и больше, чем фильтрация в памяти).
        _brand_path = f"/{brand}" if brand and brand != "any" else ""
        url = f"https://www.avito.ru/{slug}/avtomobili{_brand_path}"
        # seller_type=1 — только частники.
        # s=1 — цена по возрастанию: самые дешёвые (ниже рынка) идут первыми
        #        независимо от даты выкладки. Сортировка по дате (s=104) НЕ используется
        #        — она ограничивает выдачу только сегодняшними, а нам нужны ВСЕ даты.
        # Страницы с нечётным номером — цена по возрастанию (s=1),
        # с чётным — без сортировки (Авито-релевантность, разные даты).
        _sort = "1" if p % 2 == 1 else ""
        params: dict = {"seller_type": "1"}
        if _sort:
            params["s"] = _sort
        if p > 1:
            params["p"] = p
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        # Часть IP в пуле может быть в бане у Авито (403). Ретраим со СВЕЖИМ IP
        # до 6 раз — при 1000 ротирующихся IP почти всегда найдётся рабочий.
        _uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
        for attempt in range(6):
            _hdrs = {
                "User-Agent": _uas[attempt % len(_uas)],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.avito.ru/",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            try:
                r = session.get(url, params=params, headers=_hdrs, timeout=15, proxies=_avito_proxies())
                print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200 and ('"urlPath"' in r.text or 'data-marker="item"' in r.text):
                    res = _parse_avito_html(r.text, slug, today)
                    if res:
                        return res
                    # Страница получена, но парсер вернул 0 — пробуем следующий IP
                    print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: парсер 0 объявлений, ретрай")
                if r.status_code in (403, 429, 503):
                    time.sleep(random.uniform(2.0, 4.0))
                    continue  # IP в бане — пробуем другой
                elif r.status_code == 200:
                    time.sleep(random.uniform(1.5, 3.0))
                    continue  # парсер дал 0 — пробуем другой IP
                break  # иной код — не ретраим
            except Exception as e:
                _mark_proxy_failed(str(e))
                print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: {str(e)[:60]}")
                time.sleep(random.uniform(1.0, 2.0))
                continue
        return []

    def _try_avito_rss(p: int) -> list[dict]:
        """Попытка получить данные через RSS Авито (менее защищён антиботом)."""
        if p > 2:
            return []
        import xml.etree.ElementTree as ET

        _brand_rss = f"/{brand}" if brand and brand != "any" else ""
        _price_params = ""
        if price_min > 0:
            _price_params += f"&pmin={price_min}"
        if price_max < 99_000_000:
            _price_params += f"&pmax={price_max}"
        rss_urls = [
            f"https://www.avito.ru/{slug}/avtomobili{_brand_rss}?output_type=rss&seller_type=1&s=104{_price_params}",
            f"https://www.avito.ru/{slug}/avtomobili?output_type=rss{_price_params}",
        ]
        for rss_url in rss_urls:
            try:
                r = session.get(
                    rss_url, timeout=8,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Feedfetcher-Google; +http://www.google.com/feedfetcher.html)", "Accept": "application/rss+xml,*/*"},
                    proxies=_avito_proxies(),
                )
                print(f"  [Авито RSS] {rss_url}: HTTP {r.status_code}")
                if r.status_code == 200 and ("<rss" in r.text or "<channel" in r.text):
                    root = ET.fromstring(r.text)
                    ns = {"media": "http://search.yahoo.com/mrss/"}
                    items_out = []
                    for item in root.findall(".//item"):
                        link = item.findtext("link", "") or ""
                        title = item.findtext("title", "")
                        desc = item.findtext("description", "")
                        if not link or "avito.ru" not in link:
                            continue
                        price_int = 0
                        price_str = ""
                        for pm in re.finditer(r'(\d[\d\s]{3,10})\s*(?:₽|руб)', desc + " " + title):
                            v = int(re.sub(r"[^\d]", "", pm.group(1)))
                            if 10_000 < v < 99_000_000:
                                price_int = v
                                price_str = f"{v:,} ₽".replace(",", " ")
                                break
                        if not price_int:
                            continue
                        photo_url = ""
                        enclosure = item.find("enclosure")
                        if enclosure is not None:
                            photo_url = enclosure.get("url", "")
                        media_content = item.find("media:content", ns)
                        if media_content is not None and not photo_url:
                            photo_url = media_content.get("url", "")
                        listing = {
                            "source": "avito", "title": title,
                            "price": price_str, "url": link.strip(), "date": str(today),
                            "_photos": 1 if photo_url else 0, "_days_on_site": 0,
                            "description": re.sub(r"<[^>]+>", " ", desc)[:300].strip(),
                            "seller": "", "_photo_url": photo_url,
                            "_price_int": price_int, "mileage": 0,
                            "_avito_price_filtered": False,
                        }
                        listing["_hot_score"] = hot_score(listing)
                        items_out.append(listing)
                    if items_out:
                        print(f"  [Авито RSS] {len(items_out)} объявлений из RSS")
                        return items_out
            except Exception as e:
                print(f"  [Авито RSS] ошибка: {e}")
        return []

    def _try_scraperapi(p: int) -> list[dict]:
        """ScraperAPI с JS-рендером — обходит блокировку IP через резидентные прокси."""
        if not SCRAPER_API_KEY:
            return []
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params_str = f"seller_type=1"
        if p > 1:
            params_str += f"&p={p}"
        if price_min > 0:
            params_str += f"&pmin={price_min}"
        if price_max < 99_000_000:
            params_str += f"&pmax={price_max}"
        full_url = f"{url}?{params_str}"
        try:
            r = _avito_scraperapi(full_url)
            if r and r.status_code == 200:
                print(f"  [ScraperAPI] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if '"urlPath"' in r.text or 'data-marker="item"' in r.text or '__NEXT_DATA__' in r.text:
                    result = _parse_avito_html(r.text, slug, today)
                    if result:
                        return result
            elif r:
                print(f"  [ScraperAPI] стр.{p}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  [ScraperAPI] стр.{p}: {e}")
        return []

    def _try_scraperapi_fast(p: int) -> list[dict]:
        """Быстрый ScraperAPI БЕЗ JS-рендера — Авито отдаёт __NEXT_DATA__ прямо в HTML,
        поэтому рендер не нужен. Резидентные IP ScraperAPI Авито не блокирует — самый
        надёжный метод. Без render укладывается в окно 22с."""
        if not SCRAPER_API_KEY:
            return []
        import requests as _req
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params_str = "seller_type=1"
        if p > 1:
            params_str += f"&p={p}"
        if price_min > 0:
            params_str += f"&pmin={price_min}"
        if price_max < 99_000_000:
            params_str += f"&pmax={price_max}"
        full_url = f"{url}?{params_str}"
        # Пробуем сначала premium (резидентные RU IP), затем обычный
        for opts in ({"premium": "true", "country_code": "ru"}, {"country_code": "ru"}):
            try:
                r = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": full_url, **opts,
                }, timeout=14)
                if r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text or 'data-marker="item"' in r.text):
                    result = _parse_avito_html(r.text, slug, today)
                    if result:
                        print(f"  [ScraperAPI-fast] стр.{p}: {len(result)} объявлений ({'premium' if 'premium' in opts else 'std'})")
                        return result
                elif r.status_code in (401, 403):
                    # Кредиты ScraperAPI кончились / ключ недействителен — нет смысла повторять
                    print(f"  [ScraperAPI-fast] стр.{p}: HTTP {r.status_code} — кредиты ScraperAPI исчерпаны (пополни на scraperapi.com)")
                    return []
                else:
                    print(f"  [ScraperAPI-fast] стр.{p}: HTTP {r.status_code}, нет данных")
            except Exception as e:
                print(f"  [ScraperAPI-fast] стр.{p}: {str(e)[:50]}")
        return []

    def _try_curl_cffi(p: int) -> list[dict]:
        """curl_cffi — точная имитация TLS-отпечатка Chrome. Обходит большинство анти-бот систем."""
        try:
            from curl_cffi import requests as cffi_req
        except ImportError:
            return []
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params: dict = {"seller_type": "1"}
        if p > 1:
            params["p"] = p
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        proxies = AVITO_PROXIES or {}
        try:
            r = cffi_req.get(
                url,
                params=params,
                impersonate="chrome124",
                headers={
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://www.avito.ru/",
                },
                proxies=proxies,
                timeout=8,
            )
            print(f"  [curl_cffi] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
            if r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text):
                return _parse_avito_html(r.text, slug, today)
        except Exception as e:
            print(f"  [curl_cffi] стр.{p}: {e}")
        return []

    def _try_yandex_search(p: int) -> list[dict]:
        """Поиск Авито через Яндекс XML — Яндекс не блокирует датацентровые IP."""
        if p > 1:
            return []
        try:
            import requests as _rq
            price_q = ""
            if price_min > 0 and price_max < 99_000_000:
                price_q = f" цена от {price_min} до {price_max}"
            elif price_max < 99_000_000:
                price_q = f" цена до {price_max}"
            query = f"site:avito.ru/{slug}/avtomobili частник{price_q}"
            r = _rq.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kl": "ru-ru"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                },
                timeout=15,
            )
            if r.status_code != 200:
                return []
            # Извлекаем URL объявлений Авито из результатов поиска
            avito_urls = list(dict.fromkeys(re.findall(
                rf'https?://(?:www\.)?avito\.ru/{re.escape(slug)}/[a-z0-9_/-]+-\d{{5,}}',
                r.text
            )))
            if not avito_urls:
                print(f"  [DDG] нет URL в результатах поиска")
                return []
            print(f"  [DDG] найдено {len(avito_urls)} URL Авито")
            # Пробуем загрузить первые 5 страниц объявлений напрямую
            items_out = []
            for item_url in avito_urls[:8]:
                try:
                    ri = _rq.get(item_url, timeout=8, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Referer": "https://www.avito.ru/",
                    })
                    if ri.status_code == 200:
                        items = _parse_avito_html(ri.text, slug, today)
                        # Если это страница одного объявления — оно может не распарситься как список,
                        # пробуем _avito_item_from_json напрямую через __NEXT_DATA__
                        if not items:
                            nd_m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', ri.text, re.S)
                            if nd_m:
                                try:
                                    nd = json.loads(nd_m.group(1))
                                    for path in ("props.initialState.advert", "props.pageProps.advert"):
                                        adv = _deep_get(nd, path)
                                        if adv:
                                            item = _avito_item_from_json(adv, today)
                                            if item:
                                                items_out.append(item)
                                except Exception:
                                    pass
                        else:
                            items_out.extend(items)
                except Exception:
                    pass
            return items_out
        except Exception as e:
            print(f"  [DDG] ошибка: {e}")
        return []

    def _try_googlebot_ua(p: int) -> list[dict]:
        """Запрос с User-Agent Googlebot — некоторые сайты открывают ботам поиска."""
        try:
            import requests as _rq
            url = f"https://www.avito.ru/{slug}/avtomobili"
            params: dict = {"seller_type": "1"}
            if p > 1:
                params["p"] = p
            if price_min > 0:
                params["pmin"] = price_min
            if price_max < 99_000_000:
                params["pmax"] = price_max
            r = _rq.get(url, params=params, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "Accept": "text/html,*/*;q=0.8",
                "Accept-Language": "ru",
                "From": "googlebot(at)googlebot.com",
            }, proxies=_avito_proxies())
            print(f"  [Googlebot UA] стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
            if r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text):
                return _parse_avito_html(r.text, slug, today)
        except Exception as e:
            _mark_proxy_failed(str(e))
            print(f"  [Googlebot UA] стр.{p}: {e}")
        return []

    def _try_avito_lite(p: int) -> list[dict]:
        """Авито lite — упрощённая версия сайта, меньше JS-защиты."""
        try:
            import requests as _rq
            # Пробуем несколько вариантов облегчённых эндпоинтов
            urls_to_try = [
                f"https://m.avito.ru/{slug}/avtomobili",
                f"https://avito.ru/{slug}/avtomobili",  # без www
            ]
            params: dict = {"seller_type": "1", "forceLocation": "1"}
            if p > 1:
                params["p"] = p
            if price_min > 0:
                params["pmin"] = price_min
            if price_max < 99_000_000:
                params["pmax"] = price_max
            hdrs = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            }
            for url in urls_to_try:
                try:
                    r = _rq.get(url, params=params, headers=hdrs, timeout=12, proxies=_avito_proxies(), allow_redirects=True)
                    print(f"  [Авито lite] {url} стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                    if r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text or 'data-marker="item"' in r.text):
                        result = _parse_avito_html(r.text, slug, today)
                        if result:
                            return result
                except Exception as e:
                    _mark_proxy_failed(str(e))
                    print(f"  [Авито lite] {url}: {e}")
        except Exception as e:
            _mark_proxy_failed(str(e))
            print(f"  [Авито lite] {e}")
        return []

    def _try_avito_json_api(p: int) -> list[dict]:
        """Avito internal JSON listing endpoint — returns structured data without HTML parsing."""
        try:
            import requests as _req
        except ImportError:
            return []
        location_id = AVITO_LOCATION_IDS.get(region, 637640)
        params: dict = {
            "categoryId": 9,
            "locationId": location_id,
            "params[109]": 106,
            "page": p,
        }
        if price_min > 0:
            params["priceMin"] = price_min
        if price_max < 99_000_000:
            params["priceMax"] = price_max
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "x-requested-with": "XMLHttpRequest",
            "Referer": f"https://www.avito.ru/{slug}/avtomobili",
        }
        try:
            r = _req.get(
                "https://www.avito.ru/web/1/listing",
                params=params,
                headers=headers,
                timeout=30,
            )
            if r.status_code == 404:
                print(f"  [Авито JSON API] стр.{p}: HTTP 404 — эндпоинт недоступен")
                return []
            if r.status_code != 200:
                print(f"  [Авито JSON API] стр.{p}: HTTP {r.status_code}")
                return []
            try:
                data = r.json()
            except Exception:
                print(f"  [Авито JSON API] стр.{p}: не JSON-ответ")
                return []
            raw_items = (
                data.get("data", {}).get("items", [])
                or data.get("items", [])
                or data.get("result", {}).get("items", [])
            )
            if not raw_items:
                print(f"  [Авито JSON API] стр.{p}: пустой ответ (нет items)")
                return []
            results_out: list[dict] = []
            for it in raw_items:
                item = _avito_item_from_json(it, today)
                if item:
                    results_out.append(item)
            if results_out:
                print(f"  [Авито JSON API] стр.{p}: {len(results_out)} объявлений")
            else:
                print(f"  [Авито JSON API] стр.{p}: raw_items={len(raw_items)}, после фильтра=0")
            return results_out
        except Exception as e:
            print(f"  [Авито JSON API] стр.{p}: {e}")
        return []

    # Определяем рабочий метод: на стр.1 запускаем ВСЕ методы параллельно и
    # берём первый, который вернул объявления. Это быстрее, чем пробовать
    # их последовательно (ждать таймаут каждого по очереди).
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed
    working_method = None
    page1_batch: list[dict] = []
    def _try_free_proxies(p: int) -> list[dict]:
        """Пробуем бесплатные российские прокси из публичных списков."""
        import requests as _rq
        global _free_proxy_cache, _free_proxy_cache_time, _working_free_proxies, _working_free_proxies_time

        # Если кеш пустой — быстро получаем минимальный список (не ждём прогрев)
        if time.time() - _free_proxy_cache_time > 600 or not _free_proxy_cache:
            fresh: list[str] = []
            try:
                r = _rq.get(
                    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=RU&ssl=yes&anonymity=all",
                    timeout=5,
                )
                if r.status_code == 200:
                    fresh += [ln.strip() for ln in r.text.splitlines() if ln.strip()]
            except Exception:
                pass
            if fresh:
                _free_proxy_cache = fresh
                _free_proxy_cache_time = time.time()

        # Рабочие прокси (проверенные прогревом) идут первыми
        priority = list(_working_free_proxies)
        rest = [x for x in _free_proxy_cache if x not in set(priority)]
        random.shuffle(rest)
        free_proxies = priority + rest
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params: dict = {"seller_type": "1"}
        if p > 1:
            params["p"] = p
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": "https://www.avito.ru/",
        }

        def _try_one(proxy_addr: str) -> list[dict]:
            proxies = {"http": f"http://{proxy_addr}", "https": f"http://{proxy_addr}"}
            try:
                r = _rq.get(url, params=params, headers=_headers, proxies=proxies, timeout=8)
                if r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text):
                    result = _parse_avito_html(r.text, slug, today)
                    if result:
                        print(f"  [FreeProxy] {proxy_addr}: {len(result)} объявлений")
                        return result
            except Exception:
                pass
            return []

        # Пробуем до 16 бесплатных прокси ПАРАЛЛЕЛЬНО и берём первый рабочий —
        # последовательно это было до 80с, параллельно ~8с.
        from concurrent.futures import ThreadPoolExecutor as _TPEfp, as_completed as _acfp
        candidates = free_proxies[:16]
        if not candidates:
            return []
        with _TPEfp(max_workers=min(16, len(candidates))) as _exfp:
            futs = [_exfp.submit(_try_one, pa) for pa in candidates]
            try:
                for fut in _acfp(futs, timeout=12):
                    try:
                        res = fut.result()
                    except Exception:
                        res = []
                    if res:
                        return res
            except Exception:
                pass
        return []

    def _try_yandex_snippets(p: int) -> list[dict]:
        """
        Ищет объявления Авито через DuckDuckGo (html + lite).
        Стратегия основана на живых тестах с Railway IP:
        - DDG html с паузой 3-5с = 10-20 объявлений за запрос
        - DDG lite = отдельный rate-limit счётчик, резерв
        - Чередование html/lite снижает вероятность 202
        - 12 марок × до 20 объявлений = потенциально 100+ объявлений
        """
        if p > 1:
            return []
        try:
            import requests as _rq
        except ImportError:
            return []

        slug_ru_name = {
            "ekaterinburg": "Екатеринбург", "moskva": "Москва", "spb": "Санкт-Петербург",
            "novosibirsk": "Новосибирск", "kazan": "Казань", "chelyabinsk": "Челябинск",
            "ufa": "Уфа", "krasnodar": "Краснодар", "omsk": "Омск",
            "rostov-na-donu": "Ростов", "tyumen": "Тюмень", "perm": "Пермь",
            "krasnoyarsk": "Красноярск", "voronezh": "Воронеж", "samara": "Самара",
        }.get(slug, slug)

        _avito_url_re = re.compile(
            r'(?:https?://)?(?:www\.|m\.)?avito\.ru/[a-z0-9_.-]+/avtomobili/[a-z0-9_.%-]*\d{6,}',
            re.I,
        )
        _price_re = re.compile(r"(\d[\d\s]{2,8})\s*(?:₽|тыс\.?\s*р(?:уб)?\.?|руб\.?)", re.I)
        _price_json_re = re.compile(r'["\']?price["\']?\s*[=:]\s*["\']?(\d{4,9})(?:\.0+)?["\']?', re.I)
        _year_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")

        def _extract_avito_urls(html: str) -> list[str]:
            import urllib.parse
            found = []
            seen = set()
            def _add(raw: str):
                if not raw.startswith("http"):
                    raw = "https://" + raw
                clean = raw.split("?")[0].split("#")[0].rstrip("/")
                if slug and f"/{slug}/" not in clean:
                    return
                if clean not in seen:
                    seen.add(clean)
                    found.append(clean)
            decoded = html
            for _ in range(2):
                for m in _avito_url_re.finditer(decoded):
                    _add(m.group(0))
                try:
                    nxt = urllib.parse.unquote(decoded)
                except Exception:
                    break
                if nxt == decoded:
                    break
                decoded = nxt
            return found

        def _parse_price_snip(text: str) -> int:
            for m in _price_re.finditer(text):
                raw = re.sub(r"\D", "", m.group(1))
                if not raw:
                    continue
                val = int(raw)
                suffix = m.group(0)[len(m.group(1)):].strip().lower()
                if "тыс" in suffix:
                    val *= 1000
                if 50_000 <= val <= 50_000_000:
                    return val
            return 0

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }

        def _is_blocked(text: str, status: int) -> bool:
            return status == 202 or status == 429 or (status != 200) or len(text) < 2000

        def _fetch_ddg(q: str, use_lite: bool, proxy=None) -> str:
            """Один запрос к DDG html или lite. Возвращает HTML или ''."""
            url = "https://lite.duckduckgo.com/lite/" if use_lite else "https://html.duckduckgo.com/html/"
            params = {"q": q, "kl": "ru-ru"}
            try:
                r = _rq.get(url, params=params, headers=headers, timeout=10, proxies=proxy)
                if _is_blocked(r.text, r.status_code):
                    return ""
                return r.text
            except Exception:
                return ""

        def _fetch_alt_engines(q: str, proxy=None) -> str:
            """Резервные поисковики, когда DDG отдаёт 202/429.
            Mojeek, Brave, Startpage — все индексируют avito.ru и имеют
            отдельные счётчики лимитов, поэтому повышают надёжность."""
            engines = [
                ("https://www.mojeek.com/search", {"q": q}),
                ("https://search.brave.com/search", {"q": q, "source": "web"}),
                ("https://lite.duckduckgo.com/lite/", {"q": q, "kl": "ru-ru"}),
            ]
            for eurl, eparams in engines:
                try:
                    r = _rq.get(eurl, params=eparams, headers=headers, timeout=10, proxies=proxy)
                    if r.status_code == 200 and "avito.ru" in r.text and len(r.text) > 2000:
                        return r.text
                except Exception:
                    continue
            return ""

        def _parse_serp(html: str) -> list[dict]:
            import urllib.parse as _upq
            out: list[dict] = []
            ctx_html = html
            for _ in range(2):
                try:
                    ctx_html = _upq.unquote(ctx_html)
                except Exception:
                    break
            found_urls = _extract_avito_urls(ctx_html)
            for url in found_urls:
                clean_url = url.split("?")[0]
                pos = ctx_html.find(url)
                context = ctx_html[max(0, pos-400):pos+800] if pos >= 0 else ""
                context_clean = re.sub(r"<[^>]+>", " ", context)
                context_clean = re.sub(r"&[a-z]+;", " ", context_clean)
                context_clean = re.sub(r"\s+", " ", context_clean).strip()

                price_int = _parse_price_snip(context_clean)
                if not price_int:
                    for m in _price_json_re.finditer(context_clean):
                        v = int(m.group(1))
                        if 30_000 <= v <= 99_000_000:
                            price_int = v
                            break
                if price_int > 0 and not (price_min <= price_int <= price_max):
                    continue

                url_path = clean_url.split("/avtomobili/")[-1] if "/avtomobili/" in clean_url else ""
                if url_path:
                    url_title = re.sub(r'_\d{6,}$', '', url_path).replace("_", " ").replace("-", " ")
                    title = re.sub(r'\s+', ' ', url_title).strip()[:80].title()
                else:
                    title_src = re.sub(
                        r'https?://\S+|//\S+|uddg=\S+|rut=\S+|duckduckgo\.com\S*|www\.|avito\.ru\S*',
                        ' ', context_clean, flags=re.I,
                    )
                    title = re.sub(r'\s+', ' ', title_src).strip(" -|·,")[:80] or f"Авто на Авито — {slug_ru_name}"

                # Fix C: Extract year from URL path first (more reliable than snippet)
                year_from_url = 0
                if url_path:
                    ym_url = re.search(r'\b(19[5-9]\d|20[012]\d)\b', url_path)
                    if ym_url:
                        year_from_url = int(ym_url.group(1))
                year_m = _year_re.search(context_clean)
                year = year_from_url or (int(year_m.group(1)) if year_m else 0)

                # Hard filter: only obviously impossible year/budget combos.
                # Thresholds relaxed — DDG often shows older cars that are valid,
                # and over-filtering leads to 0 results for cheap budgets.
                if year >= 2024 and price_max < 2_000_000:
                    continue
                if year >= 2022 and price_max < 800_000:
                    continue
                if year >= 2020 and price_max < 400_000:
                    continue

                photo_url = ""
                wide = ctx_html[max(0, pos-1000):pos+1500] if pos >= 0 else ""
                img_m = re.search(
                    r'(https?:)?//(?:avatars\.mds\.yandex\.net|[a-z0-9.]*avito\.st|[a-z0-9.]*img\.avito[.\w]*)/[^\s"\'<>]+',
                    wide,
                )
                if img_m:
                    photo_url = img_m.group(0)
                    if photo_url.startswith("//"):
                        photo_url = "https:" + photo_url

                out.append({
                    "source": "avito", "title": title,
                    "price": f"{price_int:,} ₽".replace(",", " ") if price_int else "цена не указана",
                    "_price_int": price_int, "url": clean_url, "_photo_url": photo_url,
                    "description": context_clean[:400], "seller": "Авито (частник)",
                    "_year": year, "_days_on_site": 0, "_photos": 1 if photo_url else 0,
                    "mileage": 0, "_avito_price_filtered": False,
                })
            return out

        # Список марок зависит от бюджета
        if price_max <= 200_000:
            # Fix D: For cheap budgets, use specific cheap model names to avoid DDG
            # returning expensive Chinese brands (EXEED, Tank, Haval, Geely, Chery etc.)
            _all_brands = [
                "lada", "ваз", "daewoo nexia", "daewoo matiz", "chevrolet lacetti",
                "nissan almera", "toyota corolla", "hyundai accent", "kia rio",
                "ford focus", "opel astra", "renault logan", "volkswagen polo",
            ]
        elif price_max <= 500_000:
            _all_brands = [
                "lada", "kia", "hyundai", "toyota", "nissan", "renault",
                "volkswagen", "ford", "opel", "chevrolet", "mitsubishi",
                "honda", "mazda", "skoda", "daewoo", "bmw", "mercedes",
            ]
        else:
            _all_brands = [
                "lada", "kia", "hyundai", "toyota", "nissan", "volkswagen",
                "renault", "ford", "skoda", "bmw", "mercedes", "mazda",
                "chevrolet", "mitsubishi", "honda", "opel",
            ]

        results_out: list[dict] = []
        seen_urls: set[str] = set()

        # Подготавливаем список прокси для ротации IP (снижает вероятность 202)
        proxy_pool = [None]  # начинаем без прокси (Railway IP)
        for _pa in list(_working_free_proxies)[:4]:
            proxy_pool.append({"http": f"http://{_pa}", "https": f"http://{_pa}"})

        proxy_idx = 0
        lite_flag = False  # чередуем html/lite

        # Год и ценовые подсказки для поиска — смещают DDG к нужному сегменту
        _price_hint = f" до {price_max // 1000}тыс" if price_max < 10_000_000 else ""
        _year_hint = ""
        if price_max <= 150_000:
            _year_hint = " 2000 2005 2010"  # старые авто
        elif price_max <= 300_000:
            _year_hint = " 2008 2012 2015"
        elif price_max <= 600_000:
            _year_hint = " 2012 2016 2018"

        # Тайм-лимит для всего DDG-цикла: максимум 45 секунд
        _ddg_deadline = time.time() + 45

        for brand in _all_brands:
            if len(results_out) >= 40:
                break
            if time.time() > _ddg_deadline:
                print(f"  [ddg] тайм-лимит 45с, остановка на {brand}")
                break
            q = f"site:avito.ru/{slug}/avtomobili {brand}{_price_hint}{_year_hint}"
            # Пауза 2-3.5с между запросами — достаточно для обхода DDG rate-limit,
            # но не так долго, чтобы вылезти за тайм-лимит scrape_avito.
            time.sleep(random.uniform(2.0, 3.5))
            proxy = proxy_pool[proxy_idx % len(proxy_pool)]
            html = _fetch_ddg(q, use_lite=lite_flag, proxy=proxy)
            if not html:
                # 202/блок — сразу пробуем через прокси и другой endpoint
                proxy_idx += 1
                proxy = proxy_pool[proxy_idx % len(proxy_pool)]
                lite_flag = not lite_flag
                time.sleep(2)
                html = _fetch_ddg(q, use_lite=lite_flag, proxy=proxy)
            if not html:
                # DDG полностью заблокирован — резервные поисковики (Mojeek/Brave)
                html = _fetch_alt_engines(q, proxy=None)
                if html:
                    print(f"  [alt-engine] {brand}: получены данные через резерв")
            if html:
                batch = _parse_serp(html)
                added = 0
                for it in batch:
                    if it["url"] not in seen_urls:
                        seen_urls.add(it["url"])
                        results_out.append(it)
                        added += 1
                eng = "ddglite" if lite_flag else "ddg"
                if added:
                    print(f"  [{eng}] {brand}: +{added} (итого={len(results_out)})")
                else:
                    print(f"  [{eng}] {brand}: 0 объявлений для {slug}")
            else:
                print(f"  [ddg] {brand}: заблокирован (202/429), пропускаем")
            # Чередуем движок и ротируем прокси
            lite_flag = not lite_flag
            proxy_idx += 1

        if results_out:
            print(f"  [DDG итого] {len(results_out)} объявлений Авито")
        return results_out

    # free_proxies даёт настоящую страницу Авито (десятки объявлений), DuckDuckGo —
    # ещё несколько. Запускаем ВСЁ параллельно и СЛИВАЕМ результаты, а не берём
    # первый ответивший метод (иначе теряем большие пачки, что приходят чуть позже).
    _no_proxy_methods = [_try_scraperapi_fast, _try_free_proxies, _try_yandex_snippets, _try_cffi_web, _try_curl_cffi, _try_cs_web, _try_mobile_site, _try_web_html, _try_avito_public_api, _try_avito_rss, _try_googlebot_ua, _try_avito_lite, _try_scraperapi, _try_avito_json_api]
    _use_proxy = AVITO_PROXIES and not _proxy_auth_failed
    if _use_proxy:
        # Платный прокси. Основной метод — _try_web_html.
        # Резервные методы: мобильный сайт, RSS, Googlebot UA — если часть IP забанена.
        all_methods = [_try_web_html, _try_avito_lite, _try_avito_rss, _try_googlebot_ua, _try_avito_public_api]
    else:
        all_methods = _no_proxy_methods
    # Список задач. С прокси — страницы с человеческой задержкой между ними.
    if _use_proxy:
        # С мобильным прокси запускаем страницы с задержкой 1-3с (имитация человека)
        # Параллелизм ограничен 3 потоками — Авито считает больше подозрительным
        tasks = (
            [(_try_web_html, p) for p in range(1, 13)] +
            [(_try_avito_lite, p) for p in range(1, 5)] +
            [(_try_avito_rss, 1), (_try_googlebot_ua, 1), (_try_avito_public_api, 1)]
        )
        _cap = 300
        _deadline_s = 90
    else:
        tasks = [(m, 1) for m in all_methods]
        _cap = 40
        _deadline_s = 35
    def _listing_key(u: str) -> str:
        """Канонический ключ объявления — числовой ID в конце urlPath.
        Авито повторяет рекламные объявления на каждой странице с тем же ID,
        даже если в URL есть лишние параметры. Дедупим по ID."""
        if not u:
            return ""
        base = u.split("?")[0].rstrip("/")
        m = re.search(r'(\d{6,})$', base)
        return m.group(1) if m else base

    # С прокси: макс 3 потока — имитируем человека, не триггерим rate-limit Авито
    # Без прокси: до 12 потоков — пробуем всё параллельно
    _max_w = 3 if AVITO_PROXIES else min(12, len(tasks))
    _ex = _TPE(max_workers=_max_w)
    merged: dict[str, dict] = {}
    _seen_keys: set = set()
    _soft_deadline = time.time() + _deadline_s
    try:
        fut_map = {_ex.submit(m, pg): (m, pg) for (m, pg) in tasks}
        for fut in _as_completed(fut_map, timeout=70):
            try:
                b = fut.result()
            except Exception:
                b = []
            if b:
                added = 0
                for it in b:
                    u = it.get("url")
                    key = _listing_key(u)
                    if u and key and key not in _seen_keys:
                        _seen_keys.add(key)
                        merged[u] = it
                        added += 1
                if added:
                    _m, _pg = fut_map[fut]
                    print(f"  [Авито API] {_m.__name__} стр.{_pg}: +{added} (всего {len(merged)})")
            # достаточно набрали или вышло время — больше не ждём медленные методы
            if len(merged) >= _cap or (merged and time.time() > _soft_deadline):
                break
    except Exception as e:
        print(f"  [Авито API] пул: {str(e)[:60]}")
    finally:
        _ex.shutdown(wait=False)

    results = list(merged.values())

    # Если прокси сломан (407) и ничего не нашли — перезапускаем без прокси
    if not results and _proxy_auth_failed and _use_proxy:
        print(f"  [Авито] прокси не работает (407) — повтор без прокси")
        fallback_tasks = [(m, 1) for m in _no_proxy_methods]
        _ex2 = _TPE(max_workers=min(8, len(fallback_tasks)))
        try:
            fut_map2 = {_ex2.submit(m, pg): (m, pg) for (m, pg) in fallback_tasks}
            for fut in _as_completed(fut_map2, timeout=40):
                try:
                    b = fut.result()
                except Exception:
                    b = []
                if b:
                    for it in b:
                        u = it.get("url")
                        key = _listing_key(u)
                        if u and key and key not in _seen_keys:
                            _seen_keys.add(key)
                            merged[u] = it
                if len(merged) >= 40 or (merged and time.time() > _soft_deadline + 40):
                    break
        except Exception as e:
            print(f"  [Авито fallback] пул: {str(e)[:60]}")
        finally:
            _ex2.shutdown(wait=False)
        results = list(merged.values())

    if not results:
        print(f"  [Авито API] стр.1: 0 объявлений")
        return results
    print(f"  [Авито API] объединено {len(results)} объявлений из всех методов")
    return results


def _scrape_avito_direct(slug: str, pages: int, price_min: int, price_max: int, today) -> list[dict]:
    """Прямой запрос к Авито без ScraperAPI (мобильный User-Agent)."""
    try:
        import requests as _req
    except ImportError:
        return []

    results = []
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,*/*",
        "Referer": "https://www.avito.ru/",
    })

    for p in range(1, pages + 1):
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params: dict = {"p": p}
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  [Авито прямой] HTTP {r.status_code}")
                break
            batch = _parse_avito_html(r.text, slug, today)
            if not batch:
                break
            results.extend(batch)
            time.sleep(0.3)
        except Exception as e:
            print(f"  [Авито прямой] стр.{p}: {e}")
            break

    return results


# Кэш результатов Авито по региону — резко снижает число запросов к Авито
# (а значит и риск блокировки 429), когда много пользователей ищут подряд.
_AVITO_REGION_CACHE: dict[str, tuple[float, list[dict]]] = {}
_AVITO_REGION_CACHE_TTL = 24 * 60 * 60  # 24 часа — дольше кэш = меньше блокировок
_AVITO_CACHE_FILE = Path("avito_region_cache.json")


def _load_avito_cache():
    """Загружает кэш Авито с диска при старте — чтобы он пережил перезапуск бота."""
    if not _AVITO_CACHE_FILE.exists():
        return
    try:
        raw = json.loads(_AVITO_CACHE_FILE.read_text(encoding="utf-8"))
        # Версионирование кэша: отбрасываем старые форматы без version=2
        if not isinstance(raw, dict) or raw.get("version") != 4:
            print(f"  [Авито] кэш устарел (нет version=4) — сбрасываем")
            return
        data = raw.get("data", {})
        now = time.time()
        for region, entry in data.items():
            ts, items = entry[0], entry[1]
            # Загружаем ВСЕ записи — устаревшие используются как запасной кэш
            # при блокировке Авито. Проверка TTL происходит в scrape_avito.
            # Выбрасываем старые записи без цены (junk из прошлых версий).
            clean_items = [i for i in items if i.get("_price_int", 0)]
            _AVITO_REGION_CACHE[region] = (ts, clean_items)
        print(f"  [Авито] кэш с диска: {len(_AVITO_REGION_CACHE)} регионов")
    except Exception as e:
        print(f"  [Авито] не удалось загрузить кэш: {e}")


def _save_avito_cache():
    """Сохраняет кэш Авито на диск."""
    try:
        _AVITO_CACHE_FILE.write_text(
            json.dumps({"version": 4, "data": _AVITO_REGION_CACHE}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"  [Авито] не удалось сохранить кэш: {e}")


def _avito_price_bucket(price_min: int, price_max: int) -> str:
    """Округляем бюджет до ближайшего «слота» (100k шаг), чтобы пользователи
    с похожим бюджетом разделяли один кэш, а не делали отдельный запрос каждый."""
    lo = (price_min // 100_000) * 100_000
    hi = ((price_max + 99_999) // 100_000) * 100_000
    return f"{lo}_{hi}"


def scrape_avito(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000, sort_by_date: bool = False, brand: str = "") -> list[dict]:
    """
    Парсер Авито. Кэш хранится по РЕГИОНУ (без разбивки по цене), чтобы один
    успешный скрейп покрывал все ценовые диапазоны и не вызывал повторных блокировок.
    """
    now = time.time()
    # С рабочим прокси скрейпим С ФИЛЬТРОМ бюджета в URL (Авито сам отдаёт
    # релевантные объявления нужной цены, а не рекламные новинки дилеров без
    # цены). Кэш — по бюджет-слоту. 1000 IP делают частый скрейп безопасным.
    # Без прокси — старая схема (один кэш на регион, фильтр в памяти).
    if AVITO_PROXIES:
        bucket = _avito_price_bucket(price_min, price_max)
        cache_key = f"{region}_{bucket}" + (f"_{brand}" if brand else "")
        _scrape_pmin, _scrape_pmax = price_min, price_max
        _cache_ttl = 90 * 60  # 1.5 часа — чаще обновляем чтобы находить новые ниже рынка
    else:
        cache_key = region + (f"_{brand}" if brand else "")
        _scrape_pmin, _scrape_pmax = 0, 99_000_000
        _cache_ttl = _AVITO_REGION_CACHE_TTL
    cached = _AVITO_REGION_CACHE.get(cache_key)
    # Игнорируем кэш из старых записей без цены (DDG-мусор прошлых версий).
    _cache_is_priceless = bool(
        cached and AVITO_PROXIES
        and cached[1]
        and sum(1 for i in cached[1] if i.get("_price_int", 0)) < max(1, len(cached[1]) // 2)
    )
    if cached and (now - cached[0]) < _cache_ttl and not _cache_is_priceless:
        items = cached[1]
        print(f"  [Авито] кэш {cache_key}: {len(items)} объявлений (возраст {int(now-cached[0])}с)")
    else:
        # Скрейпим с фильтром бюджета (прокси) или без (бесплатный режим).
        items = _scrape_avito_raw(region, pages=pages, price_min=_scrape_pmin, price_max=_scrape_pmax, sort_by_date=sort_by_date, brand=brand)
        if items:
            _AVITO_REGION_CACHE[cache_key] = (now, items)
            _save_avito_cache()
            print(f"  [Авито] скрейп OK: {len(items)} объявлений → кэш ({cache_key})")
        else:
            # Скрейп вернул 0. Ищем любой кэш региона.
            any_cached: list[dict] = []
            any_cached_age = 999_999
            for k, (ts, its) in _AVITO_REGION_CACHE.items():
                if k == region or k.startswith(region + "_"):
                    if len(its) > len(any_cached):
                        any_cached = its
                        any_cached_age = now - ts
            if any_cached:
                items = any_cached
                print(f"  [Авито] блокировка → старый кэш {region}: {len(items)} шт (возраст {int(any_cached_age)}с)")
            elif cached:
                items = cached[1]
                print(f"  [Авито] пусто → устаревший кэш: {len(items)} шт")

    # Фильтр по бюджету в памяти.
    # С рабочим прокси Авито отдаёт реальные цены, поэтому объявления БЕЗ цены —
    # это мусор (DDG/устаревший кэш). Требуем цену и строгое попадание в бюджет.
    if AVITO_PROXIES:
        out = [
            it for it in items
            if it.get("_price_int") and (price_min <= it["_price_int"] <= price_max)
        ]
    else:
        # Без прокси цену часто не достать — пропускаем безценовые как кандидатов.
        out = [
            it for it in items
            if (not it.get("_price_int")) or (price_min <= it["_price_int"] <= price_max)
        ]

    # Fix A: Hard post-merge year/budget filter — eliminates DDG results with
    # price_int=0 that are obviously wrong year/budget combos (e.g. 2025 EXEED
    # in a 0–100k budget search).
    _title_year_re = re.compile(r'\b(19[5-9]\d|20[012]\d)\b')

    def _year_budget_ok(it: dict, pmax: int) -> bool:
        y = it.get("_year") or it.get("year") or 0
        try:
            y = int(str(y)[:4])
        except Exception:
            y = 0
        # Если год не в поле _year — ищем в заголовке (напр. "Granta 1.6 MT, 2026")
        if y == 0:
            title = it.get("title", "") + " " + it.get("url", "")
            ym = _title_year_re.search(title)
            if ym:
                y = int(ym.group(1))
        if y >= 2023 and pmax < 1_500_000:
            return False
        if y >= 2021 and pmax < 700_000:
            return False
        if y >= 2019 and pmax < 350_000:
            return False
        if y >= 2016 and pmax < 200_000:
            return False
        return True

    out = [it for it in out if it.get("_price_int", 0) > 0 or _year_budget_ok(it, price_max)]
    return out


def _scrape_avito_raw(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000, sort_by_date: bool = False, brand: str = "") -> list[dict]:
    """
    Бесплатный парсер Авито. Стратегия (порядок попыток):
    1. _avito_api_fetch: cloudscraper+Android UA, m.avito.ru, публичный API, веб-API —
       всё это легче проходит с датацентровых IP, чем десктопный скрейпинг.
    2. Прямой HTTP-запрос с Desktop UA (иногда работает в определённых регионах).
    3. Headless Playwright + stealth — последний резерв, требует больше времени.
    """
    slug = AVITO_SLUGS.get(region, region)
    today = datetime.date.today()

    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return []

    # ── Метод 1: API / мобильный сайт / cloudscraper ─────────────
    print(f"  [Авито] пробуем API-методы для {region}…")
    api_results = _avito_api_fetch(region, pages, price_min, price_max, today)
    if api_results:
        print(f"  [Авито] API-метод дал {len(api_results)} объявлений")
        return api_results
    # API-методы не дали результатов — пробуем прямой HTML-скрейпинг (методы 2-3)
    print(f"  [Авито] API дал 0 — пробуем HTML-скрейпинг…")

    def _build_url(p: int) -> str:
        qs_parts = ["seller_type=1"]  # только частники
        if p > 1:
            qs_parts.append(f"p={p}")
        if price_min > 0:
            qs_parts.append(f"pmin={price_min}")
        if price_max < 99_000_000:
            qs_parts.append(f"pmax={price_max}")
        # s=104 — по дате (только свежие). Без сортировки Авито отдаёт
        # релевантные объявления любых дат — это даёт больше машин ниже рынка.
        if sort_by_date:
            qs_parts.append("s=104")
        u = f"https://www.avito.ru/{slug}/avtomobili"
        if qs_parts:
            u += "?" + "&".join(qs_parts)
        return u

    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.avito.ru/",
    }

    def _fetch_page(p: int) -> list[dict]:
        url = _build_url(p)
        url_has_price_filter = price_max < 99_000_000 or price_min > 0

        def _page_has_listings(t: str) -> bool:
            """Проверяем что страница содержит реальные объявления, а не заглушку."""
            return (
                '"urlPath"' in t or
                'data-marker="item"' in t or
                ('__NEXT_DATA__' in t and (f'"/{slug}/' in t or '"catalog"' in t)) or
                ('"items"' in t and (f'"/{slug}/' in t or '"priceDetailed"' in t))
            )

        def _try_fetch(fetch_url: str) -> str | None:
            """Пробуем: быстрый прямой запрос → headless-браузер (только без прокси)."""
            # 1. Прямой запрос через прокси (если есть) или напрямую
            try:
                r2 = _req.get(fetch_url, timeout=8, headers=_HEADERS, proxies=_avito_proxies())
                if r2.status_code == 200 and _page_has_listings(r2.text):
                    return r2.text
            except Exception:
                pass
            # 2. Headless-браузер с прокси (SOCKS5 поддерживает HTTPS, HTTP — нет)
            if AVITO_PROXIES and AVITO_PROXY_PROTOCOL == "http":
                return None  # HTTP-прокси не поддерживает CONNECT для HTTPS
            html = _avito_fetch_html(fetch_url)
            if html and _page_has_listings(html):
                return html
            return None

        try:
            text = _try_fetch(url)
            from_fallback = False

            if not text:
                # Fallback URL без ценового фильтра
                fallback_url = f"https://www.avito.ru/{slug}/avtomobili?seller_type=1" + (f"&p={p}" if p > 1 else "")
                text = _try_fetch(fallback_url)
                if not text:
                    print(f"  [Авито] стр.{p}: нет данных")
                    return []
                from_fallback = True
                url_has_price_filter = False
                print(f"  [Авито] стр.{p}: fallback URL, {len(text):,}б")
            else:
                print(f"  [Авито] стр.{p}: {len(text):,}б")
            batch = _parse_avito_html(text, slug, today)

            # Если price-filtered URL вернул страницу но 0 items (CAPTCHA/пустая) — пробуем fallback
            if not batch and not from_fallback:
                fallback_url = f"https://www.avito.ru/{slug}/avtomobili?seller_type=1" + (f"&p={p}" if p > 1 else "")
                text2 = _try_fetch(fallback_url)
                if text2:
                    batch2 = _parse_avito_html(text2, slug, today)
                    if batch2:
                        text = text2
                        batch = batch2
                        from_fallback = True
                        url_has_price_filter = False
                        print(f"  [Авито] стр.{p}: fallback дал {len(batch)} объявлений")

            # Помечаем: пришли ли из URL с ценовым фильтром Авито
            for it in batch:
                it["_avito_price_filtered"] = url_has_price_filter and not from_fallback

            # Строим карты: цена, фото, описание — глобальный скан всей страницы
            price_map: dict[str, int] = {}
            image_map: dict[str, str] = {}
            desc_map: dict[str, str] = {}
            title_map: dict[str, str] = {}
            mileage_map: dict[str, int] = {}

            # Все urlPath объявлений этого города
            listing_pat = re.compile(
                r'"urlPath"\s*:\s*"(/' + re.escape(slug) + r'/[a-z0-9_./-]+-\d{5,})"'
            )
            slug_matches = list(listing_pat.finditer(text))
            print(f"  [Авито] найдено listing urlPath: {len(slug_matches)}")

            if slug_matches:
                # Глобальный скан: находим ВСЕ цены, фото, описания, заголовки
                # и привязываем к ближайшему urlPath по позиции в тексте

                # Все цены — valueText с числом
                all_prices: list[tuple[int, int]] = []  # (позиция, цена)
                for pm in re.finditer(r'"valueText"\s*:\s*"([\d][\d\s.,]{1,18}(?:₽|руб|\\u20bd|р\.)?)"', text):
                    d = re.sub(r"[^\d]", "", pm.group(1))
                    if d and 10_000 < int(d) < 99_000_000:
                        all_prices.append((pm.start(), int(d)))
                # Fallback: priceDetailed → value (число)
                for pm in re.finditer(r'"priceDetailed"\s*:\s*\{[^}]{0,200}"value"\s*:\s*(\d{4,9})', text):
                    val = int(pm.group(1))
                    if 10_000 < val < 99_000_000:
                        all_prices.append((pm.start(), val))
                # Прямое "price":NNN (только если нет valueText рядом)
                for pm in re.finditer(r'"price"\s*:\s*(\d{5,8})\b', text):
                    val = int(pm.group(1))
                    if 10_000 < val < 99_000_000:
                        all_prices.append((pm.start(), val))

                # Все фото — img.avito.st (расширенный поиск без требования расширения)
                all_images: list[tuple[int, str]] = []
                seen_imgs: set[str] = set()

                def _add_img(pos: int, raw_match: str) -> None:
                    raw_url = raw_match.replace("\\/", "/").replace("\\u002F", "/")
                    url_img = ("https:" + raw_url) if raw_url.startswith("//") else raw_url
                    if any(x in url_img.lower() for x in ("/stub", "placeholder", "noimage", "logo")):
                        return
                    if url_img not in seen_imgs:
                        seen_imgs.add(url_img)
                        all_images.append((pos, url_img))

                # Паттерн 1: стандартный CDN URL (img/images.avito.st), в т.ч.
                # экранированный JSON ("https:\/\/75.img.avito.st\/...") и
                # protocol-relative ("//75.img.avito.st/...").
                for im in re.finditer(r'((?:https?:)?(?:\\?/){2}(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st/[^"\'<\s,\]}{\\]{10,})', text):
                    _add_img(im.start(), im.group(1))

                # Паттерн 2: HTML-атрибуты data-src / src указывающие на CDN
                #            (мобильная/ленивая загрузка карточек выдачи).
                for im in re.finditer(r'(?:data-src|src|data-marker[^=]*)=["\'](\s*(?:https:)?//(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st/images?/[^"\']{5,})["\']', text):
                    _add_img(im.start(), im.group(1).strip())

                # Паттерн 3: srcset="//75.img.avito.st/... 1x, ... 2x"
                for im in re.finditer(r'srcset=["\']([^"\']+)["\']', text):
                    for piece in im.group(1).split(","):
                        u = piece.strip().split(" ")[0]
                        if "img.avito.st" in u or "images.avito.st" in u:
                            _add_img(im.start(), u)

                # Паттерн 4: JSON-массив "images":["https://..."] / вложенные
                #            размеры {"864x648":"https://..."} с экранированием.
                for im in re.finditer(r'"(?:images?|photos?|gallery|preview|\d+x\d+)"\s*:\s*"((?:https?:)?(?:\\?/){2}(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st(?:\\?/)[^"]{5,})"', text):
                    _add_img(im.start(), im.group(1))

                # Все описания
                all_descs: list[tuple[int, str]] = []
                for dm in re.finditer(r'"description"\s*:\s*"([^"]{30,800})"', text):
                    d = dm.group(1).replace("\\n", " ").replace('\\"', '"').strip()
                    if len(d) > 20 and not d.startswith("http") and "avito" not in d[:20]:
                        all_descs.append((dm.start(), d[:400]))

                # Все заголовки
                all_titles: list[tuple[int, str]] = []
                for tm in re.finditer(r'"title"\s*:\s*"([^"]{5,120})"', text):
                    t = tm.group(1).replace('\\"', '"')
                    if not t.startswith("http") and len(t) > 3:
                        all_titles.append((tm.start(), t))

                # Привязка к urlPath: для каждого urlPath ищем ближайший элемент
                positions = [m.start() for m in slug_matches]
                paths = [m.group(1) for m in slug_matches]

                def _nearest_path(pos: int, max_dist: int = 8000) -> str | None:
                    """Ближайший urlPath к данной позиции в тексте."""
                    best = None
                    best_d = max_dist
                    for i, p_pos in enumerate(positions):
                        d = abs(p_pos - pos)
                        if d < best_d:
                            best_d = d
                            best = paths[i]
                    return best

                # Пробег — mileage в params
                # "mileage" or "km" in params array
                for mm in re.finditer(r'"mileage"\s*:\s*(\d{3,7})', text):
                    val = int(mm.group(1))
                    if 1000 < val < 9_000_000:
                        path = _nearest_path(mm.start(), max_dist=5000)
                        if path and path not in mileage_map:
                            mileage_map[path] = val

                for pos, price in all_prices:
                    path = _nearest_path(pos, max_dist=6000)
                    if path and path not in price_map:
                        price_map[path] = price

                # Фото: привязываем к ближайшему urlPath по абсолютному расстоянию.
                # Авито может размещать urlPath как ДО, так и ПОСЛЕ блока images,
                # поэтому убираем направленное ограничение (0 < d) и берём min(abs).
                for pos, url_img in all_images:
                    best = None
                    best_d = 8000
                    for i, p_pos in enumerate(positions):
                        d = abs(p_pos - pos)  # абсолютное расстояние — направление не важно
                        if d < best_d:
                            best_d = d
                            best = paths[i]
                    if best and not image_map.get(best):
                        image_map[best] = url_img

                for pos, desc in all_descs:
                    path = _nearest_path(pos, max_dist=6000)
                    if path and path not in desc_map:
                        desc_map[path] = desc

                for pos, title in all_titles:
                    path = _nearest_path(pos, max_dist=5000)
                    if path and path not in title_map:
                        title_map[path] = title

            print(f"  [Авито] глоб.скан: цены={len(price_map)}, фото={len(image_map)}, описания={len(desc_map)}")


            for it in batch:
                path = it["url"].replace("https://www.avito.ru", "")
                if it.get("_price_int", 0) == 0 and path in price_map:
                    v = price_map[path]
                    it["_price_int"] = v
                    it["price"] = f"{v:,} ₽".replace(",", " ")
                if not it.get("_photo_url") and path in image_map:
                    it["_photo_url"] = image_map[path]
                if not it.get("description") and path in desc_map:
                    it["description"] = desc_map[path]
                if not it.get("mileage") and path in mileage_map:
                    it["mileage"] = mileage_map[path]

            # Если _parse_avito_html не нашёл объявлений — строим их из regex-карт
            if not batch and (price_map or image_map or title_map):
                for url_p, title in title_map.items():
                    item_url = "https://www.avito.ru" + url_p
                    price_int = price_map.get(url_p, 0)
                    price_str = f"{price_int:,} ₽".replace(",", " ") if price_int else ""
                    item = {
                        "source": "avito", "title": title,
                        "price": price_str, "url": item_url,
                        "date": str(today), "_photos": 0, "_days_on_site": 0,
                        "description": desc_map.get(url_p, ""),
                        "seller": "", "_photo_url": image_map.get(url_p, ""),
                        "_price_int": price_int,
                        "_avito_price_filtered": url_has_price_filter and not from_fallback,
                    }
                    item["_hot_score"] = hot_score(item)
                    batch.append(item)
                if batch:
                    print(f"  [Авито] regex fallback: построено {len(batch)} объявлений")

            print(f"  [Авито] стр.{p}: {len(batch)} объявлений")
            return batch
        except Exception as e:
            print(f"  [Авито] стр.{p}: {e}")
            return []

    # Параллельно запрашиваем все страницы (5 потоков — лимит конкурентности ScraperAPI)
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_fetch_page, p): p for p in range(1, pages + 1)}
        for fut in as_completed(futs):
            results.extend(fut.result())

    # Глобальный fallback: если 0 результатов — пробуем без ценового фильтра.
    # Авито часто отдаёт CAPTCHA именно на URL с pmin/pmax, поэтому сканируем
    # несколько страниц обычного списка и фильтруем по цене на нашей стороне.
    if not results and (price_min > 0 or price_max < 99_000_000):
        print(f"  [Авито] 0 результатов с ценовым фильтром — пробуем без фильтра")
        import requests as _req_fb
        fb_results: list[dict] = []

        def _fetch_fallback_page(fb_page: int) -> list[dict]:
            try:
                fallback_url = f"https://www.avito.ru/{slug}/avtomobili?seller_type=1"
                if sort_by_date:
                    fallback_url += "&s=104"
                if fb_page > 1:
                    fallback_url += f"&p={fb_page}"
                # Прямой запрос первой — бесплатно и быстро, при неудаче — headless-браузер
                fb_text = ""
                try:
                    r_direct = _req_fb.get(fallback_url, timeout=8, headers=_HEADERS, proxies=_avito_proxies())
                    if r_direct.status_code == 200 and ('"urlPath"' in r_direct.text or 'data-marker="item"' in r_direct.text):
                        fb_text = r_direct.text
                except Exception:
                    pass
                if not fb_text:
                    html = _avito_fetch_html(fallback_url)
                    if html and ('"urlPath"' in html or 'data-marker="item"' in html):
                        fb_text = html
                if not fb_text:
                    return []
                batch_fb = _parse_avito_html(fb_text, slug, today)
                price_map_fb: dict[str, int] = {}
                image_map_fb: dict[str, str] = {}
                desc_map_fb: dict[str, str] = {}
                for m in re.finditer(r'"urlPath"\s*:\s*"(/[^"]+)"', fb_text):
                    url_p = m.group(1)
                    chunk = fb_text[m.end():m.end() + 3000]
                    pm = re.search(r'"value"\s*:\s*(\d{4,9})', chunk)
                    if pm:
                        val = int(pm.group(1))
                        if 10_000 < val < 99_000_000:
                            price_map_fb[url_p] = val
                    elif True:
                        pm2 = re.search(r'"valueText"\s*:\s*"([^"]+)"', chunk)
                        if pm2:
                            digits = re.sub(r"[^\d]", "", pm2.group(1))
                            if digits and 10_000 < int(digits) < 99_000_000:
                                price_map_fb[url_p] = int(digits)
                    img_m = re.search(
                        r'"(?:864x648|1280x960|640x480|432x324|320x240)"\s*:\s*"((?:https:)?(?:\\?/){2}(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st[^"\\]{10,}\.(?:jpg|jpeg|webp|png))"',
                        chunk
                    )
                    if img_m:
                        raw = img_m.group(1).replace("\\/", "/")
                        image_map_fb[url_p] = ("https:" + raw) if raw.startswith("//") else raw
                    dm = re.search(r'"description"\s*:\s*"([^"]{25,})"', chunk)
                    if dm:
                        d = dm.group(1).replace("\\n", " ").replace('\\"', '"').strip()
                        if len(d) > 20 and not d.startswith("http"):
                            desc_map_fb[url_p] = d[:350]
                for it in batch_fb:
                    path = it["url"].replace("https://www.avito.ru", "")
                    if it.get("_price_int", 0) == 0 and path in price_map_fb:
                        v = price_map_fb[path]
                        it["_price_int"] = v
                        it["price"] = f"{v:,} ₽".replace(",", " ")
                    if not it.get("_photo_url") and path in image_map_fb:
                        it["_photo_url"] = image_map_fb[path]
                    if not it.get("description") and path in desc_map_fb:
                        it["description"] = desc_map_fb[path]
                    it["_avito_price_filtered"] = False
                print(f"  [Авито] fallback стр.{fb_page}: {len(batch_fb)} объявлений")
                return batch_fb
            except Exception as e:
                print(f"  [Авито] fallback стр.{fb_page} ошибка: {e}")
                return []

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(_fetch_fallback_page, p) for p in range(1, 4)]
            for fut in as_completed(futs):
                fb_results.extend(fut.result())
        results = fb_results
        print(f"  [Авито] fallback итого: {len(results)} объявлений")

    print(f"  [Авито] итого {len(results)} объявлений")
    return results



# ── FSM состояния ────────────────────────────────────────────────

class Setup(StatesGroup):
    category = State()
    brand = State()
    region = State()
    price_min = State()
    price_max = State()


class TrackBrand(StatesGroup):
    choosing = State()


# ── Бот ─────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ── Подписка на канал ────────────────────────────────────────────
REQUIRED_CHANNEL = "@ekbdrivee"
REQUIRED_CHANNEL_URL = "https://t.me/ekbdrivee"

_SUBSCRIBE_MSG = (
    "📢 *Для использования бота необходимо подписаться на наш канал!*\n\n"
    "PerekupDrive — это сообщество перекупщиков и охотников за выгодными авто.\n\n"
    "🔥 В канале:\n"
    "• Свежие объявления ниже рынка\n"
    "• Советы по покупке и проверке авто\n"
    "• Уведомления по машинам которые только вышли на рынок\n\n"
    "👇 Подпишись и нажми *«Я подписался»*"
)

async def _is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception as e:
        print(f"  [подписка] ошибка проверки {user_id}: {e}")
        # Если бот не админ канала — get_chat_member вернёт ошибку.
        # В этом случае НЕ пропускаем (False), чтобы не обходить проверку.
        return False

def _subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")],
    ])

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

class SubscriptionMiddleware(BaseMiddleware):
    """Подписка отключена — пропускаем всех."""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        return await handler(event, data)

async def _check_and_gate(msg_or_cb) -> bool:
    """Оставлен для совместимости, основная проверка теперь в middleware."""
    return True

# URL-ID маппинг для кнопок
_id_to_url: dict[str, str] = {}
_url_to_id: dict[str, str] = {}
_id_counter = 0


def url_to_id(url: str) -> str:
    global _id_counter
    if url not in _url_to_id:
        _id_counter += 1
        sid = str(_id_counter)
        _url_to_id[url] = sid
        _id_to_url[sid] = url
    return _url_to_id[url]


def id_to_url(sid: str) -> str:
    return _id_to_url.get(sid, sid)


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Найти авто"), KeyboardButton(text="🌐 Глобальный поиск")],
        [KeyboardButton(text="🆕 Новые сегодня"), KeyboardButton(text="🎯 Следить за маркой")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🚗 Мой гараж")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="🤝 Пригласить друга"), KeyboardButton(text="♻️ Сбросить историю")],
    ],
    resize_keyboard=True,
    persistent=True,
)



def region_keyboard():
    rows = []
    items = list(REGIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for slug, name in items[i:i+2]:
            row.append(InlineKeyboardButton(text=name, callback_data=f"region|{slug}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="setup_back_to_category")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Категории и марки ────────────────────────────────────────────

CATEGORY_LABELS = {
    "all":      "🚗 Все автомобили",
    "foreign":  "🌍 Иномарки",
    "domestic": "🇷🇺 Отечественные",
    "moto":     "🏍 Мото / Квадро",
    "misc":     "🔧 Разное",
}

# Ключевые слова для фильтрации по категории в заголовке объявления
DOMESTIC_BRANDS = [
    "ваз", "vaz", "lada", "лада", "газ", "gaz", "уаз", "uaz",
    "москвич", "moskvich", "нива", "niva", "волга", "volga", "ока", "oka",
    "иж", "izh",
]

FOREIGN_BRANDS_LIST = [
    "kia", "toyota", "chevrolet", "hyundai", "renault", "volkswagen",
    "ford", "nissan", "mazda", "bmw", "mercedes", "opel", "skoda",
    "audi", "mitsubishi", "daewoo", "honda", "peugeot", "volvo",
    "subaru", "suzuki", "lexus", "infiniti", "jeep", "land rover",
    "porsche", "alfa", "citroen", "seat", "fiat",
]

FOREIGN_BRANDS_DISPLAY = [
    ("Kia", "kia"), ("Toyota", "toyota"), ("Chevrolet", "chevrolet"),
    ("Hyundai", "hyundai"), ("Renault", "renault"), ("Volkswagen", "volkswagen"),
    ("Ford", "ford"), ("Nissan", "nissan"), ("Mazda", "mazda"),
    ("BMW", "bmw"), ("Mercedes", "mercedes"), ("Opel", "opel"),
    ("Skoda", "skoda"), ("Audi", "audi"), ("Mitsubishi", "mitsubishi"),
    ("Daewoo", "daewoo"), ("Honda", "honda"), ("Peugeot", "peugeot"),
    ("Volvo", "volvo"), ("Subaru", "subaru"), ("Suzuki", "suzuki"),
    ("Lexus", "lexus"), ("Infiniti", "infiniti"),
]

DOMESTIC_BRANDS_DISPLAY = [
    ("ВАЗ/Lada", "lada"), ("ГАЗ", "gaz"), ("УАЗ", "uaz"),
    ("Москвич", "moskvich"), ("Нива", "niva"),
]

# Русские синонимы для брендов (для фильтрации по заголовку)
BRAND_RU_ALIASES: dict[str, list[str]] = {
    "bmw": ["бмв", "bmw"],
    "mercedes": ["мерседес", "mercedes"],
    "volkswagen": ["фольксваген", "volkswagen", "vw"],
    "audi": ["ауди", "audi"],
    "toyota": ["тойота", "toyota"],
    "kia": ["киа", "kia"],
    "hyundai": ["хендай", "хундай", "hyundai"],
    "renault": ["рено", "renault"],
    "chevrolet": ["шевроле", "chevrolet"],
    "nissan": ["ниссан", "nissan"],
    "mazda": ["мазда", "mazda"],
    "mitsubishi": ["митсубиши", "митсубиси", "mitsubishi"],
    "opel": ["опель", "opel"],
    "ford": ["форд", "ford"],
    "skoda": ["шкода", "skoda"],
    "honda": ["хонда", "honda"],
    "subaru": ["субару", "subaru"],
    "suzuki": ["сузуки", "suzuki"],
    "peugeot": ["пежо", "peugeot"],
    "volvo": ["вольво", "volvo"],
    "lexus": ["лексус", "lexus"],
    "infiniti": ["инфинити", "infiniti"],
    "daewoo": ["дэу", "daewoo"],
    "lada": ["лада", "ваз", "lada", "vaz", "ладa"],
    "gaz": ["газ", "gaz", "волга", "волгa"],
    "uaz": ["уаз", "uaz"],
    "moskvich": ["москвич", "moskvich"],
    "niva": ["нива", "niva"],
}


def _match_brand(title: str, brand_key: str) -> bool:
    """Проверяет, содержит ли заголовок объявления указанную марку."""
    tl = title.lower()
    aliases = BRAND_RU_ALIASES.get(brand_key.lower(), [brand_key.lower()])
    return any(a in tl for a in aliases)


_MOTO_KEYWORDS = [
    "скутер", "мотоцикл", "мопед", "квадроцикл", "питбайк", "мотобайк",
    "scooter", "moto", "motorcycle", "atv", "квадро", "enduro", "эндуро",
    "питбайк", "мотик", "vespa", "yamaha ybr", "honda cbr",
    "kawasaki", "suzuki gsx", "yamaha r1", "yamaha r6", "ktm",
    "2-колесный", "двухколесный", "снегоход", "гидроцикл",
    "кубов", "куб.см", "cc ",
]

def _is_moto(title: str) -> bool:
    """Возвращает True если объявление о мото/скутере а не об автомобиле."""
    tl = title.lower()
    # Исключаем только если НЕТ явных маркеров автомобиля
    car_markers = ["автомобил", "легковой", "внедорожник", "кроссовер", "седан",
                   "хэтчбек", "универсал", "минивэн", "пикап", "кабриолет",
                   "лада", "vaz", "ваз", "газ", "уаз",
                   "toyota", "honda accord", "honda cr", "honda hr", "honda fit",
                   "kia", "hyundai", "nissan", "mazda", "bmw", "audi",
                   "mercedes", "volkswagen", "skoda", "opel", "ford", "renault",
                   "haval", "geely", "chery", "changan", "lixiang", "exeed",
                   "mitsubishi", "субару", "subaru", "lexus", "лексус",
                   "infiniti", "инфинити", "volvo", "вольво", "peugeot", "пежо",
                   "citroen", "ситроен", "chevrolet", "шевроле", "datsun",
                   "suzuki sx", "suzuki vitara", "suzuki jimny", "suzuki swift",
                   "suzuki grand", "suzuki kizashi", "land rover", "jeep",
                   "москвич", "нива", "приора", "гранта", "калина", "веста"]
    has_car = any(k in tl for k in car_markers)
    if has_car:
        return False
    return any(k in tl for k in _MOTO_KEYWORDS)


def _match_brand_item(it: dict, brand: str) -> bool:
    """Проверяет марку в заголовке, URL и описании объявления.

    Авито при скрейпинге через URL-фильтр марки (напр. /mitsubishi/avtomobili)
    может возвращать заголовки БЕЗ названия марки (например «Outlander, 2018»
    вместо «Mitsubishi Outlander, 2018»). Поэтому проверяем все доступные поля.
    """
    title = it.get("title", "")
    if _match_brand(title, brand):
        return True
    # Проверяем URL — Авито кодирует марку в пути: /ekaterinburg/avtomobili/mitsubishi-...
    url = it.get("url", "").lower()
    aliases = BRAND_RU_ALIASES.get(brand.lower(), [brand.lower()])
    for alias in aliases:
        if alias in url:
            return True
    # Проверяем описание (первые 200 символов)
    desc = it.get("description", "")[:200]
    if _match_brand(desc, brand):
        return True
    return False


def _filter_by_category(items: list[dict], category: str, brand: str) -> list[dict]:
    """Фильтрует список объявлений по категории и марке. Всегда исключает мото/скутеры."""
    # Всегда убираем скутеры/мотоциклы из поиска авто
    items = [it for it in items if not _is_moto(it.get("title", ""))]

    if not category or category == "all":
        pass  # без фильтра по марке
    elif category == "domestic":
        items = [it for it in items if any(k in it.get("title", "").lower() for k in DOMESTIC_BRANDS)]
    elif category == "foreign":
        # Иномарки: исключаем только те, где в заголовке явно указан отечественный бренд.
        # Если заголовок не содержит отечественного бренда — считаем иномаркой
        # (Авито при brand-URL может возвращать заголовки без марки).
        items = [it for it in items if not any(k in it.get("title", "").lower() for k in DOMESTIC_BRANDS)]

    if brand and brand != "any":
        # Проверяем марку в заголовке, URL и описании — Авито при brand-URL-фильтрации
        # может опускать название марки из заголовка объявления.
        items = [it for it in items if _match_brand_item(it, brand)]

    return items


def category_keyboard(damaged_on: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Все автомобили", callback_data="cat|all")],
        [
            InlineKeyboardButton(text="🌍 Иномарки", callback_data="cat|foreign"),
            InlineKeyboardButton(text="🇷🇺 Отечественные", callback_data="cat|domestic"),
        ],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="setup_cancel")],
    ])


def brands_keyboard(category: str, prefix: str = "brand") -> InlineKeyboardMarkup:
    """Клавиатура выбора марки в сетке 2 колонки."""
    if category == "domestic":
        brand_list = DOMESTIC_BRANDS_DISPLAY
    else:
        brand_list = FOREIGN_BRANDS_DISPLAY

    rows = []
    for i in range(0, len(brand_list), 2):
        row = []
        for name, key in brand_list[i:i+2]:
            row.append(InlineKeyboardButton(text=name, callback_data=f"{prefix}|{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔍 Любая марка", callback_data=f"{prefix}|any")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="setup_back_to_category")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def track_brands_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора марки для слежения (все марки + отключить)."""
    all_brands = FOREIGN_BRANDS_DISPLAY + DOMESTIC_BRANDS_DISPLAY
    rows = []
    for i in range(0, len(all_brands), 2):
        row = []
        for name, key in all_brands[i:i+2]:
            row.append(InlineKeyboardButton(text=name, callback_data=f"track|{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Отключить слежку", callback_data="track|off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "check_subscription")
async def cb_check_subscription(cb: CallbackQuery):
    await cb.answer()
    if await _is_subscribed(cb.from_user.id):
        await cb.message.edit_text(
            "✅ Подписка подтверждена! Добро пожаловать в PerekupDrive 🚗\n\n"
            "Нажми /start чтобы начать поиск.",
        )
    else:
        await cb.message.answer(
            "❌ Ты ещё не подписан на канал. Подпишись и нажми кнопку снова.",
            reply_markup=_subscribe_keyboard(),
        )


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if not await _check_and_gate(msg):
        return
    analytics.track("start", uid=msg.from_user.id, username=msg.from_user.username)
    # Handle referral parameter
    text_parts = (msg.text or "").split()
    if len(text_parts) > 1:
        param = text_parts[1]
        if param.startswith("ref_"):
            try:
                inviter_uid = int(param[4:])
                if inviter_uid != msg.from_user.id:
                    _record_referral(msg.from_user.id, inviter_uid)
                    await msg.answer(
                        "👋 *Добро пожаловать в PerekupDrive!*\n\n"
                        "Ты получил *7 дней полного доступа*.\n"
                        "Всё бесплатно, без ограничений.\n\n"
                        "🎯 *Что сделать прямо сейчас:*\n\n"
                        "1️⃣ Настроить поиск по всем площадкам (Авито, Дром, Авто.ру, ВК, Telegram) под свои параметры.\n\n"
                        "2️⃣ Сохранить 3 интересных авто в Избранное.\n\n"
                        "3️⃣ Включить поискового агента — бот сам пришлёт новые объявления.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="▶️ Начать поиск", callback_data="open_settings")],
                        ]),
                    )
            except Exception:
                pass
    _get_or_create_referral(msg.from_user.id)
    s = load_settings(msg.from_user.id)
    name = msg.from_user.first_name or "друг"
    is_new_user = not s.get("region")

    if is_new_user:
        # Новый пользователь — красивое приветствие
        await msg.answer(
            f"👋 *Добро пожаловать в PerekupDrive, {name}!*\n\n"
            f"Ты получил *7 дней полного доступа*.\n"
            f"Всё бесплатно, без ограничений.\n\n"
            f"🎯 *Что сделать прямо сейчас:*\n\n"
            f"1️⃣ Настроить поиск по всем площадкам (Авито, Дром, Авто.ру, ВК, Telegram) под свои параметры.\n\n"
            f"2️⃣ Сохранить интересные авто в Избранное.\n\n"
            f"3️⃣ Включить поискового агента — бот сам пришлёт новые объявления.\n\n"
            f"👇 Начнём с настройки поиска:",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        await msg.answer("🔍 Шаг 1/4: Что ищем?", reply_markup=category_keyboard())
        await state.set_state(Setup.category)
    else:
        # Старый пользователь — дружелюбное приветствие
        await msg.answer(
            f"👋 Привет, {name}! Я *PerekupDrive* — бот для поиска авто ниже рыночной цены.\n\n"
            f"🔍 Ищу объявления от частных лиц на Авито\n"
            f"📊 Сравниваю цены с рынком и нахожу выгодные\n"
            f"🔔 Могу присылать уведомления когда появится новое выгодное авто",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return  # тихо игнорируем не-админов
    try:
        text = analytics.format_stats_text(REGIONS)
    except Exception as e:
        await msg.answer(f"Не удалось собрать статистику: {e}")
        return
    await msg.answer(text, parse_mode="Markdown")


@dp.message(Command("dashboard"))
async def cmd_dashboard(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("❌ Только для администраторов.")
        return
    try:
        text = analytics.format_stats_text(REGIONS)
    except Exception as e:
        text = f"Ошибка получения статистики: {e}"

    key = os.getenv("DASHBOARD_KEY", "")
    # Railway может давать домен через разные переменные
    railway_url = (
        os.getenv("RAILWAY_PUBLIC_DOMAIN")
        or os.getenv("RAILWAY_STATIC_URL")
        or os.getenv("RAILWAY_SERVICE_URL")
        or os.getenv("PUBLIC_URL")
        or ""
    ).strip().rstrip("/")
    # Убираем протокол если он уже есть — добавим сами
    if railway_url.startswith("https://"):
        railway_url = railway_url[8:]
    elif railway_url.startswith("http://"):
        railway_url = railway_url[7:]

    if railway_url:
        dash_url = f"https://{railway_url}/?key={key}" if key else f"https://{railway_url}/"
    else:
        dash_url = None

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть дашборд", url=dash_url)],
    ]) if dash_url else None

    if not dash_url:
        # Показать какие Railway-переменные домена реально заданы
        found_vars = {k: v for k, v in os.environ.items() if "RAILWAY" in k or "PUBLIC" in k or "URL" in k or "DOMAIN" in k}
        var_hint = "\n".join(f"`{k}={v}`" for k, v in list(found_vars.items())[:8]) or "не найдено"
        text += f"\n\n⚠️ Не найден публичный домен Railway.\n*Env vars:*\n{var_hint}\n\nДобавь `RAILWAY_PUBLIC_DOMAIN` в Railway → Variables"

    await msg.answer(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


# Все возможные площадки для мониторинга
_MONITOR_SOURCES = [
    ("avito",  "Авито"),
    ("drom",   "Дром"),
    ("autoru", "Auto.ru"),
    ("vk",     "ВКонтакте"),
    ("tg",     "Telegram"),
]

def _monitor_sources(s: dict) -> list[str]:
    """Возвращает список включённых площадок. По умолчанию — все."""
    src = s.get("monitor_sources")
    if not src or not isinstance(src, list):
        return [k for k, _ in _MONITOR_SOURCES]
    return src


def _notify_keyboard(s: dict) -> InlineKeyboardMarkup:
    enabled = s.get("monitor_enabled", False)
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    toggle_text = "🔕 Выключить мониторинг" if enabled else "🔔 Включить мониторинг"
    active_src = _monitor_sources(s)
    src_label = ", ".join(n for k, n in _MONITOR_SOURCES if k in active_src) or "Не выбраны"
    monitor_regions = s.get("monitor_regions", [])
    extra_reg_label = f"+{len(monitor_regions)} регионов" if monitor_regions else "только мой регион"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="notify_toggle")],
        [InlineKeyboardButton(text=f"🌐 Площадки: {src_label}", callback_data="notify_sources")],
        [InlineKeyboardButton(text=f"📍 Регионы: {extra_reg_label}", callback_data="notify_regions")],
        [
            InlineKeyboardButton(text=f"⏱ Каждые {interval} мин", callback_data="notify_interval"),
            InlineKeyboardButton(text=f"📉 Скидка от {min_pct}%", callback_data="notify_pct"),
        ],
        [InlineKeyboardButton(text="⭐ Моё избранное", callback_data="notify_favs")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="notify_back")],
    ])


def _notify_sources_keyboard(s: dict) -> InlineKeyboardMarkup:
    active = set(_monitor_sources(s))
    rows = []
    for key, label in _MONITOR_SOURCES:
        check = "✅" if key in active else "☐"
        rows.append([InlineKeyboardButton(text=f"{check} {label}", callback_data=f"notify_src_toggle|{key}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="notify_sources_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _notify_regions_keyboard(s: dict, page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура выбора дополнительных регионов для мониторинга."""
    extra = set(s.get("monitor_regions", []))
    region_list = list(REGIONS.items())  # [(slug, name), ...]
    per_page = 8
    total_pages = (len(region_list) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    chunk = region_list[page * per_page: (page + 1) * per_page]
    rows = []
    for slug, name in chunk:
        check = "✅" if slug in extra else "☐"
        rows.append([InlineKeyboardButton(text=f"{check} {name}", callback_data=f"notify_reg_toggle|{slug}|{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"notify_reg_page|{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"notify_reg_page|{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="notify_sources_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "notify_settings")
async def cb_notify_settings(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = s.get("monitor_enabled", False)
    status = "✅ Включён" if enabled else "❌ Выключен"
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    active_src = _monitor_sources(s)
    src_names = ", ".join(n for k, n in _MONITOR_SOURCES if k in active_src)
    extra_regions = s.get("monitor_regions", [])
    reg_names = ", ".join(REGIONS.get(r, r) for r in extra_regions) if extra_regions else "нет"
    own_region = REGIONS.get(s.get("region", ""), s.get("region", ""))
    await cb.message.answer(
        f"🔔 *Настройки уведомлений*\n\n"
        f"Статус: {status}\n"
        f"Интервал проверки: каждые {interval} мин\n"
        f"Минимальная скидка: {min_pct}% ниже рынка\n"
        f"Площадки: {src_names}\n"
        f"Регион: {own_region}"
        + (f"\nДоп. регионы: {reg_names}" if extra_regions else "") +
        f"\n\nПри появлении выгодного авто — сразу пришлю с фото, ценой и скидкой от рынка.",
        parse_mode="Markdown",
        reply_markup=_notify_keyboard(s),
    )


@dp.callback_query(F.data == "notify_toggle")
async def cb_notify_toggle(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = not s.get("monitor_enabled", False)
    s["monitor_enabled"] = enabled
    save_settings(uid, s)
    if enabled:
        _start_monitor(uid)
        region_name = REGIONS.get(s.get("region", ""), s.get("region", ""))
        # Сеем seen текущим каталогом региона: чтобы НЕ завалить пользователя
        # всем существующим бэклогом, а присылать ТОЛЬКО реально новые авто,
        # которые появятся ПОСЛЕ включения мониторинга.
        try:
            region_slug = s.get("region", "")
            loop = asyncio.get_running_loop()
            existing = await loop.run_in_executor(
                None, lambda: scrape_avito(region_slug, pages=2, sort_by_date=False)
            )
            if existing:
                seen = load_seen(uid)
                seen.update(it["url"] for it in existing if it.get("url"))
                save_seen(uid, seen)
                print(f"  [монитор] uid={uid}: seed seen {len(existing)} текущих объявлений")
        except Exception as e:
            print(f"  [монитор] seed seen ошибка: {e}")
        active_src = _monitor_sources(s)
        src_names = ", ".join(n for k, n in _MONITOR_SOURCES if k in active_src)
        extra_regions = s.get("monitor_regions", [])
        all_regions_names = region_name
        if extra_regions:
            all_regions_names += ", " + ", ".join(REGIONS.get(r, r) for r in extra_regions)
        await cb.message.answer(
            f"✅ *Мониторинг включён!*\n\n"
            f"🔔 Слежу за площадками: *{src_names}*\n"
            f"📍 Регионы: *{all_regions_names}*\n\n"
            f"Как только появится новое авто ниже рынка — сразу пришлю с фото, ценой и скидкой.\n"
            f"Проверяю каждые ~2 минуты. Текущие объявления не показываю — только свежие.",
            parse_mode="Markdown",
            reply_markup=_notify_keyboard(s),
        )
    else:
        _stop_monitor(uid)
        await cb.message.answer(
            "🔕 Мониторинг выключен.",
            reply_markup=_notify_keyboard(s),
        )


@dp.callback_query(F.data == "notify_interval")
async def cb_notify_interval(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    current = s.get("monitor_interval_min", 5)
    options = [5, 10, 15, 30, 60]
    next_val = options[(options.index(current) + 1) % len(options)] if current in options else 15
    s["monitor_interval_min"] = next_val
    save_settings(uid, s)
    if s.get("monitor_enabled"):
        _stop_monitor(uid)
        _start_monitor(uid)
    await cb.message.edit_reply_markup(reply_markup=_notify_keyboard(s))


@dp.callback_query(F.data == "notify_pct")
async def cb_notify_pct(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    current = s.get("monitor_min_savings_pct", 10)
    options = [5, 10, 15, 20, 25]
    next_val = options[(options.index(current) + 1) % len(options)] if current in options else 10
    s["monitor_min_savings_pct"] = next_val
    save_settings(uid, s)
    await cb.message.edit_reply_markup(reply_markup=_notify_keyboard(s))


@dp.callback_query(F.data == "notify_favs")
async def cb_notify_favs(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    fav_file = user_dir(uid) / "favorites.json"
    if not fav_file.exists():
        await cb.message.answer("⭐ У тебя пока нет сохранённых объявлений.")
        return
    favs = json.loads(fav_file.read_text(encoding="utf-8"))
    if not favs:
        await cb.message.answer("⭐ Список избранного пуст.")
        return
    lines = [f"• {it.get('title','')} — {it.get('price','?')}\n  {it.get('url','')}" for it in favs[-10:]]
    await cb.message.answer(f"⭐ *Избранное* ({len(favs)} шт.):\n\n" + "\n\n".join(lines), parse_mode="Markdown")


@dp.callback_query(F.data == "notify_back")
async def cb_notify_back(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    region = s.get("region", "")
    region_name = REGIONS.get(region, region)
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    await cb.message.answer(
        f"👋 Твои настройки:\n📍 {region_name}\n💰 {pmin:,}–{pmax:,} ₽",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
            [InlineKeyboardButton(text="⚙️ Изменить настройки", callback_data="change_settings")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="notify_settings")],
        ])
    )


@dp.callback_query(F.data == "notify_sources")
async def cb_notify_sources(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    active = _monitor_sources(s)
    src_names = ", ".join(n for k, n in _MONITOR_SOURCES if k in active) or "нет"
    await cb.message.answer(
        f"🌐 *Площадки для мониторинга*\n\n"
        f"Выбери откуда получать уведомления о выгодных авто.\n"
        f"Сейчас включены: *{src_names}*\n\n"
        f"Нажми на площадку чтобы включить/выключить:",
        parse_mode="Markdown",
        reply_markup=_notify_sources_keyboard(s),
    )


@dp.callback_query(F.data.startswith("notify_src_toggle|"))
async def cb_notify_src_toggle(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    key = cb.data.split("|", 1)[1]
    s = load_settings(uid)
    active = set(_monitor_sources(s))
    if key in active:
        active.discard(key)
    else:
        active.add(key)
    # Не даём выключить всё
    if not active:
        active = {key}
    s["monitor_sources"] = list(active)
    save_settings(uid, s)
    await cb.message.edit_reply_markup(reply_markup=_notify_sources_keyboard(s))


@dp.callback_query(F.data == "notify_regions")
async def cb_notify_regions(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    extra = s.get("monitor_regions", [])
    own = REGIONS.get(s.get("region", ""), s.get("region", ""))
    extra_names = ", ".join(REGIONS.get(r, r) for r in extra) if extra else "нет"
    await cb.message.answer(
        f"📍 *Регионы мониторинга*\n\n"
        f"Основной регион (всегда включён): *{own}*\n"
        f"Дополнительные: *{extra_names}*\n\n"
        f"Выбери дополнительные регионы для мониторинга:",
        parse_mode="Markdown",
        reply_markup=_notify_regions_keyboard(s, page=0),
    )


@dp.callback_query(F.data.startswith("notify_reg_toggle|"))
async def cb_notify_reg_toggle(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    parts = cb.data.split("|")
    slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    s = load_settings(uid)
    extra = set(s.get("monitor_regions", []))
    own = s.get("region", "")
    if slug == own:
        await cb.answer("Основной регион нельзя убрать", show_alert=True)
        return
    if slug in extra:
        extra.discard(slug)
    else:
        extra.add(slug)
    s["monitor_regions"] = list(extra)
    save_settings(uid, s)
    await cb.message.edit_reply_markup(reply_markup=_notify_regions_keyboard(s, page=page))


@dp.callback_query(F.data.startswith("notify_reg_page|"))
async def cb_notify_reg_page(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    page = int(cb.data.split("|")[1])
    s = load_settings(uid)
    await cb.message.edit_reply_markup(reply_markup=_notify_regions_keyboard(s, page=page))


@dp.callback_query(F.data == "notify_sources_back")
async def cb_notify_sources_back(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = s.get("monitor_enabled", False)
    status = "✅ Включён" if enabled else "❌ Выключен"
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    active_src = _monitor_sources(s)
    src_names = ", ".join(n for k, n in _MONITOR_SOURCES if k in active_src)
    extra_regions = s.get("monitor_regions", [])
    reg_names = ", ".join(REGIONS.get(r, r) for r in extra_regions) if extra_regions else "нет"
    own_region = REGIONS.get(s.get("region", ""), s.get("region", ""))
    await cb.message.answer(
        f"🔔 *Настройки уведомлений*\n\n"
        f"Статус: {status}\n"
        f"Интервал: каждые {interval} мин\n"
        f"Минимальная скидка: {min_pct}% ниже рынка\n"
        f"Площадки: {src_names}\n"
        f"Регион: {own_region}"
        + (f"\nДополнительные регионы: {reg_names}" if extra_regions else ""),
        parse_mode="Markdown",
        reply_markup=_notify_keyboard(s),
    )


@dp.callback_query(F.data == "change_settings")
async def cb_change_settings(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer("🔍 Шаг 1/4: Что ищем?", reply_markup=category_keyboard())
    await state.set_state(Setup.category)


# ── FSM: выбор категории ─────────────────────────────────────────

@dp.callback_query(F.data.startswith("cat|"), Setup.category)
async def cb_category(cb: CallbackQuery, state: FSMContext):
    value = cb.data.split("|", 1)[1]
    await cb.answer()

    data = await state.get_data()
    if value == "toggle_damaged":
        damaged = not data.get("damaged", False)
        await state.update_data(damaged=damaged)
        await cb.message.edit_reply_markup(reply_markup=category_keyboard(damaged_on=damaged))
        return

    await state.update_data(category=value, brand="")
    cat_label = CATEGORY_LABELS.get(value, value)

    if value in ("foreign", "domestic"):
        await cb.message.answer(
            f"✅ Категория: {cat_label}\n\n🔍 Шаг 2/4: Выбери марку:",
            reply_markup=brands_keyboard(value),
        )
        await state.set_state(Setup.brand)
    else:
        await cb.message.answer(
            f"✅ Категория: {cat_label}\n\n📍 Шаг 3/4: Выбери город:",
            reply_markup=region_keyboard(),
        )
        await state.set_state(Setup.region)


# ── FSM: выбор марки ─────────────────────────────────────────────

@dp.callback_query(F.data.startswith("brand|"), Setup.brand)
async def cb_brand(cb: CallbackQuery, state: FSMContext):
    brand_key = cb.data.split("|", 1)[1]
    await cb.answer()
    brand = "" if brand_key == "any" else brand_key
    await state.update_data(brand=brand)
    brand_label = brand.capitalize() if brand else "Любая"
    await cb.message.answer(
        f"✅ Марка: {brand_label}\n\n📍 Шаг 3/4: Выбери город:",
        reply_markup=region_keyboard(),
    )
    await state.set_state(Setup.region)


def price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌑 до 100 000 ₽", callback_data="price_range:0:100000"),
            InlineKeyboardButton(text="💵 100–300 тыс", callback_data="price_range:100000:300000"),
        ],
        [
            InlineKeyboardButton(text="💵 300–500 тыс", callback_data="price_range:300000:500000"),
            InlineKeyboardButton(text="💵 500т–1 млн", callback_data="price_range:500000:1000000"),
        ],
        [
            InlineKeyboardButton(text="💎 1–3 млн", callback_data="price_range:1000000:3000000"),
            InlineKeyboardButton(text="💎 3–5 млн", callback_data="price_range:3000000:5000000"),
        ],
        [
            InlineKeyboardButton(text="👑 от 5 млн", callback_data="price_range:5000000:99000000"),
            InlineKeyboardButton(text="🔄 Любая цена", callback_data="price_range:0:99000000"),
        ],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="price_manual")],
        [InlineKeyboardButton(text="◀️ Назад (город)", callback_data="setup_back_to_region")],
    ])


@dp.callback_query(F.data.startswith("region|"))
async def cb_region(cb: CallbackQuery, state: FSMContext):
    slug = cb.data.split("|", 1)[1]
    await state.update_data(region=slug)
    await cb.answer(f"✅ {REGIONS.get(slug, slug)}")
    await cb.message.answer(
        f"📍 Регион: {REGIONS.get(slug, slug)}\n\n"
        f"💰 Шаг 4/4: Выбери диапазон цен:",
        reply_markup=price_keyboard()
    )
    await state.set_state(Setup.price_min)


@dp.callback_query(F.data.startswith("price_range:"), Setup.price_min)
async def cb_price_range(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    parts = cb.data.split(":")
    pmin = int(parts[1])
    pmax = int(parts[2])
    data = await state.get_data()
    region = data.get("region", "ekaterinburg")
    category = data.get("category", "all")
    brand = data.get("brand", "")
    damaged = data.get("damaged", False)
    settings = load_settings(cb.from_user.id)
    settings.update({"region": region, "price_min": pmin, "price_max": pmax, "category": category, "brand": brand, "damaged": damaged})
    save_settings(cb.from_user.id, settings)
    await state.clear()
    region_name = REGIONS.get(region, region)
    cat_label = CATEGORY_LABELS.get(category, category)
    brand_label = f" · {brand.capitalize()}" if brand else ""
    await cb.message.answer(
        f"✅ Настройки сохранены!\n\n"
        f"📍 Регион: {region_name}\n"
        f"🔍 Категория: {cat_label}{brand_label}\n"
        f"💰 Бюджет: {pmin:,} – {pmax:,} ₽\n\n"
        f"Нажми кнопку чтобы найти авто:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
        ])
    )


@dp.callback_query(F.data == "price_manual", Setup.price_min)
async def cb_price_manual(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer(
        "💰 Шаг 4/4: Введи минимальную цену в рублях\n"
        "(например: 300000 или 0 для любой цены):"
    )


@dp.message(Setup.price_min)
async def fsm_price_min(msg: Message, state: FSMContext):
    digits = re.sub(r"[^\d]", "", msg.text or "")
    pmin = int(digits) if digits else 0
    await state.update_data(price_min=pmin)
    await msg.answer(
        f"✅ Минимальная цена: {pmin:,} ₽\n\n"
        f"💰 Теперь введи максимальную цену\n"
        f"(например: 1000000):"
    )
    await state.set_state(Setup.price_max)


@dp.message(Setup.price_max)
async def fsm_price_max(msg: Message, state: FSMContext):
    digits = re.sub(r"[^\d]", "", msg.text or "")
    pmax = int(digits) if digits else 99_000_000
    data = await state.get_data()
    region = data.get("region", "ekaterinburg")
    pmin = data.get("price_min", 0)
    category = data.get("category", "all")
    brand = data.get("brand", "")
    damaged = data.get("damaged", False)

    s = load_settings(msg.from_user.id)
    s.update({
        "region": region, "price_min": pmin, "price_max": pmax,
        "category": category, "brand": brand, "damaged": damaged,
    })
    save_settings(msg.from_user.id, s)
    await state.clear()

    region_name = REGIONS.get(region, region)
    cat_label = CATEGORY_LABELS.get(category, category)
    brand_label = f" · {brand.capitalize()}" if brand else ""
    await msg.answer(
        f"✅ Настройки сохранены!\n\n"
        f"📍 Регион: {region_name}\n"
        f"🔍 Категория: {cat_label}{brand_label}\n"
        f"💰 Бюджет: {pmin:,} – {pmax:,} ₽\n\n"
        f"Нажми кнопку чтобы найти авто:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
        ])
    )


# ── FSM: кнопки «Назад» ──────────────────────────────────────────

@dp.callback_query(F.data == "setup_cancel")
async def cb_setup_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.answer("Настройка отменена.", reply_markup=MAIN_KEYBOARD)


@dp.callback_query(F.data == "setup_back_to_category")
async def cb_setup_back_to_category(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(brand="", category="")
    await cb.message.answer("🔍 Шаг 1/4: Что ищем?", reply_markup=category_keyboard())
    await state.set_state(Setup.category)


@dp.callback_query(F.data == "setup_back_to_region")
async def cb_setup_back_to_region(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await cb.message.answer(
        "📍 Шаг 3/4: Выбери город:",
        reply_markup=region_keyboard(),
    )
    await state.set_state(Setup.region)


@dp.callback_query(F.data == "setup_back_to_price")
async def cb_setup_back_to_price(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    region = data.get("region", "")
    region_name = REGIONS.get(region, region)
    await cb.message.answer(
        f"📍 Регион: {region_name}\n\n💰 Шаг 4/4: Выбери диапазон цен:",
        reply_markup=price_keyboard(),
    )
    await state.set_state(Setup.price_min)


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🔍 Шаг 1/4: Что ищем?", reply_markup=category_keyboard())
    await state.set_state(Setup.category)


ALL_SOURCES = ["drom", "autoru", "avito", "vk", "tg"]
SOURCE_NAMES = {
    "drom":   "🔵 Дром",
    "autoru": "🟠 Auto.ru",
    "avito":  "🔴 Авито",
    "vk":     "📘 ВКонтакте",
    "tg":     "✈️ Telegram",
}


def sources_keyboard(enabled: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for src in ALL_SOURCES:
        check = "✅" if src in enabled else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{check} {SOURCE_NAMES[src]}",
            callback_data=f"toggle_src|{src}"
        )])
    rows.append([
        InlineKeyboardButton(text="🌐 Все площадки", callback_data="src_all"),
        InlineKeyboardButton(text="🔍 Искать", callback_data="start_search"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Назад (цена)", callback_data="setup_back_to_price")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_enabled_sources(s: dict) -> list[str]:
    """Возвращает список включённых площадок, по умолчанию — все."""
    enabled = s.get("sources", [])
    if not enabled:
        return list(ALL_SOURCES)
    return enabled


@dp.message(Command("search"))
@dp.message(F.text == "🔍 Найти авто")
async def cmd_search(msg: Message):
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой поиск: /start")
        return
    enabled = _get_enabled_sources(s)
    await msg.answer("Выбери площадки для поиска:", reply_markup=sources_keyboard(enabled))


@dp.message(F.text == "🆕 Новые сегодня")
async def cmd_new_today(msg: Message):
    """Поиск свежих объявлений за последние 24 часа, сортировка по дате."""
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой поиск: /start")
        return

    now_ts = time.time()
    last = _last_search_at.get(uid, 0)
    wait_left = SEARCH_COOLDOWN_SEC - (now_ts - last)
    if wait_left > 0:
        await msg.answer(f"⏳ Подожди {int(wait_left) + 1} сек перед новым поиском.")
        return
    _last_search_at[uid] = now_ts

    region = s["region"]
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    category = s.get("category", "all")
    brand = s.get("brand", "")
    region_name = REGIONS.get(region, region)

    await msg.answer(
        f"🆕 Ищу свежие объявления в {region_name} за последние 24 часа...\n"
        f"💰 Бюджет: {pmin:,}–{pmax:,} ₽".replace(",", " ")
    )

    loop = asyncio.get_running_loop()
    skipped = load_skipped(uid)
    seen = load_seen(uid)

    # Запускаем Авито с сортировкой по дате
    items_avito = await loop.run_in_executor(
        None, lambda: scrape_avito(region, pages=5, price_min=pmin, price_max=pmax, sort_by_date=True)
    )
    items = list(items_avito)

    seen_norm_today = {_norm_url(u) for u in load_seen(uid)}
    skipped_norm_today = {_norm_url(u) for u in skipped}
    _seen_u2: set[str] = set()
    deduped2: list[dict] = []
    for i in items:
        u = _norm_url(i.get("url", ""))
        if u and u not in _seen_u2:
            _seen_u2.add(u)
            i["url"] = u
            deduped2.append(i)
    items = deduped2

    # Фильтр: только за последние 24 часа (_days_on_site <= 1)
    fresh = [it for it in items if it.get("_days_on_site", 0) <= 1]

    suitable = [
        i for i in fresh
        if not is_dealer(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm_today
    ]
    suitable = _filter_by_category(suitable, category, brand)
    # Авито-эталон для "Сегодня": берём широкий срез без ценового фильтра
    _avito_ref_today = await loop.run_in_executor(
        None, lambda: scrape_avito(region, pages=8, price_min=0, price_max=99_000_000)
    )
    for _ar in _avito_ref_today:
        _ar["_market_ref_only"] = True
    suitable = rank_by_market_price(suitable, ref_items=_avito_ref_today, avito_only_median=True)
    suitable = _sort_by_deal(suitable)

    if not suitable:
        await msg.answer(
            f"😔 Свежих объявлений за последние 24 часа не нашлось.\n"
            f"Попробуй 🔍 Найти авто для более широкого поиска.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track("new_today", uid=uid, region=region, results=len(suitable))
    below = sum(1 for x in suitable if x.get("_savings_pct", 0) > 0)
    await msg.answer(
        f"✅ Найдено {len(suitable)} свежих объявлений!\n"
        f"🔥 Ниже рынка: {below} шт. — они первые"
    )
    await send_batch(msg.chat.id, uid, 0)


@dp.message(F.text == "🌐 Глобальный поиск")
async def cmd_global_search(msg: Message):
    """Глобальный поиск: все площадки + Telegram-каналы автопродаж города."""
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой поиск: /start")
        return

    # Лимит частоты
    now_ts = time.time()
    last = _last_search_at.get(uid, 0)
    wait_left = SEARCH_COOLDOWN_SEC - (now_ts - last)
    if wait_left > 0:
        await msg.answer(f"⏳ Подожди {int(wait_left) + 1} сек перед новым поиском.")
        return
    _last_search_at[uid] = now_ts

    region = s["region"]
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    region_name = REGIONS.get(region, region)

    await msg.answer(
        f"🌐 Глобальный поиск в {region_name} ({pmin:,}–{pmax:,} ₽)\n"
        f"Ищу на всех площадках + TG-каналы автопродаж...".replace(",", " ")
    )

    loop = asyncio.get_running_loop()
    skipped = load_skipped(uid)
    seen = load_seen(uid)

    # Запускаем все источники + TG-каналы параллельно
    scraper_map = {
        "drom":   lambda: scrape_drom(region, pages=15, price_min=pmin, price_max=pmax),
        "autoru": lambda: scrape_autoru(region, pages=12, price_min=pmin, price_max=pmax),
        "avito":  lambda: scrape_avito(region, pages=10, price_min=pmin, price_max=pmax, sort_by_date=False),
        "vk":     lambda: scrape_vk_groups(region, pmin, pmax),
    }
    tg_task = loop.run_in_executor(None, lambda: scrape_tg_channels(region, pmin, pmax))
    tasks = [loop.run_in_executor(None, fn) for fn in scraper_map.values()]
    all_results = await asyncio.gather(*tasks, tg_task, return_exceptions=True)

    items: list[dict] = []
    src_names = list(scraper_map.keys())
    stat_parts: list[str] = []
    for src, batch in zip(src_names, all_results[:-1]):
        if isinstance(batch, list):
            items.extend(batch)
        tag = SOURCE_TAGS.get(src, src)
        cnt = len(batch) if isinstance(batch, list) else 0
        stat_parts.append(f"{tag}: {cnt}")

    tg_batch = all_results[-1]
    if isinstance(tg_batch, list):
        items.extend(tg_batch)
        stat_parts.append(f"✈️ Telegram: {len(tg_batch)}")

    if stat_parts:
        await msg.answer("📊 " + " | ".join(stat_parts))

    _seen_g: set[str] = set()
    deduped_g: list[dict] = []
    for i in items:
        u = _norm_url(i.get("url", ""))
        if u and u not in _seen_g:
            _seen_g.add(u)
            i["url"] = u
            deduped_g.append(i)
    items = deduped_g

    seen_norm_g = {_norm_url(u) for u in seen}
    skipped_norm_g = {_norm_url(u) for u in skipped}

    # Парсим цену из текста там где не распарсилась
    for it in items:
        if not it.get("_price_int") and it.get("price"):
            p = parse_price(it["price"])
            if p and 10_000 < p < 99_000_000:
                it["_price_int"] = p

    suitable = [
        i for i in items
        if not is_dealer(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm_g
        and i["url"] not in seen_norm_g
    ]
    suitable = rank_by_market_price(suitable)
    suitable = _sort_by_deal(suitable)

    if not suitable:
        await msg.answer(
            f"😔 Не нашёл новых объявлений. Нажми ♻️ Сбросить историю и попробуй снова.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track(
        "global_search", uid=uid, region=region, price_min=pmin, price_max=pmax,
        results=len(suitable),
    )
    below = sum(1 for x in suitable if x.get("_savings_pct", 0) > 0)
    await msg.answer(f"✅ Найдено {len(suitable)} объявлений!\n🔥 Ниже рынка: {below} шт. — они первые")
    await send_batch(msg.chat.id, uid, 0)


@dp.message(F.text == "📢 VK + TG Барахолка")
async def cmd_vk_tg_search(msg: Message):
    """Поиск в пабликах ВКонтакте и Telegram-каналах автобарахолок города."""
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой поиск: /start")
        return

    now_ts = time.time()
    last = _last_search_at.get(uid, 0)
    wait_left = SEARCH_COOLDOWN_SEC - (now_ts - last)
    if wait_left > 0:
        await msg.answer(f"⏳ Подожди {int(wait_left) + 1} сек.")
        return
    _last_search_at[uid] = now_ts

    region = s["region"]
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    region_name = REGIONS.get(region, region)

    await msg.answer(
        f"📢 Ищу в VK пабликах и TG-каналах города {region_name}...\n"
        f"💰 Бюджет: {pmin:,} – {pmax:,} ₽".replace(",", " ")
    )

    loop = asyncio.get_running_loop()
    skipped = load_skipped(uid)
    seen = load_seen(uid)

    vk_task = loop.run_in_executor(None, lambda: scrape_vk_groups(region, pmin, pmax))
    tg_task = loop.run_in_executor(None, lambda: scrape_tg_channels(region, pmin, pmax))
    vk_result, tg_result = await asyncio.gather(vk_task, tg_task, return_exceptions=True)

    items: list[dict] = []
    stat_parts: list[str] = []
    vk_cnt = len(vk_result) if isinstance(vk_result, list) else 0
    tg_cnt = len(tg_result) if isinstance(tg_result, list) else 0
    analytics.track("vk_tg_raw", uid=uid, region=region, vk=vk_cnt, tg=tg_cnt)
    if isinstance(vk_result, list) and vk_result:
        items.extend(vk_result)
        stat_parts.append(f"📘 VK: {len(vk_result)}")
    if isinstance(tg_result, list) and tg_result:
        items.extend(tg_result)
        stat_parts.append(f"📢 TG: {len(tg_result)}")

    if stat_parts:
        await msg.answer("📊 " + " | ".join(stat_parts))

    _seen_v: set[str] = set()
    deduped_v: list[dict] = []
    for i in items:
        u = _norm_url(i.get("url", ""))
        if u and u not in _seen_v:
            _seen_v.add(u)
            i["url"] = u
            deduped_v.append(i)
    items = deduped_v

    skipped_norm_v = {_norm_url(u) for u in skipped}

    # Парсим цену из текста если не распарсилась
    for it in items:
        if not it.get("_price_int") and it.get("price"):
            p = parse_price(it["price"])
            if p and 10_000 < p < 99_000_000:
                it["_price_int"] = p

    suitable = [
        i for i in items
        if in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm_v
    ]
    suitable = rank_by_market_price(suitable)
    suitable = _sort_by_deal(suitable)

    if not suitable:
        await msg.answer(
            f"😔 Не нашёл объявлений в VK/TG пабликах {region_name}.\n\n"
            f"💡 Совет: VK паблики иногда закрытые — попробуй «🌐 Глобальный поиск»",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track("vk_tg_search", uid=uid, region=region, results=len(suitable))
    below = sum(1 for x in suitable if x.get("_savings_pct", 0) > 0)
    await msg.answer(
        f"✅ Найдено {len(suitable)} объявлений в VK+TG пабликах!\n"
        f"🔥 Ниже рынка: {below} шт. — они первые"
    )
    await send_batch(msg.chat.id, uid, 0)


@dp.callback_query(F.data.startswith("toggle_src|"))
async def cb_toggle_src(cb: CallbackQuery):
    src = cb.data.split("|")[1]
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = list(_get_enabled_sources(s))
    if src in enabled:
        if len(enabled) > 1:  # оставляем хотя бы одну
            enabled.remove(src)
    else:
        enabled.append(src)
    s["sources"] = enabled
    save_settings(uid, s)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=sources_keyboard(enabled))


@dp.callback_query(F.data == "src_all")
async def cb_src_all(cb: CallbackQuery):
    uid = cb.from_user.id
    s = load_settings(uid)
    s["sources"] = list(ALL_SOURCES)
    save_settings(uid, s)
    await cb.answer("Все площадки включены")
    await cb.message.edit_reply_markup(reply_markup=sources_keyboard(ALL_SOURCES))


@dp.callback_query(F.data == "open_settings")
async def cb_open_settings(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.answer("🔍 Шаг 1/4: Что ищем?", reply_markup=category_keyboard())
    await state.set_state(Setup.category)


@dp.callback_query(F.data == "do_global_search")
async def cb_do_global_search(cb: CallbackQuery):
    await cb.answer()
    await cmd_global_search(cb.message)


@dp.callback_query(F.data == "do_search")
async def cb_do_search(cb: CallbackQuery):
    uid = cb.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await cb.answer()
        await cb.message.answer("Сначала настрой поиск: /start")
        return
    await cb.answer()
    enabled = _get_enabled_sources(s)
    await cb.message.answer("Выбери площадки для поиска:", reply_markup=sources_keyboard(enabled))


@dp.callback_query(F.data == "start_search")
async def cb_start_search(cb: CallbackQuery):
    await cb.answer()
    await do_search_for_user(cb.from_user.id, cb.message)


# ── Загрузка деталей объявления ─────────────────────────────────

# Плейсхолдеры Дрома которые не являются фото машины
_DROM_PLACEHOLDER_URLS = ["drom.ru/img/app", "/placeholder", "mascot", "no-photo", "nophoto", "default"]

# Маркеры снятого объявления в HTML/JSON страницы
_SOLD_MARKERS = [
    "снят с продажи", "снято с продажи", "объявление снято",
    "объявление не найдено", "объявление недоступно", "объявление удалено",
    '"isSold":true', '"sold":true', '"status":"sold"', '"status":"inactive"',
    '"isArchived":true', 'bulletin-sold', 'data-bulletin-status="sold"',
    "listing not found", "offer not found",
]

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _fetch_and_check(url: str, source: str) -> dict | None:
    """
    Один запрос на страницу объявления:
    - проверяет активность (None = снято)
    - возвращает фото и описание
    """
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        # Referer важен: Auto.ru и Авито блокируют без него
        if source == "autoru":
            _referer = "https://auto.ru/"
        elif source == "drom":
            _referer = "https://auto.drom.ru/"
        else:
            _referer = "https://www.avito.ru/"
        _headers = {**_FETCH_HEADERS, "Referer": _referer}
        # Для Auto.ru пробуем curl_cffi (обходит блокировку)
        r = None
        if source == "autoru":
            try:
                from curl_cffi import requests as _cffi
                r = _cffi.get(url, impersonate="chrome124", timeout=12, headers=_headers)
            except Exception:
                pass
        if r is None:
            r = _req.get(url, headers=_headers, timeout=12, allow_redirects=True)
        if r.status_code in (404, 410):
            return None
        text = r.text

        # Проверяем маркеры снятого объявления
        text_lower = text.lower()
        if any(m.lower() in text_lower for m in _SOLD_MARKERS):
            return None

        soup = _BS(text, "lxml")

        # Фото: og:image
        photo_url = ""
        _PHOTO_REJECT = ("/stub", "noimage", "placeholder", "/ava/", "/avatar/",
                         "/userava", "/user_ava", "/logo", "/icon", "favicon",
                         "/brand", "/promo", "/static/", "default_image",
                         "opengraph-default", "avito-app", "avito_app",
                         "apple-touch", "banner", "fallback")

        def _ok_photo(u: str, src: str) -> bool:
            lo = u.lower()
            if any(x in lo for x in _PHOTO_REJECT):
                return False
            if src == "avito" and "img.avito.st" not in lo and "images.avito.st" not in lo:
                return False
            # Auto.ru фото всегда на avatars.mds.yandex.net — остальное (логотипы, иконки) отклоняем
            if src == "autoru" and "avatars.mds.yandex.net" not in lo:
                return False
            return True

        og = soup.select_one("meta[property='og:image']")
        if og:
            _cand = og.get("content", "").strip()
            if _cand and _ok_photo(_cand, source):
                photo_url = _cand
        # Отфильтровываем плейсхолдеры (логотип Дрома, хомяка и т.п.)
        if photo_url and any(p in photo_url for p in _DROM_PLACEHOLDER_URLS):
            photo_url = ""
        # Для Auto.ru: парсим фото из __INITIAL_STATE__ если og:image пустой
        description = ""  # инициализируем до всех проверок
        if not photo_url and source == "autoru":
            _am = re.search(r'"(?:1200x900|832x624|456x342)"\s*:\s*"((?:https?:)?//[^"]{15,})"', text)
            if _am:
                raw = _am.group(1).replace("\\/", "/")
                photo_url = ("https:" + raw) if raw.startswith("//") else raw
            if not photo_url:
                _am2 = re.search(r'"((?:https?:)?//avatars\.mds\.yandex\.net/[^"]{10,})"', text)
                if _am2:
                    raw = _am2.group(1).replace("\\/", "/")
                    photo_url = ("https:" + raw) if raw.startswith("//") else raw
            # Описание для Auto.ru из JSON
            if not description:
                _dm = re.search(r'"description"\s*:\s*"((?:\\.|[^"\\]){20,400})"', text)
                if _dm:
                    description = _dm.group(1).replace("\\n", " ").replace('\\"', '"').strip()
        # Если og:image не подошёл — берём первую img с CDN (только реальные CDN)
        if not photo_url:
            _cdn_kw_by_src = {
                "avito":  ["img.avito.st", "images.avito.st"],
                "autoru": ["avatars.mds.yandex", "s.auto.ru"],
                "drom":   ["drom.ru/photos", "dromcdn"],
            }
            _cdn_kw = _cdn_kw_by_src.get(source, ["img.avito.st", "images.avito.st",
                                                    "avatars.mds.yandex", "dromcdn"])
            for img in soup.select("img[src]"):
                src_attr = img.get("src", "")
                if src_attr.startswith("http") and any(x in src_attr for x in _cdn_kw):
                    if _ok_photo(src_attr, source):
                        photo_url = src_attr
                        break
        if photo_url and not photo_url.startswith("http"):
            photo_url = "https:" + photo_url if photo_url.startswith("//") else ""

        # Описание продавца
        if not description:
            if source == "drom":
                desc_el = (
                    soup.select_one("[data-ftid='bull_description']")
                    or soup.select_one("[data-ftid='item_description']")
                    or soup.select_one("div[class*='bull-item__description']")
                    or soup.select_one("div[class*='comment']")
                    or soup.select_one("div[class*='description']")
                )
                description = desc_el.get_text(strip=True)[:500] if desc_el else ""
                # Дром может рендерить описание в JSON внутри <script>
                if not description:
                    _djm = re.search(
                        r'"(?:description|comment|text)"\s*:\s*"((?:\\.|[^"\\]){20,500})"', text
                    )
                    if _djm:
                        description = _djm.group(1).replace("\\n", "\n").replace('\\"', '"').strip()[:500]
                # Парсим характеристики как описание (год, пробег, двигатель)
                if not description:
                    _chars: list[str] = []
                    for _li in soup.select("li[class*='param'], li[class*='char'], span[class*='value']"):
                        _t = _li.get_text(strip=True)
                        if _t and len(_t) < 60:
                            _chars.append(_t)
                    if _chars:
                        description = " · ".join(_chars[:6])
            elif source == "avito":
                desc_el = (
                    soup.select_one("div[itemprop='description']")
                    or soup.select_one("[data-marker='item-view/item-description']")
                    or soup.select_one("div[class*='description-text']")
                )
                description = desc_el.get_text(strip=True)[:500] if desc_el else ""
            else:
                desc_el = (
                    soup.select_one("div[class*='description']")
                    or soup.select_one("p[class*='description']")
                    or soup.select_one("[itemprop='description']")
                )
                description = desc_el.get_text(strip=True)[:500] if desc_el else ""

        return {"_photo_url": photo_url, "description": description}
    except Exception:
        return {}  # Ошибка сети — считаем активным, без деталей


async def enrich_and_filter(items: list[dict], max_check: int = 25) -> list[dict]:
    """
    Параллельно загружает страницы топ-N объявлений,
    фильтрует снятые и обогащает фото+описанием.
    """
    loop = asyncio.get_running_loop()
    to_check = items[:max_check]
    rest = items[max_check:]

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _fetch_and_check, i["url"], i.get("source", "")) for i in to_check]
    )
    active = []
    for item, details in zip(to_check, results):
        if details is None:
            continue  # снято
        item["_enriched"] = True
        if details.get("_photo_url"):
            item["_photo_url"] = details["_photo_url"]
        if details.get("description"):
            item["description"] = details["description"]
        active.append(item)

    return active + rest


async def _ensure_photo(item: dict) -> None:
    """Догружает недостающие данные (обычно описание) со страницы объявления.

    Основной парсер (_parse_avito_html через BeautifulSoup, как Дром) уже
    извлекает фото/описание/цену прямо из карточек поисковой выдачи, поэтому
    в большинстве случаев тут ничего грузить не нужно — выходим сразу.
    """
    # Описание считается реальным только если оно НЕ синтезировано из заголовка/
    # параметров (_desc_synthetic). Синтетику пытаемся заменить настоящим текстом
    # объявления, дозагрузив страницу.
    _has_real_desc = bool(item.get("description")) and not item.get("_desc_synthetic")

    # Всё уже собрано из карточки поиска — дополнительный запрос не нужен.
    if item.get("_photo_url") and _has_real_desc and item.get("_price_int"):
        return

    need_photo = not item.get("_photo_url")
    need_desc = not _has_real_desc
    need_price = not item.get("_price_int")
    if not need_photo and not need_desc and not need_price:
        return
    source = item.get("source", "")
    url = item.get("url", "")
    if not url:
        return

    loop = asyncio.get_running_loop()

    _HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Referer": "https://www.avito.ru/",
    }

    def _extract_from_page(text: str) -> tuple[str, str, int]:
        photo, desc, price_int = "", "", 0

        _OG_REJECT = ("logo", "stub", "noimage", "placeholder", "icon", "favicon", "/nophoto",
                      "apple-touch", "opengraph-default", "avito-app", "avito_app", "brand",
                      "promo", "banner", "fallback", "default_image")

        def _is_real_photo(url: str, src: str) -> bool:
            """Проверяет что URL — реальное фото (не логотип/заглушка)."""
            lo = url.lower()
            if any(x in lo for x in _OG_REJECT):
                return False
            if src == "avito":
                # Реальные фото объявлений только на img.avito.st или images.avito.st
                if "img.avito.st" not in lo and "images.avito.st" not in lo:
                    return False
            return True

        # 1. og:image — самый надёжный для страниц объявлений
        if need_photo:
            og = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
                r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                text
            )
            if og:
                candidate = (og.group(1) or og.group(2) or "").strip()
                if candidate and _is_real_photo(candidate, source):
                    photo = candidate
            # twitter:image как запасной вариант (на части моб. страниц нет og:image)
            if not photo:
                tw = re.search(
                    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']'
                    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
                    text
                )
                if tw:
                    candidate = (tw.group(1) or tw.group(2) or "").strip()
                    if candidate and _is_real_photo(candidate, source):
                        photo = candidate

        # 2. __NEXT_DATA__ JSON
        nd_m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S)
        nd_json = None
        if nd_m:
            try:
                nd_json = json.loads(nd_m.group(1))
            except Exception:
                pass

        if nd_json:
            _IMG_CDNS = ("avito.st", "avatars.mds.yandex", "auto.ru", "drom.ru", "dromcdn")
            def _find_img(obj, depth=0) -> str:
                if depth > 15 or obj is None:
                    return ""
                if isinstance(obj, str):
                    if any(cdn in obj for cdn in _IMG_CDNS) and len(obj) > 15 and (obj.startswith("//") or obj.startswith("http")):
                        raw = obj.replace("\\/", "/")
                        u = ("https:" + raw) if raw.startswith("//") else raw
                        if not any(x in u.lower() for x in ("/stub", "noimage", "logo", "placeholder", "/icon", "favicon")):
                            return u
                    return ""
                if isinstance(obj, list):
                    for el in obj:
                        r = _find_img(el, depth + 1)
                        if r:
                            return r
                    return ""
                if isinstance(obj, dict):
                    for size in ("1208x906", "864x648", "1280x960", "640x480", "432x324", "320x240"):
                        v = obj.get(size)
                        if isinstance(v, str) and "avito" in v:
                            raw = v.replace("\\/", "/")
                            u = ("https:" + raw) if raw.startswith("//") else raw
                            if not any(x in u.lower() for x in ("/stub", "noimage")):
                                return u
                    for k in ("images", "photos", "gallery", "media", "image", "photo",
                              "item", "initialData", "data", "props", "pageProps", "advert"):
                        if k in obj:
                            r = _find_img(obj[k], depth + 1)
                            if r:
                                return r
                    for v in obj.values():
                        if isinstance(v, str):
                            r = _find_img(v, depth + 1)
                            if r:
                                return r
                        elif isinstance(v, (dict, list)):
                            r = _find_img(v, depth + 1)
                            if r:
                                return r
                return ""

            if need_photo and not photo:
                photo = _find_img(nd_json)

            if need_desc and not desc:
                def _find_desc(obj, depth=0) -> str:
                    if depth > 10 or not isinstance(obj, (dict, list)):
                        return ""
                    if isinstance(obj, list):
                        for el in obj:
                            r = _find_desc(el, depth + 1)
                            if r:
                                return r
                        return ""
                    for k in ("description", "descriptionFull", "shortDescription"):
                        v = obj.get(k, "")
                        if isinstance(v, str) and len(v) > 30 and not v.startswith("http"):
                            return v[:400]
                    for k in ("item", "initialData", "data", "props", "pageProps", "advert"):
                        if k in obj:
                            r = _find_desc(obj[k], depth + 1)
                            if r:
                                return r
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            r = _find_desc(v, depth + 1)
                            if r:
                                return r
                    return ""
                desc = _find_desc(nd_json)

            if need_price and not price_int:
                raw_nd = nd_m.group(1) if nd_m else ""
                for pat in [
                    r'"valueText"\s*:\s*"([\d][\d\s.,]{1,18}(?:₽|руб|\\u20bd)?)"',
                    r'"priceDetailed"\s*:\s*\{[^}]{0,300}"value"\s*:\s*(\d{4,9})',
                    r'"price"\s*:\s*(\d{5,9})',
                ]:
                    pm = re.search(pat, raw_nd or text)
                    if pm:
                        d = re.sub(r"[^\d]", "", pm.group(1))
                        if d and 10_000 < int(d) < 99_000_000:
                            price_int = int(d)
                            break

        # 3. Для Auto.ru: разбор __INITIAL_STATE__ (Auto.ru не использует __NEXT_DATA__)
        if need_photo and not photo and source == "autoru":
            for _marker in ("window.__INITIAL_STATE__=", "window.__INITIAL_STATE__ ="):
                _idx = text.find(_marker)
                if _idx == -1:
                    continue
                _brace = text.find("{", _idx)
                if _brace == -1:
                    continue
                _end = text.find("</script>", _brace)
                _json_str = text[_brace:_end].rstrip("; \n\r") if _end != -1 else text[_brace:_brace + 800_000]
                try:
                    _st_data = json.loads(_json_str)
                    _offers = (
                        _deep_get(_st_data, "listing.data.offers")
                        or _deep_get(_st_data, "search.offers.offers")
                        or []
                    )
                    if _offers:
                        offer0 = _offers[0]
                        photos_list = offer0.get("photos", [])
                        if photos_list:
                            sizes = photos_list[0].get("sizes", {})
                            _p = sizes.get("1200x900") or sizes.get("832x624") or sizes.get("456x342") or ""
                            if _p:
                                photo = _p.replace("\\/", "/")
                                if photo.startswith("//"):
                                    photo = "https:" + photo
                        if need_desc and not desc:
                            desc = offer0.get("description", "")[:400]
                        break
                except Exception:
                    pass
            # Regex fallback для Auto.ru: avatars.mds.yandex.net CDN
            if not photo:
                _am = re.search(r'"(?:1200x900|832x624|456x342)"\s*:\s*"((?:https?:)?//[^"]{15,})"', text)
                if _am:
                    raw = _am.group(1).replace("\\/", "/")
                    photo = ("https:" + raw) if raw.startswith("//") else raw

        # 3b. Regex fallback — любой avito.st URL (широкий паттерн)
        if need_photo and not photo:
            m = re.search(r'((?:https?:)?//(?:[a-z0-9-]+\.)?(?:img|images)\.avito\.st/[^"\'<\s\\]{10,})', text)
            if m:
                raw = m.group(1).replace("\\/", "/")
                candidate = ("https:" + raw) if raw.startswith("//") else raw
                if not any(x in candidate.lower() for x in ("/stub", "noimage", "logo", "/icon", "favicon")):
                    photo = candidate

        # 4. Regex desc fallback
        if need_desc and not desc:
            for dpat in [
                # Версия, которая корректно обрабатывает экранированные кавычки (\")
                # и переводы строк (\n, \r, \t) внутри JSON-строки описания.
                r'"descriptionFull"\s*:\s*"((?:\\.|[^"\\]){30,})"',
                r'"description"\s*:\s*"((?:\\.|[^"\\]){30,})"',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,})["\']',
            ]:
                dm = re.search(dpat, text)
                if dm:
                    desc = (
                        dm.group(1)
                        .replace("\\n", " ")
                        .replace("\\r", " ")
                        .replace("\\t", " ")
                        .replace('\\"', '"')
                        .replace("\\/", "/")
                    )[:400]
                    break

        return photo, desc, price_int

    def _fetch() -> tuple[str, str, int]:
        photo, desc, price_int = "", "", 0
        try:
            import requests as _req
            if source == "avito":
                # Мобильный URL блокируется реже чем десктопный
                mobile_url = url.replace("https://www.avito.ru/", "https://m.avito.ru/")
                _MOB_HDR = {
                    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": "https://m.avito.ru/",
                }

                def _direct() -> tuple[str, str, int]:
                    # 1. curl_cffi — лучший TLS-fingerprint Chrome, обходит Railway-блок
                    try:
                        from curl_cffi import requests as _cffi
                        r = _cffi.get(url, impersonate="chrome124", timeout=15, headers={
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                            "Accept-Language": "ru-RU,ru;q=0.9",
                            "Referer": "https://www.avito.ru/",
                        }, proxies=_avito_proxies())
                        if r.status_code == 200 and len(r.text) > 5000:
                            res = _extract_from_page(r.text)
                            if res[0] or res[1]:
                                return res
                    except Exception:
                        pass
                    # 2. Пробуем мобильный URL
                    try:
                        r = _req.get(mobile_url, timeout=10, headers=_MOB_HDR, proxies=_avito_proxies())
                        if r.status_code == 200 and len(r.text) > 5000:
                            res = _extract_from_page(r.text)
                            if res[0] or res[1]:
                                return res
                    except Exception:
                        pass
                    # 3. Десктопный URL
                    try:
                        r = _req.get(url, timeout=10, headers=_HDR, proxies=_avito_proxies())
                        if r.status_code == 200 and len(r.text) > 5000:
                            res = _extract_from_page(r.text)
                            if res[0] or res[1]:
                                return res
                    except Exception:
                        pass
                    # 4. cloudscraper — обходит антибот-защиту (429/403)
                    try:
                        import cloudscraper
                        cs = cloudscraper.create_scraper(
                            browser={"browser": "chrome", "platform": "android", "mobile": True}
                        )
                        r = cs.get(mobile_url, timeout=12, proxies=_avito_proxies())
                        if r.status_code == 200 and len(r.text) > 3000:
                            res = _extract_from_page(r.text)
                            if res[0] or res[1]:
                                return res
                    except Exception:
                        pass
                    return "", "", 0

                # Playwright (_via_browser) убран — слишком медленный для 10+
                # параллельных запросов. Оставлен только быстрый _direct.
                photo, desc, price_int = _direct()
            elif source == "autoru":
                _autoru_hdr = dict(_HDR)
                _autoru_hdr["Referer"] = "https://auto.ru/"
                _autoru_hdr["Accept"] = "text/html,application/xhtml+xml,*/*;q=0.9"
                # Пробуем через прокси (РФ IP) — Auto.ru блокирует зарубежные серверы
                try:
                    from curl_cffi import requests as _cffi
                    r = _cffi.get(url, impersonate="chrome124", timeout=15,
                                  headers=_autoru_hdr, proxies=_avito_proxies())
                    if r.status_code == 200 and len(r.text) > 5000:
                        p, d, pi = _extract_from_page(r.text)
                        if p: photo = p
                        if d: desc = d
                        if pi: price_int = pi
                except Exception:
                    pass
                if not photo and not desc:
                    try:
                        r = _req.get(url, timeout=10, headers=_autoru_hdr,
                                     proxies=_avito_proxies())
                        if r.status_code == 200:
                            p, d, pi = _extract_from_page(r.text)
                            if p: photo = p
                            if d: desc = d
                            if pi: price_int = pi
                    except Exception:
                        pass
            elif source == "drom":
                _drom_hdr = {**_HDR, "Referer": "https://auto.drom.ru/"}
                _drom_text = ""
                # 1. cloudscraper — обходит антибот-защиту Дрома
                try:
                    import cloudscraper as _cs
                    _cs_sess = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
                    _r = _cs_sess.get(url, timeout=12, headers=_drom_hdr)
                    if _r.status_code == 200 and len(_r.text) > 3000:
                        _drom_text = _r.text
                except Exception:
                    pass
                # 2. curl_cffi fallback
                if not _drom_text:
                    try:
                        from curl_cffi import requests as _cffi
                        _r = _cffi.get(url, impersonate="chrome124", timeout=12, headers=_drom_hdr)
                        if _r.status_code == 200 and len(_r.text) > 3000:
                            _drom_text = _r.text
                    except Exception:
                        pass
                # 3. plain requests fallback
                if not _drom_text:
                    try:
                        _r = _req.get(url, timeout=10, headers=_drom_hdr)
                        if _r.status_code == 200:
                            _drom_text = _r.text
                    except Exception:
                        pass
                if _drom_text:
                    p, d, pi = _extract_from_page(_drom_text)
                    if not p:
                        # Дром-специфичный фолбэк: ищем URL фото на CDN
                        _dm = re.search(
                            r'https?://[^"\'<\s]*\.drom\.ru/[^"\'\s\\]{10,}\.(?:jpg|jpeg|webp|png)',
                            _drom_text
                        )
                        if _dm:
                            p = _dm.group(0).replace("\\/", "/")
                    if p: photo = p
                    if d: desc = d
                    if pi: price_int = pi
            elif source in ("vk", "tg", "tg_channel"):
                # VK и TG: загружаем страницу поста и берём og:image / первое фото
                _vk_hdr = {
                    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }
                _vk_url = url
                if source == "vk" and "vk.com/" in url and "m.vk.com" not in url:
                    _vk_url = url.replace("vk.com/", "m.vk.com/")
                try:
                    r = _req.get(_vk_url, timeout=8, headers=_vk_hdr)
                    if r.status_code == 200 and len(r.text) > 1000:
                        # og:image (не проверяем домен — любой CDN разрешён)
                        _og = re.search(
                            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
                            r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                            r.text
                        )
                        if _og:
                            _cand = (_og.group(1) or _og.group(2) or "").strip()
                            if _cand and "sticker" not in _cand and "emoji" not in _cand:
                                photo = _cand
                        # Первая картинка с userapi.com / вложения VK
                        if not photo:
                            for _im in re.finditer(r'https?://[^\s"\'<>]+(?:userapi\.com|vkuseravatar)[^\s"\'<>]*\.(?:jpg|jpeg|webp|png)', r.text):
                                _c = _im.group(0)
                                if "sticker" not in _c and "emoji" not in _c:
                                    photo = _c
                                    break
                        # Описание из og:description
                        if not desc:
                            _dsc = re.search(
                                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']'
                                r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']',
                                r.text
                            )
                            if _dsc:
                                desc = (_dsc.group(1) or _dsc.group(2) or "").strip()[:400]
                except Exception:
                    pass
        except Exception:
            pass
        return photo, desc, price_int

    photo, desc, price_int = await loop.run_in_executor(None, _fetch)
    if photo:
        item["_photo_url"] = photo
    # Перезаписываем синтетическое (собранное из заголовка) описание реальным
    # текстом объявления, если он получен со страницы.
    if desc and (not item.get("description") or item.get("_desc_synthetic")):
        item["description"] = desc[:400]
        item["_desc_synthetic"] = False
    if price_int and not item.get("_price_int"):
        item["_price_int"] = price_int
        item["price"] = f"{price_int:,} ₽".replace(",", " ")


SOURCE_TAGS = {
    "autoru":     "🟠 Auto.ru",
    "avito":      "🔴 Авито",
    "drom":       "🔵 Дром",
    "tg_channel": "📢 TG-канал",
    "tg":         "✈️ Telegram",
    "vk":         "📘 ВКонтакте",
}

# Кеш результатов поиска: uid -> list[dict]
_search_cache: dict[int, list[dict]] = {}

# PostgreSQL кеш поиска (переживает перезапуск Railway)
_DB_URL = os.getenv("DATABASE_URL", "")
_db_conn = None
_db_lock = __import__("threading").Lock()

def _get_db():
    global _db_conn
    if not _DB_URL:
        return None
    import psycopg2
    with _db_lock:
        try:
            if _db_conn is None or _db_conn.closed:
                _db_conn = psycopg2.connect(_DB_URL, connect_timeout=5)
                _db_conn.autocommit = True
                with _db_conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS search_cache (
                            uid BIGINT PRIMARY KEY,
                            items TEXT,
                            updated_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
        except Exception:
            return None
        return _db_conn

import atexit as _atexit
_atexit.register(lambda: _db_conn and _db_conn.close())


def _save_cache(uid: int, items: list[dict]):
    # Сохраняем в PostgreSQL (переживает рестарт)
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO search_cache(uid, items, updated_at) VALUES(%s,%s,NOW()) "
                    "ON CONFLICT(uid) DO UPDATE SET items=EXCLUDED.items, updated_at=NOW()",
                    (uid, json.dumps(items[:500], ensure_ascii=False, default=str))
                )
            return
    except Exception:
        pass
    # Fallback: файл
    try:
        f = user_dir(uid) / "last_search.json"
        f.write_text(json.dumps(items, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _load_cache(uid: int) -> list[dict]:
    # Сначала из PostgreSQL
    try:
        db = _get_db()
        if db:
            cur = db.cursor()
            cur.execute("SELECT items FROM search_cache WHERE uid=%s", (uid,))
            row = cur.fetchone()
            cur.close()
            if row:
                return json.loads(row[0])
    except Exception:
        pass
    # Fallback: файл
    try:
        f = user_dir(uid) / "last_search.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


async def send_batch(chat_id: int, uid: int, offset: int):
    """Отправляет 10 объявлений из кеша начиная с offset.
    Список уже отсортирован и отфильтрован — берём напрямую срез [offset:offset+10].
    """
    items = _search_cache.get(uid) or _load_cache(uid)
    if items:
        _search_cache[uid] = items  # восстанавливаем в память после перезапуска
    if not items:
        await bot.send_message(chat_id, "✅ Объявления закончились. Нажми /search для нового поиска.")
        return
    total = len(items)
    if offset >= total:
        await bot.send_message(
            chat_id,
            f"✅ Показаны все {total} объявлений. Нажми /search для нового поиска.",
        )
        return

    async def _send_item(item: dict):
        url = item.get("url", "")
        # Если нет описания — быстро догружаем со страницы
        if not item.get("description") and url:
            try:
                loop_s = asyncio.get_running_loop()
                details = await asyncio.wait_for(
                    loop_s.run_in_executor(None, _fetch_and_check, url, item.get("source", "")),
                    timeout=6
                )
                if details is None:
                    return  # продано — пропускаем
                if details.get("description"):
                    item["description"] = details["description"]
                if details.get("_photo_url") and not item.get("_photo_url"):
                    item["_photo_url"] = details["_photo_url"]
            except Exception:
                pass
        sid = url_to_id(url)
        days = item.get("_days_on_site", 0)
        _date_known = item.get("_date_known", False) or item.get("date", "") == str(datetime.date.today())
        if days == 0 and not _date_known:
            days_str = "🟢 недавно"
        elif days == 0:
            days_str = "🟢 сегодня"
        elif days == 1:
            days_str = "🟡 вчера"
        elif days <= 3:
            days_str = f"🟠 {days} дн. назад"
        else:
            days_str = f"⚪ {days} дн. назад"
        score = item.get("_hot_score", 0)
        hot_tag = " 🔥" if score >= 15 else " ⭐" if score >= 5 else ""
        source_tag = SOURCE_TAGS.get(item.get("source", ""), "🔵")

        _pi = item.get("_price_int", 0)
        price_line = (
            item.get("price") or
            (f"{_pi:,} ₽".replace(",", " ") if _pi else "—")
        )
        # Рыночную цену показываем на КАЖДОЙ машине, где она известна.
        deal_line = ""
        market = item.get("_market_price", 0)
        pct = item.get("_savings_pct", 0)
        if market and _pi:
            saving = market - _pi
            if pct > 0:
                # Дешевле рынка
                price_line += f"  🔻 рынок ~{market:,} ₽ (-{pct}%)".replace(",", " ")
                tier = "🟢 ВЫГОДНО" if pct >= 25 else "🟡 ниже рынка"
                deal_line = f"\n{tier}: дешевле рынка на ~{saving:,} ₽".replace(",", " ")
            elif pct < 0:
                # Дороже рынка
                price_line += f"  🔺 рынок ~{market:,} ₽ (+{abs(pct)}%)".replace(",", " ")
            else:
                # По рынку
                price_line += f"  ≈ рынок ~{market:,} ₽".replace(",", " ")

        mileage = item.get("mileage", 0)
        mileage_str = ""
        if mileage and mileage < 900_000:
            mileage_str = f"  ·  🛣 {mileage:,} км".replace(",", " ")

        dealer_tag = " 🏢" if item.get("_is_dealer") else ""
        caption = (
            f"{source_tag} {item.get('title', '')}{hot_tag}{dealer_tag}\n"
            f"💰 {price_line}{deal_line}\n"
            f"📅 {days_str}{mileage_str}"
        )
        if not item.get("description") and item.get("title"):
            item["description"] = _avito_desc_from_title(item["title"], item.get("mileage", 0))
        if item.get("description"):
            _desc = item["description"][:350].strip()
            if len(item["description"]) > 350:
                _desc += "…"
            caption += f"\n\n📝 {_desc}"

        source = item.get("source", "")
        seller = item.get("seller", "")
        if source == "vk":
            caption += f"\n👤 Продавец: {seller}" if seller else ""
            row1 = [
                InlineKeyboardButton(text="📘 Объявление ВК", url=url),
                InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
            ]
        elif source == "tg_channel":
            caption += f"\n📢 Канал: {seller}" if seller else ""
            seller_url = item.get("_seller_url", url)
            row1 = [
                InlineKeyboardButton(text="💬 Открыть в TG", url=seller_url),
                InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
            ]
        else:
            row1 = [
                InlineKeyboardButton(text="🔗 Открыть", url=url),
                InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
            ]
        row2 = [
            InlineKeyboardButton(text="❌ Скрыть", callback_data=f"hide|{sid}|{uid}"),
            InlineKeyboardButton(text="📋 Похожие", callback_data=f"sim|{sid}|{uid}"),
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[row1, row2])

        photo_url = item.get("_photo_url", "")
        if photo_url:
            try:
                import requests as _req
                from aiogram.types import BufferedInputFile
                loop = asyncio.get_running_loop()

                def _download_photo():
                    _item_source = item.get("source", "")
                    # Referer зависит от источника
                    if _item_source == "autoru":
                        _referer = "https://auto.ru/"
                    elif _item_source == "drom":
                        _referer = "https://auto.drom.ru/"
                    else:
                        _referer = "https://www.avito.ru/"
                    # 1. curl_cffi — обходит блокировку CDN с Railway IP
                    try:
                        from curl_cffi import requests as _cffi
                        r = _cffi.get(photo_url, impersonate="chrome124", timeout=8,
                                      headers={"Referer": _referer},
                                      proxies=_avito_proxies())
                        if r.status_code == 200 and len(r.content) > 3_000:
                            return r.content
                    except Exception:
                        pass
                    # 2. Обычный requests с Referer
                    try:
                        r2 = _req.get(photo_url, timeout=8, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "Referer": _referer,
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        }, proxies=_avito_proxies())
                        if r2.status_code == 200 and len(r2.content) > 3_000:
                            return r2.content
                    except Exception:
                        pass
                    return None

                content = await loop.run_in_executor(None, _download_photo)
                if content:
                    photo_bytes = BufferedInputFile(content, filename="photo.jpg")
                    await bot.send_photo(chat_id, photo=photo_bytes, caption=caption, reply_markup=kb)
                    return
            except Exception:
                pass
            # Fallback: передаём URL напрямую Telegram
            try:
                await bot.send_photo(chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                return
            except Exception:
                pass
        await bot.send_message(chat_id, caption, reply_markup=kb)

    # Отбираем кандидатов и дозагружаем фото/описание только для них (см. ниже).
    s = load_settings(uid)
    _pmin = s.get("price_min", 0)
    _pmax = s.get("price_max", 99_000_000)
    # Низкая параллельность + увеличенный таймаут: Авито агрессивно отдаёт 429
    # Увеличена параллельность: 6 одновременных запросов с 6-сек таймаутом
    # вместо 3×12 — итоговое время ожидания вдвое меньше.
    sem = asyncio.Semaphore(10)

    async def _prefetch(it):
        # Грузим если нет фото ИЛИ нет описания
        if it.get("_photo_url") and it.get("description"):
            return
        async with sem:
            try:
                # Дром требует больше времени (cloudscraper + 3 fallback)
                _t = 14 if it.get("source") == "drom" else 6
                await asyncio.wait_for(_ensure_photo(it), timeout=_t)
            except Exception:
                pass

    # Список уже отсортирован в do_search_for_user. Берём срез напрямую.
    batch = items[offset:offset + 10]

    # Дозагружаем фото только для тех у кого нет
    await asyncio.gather(*[_prefetch(it) for it in batch])

    for item in batch:
        await _send_item(item)
        await asyncio.sleep(0.01)

    next_offset = offset + len(batch)
    shown_str = f"{next_offset}/{total}"
    if next_offset < total:
        nav_row = [InlineKeyboardButton(text="➡️ Ещё", callback_data=f"page|{uid}|{next_offset}")]
        if offset > 0:
            prev_offset = max(0, offset - 10)
            nav_row.insert(0, InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page|{uid}|{prev_offset}"))
        await bot.send_message(
            chat_id,
            f"Показано {shown_str}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_row])
        )
    else:
        nav_row = []
        if offset > 0:
            prev_offset = max(0, offset - 10)
            nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page|{uid}|{prev_offset}"))
        await bot.send_message(
            chat_id,
            f"✅ Показаны все {total} объявлений.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_row]) if nav_row else MAIN_KEYBOARD,
        )

    # Сохраняем показанные в seen (нормализуем URL, кап 2000)
    seen = load_seen(uid)
    for item in batch:
        u = item.get("url", "")
        if u:
            seen.add(_norm_url(u))
    if len(seen) > 2000:
        seen = set(list(seen)[-1500:])
    save_seen(uid, seen)


async def do_search_for_user(uid: int, reply_to):
    if not await _is_subscribed(uid) and uid not in ADMIN_IDS:
        await reply_to.answer(
            "📢 Для поиска нужно подписаться на канал.",
            reply_markup=_subscribe_keyboard(),
        )
        return
    s = load_settings(uid)
    if not s.get("region"):
        await reply_to.answer("Сначала настрой поиск: /start")
        return

    # Лимит частоты: не чаще раза в SEARCH_COOLDOWN_SEC секунд на пользователя
    now_ts = time.time()
    last = _last_search_at.get(uid, 0)
    wait_left = SEARCH_COOLDOWN_SEC - (now_ts - last)
    if wait_left > 0:
        await reply_to.answer(f"⏳ Подожди {int(wait_left) + 1} сек перед новым поиском.")
        return
    _last_search_at[uid] = now_ts

    region = s["region"]
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    category = s.get("category", "all")
    brand = s.get("brand", "")
    region_name = REGIONS.get(region, region)
    enabled_sources = _get_enabled_sources(s)

    src_labels = " ".join(SOURCE_TAGS.get(src, src) for src in enabled_sources)
    await reply_to.answer(f"🔍 Ищу в {region_name} ({pmin:,}–{pmax:,} ₽)\n{src_labels}")

    skipped = load_skipped(uid)
    seen = load_seen(uid)
    loop = asyncio.get_running_loop()

    scraper_map = {
        "drom":   lambda: scrape_drom(region, pages=8, price_min=pmin, price_max=pmax),
        "autoru": lambda: scrape_autoru(region, pages=4, price_min=pmin, price_max=pmax),
        "avito":  lambda: scrape_avito(region, pages=6, price_min=pmin, price_max=pmax, sort_by_date=False, brand=(brand if brand and brand != "any" else "")),
        "vk":     lambda: scrape_vk_groups(region, pmin, pmax),
        "tg":     lambda: scrape_tg_channels(region, pmin, pmax),
    }
    # Авито-эталон ВСЕГДА запускаем параллельно — рыночная цена берётся с Авито
    # даже если пользователь ищет только ВК/TG/Дром
    _avito_ref_fut = loop.run_in_executor(
        None, lambda: scrape_avito(region, pages=8, price_min=0, price_max=99_000_000)
    )
    src_keys = [s for s in enabled_sources if s in scraper_map]
    futures = [loop.run_in_executor(None, scraper_map[src]) for src in src_keys]
    all_futs = futures + [_avito_ref_fut]
    done, pending = await asyncio.wait(all_futs, timeout=55)
    if pending:
        for f in pending:
            f.cancel()
        await reply_to.answer("⏱ Поиск занял слишком долго, показываю что успели найти...")
    results = []
    for f in futures:
        if f in done:
            try:
                results.append(f.result())
            except Exception as e:
                print(f"  [скрапер] ошибка: {e}")
                results.append([])
        else:
            results.append([])

    items = []
    stat_parts = []
    for src, batch in zip(src_keys, results):
        items.extend(batch)
        tag = SOURCE_TAGS.get(src, src)
        stat_parts.append(f"{tag}: {len(batch)}")

    if stat_parts:
        await reply_to.answer("📊 " + " | ".join(stat_parts))

    # Если Авито — единственный включённый источник и вернул 0 результатов,
    # автоматически добавляем Дром как запасной источник.
    avito_enabled = "avito" in enabled_sources and "avito" in scraper_map
    avito_count = 0
    if avito_enabled:
        for src, batch in zip([s for s in enabled_sources if s in scraper_map], results):
            if src == "avito":
                avito_count = len(batch)
                break
    if avito_count == 0 and avito_enabled and "drom" not in enabled_sources:
        # Проверяем: Авито реально не ответил, или ответил но нет машин в бюджете?
        # С прокси кэш лежит под ключом region_bucket, без прокси — под region.
        avito_raw_cached = _AVITO_REGION_CACHE.get(region)
        if not avito_raw_cached and AVITO_PROXIES:
            _bucket = _avito_price_bucket(pmin, pmax)
            avito_raw_cached = _AVITO_REGION_CACHE.get(f"{region}_{_bucket}")
        if not avito_raw_cached:
            # Берём любой свежий кэш этого региона (любой бюджет-слот).
            for _k, _v in _AVITO_REGION_CACHE.items():
                if _k == region or _k.startswith(region + "_"):
                    if not avito_raw_cached or len(_v[1]) > len(avito_raw_cached[1]):
                        avito_raw_cached = _v
        avito_raw_count = len(avito_raw_cached[1]) if avito_raw_cached else 0
        if avito_raw_count > 0:
            # Авито ответил — просто нет машин в этом бюджете.
            # Показываем то, что есть (за пределами бюджета), со снятым фильтром,
            # иначе пользователь думает что бот сломан.
            print(f"  [fallback] Авито ответил ({avito_raw_count} объявлений), но ни одно не в бюджете {pmin}–{pmax}₽")
            # Fix E: Apply year/budget filter even for fallback — never show 2025 luxury
            # cars in response to a 100k budget search.
            def _year_budget_ok_fallback(it: dict, _pmax: int) -> bool:
                y = it.get("_year") or it.get("year") or 0
                try:
                    y = int(str(y)[:4])
                except Exception:
                    y = 0
                if y >= 2023 and _pmax < 1_000_000:
                    return False
                if y >= 2021 and _pmax < 600_000:
                    return False
                if y >= 2019 and _pmax < 300_000:
                    return False
                if y >= 2016 and _pmax < 150_000:
                    return False
                return True
            raw_fallback = avito_raw_cached[1] if avito_raw_cached else []
            # Сначала пробуем показать Авито-объявления В БЮДЖЕТЕ (старые/дешёвые,
            # прошедшие year-фильтр). Это приоритет — пользователь выбрал Авито.
            in_budget_avito = [
                it for it in raw_fallback
                if _year_budget_ok_fallback(it, pmax)
                and ((not it.get("_price_int")) or (pmin <= it["_price_int"] <= pmax))
            ][:30]
            if in_budget_avito:
                items.extend(in_budget_avito)
                await reply_to.answer(
                    f"🔴 Авито: показываю {len(in_budget_avito)} подходящих объявлений."
                )

        # Гарантируем результат: если в бюджете ничего нет — добавляем Дром
        # (Дром работает с Railway IP, у него реальные цены, фото и описания).
        avito_now = sum(
            1 for i in items
            if i.get("source") == "avito" and not is_dealer(i)
            and in_price_range(i, pmin, pmax) and i.get("url")
            and i["url"] not in skipped
        )
        if avito_now == 0:
            print(f"  [fallback] в бюджете {pmin}-{pmax}₽ на Авито пусто — добавляем Дром")
            try:
                drom_fallback = await loop.run_in_executor(
                    None, lambda: scrape_drom(region, pages=10, price_min=pmin, price_max=pmax)
                )
                if drom_fallback:
                    items.extend(drom_fallback)
                    if avito_raw_count > 0:
                        await reply_to.answer(
                            f"🔴 Авито: в бюджете {pmin:,}–{pmax:,} ₽ подходящих машин нет.\n"
                            f"🔵 Показываю {len(drom_fallback)} объявлений с Дрома (цена, фото, описание).".replace(",", " ")
                        )
                    else:
                        await reply_to.answer(
                            f"🔵 Авито временно недоступен — показываю {len(drom_fallback)} объявлений с Дрома "
                            f"(цена, фото, описание)."
                        )
            except Exception as e:
                print(f"  [fallback] Дром ошибка: {e}")

    dealer_count = sum(1 for i in items if is_dealer(i))
    price_count = sum(1 for i in items if not is_dealer(i) and not in_price_range(i, pmin, pmax))
    print(f"  [поиск] всего={len(items)}, дилеров={dealer_count}, вне бюджета={price_count}")
    # Дедупликация: нормализуем URL (убираем ?params, m. поддомен) + по числовому ID внутри площадки
    seen_u: set[str] = set()
    seen_domain_ids: set[str] = set()
    deduped: list[dict] = []
    for i in items:
        u = i.get("url", "")
        if not u:
            continue
        u_norm = _norm_url(u)
        if u_norm in seen_u:
            continue
        _dm = _URL_DOMAIN_RE.match(u_norm)
        _domain = (_dm.group(1) if _dm else "").replace("m.vk.com", "vk.com")
        _id_m = _URL_NORM_RE.search(u_norm.split("?")[0])
        _num_id = _id_m.group(1) if _id_m else ""
        _domain_id_key = f"{_domain}:{_num_id}" if _num_id else ""
        if _domain_id_key and _domain_id_key in seen_domain_ids:
            continue
        seen_u.add(u_norm)
        i["url"] = u_norm  # нормализуем URL в объявлении
        if _domain_id_key:
            seen_domain_ids.add(_domain_id_key)
        deduped.append(i)
    items = deduped
    print(f"  [поиск] после дедупликации: {len(items)} из исходных")

    # Для объявлений без _price_int — парсим из текстового поля price
    for it in items:
        if not it.get("_price_int") and it.get("price"):
            p = parse_price(it["price"])
            if p and 10_000 < p < 99_000_000:
                it["_price_int"] = p

    # Для объявлений где цена всё ещё неизвестна — пробуем вытащить из __NEXT_DATA__
    # на странице объявления. ВНИМАНИЕ: Railway IP получает 403 от avito.ru напрямую,
    # бесплатные прокси тоже блокируются Авито. Поэтому эта попытка редко успешна,
    # но оставляем как резерв на случай если прокси всё же пустит.
    no_price = [i for i in items if not is_dealer(i) and not i.get("_price_int") and i.get("url") and i["url"] not in skipped]
    if no_price:
        loop2 = asyncio.get_running_loop()
        _price_re_np = re.compile(r'"price"\s*:\s*\{\s*"value"\s*:\s*(\d+)', re.I)
        _price_re_np2 = re.compile(r'"priceDetailed".*?"value"\s*:\s*(\d+)', re.I | re.S)

        def _fetch_price_sync(it: dict) -> None:
            try:
                import requests as _rq
                proxies_to_try = []
                # Платный ротирующийся прокси — в приоритете (разные IP, реальные цены)
                if AVITO_PROXIES:
                    for _ in range(3):
                        proxies_to_try.append(_avito_proxies())
                for _pa in list(_working_free_proxies)[:3]:
                    proxies_to_try.append({"http": f"http://{_pa}", "https": f"http://{_pa}"})
                proxies_to_try.append(None)
                _hdrs = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }
                for _prx in proxies_to_try:
                    try:
                        r = _rq.get(it["url"], headers=_hdrs, timeout=5, proxies=_prx)
                        if r.status_code != 200:
                            continue
                        text = r.text[:120_000]
                        for rx in (
                            _price_re_np2,
                            _price_re_np,
                            re.compile(r'["\']?price["\']?\s*[=:]\s*["\']?(\d{4,9})(?:\.0+)?["\']?', re.I),
                        ):
                            m = rx.search(text)
                            if m:
                                p = int(float(m.group(1)))
                                if 10_000 < p < 99_000_000:
                                    it["_price_int"] = p
                                    it["price"] = f"{p:,} ₽".replace(",", " ")
                                    break
                        if not it.get("_photo_url"):
                            pm = re.search(
                                r'((?:https?:)?(?:\\?/){2}[a-z0-9.\-]*avito\.st(?:(?:\\?/)[\w.~\-]+)+\.(?:jpg|jpeg|webp|png|avif))',
                                text, re.I,
                            )
                            if pm:
                                raw = pm.group(1).replace("\\/", "/")
                                it["_photo_url"] = ("https:" + raw) if raw.startswith("//") else raw
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        sem_price = asyncio.Semaphore(8)
        async def _fetch_price(it):
            async with sem_price:
                try:
                    await asyncio.wait_for(loop2.run_in_executor(None, _fetch_price_sync, it), timeout=7)
                except Exception:
                    pass
        await asyncio.gather(*[_fetch_price(it) for it in no_price[:5]])

    # Получаем результат Авито-эталона (уже запущен параллельно с основными скраперами)
    try:
        _avito_ref = _avito_ref_fut.result() if (_avito_ref_fut and _avito_ref_fut in done) else []
    except Exception as _e:
        print(f"  [рынок] Авито-эталон ошибка: {_e}")
        _avito_ref = []
    if _avito_ref:
        for _ar in _avito_ref:
            _ar["_market_ref_only"] = True
        items = items + _avito_ref
        print(f"  [рынок] Авито-эталон: {len(_avito_ref)} записей для медианы цен")
    else:
        # Авито заблокирован — используем основные Авито результаты как эталон
        _main_avito = [i for i in items if i.get("source") == "avito" and i.get("_price_int", 0)]
        if _main_avito:
            for _ar in _main_avito:
                _ar["_market_ref_only"] = True  # временно — используем для медианы
            print(f"  [рынок] Авито заблокирован, эталон из основных результатов: {len(_main_avito)} шт")

    # seen хранит нормализованные URL — сравниваем тоже по нормализованным
    seen_norm = {_norm_url(u) for u in seen}
    skipped_norm = {_norm_url(u) for u in skipped}
    already_seen_count = sum(
        1 for i in items
        if not is_dealer(i) and in_price_range(i, pmin, pmax)
        and i.get("url") and i["url"] in seen_norm
    )
    print(f"  [поиск] items={len(items)}, seen={len(seen_norm)}, skipped={len(skipped_norm)}, already_seen={already_seen_count}")
    _before = len(items)
    # Показываем ВСЕ объявления (новые + просмотренные), кроме скрытых.
    # Просмотренные помечаем _already_seen — они идут в конец списка.
    suitable = [
        i for i in items
        if not i.get("_market_ref_only")
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm
    ]
    print(f"  [фильтр] после in_price_range+skipped: {len(suitable)}/{_before} (бюджет {pmin}-{pmax})")
    _bad_price = [i for i in items if not i.get("_market_ref_only") and i.get("url") and i["url"] not in skipped and not in_price_range(i, pmin, pmax)]
    if _bad_price:
        _sample = [(i.get("title","")[:30], i.get("price",""), i.get("_price_int",0)) for i in _bad_price[:5]]
        print(f"  [фильтр] вне бюджета примеры: {_sample}")
    # Фильтр по категории и марке (также убирает скутеры/мото)
    suitable = _filter_by_category(suitable, category, brand)
    print(f"  [фильтр] после category({category}/{brand}): {len(suitable)}")

    # Финальная дедупликация suitable (могут быть дубли если разные источники нашли одно)
    _seen_final: set[str] = set()
    _deduped_suitable: list[dict] = []
    for _it in suitable:
        _u = _norm_url(_it.get("url", ""))
        if _u and _u not in _seen_final:
            _seen_final.add(_u)
            _deduped_suitable.append(_it)
    suitable = _deduped_suitable
    print(f"  [фильтр] после финальной дедупликации: {len(suitable)}")

    # Помечаем уже просмотренные — они получат штраф и уйдут в конец
    for it in suitable:
        if it.get("url") and _norm_url(it["url"]) in seen_norm:
            it["_already_seen"] = True

    _avito_ref_items = [i for i in items if i.get("_market_ref_only")]
    _use_avito_only = len(_avito_ref_items) >= 5
    if not _use_avito_only:
        # Avito недоступен — используем ВСЕ площадки как референс для медианы
        _avito_ref_items = [i for i in items if not i.get("_market_ref_only") and i.get("_price_int", 0) > 0]
        print(f"  [рынок] Авито-референс пуст, используем {len(_avito_ref_items)} объявлений со всех площадок")
    else:
        print(f"  [рынок] Авито-референс: {len(_avito_ref_items)} объявлений")
    suitable = rank_by_market_price(suitable, ref_items=_avito_ref_items, avito_only_median=_use_avito_only)
    # Дилерские объявления — добавляем штраф к deal_score
    for it in suitable:
        if is_dealer(it):
            it["_is_dealer"] = True
            it["_deal_score"] = it.get("_deal_score", 0) - 30
    suitable = _sort_by_deal(suitable)

    if not suitable:
        items_in_seen_count = sum(
            1 for i in items
            if not is_dealer(i) and in_price_range(i, pmin, pmax)
            and i.get("url") and i["url"] in seen
        )
        if items_in_seen_count > 0:
            await reply_to.answer(
                f"👀 Авито нашёл {items_in_seen_count} объявлений, но все уже показывались раньше. "
                f"Нажми 🔄 Сбросить историю чтобы увидеть снова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Сбросить историю", callback_data="reset_seen")],
                ])
            )
            return
        # Диагностика — почему 0 (только из источников текущего поиска, не из кэша/эталона)
        _current_items = [i for i in items if not i.get("_market_ref_only") and i.get("source") in set(src_keys)]
        price_range_items = [i for i in _current_items if not is_dealer(i) and in_price_range(i, pmin, pmax) and i.get("url")]
        price_filtered_c = len(_current_items) - len(price_range_items) - sum(1 for i in _current_items if is_dealer(i))
        # Посмотрим сколько прошло бы без фильтра категории/марки
        without_cat_filter = [i for i in price_range_items if i["url"] not in skipped_norm]
        with_cat_filter = _filter_by_category(list(without_cat_filter), category, brand)

        hint_parts = []
        if len(without_cat_filter) > 0 and len(with_cat_filter) == 0 and brand:
            # Есть машины в категории, но марки нет — показываем все по категории с примечанием
            cat_label = CATEGORY_LABELS.get(category, category)
            fallback_items = _filter_by_category(list(without_cat_filter), category, "")
            if fallback_items:
                fallback_items = rank_by_market_price(fallback_items, ref_items=[i for i in items if i.get("_market_ref_only")], avito_only_median=True)
                for it in fallback_items:
                    if is_dealer(it):
                        it["_is_dealer"] = True
                        it["_deal_score"] = it.get("_deal_score", 0) - 30
                fallback_items = _sort_by_deal(fallback_items)
                await reply_to.answer(
                    f"⚠️ {brand.capitalize()} не нашлось. Показываю все {cat_label} ({len(fallback_items)} шт.):"
                )
                _search_cache[uid] = fallback_items
                _save_cache(uid, fallback_items)
                await send_batch(reply_to.chat.id, uid, 0)
                return
            hint_parts.append(
                f"⚠️ Найдено {len(without_cat_filter)} объявлений, но все отфильтрованы по категории «{cat_label} · {brand.capitalize()}».\n"
                f"Попробуй изменить категорию в /settings или выбрать «🚗 Все автомобили»."
            )
        elif len(without_cat_filter) > 0 and len(with_cat_filter) == 0:
            cat_label = CATEGORY_LABELS.get(category, category)
            hint_parts.append(
                f"⚠️ Найдено {len(without_cat_filter)} объявлений, но все отфильтрованы по категории «{cat_label}».\n"
                f"Попробуй изменить категорию в /settings или выбрать «🚗 Все автомобили»."
            )
        elif price_filtered_c > 0:
            hint_parts.append(f"Найдено {price_filtered_c} объявлений вне бюджета. Попробуй расширить диапазон цен: /settings")
        else:
            hint_parts.append("Попробуй «🌐 Глобальный поиск» — ищет по всем площадкам, или «📢 VK + TG Барахолка».")

        hint = "\n\n" + "\n".join(hint_parts)
        await reply_to.answer(
            f"😔 Не нашёл новых объявлений в {region_name}.{hint}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="♻️ Сбросить историю и искать снова", callback_data="reset_and_search")],
                [InlineKeyboardButton(text="⚙️ Изменить настройки", callback_data="open_settings")],
                [InlineKeyboardButton(text="🌐 Глобальный поиск", callback_data="do_global_search")],
            ])
        )
        return

    # Проверяем первые 15 объявлений: убираем проданные, загружаем фото+описание
    check_batch = suitable[:15]
    rest_batch = suitable[15:]
    loop_pre = asyncio.get_running_loop()
    sem_pre = asyncio.Semaphore(8)

    async def _check_item(it: dict) -> dict | None:
        """Проверяет активность объявления и обогащает фото/описанием.
        VK и TG — не проверяем (требуют авторизацию), только обогащаем если есть описание.
        """
        source = it.get("source", "")
        # VK и TG нельзя проверить без авторизации — оставляем как есть
        if source in ("vk", "tg", "tg_channel"):
            return it
        async with sem_pre:
            try:
                details = await asyncio.wait_for(
                    loop_pre.run_in_executor(None, _fetch_and_check, it["url"], source),
                    timeout=8
                )
                if details is None:
                    return None  # снято с продажи (Дром/Авито/Авто.ру)
                it["_enriched"] = True
                # Фото обновляем только если у объявления его нет (не перезаписываем хорошее)
                if details.get("_photo_url") and not it.get("_photo_url"):
                    it["_photo_url"] = details["_photo_url"]
                if details.get("description") and not it.get("description"):
                    it["description"] = details["description"]
                return it
            except Exception:
                return it  # при ошибке сети — оставляем объявление

    try:
        checked = await asyncio.wait_for(
            asyncio.gather(*[_check_item(it) for it in check_batch]),
            timeout=25
        ) if check_batch else []
    except asyncio.TimeoutError:
        checked = check_batch  # при таймауте — не удаляем объявления

    active = [it for it in checked if it is not None]
    sold_count = len(check_batch) - len(active)
    if sold_count:
        print(f"  [фильтр] убрано {sold_count} проданных объявлений из первых {len(check_batch)}")
    suitable = active + rest_batch

    # После загрузки цен — выкидываем только те, у кого цена ИЗВЕСТНА и вышла за бюджет.
    # Объявления без цены (_price_int=0) — оставляем: пользователь откроет ссылку и проверит.
    # Это критично для объявлений из поисковых сниппетов — там цена в HTML не всегда есть.
    # Второй price-фильтр убран — первый in_price_range уже отфильтровал.
    # Дополнительно фильтровать не нужно, это только теряет объявления с неизвестной ценой.
    print(f"  [фильтр] suitable после всех фильтров: {len(suitable)}")
    if not suitable:
        await reply_to.answer("😔 Не нашёл объявлений в твоём бюджете. Попробуй расширить диапазон цен: /settings")
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track(
        "search", uid=uid, region=region, price_min=pmin, price_max=pmax,
        source=",".join(enabled_sources), results=len(suitable),
    )
    _any_below = sum(1 for i in suitable if i.get("_savings_pct", 0) > 0)
    _seen_cnt  = sum(1 for i in suitable if i.get("_already_seen"))

    if _any_below:
        _msg = (
            f"✅ Найдено {len(suitable)} объявлений!\n"
            f"🟢 {_any_below} ниже рынка — идут первыми."
        )
    else:
        _msg = f"✅ Найдено {len(suitable)} объявлений! Показываю от дешёвых к дорогим."

    if _seen_cnt:
        _msg += f"\n♻️ {_seen_cnt} уже видел — они в конце."

    await reply_to.answer(_msg)
    await send_batch(reply_to.chat.id, uid, 0)


@dp.callback_query(F.data.startswith("page|"))
async def cb_page(cb: CallbackQuery):
    _, uid_s, offset_s = cb.data.split("|")
    uid = cb.from_user.id  # берём из Telegram, не из payload — защита от IDOR
    offset = int(offset_s)
    await cb.answer()
    # Если кеш пустой (бот перезапустился) и offset > 0 — перезапускаем поиск
    items = _search_cache.get(uid) or _load_cache(uid)
    if not items and offset > 0:
        await bot.send_message(
            cb.message.chat.id,
            "🔄 Бот перезапустился, кеш очистился. Повторяю поиск...",
        )
        await do_search_for_user(uid, cb.message)
        return
    await send_batch(cb.message.chat.id, uid, offset)


@dp.callback_query(F.data.startswith("hide|"))
async def cb_hide(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = cb.from_user.id  # защита от IDOR
    url = id_to_url(sid)
    skipped = load_skipped(uid)
    skipped.add(url)
    save_skipped(uid, skipped)
    analytics.track("hide", uid=uid, username=cb.from_user.username)
    await cb.answer("Скрыто")
    await cb.message.delete()


@dp.callback_query(F.data.startswith("fav|"))
async def cb_fav(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = cb.from_user.id  # защита от IDOR
    # Сохраняем в избранное (файл favorites.json)
    fav_file = user_dir(uid) / "favorites.json"
    favs = json.loads(fav_file.read_text(encoding="utf-8")) if fav_file.exists() else []
    url = id_to_url(sid)
    items = _search_cache.get(uid) or _load_cache(uid)
    item = next((it for it in items if it.get("url") == url), None)
    if item and url not in [f.get("url") for f in favs]:
        favs.append(item)
        fav_file.write_text(json.dumps(favs, ensure_ascii=False, default=str), encoding="utf-8")
        analytics.track("favorite", uid=uid, username=cb.from_user.username)
        await cb.answer("⭐ Добавлено в избранное!")
    else:
        await cb.answer("Уже в избранном")


@dp.callback_query(F.data.startswith("sim|"))
async def cb_similar(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = cb.from_user.id  # защита от IDOR
    url = id_to_url(sid)
    items = _search_cache.get(uid) or _load_cache(uid)
    item = next((it for it in items if it.get("url") == url), None)
    if not item:
        await cb.answer("Объявление не найдено")
        return
    key = _car_group_key(item.get("title", ""))
    similar = [it for it in items if _car_group_key(it.get("title", "")) == key and it.get("url") != url]
    if not similar:
        await cb.answer("Похожих объявлений не найдено")
        return
    await cb.answer(f"Найдено похожих: {len(similar)}")
    lines = []
    for it in similar[:5]:
        lines.append(f"• {it.get('title','')} — {it.get('price','?')} [{it.get('source','')}]")
        lines.append(f"  {it.get('url','')}")
    await cb.message.answer("🔍 Похожие объявления:\n\n" + "\n".join(lines))


@dp.message(F.text == "🎯 Следить за маркой")
async def cmd_track_brand(msg: Message):
    uid = msg.from_user.id
    s = load_settings(uid)
    current = s.get("track_brand", "")
    current_label = f"Сейчас: *{current.capitalize()}*\n\n" if current else ""
    await msg.answer(
        f"🎯 Слежение за маркой\n\n{current_label}"
        f"Когда появится новое объявление выбранной марки — сразу пришлю уведомление.\n\n"
        f"Выбери марку:",
        parse_mode="Markdown",
        reply_markup=track_brands_keyboard(),
    )


@dp.callback_query(F.data.startswith("track|"))
async def cb_track_brand(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    brand_key = cb.data.split("|", 1)[1]
    s = load_settings(uid)
    if brand_key == "off":
        s.pop("track_brand", None)
        save_settings(uid, s)
        await cb.message.answer("❌ Слежение за маркой отключено.")
    else:
        s["track_brand"] = brand_key
        save_settings(uid, s)
        # Найдём красивое название
        all_brands = dict(FOREIGN_BRANDS_DISPLAY + DOMESTIC_BRANDS_DISPLAY)
        brand_name = next((n for n, k in FOREIGN_BRANDS_DISPLAY + DOMESTIC_BRANDS_DISPLAY if k == brand_key), brand_key.capitalize())
        await cb.message.answer(
            f"✅ Слежу за *{brand_name}*\n\n"
            f"Как только появится новое объявление — пришлю уведомление.",
            parse_mode="Markdown",
        )


@dp.message(Command("favorites"))
@dp.message(F.text == "⭐ Избранное")
@dp.message(F.text == "🚗 Мой гараж")
async def cmd_favorites(msg: Message):
    uid = msg.from_user.id
    fav_file = user_dir(uid) / "favorites.json"
    if not fav_file.exists():
        await msg.answer("🚗 Мой гараж пуст — сохраняй объявления кнопкой ⭐ Сохранить.")
        return
    favs = json.loads(fav_file.read_text(encoding="utf-8"))
    if not favs:
        await msg.answer("🚗 Мой гараж пуст — сохраняй объявления кнопкой ⭐ Сохранить.")
        return
    lines = []
    for it in favs[-20:]:
        lines.append(f"• {it.get('title','')} — {it.get('price','?')}\n  {it.get('url','')}")
    await msg.answer(f"🚗 Мой гараж ({len(favs)} авто):\n\n" + "\n\n".join(lines[-10:]))


@dp.message(Command("test_avito"))
async def cmd_test_avito(msg: Message):
    """Диагностика Авито — присылает что именно возвращает ScraperAPI."""
    import requests as _req
    uid = msg.from_user.id
    s = load_settings(uid)
    region = s.get("region", "chelyabinsk")
    slug = AVITO_SLUGS.get(region, region)
    url = f"https://www.avito.ru/{slug}/avtomobili"

    await msg.answer(f"🔬 Тестирую Авито для {REGIONS.get(region, region)}...\nURL: {url}")

    def _stat(r, name: str) -> str:
        t = r.text
        has_items = 'data-marker="item"' in t
        item_count = t.count('data-marker="item"')
        has_nd = '__NEXT_DATA__' in t
        # Реальная блокировка: капча-страница ("Подтвердите что вы не робот"),
        # ограничение доступа или 429. Просто наличие слова "captcha" в тексте
        # не является блокировкой — Авито встраивает капча-JS в каждую страницу.
        has_block = (
            r.status_code in (429, 403)
            or "Доступ ограничен" in t
            or "Подтвердите, что вы не робот" in t
            or (len(t) < 50_000 and r.status_code != 200)
        )
        links = len(re.findall(rf'href="/{re.escape(slug)}/[a-z0-9_/%-]+-\d{{4,}}"', t))
        parsed = len(_parse_avito_html(t, slug, datetime.date.today())) if has_items else 0
        return (
            f"📡 {name}:\n"
            f"HTTP {r.status_code} | {len(t):,} байт\n"
            f"data-marker: {'ДА ✅' if has_items else 'нет ❌'} ({item_count} шт)\n"
            f"__NEXT_DATA__: {'ДА ✅' if has_nd else 'нет ❌'}\n"
            f"ссылки на авто: {links} шт\n"
            f"распознано объявлений: {parsed} шт\n"
            f"блокировка: {'ДА ⛔' if has_block else 'нет ✅'}"
        )

    try:
        r_direct = _req.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }, timeout=15, proxies=_avito_proxies())
        await msg.answer(_stat(r_direct, "Прямой запрос"))
        # Пример распознанного объявления — видно, извлеклись ли цена и фото
        sample_items = _parse_avito_html(r_direct.text, slug, datetime.date.today())
        with_price = sum(1 for i in sample_items if i.get("_price_int"))
        with_photo = sum(1 for i in sample_items if i.get("_photo_url"))
        if sample_items:
            ex = sample_items[0]
            await msg.answer(
                f"📋 Пример (всего {len(sample_items)}):\n"
                f"с ценой: {with_price} | с фото: {with_photo}\n\n"
                f"🚗 {ex.get('title','')}\n"
                f"💰 {ex.get('_price_int',0):,} ₽\n".replace(",", " ") +
                f"🖼 фото: {'да' if ex.get('_photo_url') else 'нет'}\n"
                f"🔗 {ex.get('url','')}"
            )
    except Exception as e:
        await msg.answer(f"Прямой запрос ошибка: {str(e)[:200]}")

    try:
        await msg.answer("Пробую headless-браузер (Playwright)...")
        loop = asyncio.get_running_loop()
        html = await loop.run_in_executor(None, _avito_fetch_html, url)
        if html:
            class _FakeResp:
                status_code = 200
                text = html
            await msg.answer(_stat(_FakeResp(), "Headless-браузер"))
        else:
            await msg.answer("Headless-браузер: пустой ответ ❌")
    except Exception as e:
        await msg.answer(f"Headless-браузер ошибка: {str(e)[:200]}")

    await msg.answer("✅ Диагностика завершена. Пришли эти результаты разработчику.")

@dp.message(Command("reset"))
@dp.message(F.text == "♻️ Сбросить историю")
async def cmd_reset(msg: Message):
    uid = msg.from_user.id
    save_seen(uid, set())
    save_skipped(uid, set())
    if uid in _search_cache:
        del _search_cache[uid]
    await msg.answer(
        "♻️ История сброшена! Теперь нажми 🔍 *Найти авто* — покажу все доступные объявления заново.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.callback_query(F.data == "reset_seen")
async def cb_reset_seen(cb: CallbackQuery):
    uid = cb.from_user.id
    save_seen(uid, set())
    save_skipped(uid, set())
    if uid in _search_cache:
        del _search_cache[uid]
    await cb.answer("✅ История сброшена!")
    await cb.message.answer(
        "✅ История сброшена! Теперь запусти поиск заново.",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.callback_query(F.data == "reset_and_search")
async def cb_reset_and_search(cb: CallbackQuery):
    uid = cb.from_user.id
    save_seen(uid, set())
    save_skipped(uid, set())
    if uid in _search_cache:
        del _search_cache[uid]
    await cb.answer("♻️ История сброшена, ищу...")
    await do_search_for_user(uid, cb.message)


@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def cmd_help(msg: Message):
    await msg.answer(
        "🤖 *PerekupDrive — умный поиск авто ниже рынка*\n\n"
        "Бот автоматически ищет объявления от частных лиц на Авито, "
        "сравнивает цены с рынком и показывает только выгодные.\n\n"
        "📌 *Кнопки меню:*\n"
        "🔍 *Найти авто* — запустить поиск по твоим настройкам\n"
        "🔔 *Уведомления* — авто-мониторинг (бот сам пришлёт когда появится выгодное авто)\n"
        "⭐ *Избранное* — сохранённые объявления\n"
        "⚙️ *Настройки* — сменить город и бюджет\n"
        "♻️ *Сбросить историю* — показать все объявления заново (если ничего не находит)\n\n"
        "📌 *Что означают значки:*\n"
        "🔻 рынок ~X₽ (-Y%) — цена ниже рыночной на Y%\n"
        "🔥 — срочная продажа (торг, уступлю, срочно)\n"
        "⭐ — объявление давно висит, продавец мотивирован снизить цену\n\n"
        "📌 *Если ничего не нашлось:*\n"
        "— Нажми ♻️ *Сбросить историю* и ищи снова\n"
        "— Или расширь бюджет в ⚙️ *Настройках*",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def _monitor_loop(uid: int):
    """Фоновая задача одного пользователя — делегирует в глобальный монитор."""
    # Просто держим флаг, глобальный монитор сам опрашивает всех активных
    while True:
        await asyncio.sleep(3600)


# ── Push-уведомления — раз в 2-3 дня ─────────────────────────────
_PUSH_MESSAGES = [
    "🚗 Привет! На рынке б/у авто появились новые выгодные предложения — первым найди машину ниже рынка: /search",
    "💰 Пока ты отдыхал, рынок изменился. Новые объявления ниже рыночной цены уже ждут тебя: /search",
    "🔍 Свежие авто с пробегом — нашёл 10+ объявлений ниже рынка в твоём городе. Смотри: /search",
    "🎯 Выгодная сделка не ждёт! Каждый день продавцы занижают цену. Поищи прямо сейчас: /search",
    "⚡️ Новые авто ниже рынка появляются каждый день. Не пропусти выгодное предложение: /search",
    "🚘 Рынок авто живёт своей жизнью — сегодня могут появиться отличные варианты в твоём бюджете: /search",
]

_PUSH_INTERVAL_SEC = 2.5 * 24 * 3600  # ~2.5 дня между уведомлениями

async def _push_notification_loop():
    """Раз в 2-3 дня отправляет всем пользователям мотивирующее сообщение для возврата в бот."""
    import random as _rnd
    # Первый запуск — подождать сутки чтобы не слать сразу после перезапуска
    await asyncio.sleep(24 * 3600)
    while True:
        now = time.time()
        if USERS_DIR.exists():
            for user_path in USERS_DIR.iterdir():
                if not user_path.is_dir() or not user_path.name.isdigit():
                    continue
                uid = int(user_path.name)
                notif_file = user_path / "last_push_notif.txt"
                try:
                    if notif_file.exists():
                        last_sent = float(notif_file.read_text().strip())
                        if now - last_sent < _PUSH_INTERVAL_SEC:
                            continue
                    msg = _rnd.choice(_PUSH_MESSAGES)
                    await bot.send_message(uid, msg)
                    notif_file.write_text(str(now))
                    await asyncio.sleep(0.1)  # защита от flood
                except Exception:
                    pass
        # Следующий обход — через сутки (каждый день проверяем кому пора слать)
        await asyncio.sleep(24 * 3600)


# ── Глобальный монитор — один цикл на всех пользователей ─────────
GLOBAL_POLL_SEC = 120   # опрос каждые 2 минуты

async def _send_monitor_item(uid: int, it: dict):
    """Отправляет одно объявление пользователю из монитора."""
    url = it.get("url", "")
    sid = url_to_id(url)
    pct = it.get("_savings_pct", 0)
    market = it.get("_market_price", 0)
    price_line = it.get("price", "—") or "—"
    if market:
        price_line += f"  🔻 рынок ~{market:,} ₽ (-{pct}%)".replace(",", " ")
    src = it.get("source", "avito")
    src_icon = {"avito": "🟠 Авито", "drom": "🔵 Дром", "autoru": "🔴 Auto.ru", "vk": "💙 ВКонтакте", "tg": "✈️ Telegram"}.get(src, "📌")
    it_region = it.get("_monitor_region", "")
    region_label = f" · {REGIONS.get(it_region, it_region)}" if it_region else ""
    caption = (
        f"🔔 {it.get('title', '')}\n"
        f"💰 {price_line}\n"
        f"📌 {src_icon}{region_label}"
    )
    if it.get("description"):
        _desc = it["description"][:180].strip()
        if len(it["description"]) > 180:
            _desc += "…"
        caption += f"\n📝 {_desc}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔗 Открыть", url=url),
            InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
        ],
        [
            InlineKeyboardButton(text="❌ Скрыть", callback_data=f"hide|{sid}|{uid}"),
        ],
    ])
    photo_url = it.get("_photo_url", "")
    sent = False
    if photo_url:
        try:
            await bot.send_photo(uid, photo=photo_url, caption=caption, reply_markup=kb)
            sent = True
        except Exception:
            pass
        if not sent:
            try:
                import requests as _req
                from aiogram.types import BufferedInputFile
                resp = _req.get(photo_url, timeout=10, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.avito.ru/"})
                if resp.status_code == 200 and len(resp.content) > 2000:
                    await bot.send_photo(uid, photo=BufferedInputFile(resp.content, "photo.jpg"), caption=caption, reply_markup=kb)
                    sent = True
            except Exception:
                pass
    if not sent:
        await bot.send_message(uid, caption, reply_markup=kb)


async def _global_monitor_loop():
    """Единый глобальный цикл — раз в 2 минуты опрашивает все источники для активных пользователей."""
    print("  [глоб.монитор] запущен")
    loop = asyncio.get_running_loop()
    # VK/TG медленнее — опрашиваем раз в 10 минут (каждый 5-й тик по 2 минуты)
    _vk_tg_tick = 0
    # Кэш результатов по (регион, источник) чтобы не скрейпить дважды для разных пользователей
    _region_src_cache: dict[str, list[dict]] = {}
    while True:
        await asyncio.sleep(GLOBAL_POLL_SEC)
        _vk_tg_tick += 1
        do_vk_tg = (_vk_tg_tick % 5 == 0)  # раз в 10 минут
        _region_src_cache.clear()
        try:
            # Собираем всех пользователей с включённым мониторингом
            if not USERS_DIR.exists():
                continue
            active_users: list[dict] = []
            for user_path in USERS_DIR.iterdir():
                if not (user_path.is_dir() and user_path.name.isdigit()):
                    continue
                try:
                    sf = user_path / "settings.json"
                    if not sf.exists():
                        continue
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    if s.get("monitor_enabled") and s.get("region"):
                        active_users.append({"uid": int(user_path.name), **s})
                except Exception:
                    pass

            if not active_users:
                continue

            # Собираем все уникальные пары (регион, источник) нужные хоть одному пользователю
            needed: dict[str, set[str]] = {}  # region → set of sources
            for u in active_users:
                user_srcs = set(_monitor_sources(u))
                all_regions = [u["region"]] + list(u.get("monitor_regions", []))
                for reg in all_regions:
                    needed.setdefault(reg, set()).update(user_srcs)

            # Скрейпим только нужные (регион, источник) параллельно
            _src_scrapers = {
                "avito":  lambda r: scrape_avito(r, pages=3, sort_by_date=False),
                "drom":   lambda r: scrape_drom(r, pages=3, price_min=0, price_max=99_000_000),
                "autoru": lambda r: scrape_autoru(r, pages=3, price_min=0, price_max=99_000_000),
                "vk":     lambda r: scrape_vk_groups(r, 0, 99_000_000),
                "tg":     lambda r: scrape_tg_channels(r, 0, 99_000_000),
            }
            tasks_m = {}
            for reg, srcs in needed.items():
                for src in srcs:
                    if src not in _src_scrapers:
                        continue
                    if src in ("vk", "tg") and not do_vk_tg:
                        continue
                    key_rs = f"{reg}:{src}"
                    fn = _src_scrapers[src]
                    tasks_m[key_rs] = loop.run_in_executor(None, lambda r=reg, f=fn: f(r))

            if tasks_m:
                done_m, _ = await asyncio.wait(list(tasks_m.values()), timeout=70)
                for key_rs, fut in tasks_m.items():
                    if fut in done_m:
                        try:
                            res = fut.result()
                            _region_src_cache[key_rs] = res if isinstance(res, list) else []
                        except Exception:
                            _region_src_cache[key_rs] = []
                    else:
                        _region_src_cache[key_rs] = []

            # Для каждого пользователя собираем raw из его регионов и площадок
            for u in active_users:
                try:
                    uid = u["uid"]
                    pmin = u.get("price_min", 0)
                    pmax = u.get("price_max", 99_000_000)
                    min_pct = u.get("monitor_min_savings_pct", MONITOR_MIN_SAVINGS_PCT)
                    track_brand = u.get("track_brand", "")
                    user_srcs = set(_monitor_sources(u))
                    all_regions = [u["region"]] + list(u.get("monitor_regions", []))

                    raw = []
                    for reg in all_regions:
                        for src in user_srcs:
                            if src in ("vk", "tg") and not do_vk_tg:
                                continue
                            key_rs = f"{reg}:{src}"
                            items_rs = _region_src_cache.get(key_rs, [])
                            # Помечаем регион для уведомлений
                            for it in items_rs:
                                it["_monitor_region"] = reg
                            raw.extend(items_rs)

                    if not raw:
                        continue

                    seen = load_seen(uid)
                    skipped = load_skipped(uid)

                    new_items = [
                        it for it in raw
                        if it.get("url")
                        and it["url"] not in seen
                        and it["url"] not in skipped
                        and not is_dealer(it)
                        and in_price_range(it, pmin, pmax)
                    ]
                    if not new_items:
                        seen.update(it["url"] for it in raw if it.get("url"))
                        save_seen(uid, seen)
                        continue

                    # Считаем рыночную цену по ВСЕМУ каталогу (raw) — чем больше, тем точнее
                    cached = _search_cache.get(uid) or _load_cache(uid)
                    pool = rank_by_market_price(raw + cached + new_items)
                    new_urls = {x["url"] for x in new_items}

                    new_below = sorted(
                        [it for it in pool
                         if it.get("url") in new_urls
                         and it.get("_below_market")
                         and it.get("_savings_pct", 0) >= min_pct],
                        key=lambda x: -x.get("_savings_pct", 0)
                    )

                    # Уведомления по слежению за маркой (независимо от скидки)
                    if track_brand:
                        brand_new = [
                            it for it in new_items
                            if it.get("url") in new_urls
                            and _match_brand(it.get("title", ""), track_brand)
                        ]
                        if brand_new:
                            brand_label = next(
                                (n for n, k in FOREIGN_BRANDS_DISPLAY + DOMESTIC_BRANDS_DISPLAY if k == track_brand),
                                track_brand.capitalize()
                            )
                            for it in brand_new[:3]:
                                it_region = it.get("_monitor_region", u.get("region", ""))
                                region_name_tb = REGIONS.get(it_region, it_region)
                                url_tb = it.get("url", "")
                                sid_tb = url_to_id(url_tb)
                                pct_tb = it.get("_savings_pct", 0)
                                market_tb = it.get("_market_price", 0)
                                price_line_tb = it.get("price", "—") or "—"
                                if market_tb and pct_tb > 0:
                                    price_line_tb += f" ▼ рынок ~{market_tb:,} ₽ (-{pct_tb}%)".replace(",", " ")
                                days_tb = it.get("_days_on_site", 0)
                                days_label_tb = "только что" if days_tb == 0 else f"{days_tb} дн. назад"
                                src_tb = it.get("source", "")
                                src_icon_tb = {"avito": "🟠", "drom": "🔵", "autoru": "🔴", "vk": "💙", "tg": "✈️"}.get(src_tb, "📌")
                                caption_tb = (
                                    f"🔔 {src_icon_tb} Новая {brand_label} в {region_name_tb}!\n"
                                    f"🚗 {it.get('title', '')}\n"
                                    f"💰 {price_line_tb}\n"
                                    f"🕐 Появилось {days_label_tb}"
                                )
                                kb_tb = InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="🔗 Открыть", url=url_tb),
                                    InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid_tb}|{uid}"),
                                ]])
                                try:
                                    photo_tb = it.get("_photo_url", "")
                                    if photo_tb:
                                        await bot.send_photo(uid, photo=photo_tb, caption=caption_tb, reply_markup=kb_tb)
                                    else:
                                        await bot.send_message(uid, caption_tb, reply_markup=kb_tb)
                                except Exception:
                                    try:
                                        await bot.send_message(uid, caption_tb, reply_markup=kb_tb)
                                    except Exception:
                                        pass
                                await asyncio.sleep(0.3)

                    if not new_below:
                        seen.update(it["url"] for it in new_items)
                        save_seen(uid, seen)
                        continue

                    # Если задан track_brand — фильтруем уведомления о скидках по марке
                    if track_brand:
                        new_below = [it for it in new_below if _match_brand(it.get("title", ""), track_brand)]

                    print(f"  [монитор] uid={uid}: {len(new_below)} новых выгодных")

                    if new_below:
                        # Группируем по регионам для шапки
                        regs_in_batch = list(dict.fromkeys(
                            REGIONS.get(it.get("_monitor_region", u.get("region", "")), it.get("_monitor_region", ""))
                            for it in new_below
                        ))
                        regs_label = ", ".join(regs_in_batch[:3])
                        srcs_in_batch = list(dict.fromkeys(it.get("source", "") for it in new_below))
                        src_icon_map = {"avito": "🟠", "drom": "🔵", "autoru": "🔴", "vk": "💙", "tg": "✈️"}
                        srcs_label = " ".join(src_icon_map.get(s, "") for s in srcs_in_batch if s)
                        await bot.send_message(
                            uid,
                            f"🔔 {srcs_label} *{regs_label}* — {len(new_below)} новых авто ниже рынка!",
                            parse_mode="Markdown",
                        )
                        for it in new_below[:5]:
                            await _send_monitor_item(uid, it)
                            await asyncio.sleep(0.3)

                    seen.update(it["url"] for it in new_items)
                    save_seen(uid, seen)

                except Exception as e:
                    print(f"  [глоб.монитор] uid обработка: {e}")

        except Exception as e:
            print(f"  [глоб.монитор] ошибка цикла: {e}")


def _start_monitor(uid: int):
    # Глобальный монитор уже запущен в main(), здесь просто сохраняем задачу-заглушку
    if uid not in _monitor_tasks or _monitor_tasks[uid].done():
        task = asyncio.get_running_loop().create_task(_monitor_loop(uid))
        _monitor_tasks[uid] = task


def _stop_monitor(uid: int):
    task = _monitor_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()


@dp.message(Command("monitor"))
@dp.message(F.text == "🔔 Уведомления")
async def cmd_monitor(msg: Message):
    """Открывает меню настроек уведомлений."""
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой регион и бюджет: /start")
        return

    enabled = s.get("monitor_enabled", False)
    status = "✅ Включён" if enabled else "❌ Выключен"
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    active_src = _monitor_sources(s)
    src_names = ", ".join(n for k, n in _MONITOR_SOURCES if k in active_src)
    extra_regions = s.get("monitor_regions", [])
    reg_names = ", ".join(REGIONS.get(r, r) for r in extra_regions) if extra_regions else "нет"
    own_region = REGIONS.get(s.get("region", ""), s.get("region", ""))
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    await msg.answer(
        f"🔔 *Настройки уведомлений*\n\n"
        f"Статус: {status}\n"
        f"Интервал проверки: каждые {interval} мин\n"
        f"Минимальная скидка: {min_pct}% ниже рынка\n"
        f"Площадки: {src_names}\n"
        f"Регион: {own_region} · Бюджет: {pmin:,}–{pmax:,} ₽".replace(",", " ")
        + (f"\nДоп. регионы: {reg_names}" if extra_regions else "") +
        f"\n\nПри появлении выгодного авто — сразу пришлю с фото, ценой и скидкой от рынка.",
        parse_mode="Markdown",
        reply_markup=_notify_keyboard(s),
    )


BOT_USERNAME = os.getenv("BOT_USERNAME", "")


@dp.message(Command("invite"))
@dp.message(F.text == "🤝 Пригласить друга")
async def cmd_invite(msg: Message):
    uid = msg.from_user.id
    entry = _get_or_create_referral(uid)
    data = _load_referrals()
    entry = data.get(str(uid), {})
    invited_count = len(entry.get("invited", []))
    bonus_days = entry.get("bonus_days", 0)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    share_text = "Нашёл бота который ищет авто ниже рынка на Авито, Дроме, Авто.ру, ВК и Telegram — попробуй!"
    await msg.answer(
        f"📲 *Пригласи друга в PerekupDrive*\n\n"
        f"Сейчас идёт тестовый период — бот полностью бесплатен для всех.\n"
        f"Поделись ссылкой — друг сразу получит доступ к боту.\n\n"
        f"👥 Приглашено: *{invited_count}* друзей\n\n"
        f"🔗 *Твоя ссылка:*\n{ref_link}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={ref_link}&text={share_text}")],
        ])
    )


async def _warmup_cache():
    """Непрерывный фоновый прогрев кэша Авито по всем городам.

    Главная идея для 50-100 пользователей без вложений: данные Авито кэшируются
    по региону на сутки, поэтому НЕЗАВИСИМО от числа пользователей нам нужен
    лишь ОДИН успешный скрейп региона в сутки. Этот цикл постоянно обновляет
    самый «старый» регион, размазывая нагрузку во времени. В итоге любой
    пользователь почти всегда попадает в уже готовый свежий кэш и получает
    объявления мгновенно, а поисковики не упираются в лимиты.

    Скрейп идёт через поисковики (DuckDuckGo/Brave/ddglite) + бесплатные прокси,
    поэтому прямого обращения к avito.ru с заблокированного IP нет и риска 429 нет.
    """
    await asyncio.sleep(20)  # дождаться старта бота и первого прогрева прокси
    loop = asyncio.get_running_loop()
    print("  [прогрев] непрерывный прогрев кэша запущен")
    while True:
        try:
            now = time.time()
            # Выбираем регион с самым старым (или отсутствующим) кэшем
            oldest_region = None
            oldest_age = -1.0
            for region in REGIONS.keys():
                cached = _AVITO_REGION_CACHE.get(region)
                age = (now - cached[0]) if cached else 10 ** 9
                if age > oldest_age:
                    oldest_age = age
                    oldest_region = region
            if oldest_region is None:
                await asyncio.sleep(60)
                continue
            # Если даже самый старый кэш ещё свежий (< 6ч) — ждём, не долбим зря
            if oldest_age < 6 * 3600:
                await asyncio.sleep(300)
                continue
            print(f"  [прогрев] обновляю {oldest_region} (возраст кэша {int(oldest_age)//60} мин)…")
            items = await loop.run_in_executor(
                None,
                lambda r=oldest_region: scrape_avito(r, pages=2, sort_by_date=False)
            )
            print(f"  [прогрев] {oldest_region}: {len(items)} объявлений в кэше")
        except Exception as e:
            print(f"  [прогрев] ошибка: {e}")
        # Пауза между регионами — размазываем нагрузку на поисковики
        await asyncio.sleep(600)  # 10 минут между городами


def _fetch_all_free_proxies() -> list[str]:
    """Собирает бесплатные прокси из нескольких источников."""
    import requests as _rq
    found: list[str] = []
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=RU&ssl=yes&anonymity=all",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=RU,UA,BY&ssl=yes&anonymity=elite",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=5000&country=RU",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    ]
    for src in sources:
        try:
            r = _rq.get(src, timeout=6)
            if r.status_code == 200:
                for ln in r.text.splitlines():
                    addr = ln.strip()
                    if addr and ":" in addr and not addr.startswith("#"):
                        found.append(addr)
        except Exception:
            pass
    try:
        r2 = _rq.get(
            "https://proxylist.geonode.com/api/proxy-list?limit=50&country=RU&protocols=https,http&sort_by=lastChecked&sort_type=desc",
            timeout=6,
        )
        if r2.status_code == 200:
            for item in r2.json().get("data", []):
                ip = item.get("ip", ""); port = item.get("port", "")
                if ip and port:
                    found.append(f"{ip}:{port}")
    except Exception:
        pass
    seen: set[str] = set()
    deduped = []
    for p in found:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _pre_warm_free_proxies_sync() -> None:
    """Тестирует бесплатные прокси против Авито и кеширует рабочие. Блокирующая функция."""
    global _free_proxy_cache, _free_proxy_cache_time, _working_free_proxies, _working_free_proxies_time
    import requests as _rq
    from concurrent.futures import ThreadPoolExecutor as _TPEw, as_completed as _acw

    print("  [прокси-прогрев] получаем список прокси...")
    all_proxies = _fetch_all_free_proxies()
    if not all_proxies:
        print("  [прокси-прогрев] ❌ не удалось получить ни одного прокси")
        return
    random.shuffle(all_proxies)
    candidates = all_proxies[:80]  # тестируем до 80 штук
    print(f"  [прокси-прогрев] тестируем {len(candidates)} прокси против Авито...")

    test_url = "https://www.avito.ru/moskva/avtomobili"
    test_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    def _test_one(addr: str) -> tuple[bool, str]:
        proxies = {"http": f"http://{addr}", "https": f"http://{addr}"}
        try:
            r = _rq.get(test_url, headers=test_headers, proxies=proxies, timeout=9)
            ok = r.status_code == 200 and ('"urlPath"' in r.text or '__NEXT_DATA__' in r.text or 'data-marker="item"' in r.text)
            return ok, addr
        except Exception:
            return False, addr

    working: list[str] = []
    with _TPEw(max_workers=30) as ex:
        futs = [ex.submit(_test_one, a) for a in candidates]
        try:
            for fut in _acw(futs, timeout=20):
                try:
                    ok, addr = fut.result()
                    if ok:
                        working.append(addr)
                        print(f"  [прокси-прогрев] ✅ {addr}")
                        if len(working) >= 10:
                            break
                except Exception:
                    pass
        except Exception:
            pass

    _free_proxy_cache = all_proxies
    _free_proxy_cache_time = time.time()
    _working_free_proxies = working
    _working_free_proxies_time = time.time()
    print(f"  [прокси-прогрев] найдено {len(working)} рабочих прокси из {len(candidates)} проверенных")


async def _proxy_warmup_loop() -> None:
    """Фоновая задача: прогревает кеш бесплатных прокси каждые 15 минут.
    Если настроен платный ротирующийся прокси — бесплатные не нужны, пропускаем."""
    if AVITO_PROXIES:
        print("  [прокси-прогрев] платный прокси активен — бесплатные не нужны, прогрев отключён")
        return
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, _pre_warm_free_proxies_sync)
        except Exception as e:
            print(f"  [прокси-прогрев] ошибка: {e}")
        await asyncio.sleep(900)  # 15 минут


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.WARNING)
    _load_avito_cache()
    # Подтягиваем username бота автоматически
    if not BOT_USERNAME:
        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username or "PerekupDriveBot"
            print(f"  [бот] username: @{BOT_USERNAME}")
        except Exception:
            BOT_USERNAME = "PerekupDriveBot"
    # Глобальный middleware проверки подписки — блокирует все апдейты
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    print("✅ Авто-брокер бот запущен!")
    print("  [ВЕРСИЯ] 2026-06-22-v18 :: subscription middleware")

    # Логируем Railway IP (нужен для добавления в whitelist прокси)
    try:
        import requests as _rq
        railway_ip = _rq.get("https://api.ipify.org", timeout=5).text.strip()
        print(f"  [Railway IP] {railway_ip}  ← добавь этот IP в whitelist прокси!")
    except Exception:
        pass

    # Тест прокси + тест доступа к Авито через прокси
    if AVITO_PROXY_HOST:
        try:
            import requests as _rq
            r = _rq.get("https://api.ipify.org", proxies=_avito_proxies(), timeout=10)
            print(f"  [прокси {AVITO_PROXY_PROTOCOL}] ✅ работает, IP: {r.text.strip()}")
            # Сразу проверяем доступ к Авито
            try:
                ra = _rq.get("https://www.avito.ru/krasnoyarsk/avtomobili",
                             proxies=_avito_proxies(), timeout=10,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
                has_listings = '"urlPath"' in ra.text or 'data-marker="item"' in ra.text
                print(f"  [Авито тест] HTTP {ra.status_code}, {len(ra.text):,}б, объявления: {'✅ да' if has_listings else '❌ нет (капча/блок)'}")
            except Exception as ea:
                print(f"  [Авито тест] ❌ {ea}")
            # Тест поисковиков через прокси — рабочий путь к Авито в обход блокировки.
            # Пробуем все три и смотрим, кто реально отдаёт ссылки на объявления.
            import urllib.parse as _up
            _ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
            _engines = [
                ("bing", "https://www.bing.com/search", {"q": "site:avito.ru/moskva/avtomobili продам", "cc": "RU"}),
                ("duckduckgo", "https://html.duckduckgo.com/html/", {"q": "site:avito.ru/moskva/avtomobili продам", "kl": "ru-ru"}),
                ("yandex", "https://yandex.ru/search/", {"text": "site:avito.ru/moskva/avtomobili продам", "lr": "225"}),
            ]
            for _eng, _url, _params in _engines:
                try:
                    rt = _rq.get(_url, params=_params, proxies=_avito_proxies(), timeout=12, headers=_ua)
                    _dec = rt.text
                    for _ in range(2):
                        _dec = _up.unquote(_dec)
                    n_avito = _dec.lower().count("avito")
                    n_urls = len(set(re.findall(r'avito\.ru/[a-z0-9_.-]+/avtomobili/[a-z0-9_.%-]*\d{6,}', _dec, re.I)))
                    print(f"  [{_eng} тест] HTTP {rt.status_code}, размер: {len(rt.text):,}б, 'avito': {n_avito}, объявлений: {n_urls}")
                    if n_avito > 0 and n_urls == 0:
                        idx = _dec.lower().find("avito.ru/")
                        if idx >= 0:
                            print(f"  [{_eng} тест] образец: {_dec[idx:idx+110].replace(chr(10), ' ')}")
                except Exception as ey:
                    print(f"  [{_eng} тест] ❌ {str(ey)[:80]}")
        except Exception as e:
            print(f"  [прокси {AVITO_PROXY_PROTOCOL}] ❌ ошибка: {e}")
            print(f"  [прокси] Добавь Railway IP в whitelist на сайте провайдера прокси!")

    loop = asyncio.get_running_loop()
    # Единый глобальный монитор — опрашивает всех активных пользователей каждые 2 минуты
    loop.create_task(_global_monitor_loop())
    print(f"  [монитор] глобальный цикл запущен (интервал {GLOBAL_POLL_SEC}с)")
    # Push-уведомления — раз в 2-3 дня всем пользователям
    loop.create_task(_push_notification_loop())
    print("  [push] цикл уведомлений запущен (интервал ~2.5 дня)")
    # Прогрев кеша бесплатных прокси — тестирует их против Авито и кеширует рабочие
    loop.create_task(_proxy_warmup_loop())
    print("  [прокси-прогрев] запущен фоновый прогрев кеша прокси")

    # Веб-дашборд аналитики — работает параллельно, не блокирует polling
    await analytics.start_dashboard(REGIONS)

    # Непрерывный фоновый прогрев кэша Авито: данные берутся через поисковики
    # (не прямой запрос к avito.ru), поэтому риска IP-блокировки нет. Благодаря
    # суточному кэшу один скрейп региона обслуживает всех пользователей — так
    # бот тянет 50-100 человек без вложений.
    loop.create_task(_warmup_cache())
    print("  [прогрев] фоновый прогрев кэша Авито запущен")

    public_commands = [
        BotCommand(command="start",     description="🚀 Главное меню"),
        BotCommand(command="search",    description="🔍 Найти авто"),
        BotCommand(command="new",       description="🆕 Новые сегодня"),
        BotCommand(command="favorites", description="🚗 Мой гараж"),
        BotCommand(command="invite",    description="🤝 Пригласить друга"),
        BotCommand(command="settings",  description="⚙️ Настройки"),
        BotCommand(command="help",      description="❓ Помощь"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats",     description="📊 Статистика"),
        BotCommand(command="dashboard", description="📈 Дашборд аналитики"),
    ]
    # Обычным пользователям — только публичные команды
    await bot.set_my_commands(public_commands)
    # Администраторам — расширенный список (виден только им)
    from aiogram.types import BotCommandScopeChat
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
