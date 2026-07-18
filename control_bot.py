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
import html
import threading
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
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/durunegonim").strip()


def _support_url() -> str:
    """Рабочая ссылка на поддержку с резервом на Telegram администратора."""
    if SUPPORT_URL:
        return SUPPORT_URL
    if ADMIN_IDS:
        return f"tg://user?id={next(iter(ADMIN_IDS))}"
    return ""


def _deploy_revision() -> str:
    """Возвращает ревизию, которую реально запустил Railway/контейнер."""
    for key in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT", "COMMIT_SHA"):
        value = os.getenv(key, "").strip()
        if value:
            return value[:12]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


DEPLOY_REVISION = _deploy_revision()
DEPLOY_SERVICE = os.getenv("RAILWAY_SERVICE_NAME", "-")
DEPLOY_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT_NAME", "-")

# ── Токен ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8923014188:AAHvNW2B5fin2XCmbVhlaLNjWhLwI3JhZ90")
# Ключ должен задаваться только в Railway Variables. Старый встроенный ключ
# возвращает 401 и зря задерживает оба защищённых источника.
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
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

# Хардкодный fallback — если env vars не заданы в Railway, используем прокси из кода
if not AVITO_PROXIES and not _proxy_auth_failed:
    _HARDCODED_PROXY = "http://ilkin:EDNyWFYHyH2Y@mproxy.site:16358"
    AVITO_PROXIES = {"http": _HARDCODED_PROXY, "https": _HARDCODED_PROXY}
    AVITO_PROXY_HOST = "mproxy.site"
    AVITO_PROXY_PORT = "16358"
    AVITO_PROXY_USER = "ilkin"
    AVITO_PROXY_PASS = "EDNyWFYHyH2Y"
    print("[прокси] ⚡ Используем встроенный прокси mproxy.site")


def _active_proxy_url() -> str:
    """Единый актуальный URL прокси из AVITO_PROXY_*.

    PROXY_URL может остаться со старым логином после замены учётных данных.
    Все источники должны использовать уже разобранную конфигурацию, иначе часть
    запросов идёт через новый логин, а часть — через устаревший.
    """
    proxy = _avito_proxies() or {}
    return proxy.get("https") or proxy.get("http") or ""


_proxy_display = f"{AVITO_PROXY_PROTOCOL}://{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}" if AVITO_PROXIES else None
print(f"[прокси] {'✅ ' + _proxy_display if _proxy_display else '❌ не настроен — Авито/Auto.ru могут не работать'}")

# Ссылка ротации IP мобильного прокси (mobileproxy.space «Ссылка для смены IP»).
# Если задана — бот сам меняет IP перед скрейпом Авито, обходя rate-limit (429).
AVITO_PROXY_ROTATE_URL = os.getenv("AVITO_PROXY_ROTATE_URL", "")

# Токен приложения Auto.ru (заголовок x-authorization для apiauto.ru).
# Эндпоинт apiauto.ru отдаёт чистый JSON без капчи Яндекса — самый надёжный
# путь для Auto.ru. Токен зашит в мобильное приложение ru.auto.ara; если задан,
# бот ходит через официальный API вместо капча-стены desktop-версии.
AUTORU_API_TOKEN = os.getenv("AUTORU_API_TOKEN", "")
_last_ip_rotate_ts = 0.0

# Отдельный пул РФ-прокси для Auto.ru. Яндекс режет капчей дата-центр/мобильный
# IP, но чистые РФ SOCKS5/резидентные IP обычно пропускает. Формат каждого:
#   socks5://user:pass@host:port  (или http://...). Список через запятую в
#   переменной AUTORU_PROXIES; ниже — дефолтные РФ-прокси пользователя.
# Не подставляем устаревший отдельный пул. Если AUTORU_PROXIES явно не задан,
# Auto.ru использует актуальный мобильный AVITO_PROXY_* вместе с Авито.
_AUTORU_PROXIES_DEFAULT: list[str] = []
AUTORU_PROXIES = [
    p.strip() for p in os.getenv("AUTORU_PROXIES", ",".join(_AUTORU_PROXIES_DEFAULT)).split(",")
    if p.strip()
]

def _autoru_proxy_dicts() -> "list[dict]":
    """Список proxy-словарей для requests/curl_cffi из пула Auto.ru.
    socks5:// → socks5h:// — DNS резолвится НА СТОРОНЕ прокси (РФ), иначе
    Яндекс видит иностранный DNS-резолвинг и чаще отдаёт капчу."""
    out = []
    for p in AUTORU_PROXIES:
        if p.startswith("socks5://"):
            p = "socks5h://" + p[len("socks5://"):]
        out.append({"http": p, "https": p})
    return out

# Диагностика готовности Auto.ru: Яндекс режет капчей любой «грязный» IP.
if AUTORU_API_TOKEN:
    print("[Auto.ru] ✅ токен apiauto.ru задан — чистый JSON без капчи")
elif AUTORU_PROXIES:
    print(f"[Auto.ru] ✅ пул РФ-прокси: {len(AUTORU_PROXIES)} шт. — обход капчи через чистые РФ IP")
elif AVITO_PROXY_ROTATE_URL:
    print("[Auto.ru] ✅ ротация IP настроена — капча будет обходиться сменой IP")
else:
    print("[Auto.ru] ⚠️ нет ни токена, ни РФ-прокси, ни ротации — Auto.ru поймает капчу")

def _rotate_proxy_ip(min_interval: float = 50.0, force: bool = False) -> bool:
    """Меняет IP мобильного прокси через ссылку ротации. Возвращает True при успехе.
    Защита: не чаще раза в min_interval секунд (ротация имеет лимиты у провайдера).
    force=True — игнорирует интервал (для критичного обхода капчи Auto.ru)."""
    global _last_ip_rotate_ts
    if not AVITO_PROXY_ROTATE_URL:
        return False
    import time as _t
    now = _t.time()
    # Провайдер запрещает частые смены и отвечает "Already change IP".
    # Даже вызовы с min_interval=0 не должны спамить endpoint подряд.
    effective_interval = min_interval if force else max(min_interval, 20.0)
    if now - _last_ip_rotate_ts < effective_interval:
        return False
    _last_ip_rotate_ts = now
    try:
        import requests as _rq

        def _proxy_ip() -> str:
            """Фактический внешний IP именно прокси, а не Railway."""
            try:
                response = _rq.get(
                    "https://api.ipify.org",
                    proxies=_avito_proxies() or {},
                    timeout=6,
                )
                value = (response.text or "").strip()
                if response.status_code == 200 and re.fullmatch(
                    r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{3,}",
                    value,
                ):
                    return value
            except Exception:
                pass
            return ""

        ip_before = _proxy_ip()
        r = _rq.get(AVITO_PROXY_ROTATE_URL, timeout=15)
        body = (r.text or "").strip()
        body_lower = body.lower()
        explicitly_rejected = (
            "already change ip" in body_lower
            or '"status":"err"' in body_lower.replace(" ", "")
            or '"status": "err"' in body_lower
        )
        html_response = (
            body_lower.startswith("<!doctype html")
            or body_lower.startswith("<html")
        )
        ok = r.status_code == 200 and not explicitly_rejected and not html_response
        ip_after = ""
        if r.status_code == 200 and not explicitly_rejected:
            # mobileproxy.space может вернуть обычную HTML-страницу даже при
            # успешной смене. Поэтому HTML сам по себе не ошибка: ждём применения
            # и подтверждаем результат по фактическому выходному IP прокси.
            for _ in range(3):
                _t.sleep(1.5)
                ip_after = _proxy_ip()
                if ip_before and ip_after and ip_after != ip_before:
                    ok = True
                    break
        ip_change = (
            f"{ip_before or '?'} → {ip_after or '?'}"
            if html_response or ip_after else (ip_before or "?")
        )
        print(
            f"[прокси] ротация IP: HTTP {r.status_code} "
            f"{'✅' if ok else '❌'} IP {ip_change}"
        )
        return ok
    except Exception as e:
        print(f"[прокси] ротация IP ошибка: {str(e)[:80]}")
        return False

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

# ── Фильтр нерабочих авто ──────────────────────────────────────────
NOT_RUNNING_KEYWORDS = [
    # без двигателя / кузов
    "без двигателя", "без мотора", "кузов без двигателя", "кузов на запчасти",
    "только кузов", "голый кузов", "кузов отдельно",
    # на запчасти
    "на запчасти", "на разбор", "под разбор", "на разборку", "разборка",
    "запчасти", "по запчастям",
    # не на ходу
    "не на ходу", "не едет", "не заводится", "не заводится вообще",
    "не запускается", "требует буксировки", "под буксир", "на буксире",
    "под эвакуатор", "на эвакуаторе", "не ездит",
    # утилизация
    "под утилизацию", "утиль", "на металлолом", "металлолом",
    # битая / после аварии
    "после пожара", "сгоревшая", "сгоревший", "пожарная",
    # двигатель отсутствует
    "двигатель отсутствует", "мотор снят", "двигатель снят",
    "нет двигателя", "нет мотора",
]

_NOT_RUNNING_RE = re.compile(
    "|".join(re.escape(k) for k in NOT_RUNNING_KEYWORDS),
    re.IGNORECASE,
)


def is_not_running(item: dict) -> bool:
    """Возвращает True если авто нерабочее (без двигателя, на запчасти, не на ходу)."""
    text = (
        item.get("title", "") + " " + item.get("description", "")
    ).lower()
    return bool(_NOT_RUNNING_RE.search(text))


def has_extreme_mileage(item: dict) -> bool:
    """Возвращает True если пробег явно запредельный (>500k км).
    Такие машины исключаются из рыночной оценки, т.к. цена не репрезентативна."""
    mileage = _item_mileage(item)
    if mileage >= 500_000:
        return True
    # Проверяем также в описании текстом
    text = (item.get("description", "").lower() or "")
    if "500" in text or "600" in text or "700" in text or "800" in text or "900" in text:
        if "тыс" in text or "км" in text:
            return True
    return False


def _item_mileage(item: dict) -> int:
    """Returns mileage in km from normalized fields or listing text."""
    for key in ("mileage", "_mileage"):
        try:
            mileage = int(item.get(key, 0) or 0)
            if mileage > 0:
                return mileage
        except Exception:
            pass
    try:
        return _extract_mileage(f"{item.get('title', '')} {item.get('description', '')}")
    except Exception:
        return 0


def _market_mileage_factor(item: dict) -> float:
    """Conservative market discount for high-mileage cars when no Avito AI price is available."""
    mileage = _item_mileage(item)
    if mileage >= 400_000:
        return 0.82
    if mileage >= 350_000:
        return 0.88
    if mileage >= 300_000:
        return 0.92
    if mileage >= 250_000:
        return 0.95
    return 1.0


HOT_WORDS = re.compile(
    r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево|продам быстро|срочная продажа"
    r"|срочно продам|срочно продаю|нужны деньги|уезжаю|переезжаю|не торгуюсь нет"
    r"|ниже рынка|ниже рыночной|выгодно|хорошая цена|торг при осмотре|торговля|торгуюсь"
    r"|мотивированный|продавец спешит|цена упала|новая цена|цена снижена|уступит)",
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
def _env_int(name: str, default: int, min_value: int = 1) -> int:
    try:
        return max(min_value, int(os.getenv(name, str(default))))
    except Exception:
        return default


SEARCH_COOLDOWN_SEC = 45
SEARCH_SOURCE_TIMEOUT_SEC = _env_int("SEARCH_SOURCE_TIMEOUT_SEC", 30)
SEARCH_CRITICAL_SOURCE_GRACE_SEC = _env_int("SEARCH_CRITICAL_SOURCE_GRACE_SEC", 25)
SEARCH_AUTORU_DEADLINE_SEC = _env_int("SEARCH_AUTORU_DEADLINE_SEC", 45)
SEARCH_PRICE_FILL_LIMIT = _env_int("SEARCH_PRICE_FILL_LIMIT", 3, 0)
SEARCH_PRICE_FILL_TIMEOUT_SEC = _env_int("SEARCH_PRICE_FILL_TIMEOUT_SEC", 4)
SEARCH_DETAIL_CHECK_LIMIT = _env_int("SEARCH_DETAIL_CHECK_LIMIT", 0, 0)
SEARCH_DETAIL_CHECK_TIMEOUT_SEC = _env_int("SEARCH_DETAIL_CHECK_TIMEOUT_SEC", 5)
SEARCH_DETAIL_TOTAL_TIMEOUT_SEC = _env_int("SEARCH_DETAIL_TOTAL_TIMEOUT_SEC", 25)
DEFAULT_TRIAL_DAYS = _env_int("DEFAULT_TRIAL_DAYS", 7, 1)
_last_search_at: dict[int, float] = {}
_SOURCE_RESULT_CACHE_TTL_SEC = 15 * 60
_source_result_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_AUTORU_BACKGROUND_LOCK = threading.Lock()


def _remember_source_results(source: str, region: str, items: list[dict]) -> list[dict]:
    """Keeps useful late results from protected/slow sources for the active search."""
    if not items:
        return items
    merged: list[dict] = []
    seen_urls: set[str] = set()
    old = _source_result_cache.get((source, region))
    pools = [items]
    if old and (time.time() - old[0]) < _SOURCE_RESULT_CACHE_TTL_SEC:
        pools.append(old[1])
    for pool in pools:
        for item in pool or []:
            url = _norm_url(item.get("url", ""))
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(dict(item))
    _source_result_cache[(source, region)] = (time.time(), merged[:200])
    return items


def _cached_source_results(
    source: str,
    region: str,
    price_min: int,
    price_max: int,
    limit: int = 40,
) -> list[dict]:
    cached = _source_result_cache.get((source, region))
    if not cached or (time.time() - cached[0]) >= _SOURCE_RESULT_CACHE_TTL_SEC:
        return []
    out: list[dict] = []
    for item in cached[1]:
        price = int(item.get("_price_int") or parse_price(item.get("price", "")) or 0)
        if price and not (price_min <= price <= price_max):
            continue
        if not item.get("url"):
            continue
        out.append(dict(item))
        if len(out) >= limit:
            break
    return out

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
    # Try PostgreSQL first (survives Railway restarts)
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("SELECT url FROM seen_urls WHERE uid=%s ORDER BY added_at DESC LIMIT 500", (uid,))
                rows = cur.fetchall()
                if rows:
                    return set(r[0] for r in rows)
    except Exception:
        pass
    # Fallback to file
    f = user_dir(uid) / "seen.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data[-300:])
    except Exception:
        pass
    return set()


def save_seen(uid: int, seen: set):
    # Save to PostgreSQL (survives Railway restarts)
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                # Delete old entries beyond 500
                cur.execute(
                    "DELETE FROM seen_urls WHERE uid=%s AND url NOT IN "
                    "(SELECT url FROM seen_urls WHERE uid=%s ORDER BY added_at DESC LIMIT 400)",
                    (uid, uid)
                )
                # Upsert new URLs
                for url in seen:
                    cur.execute(
                        "INSERT INTO seen_urls(uid, url) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                        (uid, url)
                    )
            return
    except Exception:
        pass
    # Fallback to file
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
    # Дублируем в PostgreSQL — переживает деплой (контейнер эфемерный)
    try:
        _kv_set("referrals", json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

def _restore_referrals():
    """Восстанавливает рефералов из PG, если локальный файл пуст (после деплоя)."""
    try:
        if REFERRALS_FILE.exists() and REFERRALS_FILE.stat().st_size > 5:
            return
        raw = _kv_get("referrals")
        if raw:
            REFERRALS_FILE.parent.mkdir(exist_ok=True)
            REFERRALS_FILE.write_text(raw, encoding="utf-8")
            print(f"  [referrals-restore] восстановлено из PG ({len(raw)}б)")
    except Exception as e:
        print(f"  [referrals-restore] {str(e)[:80]}")

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
    _is_new = new_uid not in invited_list
    _milestone = False
    if _is_new:
        invited_list.append(new_uid)
        data[inviter_key]["invited"] = invited_list

        # Give +3 days bonus per invited friend
        data[inviter_key]["bonus_days"] = data[inviter_key].get("bonus_days", 0) + 3

        # Milestone: 10 friends = +30 extra days
        if len(invited_list) == 10:
            data[inviter_key]["bonus_days"] = data[inviter_key].get("bonus_days", 0) + 30
            _milestone = True

    _save_referrals(data)
    # Возвращаем инфо для уведомления пригласившего
    return {"is_new": _is_new, "count": len(invited_list), "milestone": _milestone,
            "bonus_days": data[inviter_key].get("bonus_days", 0)}


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
    text = str(s or "").lower().replace(",", ".")
    # Prices in VK/TG are often written as "20k", "155к", "155 т", "1.2 млн".
    m = re.search(r"(?<!\d)(\d+(?:[.\s]\d+)?)\s*(млн|million|kk|кк|тыс|т\.?\s*р?\.?|тр|k|к)\b", text, re.I)
    if m:
        raw = re.sub(r"\s+", "", m.group(1))
        try:
            value = float(raw)
        except Exception:
            value = 0.0
        suffix = m.group(2).lower().replace(" ", "")
        mult = 1_000_000 if suffix in ("млн", "million", "kk", "кк") else 1_000
        price = int(value * mult)
        if price > 0:
            return price
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def is_dealer(item: dict) -> bool:
    text = (
        item.get("title", "") + " " +
        item.get("description", "") + " " +
        item.get("seller", "")
    ).lower()
    return any(k in text for k in DEALER_KEYWORDS)


_RESELLER_KEYWORDS = (
    "выкуп авто", "автоподбор", "подбор авто", "обмен с доплат", "трейд-ин",
    "trade-in", "автосалон", "скупка авто", "продажа авто под ключ",
    "большой выбор авто", "в наличии авто", "автокредит", "рассрочка",
)

def _seller_phone(item: dict) -> str:
    """Последние 10 цифр телефона из объявления (для склейки объявлений продавца)."""
    txt = f"{item.get('description','')} {item.get('seller','')} {item.get('title','')}"
    m = re.search(r"[78]\d{10}", re.sub(r"\D", "", txt))
    return m.group(0)[-10:] if m else ""

def _seller_type(item: dict, phone_counts: dict | None = None) -> str:
    """Тип продавца: 'dealer' (салон), 'pro' (перекуп/профи) или 'private' (частник).
    Перекупа определяем по: маркерам услуг ИЛИ одному телефону в ≥3 объявлениях."""
    if is_dealer(item):
        return "dealer"
    text = (item.get("title", "") + " " + item.get("description", "") + " "
            + item.get("seller", "")).lower()
    if any(k in text for k in _RESELLER_KEYWORDS):
        return "pro"
    if phone_counts:
        ph = _seller_phone(item)
        if ph and phone_counts.get(ph, 0) >= 3:
            return "pro"
    return "private"


def in_price_range(item: dict, price_min: int, price_max: int) -> bool:
    p = item.get("_price_int") or parse_price(item.get("price", ""))
    if p:
        return price_min <= p <= price_max
    if (item.get("source", "") or "").lower() in ("vk", "tg", "tg_channel"):
        return False
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
    if year >= 2018 and price_max < 250_000:
        return False
    if year >= 2015 and price_max < 130_000:
        return False
    if year >= 2012 and price_max < 80_000:
        return False
    # Пропускаем как кандидата: реальная цена нужна, но лучше показать
    # объявление с "—", чем потерять реальную выгодную машину.
    return True


def _social_price_is_plausible(price: int, year: int = 0) -> bool:
    """Reject prices that were probably parsed from year/mileage/power in VK/TG text."""
    if not price:
        return True
    if price < 10_000:
        return False
    if year >= 2022:
        return price >= 800_000
    if year >= 2020:
        return price >= 400_000
    if year >= 2018:
        return price >= 250_000
    if year >= 2015:
        return price >= 130_000
    if year >= 2012:
        return price >= 80_000
    if year >= 2008:
        return price >= 50_000
    return True


def _social_price_is_credit_payment(price: int, text: str) -> bool:
    if not price:
        return False
    low = str(text or "").lower()
    if not any(x in low for x in ("кредит", "месяц", "мес.", "платеж", "платёж", "взнос", "рассроч")):
        return False
    return price <= 120_000


def _sanitize_social_price(item: dict) -> None:
    source = (item.get("source", "") or "").lower()
    if source not in ("vk", "tg", "tg_channel"):
        return
    price = int(item.get("_price_int") or 0)
    if not price:
        return
    year = item.get("_year") or item.get("year") or 0
    try:
        year = int(str(year)[:4])
    except Exception:
        year = 0
    text = f"{item.get('title', '')} {item.get('description', '')}"
    if _social_price_is_plausible(price, year) and not _social_price_is_credit_payment(price, text):
        return
    item["_bad_price_int"] = price
    item["_price_int"] = 0
    item["price"] = "цена не указана"
    item["_no_price"] = True
    _clear_market_fields(item)


def _clear_market_fields(item: dict) -> None:
    """Удаляет старый результат анализа рынка, не затрагивая само объявление."""
    for key in (
        "_market_price",
        "_savings_pct",
        "_market_lvl",
        "_market_n",
        "_below_market",
        "_market_mileage_factor",
        "_deal_score",
    ):
        item.pop(key, None)


def hot_score(item: dict) -> float:
    """Базовая оценка срочности продажи."""
    title = item.get("title", "") + " " + item.get("description", "")
    score = 0.0
    if HOT_WORDS.search(title):
        score += 20.0
    return round(score, 2)


# 1) число с единицей «км/тыс км/т.км/тыс»; 2) «пробег <число>» без единицы.
_MILEAGE_RE = re.compile(
    r'(\d[\d\s]{1,7})\s*(тыс\.?\s*км|т\.?\s*км|т\.\s*км|тыс|км)',
    re.IGNORECASE,
)
_MILEAGE_PROBEG_RE = re.compile(r'пробег[:\s]*(\d[\d\s]{2,8})', re.IGNORECASE)

def _extract_mileage(text: str) -> int:
    """Достаёт пробег (км) из текста объявления Дром/ВК/ТГ, где нет числового
    поля mileage. Понимает «150 000 км», «150 тыс км», «пробег 150000».
    Возвращает 0, если не нашёл правдоподобный пробег."""
    if not text:
        return 0
    best = 0
    for m in _MILEAGE_RE.finditer(text):
        raw = re.sub(r"\D", "", m.group(1))
        if not raw:
            continue
        val = int(raw)
        unit = m.group(2).lower()
        if unit.startswith("тыс") or unit.startswith("т"):
            val *= 1000
        if 1000 <= val <= 800_000:
            best = max(best, val)
    # «пробег 191500» без единицы измерения
    for m in _MILEAGE_PROBEG_RE.finditer(text):
        raw = re.sub(r"\D", "", m.group(1))
        if not raw:
            continue
        val = int(raw)
        # «пробег 150 тыс» → тысячи
        _tail = text[m.end():m.end() + 6].lower()
        if val < 1000 and ("тыс" in _tail or "т." in _tail):
            val *= 1000
        if 1000 <= val <= 800_000:
            best = max(best, val)
    return best


def _car_group_key(title: str) -> str:
    """Извлекает марку+модель+год для группировки (напр. 'toyota camry 2018').

    Корректно обрабатывает заголовки с префиксами площадок:
      'Дром Volkswagen Golf 2012' → 'volkswagen golf 2012'
      'Авито Mazda 3 1.6 AT, 2008' → 'mazda 3 2008'
    """
    t = title.lower()
    # Удаляем префиксы площадок
    for _pfx in ("авито", "дром", "auto.ru", "autoru", "вконтакте", "юла", "youla", "yula", "tg", "telegram"):
        t = re.sub(rf'^\s*{re.escape(_pfx)}\s*', '', t)
    # Убираем скобочные пометки: "ВАЗ (LADA) 2114" → "ВАЗ 2114" (иначе не матчится с ВК)
    t = re.sub(r'\([^)]*\)', ' ', t)
    # Унификация синонимов марок (ВК пишет «Лада», Авито «ВАЗ»; латиница/кириллица)
    _BRAND_SYN = {
        "лада": "ваз", "жигули": "ваз", "lada": "ваз",
        "вaз": "ваз", "тойота": "toyota", "хендай": "hyundai", "хёндай": "hyundai",
        "хундай": "hyundai", "киа": "kia", "ниссан": "nissan", "рено": "renault",
        "фольксваген": "volkswagen", "шкода": "skoda", "мерседес": "mercedes",
        "бмв": "bmw", "ауди": "audi", "мазда": "mazda", "митсубиси": "mitsubishi",
        "опель": "opel", "форд": "ford", "шевроле": "chevrolet", "хонда": "honda",
    }
    for _ru, _en in _BRAND_SYN.items():
        t = re.sub(rf'\b{_ru}\b', _en, t)
    # В соцсетях часто пишут просто "2101", "2106", "2114" без ВАЗ/Лада.
    # Без этого ключ становится "2101 доками" и рынок не находится.
    _vaz_m = re.search(r'\b(210[1-9]|211[0-5]|21099|217[0-2]|219[0-4]|111[7-9]|2121|2131)\b', t)
    if _vaz_m and not re.search(r'\b(ваз|vaz)\b', t):
        t = f"ваз {_vaz_m.group(1)} {t}"
    # Схлопываем составные марки в одно слово, ЧТОБЫ модель (класс) не терялась
    # при обрезке до 2 слов: "mercedes-benz e 200" → "mercedes e 200" (E-класс
    # больше не смешивается с C-классом), "land rover discovery" → "landrover discovery".
    _MULTIWORD_BRAND = {
        r'mercedes[\s\-]*benz': 'mercedes', r'land[\s\-]*rover': 'landrover',
        r'alfa[\s\-]*romeo': 'alfaromeo', r'great[\s\-]*wall': 'greatwall',
        r'land[\s\-]*cruiser': 'landcruiser', r'aston[\s\-]*martin': 'astonmartin',
    }
    for _pat, _repl in _MULTIWORD_BRAND.items():
        t = re.sub(_pat, _repl, t)
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
    # Слова-мусор, которые НЕ являются маркой/моделью (часто в постах ВК/ТГ).
    # Без их фильтрации "Продам ВАЗ 2114" → ключ "продам ваз" и не матчится с Авито.
    _NON_MODEL = {
        "продам", "продаю", "продается", "продаётся", "продажа", "продано",
        "срочно", "срочная", "торг", "обмен", "куплю", "цена", "цены",
        "авто", "автомобиль", "автомобили", "машина", "машину", "машины",
        "отличное", "идеальное", "состояние", "состоянии", "новый", "новая",
        "новое", "битый", "битая", "целый", "хорошее", "хорошем", "продаётьс",
        "куплю", "обменяю", "помощь", "подбор", "выкуп", "куплю", "продаю",
        "отдам", "куплю", "продам", "in", "из", "за", "на", "по", "с", "и",
        "год", "года", "г", "выпуска", "вып", "пробег", "руб",
        # «bmw 5 series» → «bmw 5», чтоб матчилось с «bmw 520» и т.п.
        "series", "серия", "класс", "class", "седан", "хэтчбек", "универсал",
    }
    def _is_model_word(w: str) -> bool:
        if w in _NON_MODEL:
            return False
        if w.isalpha():
            return True
        if re.fullmatch(r'[a-zа-яё]{1,5}\d{1,4}[a-zа-яё]{0,3}', w):
            return True
        if re.fullmatch(r'\d{1,4}[a-zа-яё]{1,4}', w):
            return True
        if w.isdigit():
            n = int(w)
            # Модели: 3, 5, 320, 2114, 2170 и т.п. Исключаем только правдоподобные
            # годы (1980-2035) — настоящий год уже вырезан выше, это страховка.
            return n < 10000 and not (1980 <= n <= 2035)
        return False
    words = [w for w in re.sub(r'[^а-яёa-z0-9\s]', ' ', t).split() if w and _is_model_word(w)]
    if len(words) >= 3 and re.fullmatch(r'[a-zа-яё]{1,4}', words[1]) and words[2].isdigit():
        # Mazda CX-5 / Audi Q 7 / BMW X 5 -> mazda cx5 / audi q7 / bmw x5
        brand_model = f"{words[0]} {words[1]}{words[2]}"
    else:
        brand_model = " ".join(words[:2]) if len(words) >= 2 else " ".join(words)
    return f"{brand_model} {year}".strip()



# Минимальная скидка, чтобы объявление считалось реальным «ниже рынка» и попадало
# в верхний тир выдачи. −5…−8% — это фактически около рынка: такие машины можно
# показывать, но не как первые выгодные сделки.
MARKET_DEAL_MIN_PCT = 10.0


def _is_strong_below_market(it: dict) -> bool:
    """True только для сильного сигнала ниже рынка, а не для погрешности медианы."""
    if it.get("_is_junk") or is_not_running(it):
        return False
    pct = it.get("_savings_pct", 0) or 0
    if (it.get("source", "") or "").lower() == "avito":
        avito_score = it.get("_avito_rating_score")
        if it.get("_market_lvl") != "avito" and avito_score is not None and avito_score <= 0:
            return False
    if it.get("_market_lvl") == "model" and pct < 20:
        return False
    if pct >= MARKET_DEAL_MIN_PCT:
        return True
    if it.get("_market_price") and pct <= 0:
        return False
    # Если сам Авито пометил объявление как «отличная/очень хорошая цена»,
    # используем этот сигнал даже без нашей глубокой медианы. Обычная
    # «хорошая цена» без >=10% — не топ, чтобы −8% не считались находкой.
    return (it.get("_avito_rating_score") or 0) >= 2


def _is_market_candidate(it: dict) -> bool:
    """True if listing is not known to be at/above market."""
    if it.get("_is_junk") or is_not_running(it):
        return False
    price = int(it.get("_price_int") or 0)
    market = int(it.get("_market_price") or 0)
    if market and price:
        return market > price and (it.get("_savings_pct") or 0) > 0
    if (it.get("source", "") or "").lower() == "avito":
        score = it.get("_avito_rating_score")
        if score is not None and score <= 0:
            return False
    return True

# СТОП только если машина НЕ НА ХОДУ / на запчасти / утиль. Битые, крашеные,
# после ДТП, требующие ремонта — это НЕ стоп (пользователю такие нужны), лишь бы
# ездили. Поэтому список узкий — только «нерабочие» состояния.
def _best_below_market_items(items: list[dict]) -> list[dict]:
    """Strict final selector: only proven below-market cars, best discount first."""
    picked: list[dict] = []
    for it in items:
        price = int(it.get("_price_int") or 0)
        market = int(it.get("_market_price") or 0)
        pct = float(it.get("_savings_pct") or 0)
        lvl = str(it.get("_market_lvl") or "")
        n = int(it.get("_market_n") or 0)
        if not price or not market or market <= price:
            continue
        if not _is_strong_below_market(it):
            continue
        if lvl in ("avito", "drom", "autoru"):
            pass
        elif lvl in ("near", "bracket"):
            if n < 5 or pct < 18:
                continue
        else:
            if n < 8 or pct < 25:
                continue
        if is_not_running(it):
            continue
        picked.append(it)
    return _sort_by_deal(picked)


def _ranked_search_items(items: list[dict]) -> list[dict]:
    """User search order: fresh strong deals first, then the rest of budget listings."""
    ranked: list[dict] = []
    for it in items or []:
        if is_not_running(it):
            continue
        price = int(it.get("_price_int") or parse_price(it.get("price", "")) or 0)
        market = int(it.get("_market_price") or 0)
        pct = float(it.get("_savings_pct") or 0)
        lvl = str(it.get("_market_lvl") or "")
        n = int(it.get("_market_n") or 0)
        days = int(it.get("_days_on_site") or 999)
        if not price:
            continue
        if (it.get("source", "") or "").lower() == "avito":
            score = it.get("_avito_rating_score")
            if score is not None and score < 0:
                continue

        if market and market > price and _is_strong_below_market(it):
            trusted = lvl in ("avito", "drom", "autoru") or n >= 5
            if days <= 1:
                bucket = 0
            elif days <= 3:
                bucket = 1
            elif trusted and pct >= 25:
                bucket = 2
            elif trusted:
                bucket = 3
            else:
                bucket = 4
        elif not market:
            bucket = 4 if (price <= 120_000 or days <= 2) else 5
        elif market and market < price:
            bucket = 7
        else:
            bucket = 6
        seen_rank = 1 if it.get("_already_seen") else 0
        ranked.append((seen_rank, days, bucket, -pct, price, it))
    ranked.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
    return [it for *_keys, it in ranked]


def _safe_rank_search_items(items: list[dict]) -> list[dict]:
    ranked = _dedupe_search_items(_ranked_search_items(items))
    if ranked:
        return ranked
    fallback: list[dict] = []
    for it in items or []:
        if it.get("_market_ref_only"):
            continue
        if is_not_running(it):
            continue
        if not it.get("url"):
            continue
        fallback.append(it)
    if fallback:
        print(f"  [search-fallback] strict ranking removed all cards; showing {len(fallback)} basic results")
    return _dedupe_search_items(fallback)


def _dedupe_search_items(items: list[dict]) -> list[dict]:
    """Remove duplicate listings by normalized URL and stable title/price signature."""
    seen_urls: set[str] = set()
    seen_sigs: set[str] = set()
    out: list[dict] = []
    for it in items or []:
        url = _norm_url(it.get("url", ""))
        if url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            it["url"] = url
        title = re.sub(r"[^a-zа-яё0-9]+", " ", str(it.get("title", "")).lower()).strip()
        desc = re.sub(r"[^a-zа-яё0-9]+", " ", str(it.get("description", "")).lower()).strip()
        price = int(it.get("_price_int") or parse_price(it.get("price", "")) or 0)
        year = it.get("year") or it.get("_year") or ""
        sig_text = (title + " " + desc)[:140].strip()
        sig = f"{it.get('source','')}|{price}|{year}|{sig_text}" if sig_text and price else ""
        if sig:
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
        out.append(it)
    return out


_JUNK_KEYWORDS = [
    "не на ходу", "не ездит", "не заводится", "не заводилась", "не заводиться",
    "не едет", "не заведётся", "не заведется",
    "на запчасти", "на запчаст", "на разбор", "по запчастям",
    "утиль", "утилизац",
]
# Отрицания, чтобы «не на запчасти», «не на разбор» не считались стопом.
_JUNK_NEG = ("не на запчаст", "не на разбор", "не по запчаст")

_OWNERS_RE = re.compile(
    r'(\d)\s*(?:владел|собственник|хозя)', re.IGNORECASE)
_OWNERS_PTS_RE = re.compile(
    r'(?:владельц\w*|собственник\w*)[^\d]{0,12}(\d)', re.IGNORECASE)

def _extract_owners(text: str) -> int:
    """Число владельцев по ПТС из текста объявления (1/2/3…). 0 — не нашли."""
    if not text:
        return 0
    for rx in (_OWNERS_RE, _OWNERS_PTS_RE):
        m = rx.search(text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 9:
                return n
    return 0


def _text_is_junk(title: str, desc: str) -> bool:
    """True ТОЛЬКО если машина не на ходу / на запчасти / утиль. Битые/крашеные/
    после ДТП — НЕ стоп (их тоже показываем), важно лишь чтобы ездила."""
    t = f"{title} {desc}".lower()
    hit = any(k in t for k in _JUNK_KEYWORDS)
    if not hit:
        return False
    # «не на запчасти / целая» — это НЕ стоп
    if any(neg in t for neg in _JUNK_NEG) and "не на ходу" not in t:
        return False
    return True


# ── Отслеживание снижения цены ───────────────────────────────────
# Запоминаем цену объявления по URL. Если при следующей встрече цена ниже —
# продавец скинул → мотивирован → сигнал перекупу.
_PRICE_HISTORY: dict[str, int] = {}
_PRICE_HISTORY_FILE = Path("price_history.json")
_price_hist_last_save = 0.0

def _load_price_history():
    global _PRICE_HISTORY
    try:
        if _PRICE_HISTORY_FILE.exists():
            _PRICE_HISTORY = {k: int(v) for k, v in
                              json.loads(_PRICE_HISTORY_FILE.read_text(encoding="utf-8")).items()}
            print(f"  [цены] история цен: {len(_PRICE_HISTORY)} объявлений")
    except Exception as e:
        print(f"  [цены] не удалось загрузить историю цен: {e}")

def _save_price_history(force: bool = False):
    global _price_hist_last_save
    now = time.time()
    if not force and now - _price_hist_last_save < 60:
        return
    _price_hist_last_save = now
    try:
        # Не даём файлу расти бесконечно — держим последние 20000 записей.
        data = _PRICE_HISTORY
        if len(data) > 20000:
            data = dict(list(data.items())[-20000:])
            _PRICE_HISTORY.clear()
            _PRICE_HISTORY.update(data)
        _PRICE_HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _note_price_drop(url: str, price: int) -> int:
    """Сохраняет текущую цену и возвращает размер снижения (₽) с прошлой встречи.
    0 — если цена не снижалась или объявление новое."""
    if not url or not price or price <= 0:
        return 0
    key = _norm_url(url)
    old = _PRICE_HISTORY.get(key, 0)
    _PRICE_HISTORY[key] = price
    if old and price < old:
        drop = old - price
        # Игнорируем микро-колебания (<2% и <5000₽)
        if drop >= 5000 and drop >= old * 0.02:
            return drop
    return 0


def _traffic_light(item: dict) -> str:
    """🚦 Светофор выгодности/чистоты объявления (как у Haraba).
    🟢 — выгодно и чисто; 🟡 — нейтрально; 🔴 — рискованно/дорого."""
    pct = item.get("_savings_pct", 0)
    is_junk = item.get("_is_junk")          # битый / не на ходу / на запчасти
    is_dealer = item.get("_is_dealer")
    has_market = bool(item.get("_market_price"))
    # Красный: явный риск или заметно дороже рынка
    if is_junk:
        return "🔴"
    if has_market and pct <= -10:
        return "🔴"
    # Зелёный: ощутимо дешевле рынка и без явных рисков
    if has_market and pct >= 15 and not is_dealer:
        return "🟢"
    # Жёлтый: всё остальное (около рынка, небольшая скидка, дилер, нет рынка)
    return "🟡"


def _liquidity_note(item: dict) -> str:
    """📊 Ликвидность модели: сколько таких в продаже и средний срок продажи."""
    cnt = item.get("_liq_count", 0)
    days = item.get("_liq_days", 0)
    if cnt < 3:
        return ""
    parts = [f"в продаже ~{cnt}"]
    if days and days > 0:
        if days <= 14:
            parts.append(f"продаётся быстро (~{days} дн.)")
        elif days <= 45:
            parts.append(f"средний срок ~{days} дн.")
        else:
            parts.append(f"продаётся долго (~{days} дн.)")
    return " · ".join(parts)


def _market_confidence_text(item: dict) -> str:
    """Понятное пользователю качество рыночной оценки."""
    lvl = str(item.get("_market_lvl") or "")
    sample_count = int(item.get("_market_n") or 0)
    if lvl in {"avito", "drom", "autoru"}:
        platform = {"avito": "Авито", "drom": "Дром", "autoru": "Auto.ru"}[lvl]
        return f"высокая (оценка {platform})"
    if lvl == "near":
        return "высокая (точный год)"
    if lvl == "bracket":
        return "средняя (±2 года)"
    if lvl == "medium":
        return "средняя (±3 года)"
    if lvl == "wide":
        return "ограниченная (±5 лет)"
    if lvl == "model":
        return "низкая (мало данных по году)"
    if sample_count >= 7:
        return "высокая"
    if sample_count >= 4:
        return "средняя"
    return "ограниченная"


# ── Haraba.ru анализ конкурентов ───────────────────────────────────────────
def scrape_haraba(region: str, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    """Парсит объявления с Haraba.ru для анализа конкурентов."""
    import requests
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        # Haraba URL - например: https://m.haraba.ru/search?region=moscow
        url = f"https://m.haraba.ru/search"
        params = {
            "region": region.lower(),
            "priceFrom": price_min,
            "priceTo": price_max,
            "sort": "-date"  # новые сначала
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            print(f"  [Haraba] HTTP {r.status_code}")
            return []
        
        # Простой парсинг JSON API (если есть) или парсинг HTML
        items = []
        try:
            data = r.json()
            if "items" in data:
                for item in data.get("items", []):
                    items.append({
                        "title": item.get("title", ""),
                        "_price_int": item.get("price", 0),
                        "url": item.get("url", ""),
                        "description": item.get("description", ""),
                        "_days_on_site": item.get("daysOnSite", 0),
                        "source": "haraba",
                        "_photos_count": len(item.get("photos", [])),
                    })
        except:
            # Fallback на HTML парсинг если JSON не работает
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            # Простой парсинг структуры
            for card in soup.find_all("div", class_=["item", "listing", "card"]):
                title = card.find("h2, h3, a")
                price = card.find("span", class_=["price", "cost"])
                if title and price:
                    items.append({
                        "title": title.get_text().strip(),
                        "_price_int": int("".join(filter(str.isdigit, price.get_text() or "0"))) or 0,
                        "url": card.find("a", href=True)["href"] if card.find("a", href=True) else "",
                        "description": card.get_text().strip(),
                        "source": "haraba",
                    })
        
        print(f"  [Haraba] найдено {len(items)} объявлений")
        return items
    except Exception as e:
        print(f"  [Haraba] ошибка: {e}")
        return []


def analyze_competitors(haraba_items: list[dict], our_items: list[dict]) -> dict:
    """Анализирует конкурентов на Haraba и сравнивает с нашими объявлениями."""
    analysis = {
        "haraba_avg_price": 0,
        "our_avg_price": 0,
        "haraba_count": len(haraba_items),
        "our_count": len(our_items),
        "price_gap_pct": 0,
        "haraba_advantages": [],
        "our_advantages": [],
        "haraba_models": {},
        "our_models": {},
        "recommendations": [],
    }
    
    if not haraba_items and not our_items:
        return analysis
    
    # Средние цены
    if haraba_items:
        prices = [it.get("_price_int", 0) for it in haraba_items if it.get("_price_int", 0) > 0]
        if prices:
            analysis["haraba_avg_price"] = int(sum(prices) / len(prices))
    
    if our_items:
        prices = [it.get("_price_int", 0) for it in our_items if it.get("_price_int", 0) > 0]
        if prices:
            analysis["our_avg_price"] = int(sum(prices) / len(prices))
    
    # Разница в цене
    if analysis["haraba_avg_price"] > 0 and analysis["our_avg_price"] > 0:
        gap = (analysis["haraba_avg_price"] - analysis["our_avg_price"]) / analysis["haraba_avg_price"]
        analysis["price_gap_pct"] = round(gap * 100, 1)
    
    # Анализ фото
    haraba_with_photos = sum(1 for it in haraba_items if it.get("_photos_count", 0) > 3)
    our_with_photos = sum(1 for it in our_items if it.get("_photos_count", 0) > 3)
    
    # Характеристики
    haraba_models = {}
    for it in haraba_items:
        title = it.get("title", "").lower()
        for word in title.split():
            if len(word) > 3:
                haraba_models[word] = haraba_models.get(word, 0) + 1
    analysis["haraba_models"] = dict(sorted(haraba_models.items(), key=lambda x: -x[1])[:10])
    
    our_models = {}
    for it in our_items:
        title = it.get("title", "").lower()
        for word in title.split():
            if len(word) > 3:
                our_models[word] = our_models.get(word, 0) + 1
    analysis["our_models"] = dict(sorted(our_models.items(), key=lambda x: -x[1])[:10])
    
    # Преимущества
    if haraba_with_photos > our_with_photos:
        analysis["haraba_advantages"].append(f"Больше фото: {haraba_with_photos} vs {our_with_photos}")
        analysis["recommendations"].append("📷 Добавить больше фотографий объявлений")
    
    if analysis["price_gap_pct"] < -10:
        analysis["our_advantages"].append(f"Ниже цена на {abs(analysis['price_gap_pct']):.1f}%")
    elif analysis["price_gap_pct"] > 10:
        analysis["haraba_advantages"].append(f"На {analysis['price_gap_pct']:.1f}% дешевле")
        analysis["recommendations"].append("💰 Снизить цены или улучшить предложение")
    
    if analysis["haraba_count"] > analysis["our_count"] * 2:
        analysis["recommendations"].append(f"📈 Добавить объявлений (их {analysis['haraba_count']}, у нас {analysis['our_count']})")
    
    if haraba_items and our_items:
        haraba_avg_desc_len = sum(len(it.get("description", "")) for it in haraba_items) // len(haraba_items)
        our_avg_desc_len = sum(len(it.get("description", "")) for it in our_items) // len(our_items)
        if haraba_avg_desc_len > our_avg_desc_len * 1.5:
            analysis["recommendations"].append("✍️ Писать более подробные описания")
    
    return analysis


def get_fresh_avito_items(region: str, max_age_minutes: int = 30) -> list[dict]:
    """Получает САМЫЕ СВЕЖИЕ объявления Авито (только что загруженные, ≤30 мин назад)."""
    # Парсим с sort_by_date=True чтобы новые были первыми
    # и берём только первую страницу (10-20 самых свежих)
    items = scrape_avito(region, pages=1, sort_by_date=True)
    
    now = time.time()
    fresh = []
    for it in items:
        upload_time = it.get("_upload_timestamp", now)
        age_minutes = (now - upload_time) / 60
        
        # Если объявление младше max_age_minutes
        if age_minutes <= max_age_minutes:
            it["_age_minutes"] = round(age_minutes, 1)
            it["_is_fresh"] = True
            fresh.append(it)
    
    print(f"  [Авито Fresh] найдено {len(fresh)} свежих объявлений (≤{max_age_minutes} мин)")
    return fresh


def rank_by_market_price(items: list[dict], ref_items: list[dict] | None = None,
                          avito_only_median: bool = False) -> list[dict]:
    """
    Вычисляет рыночную цену по медиане Авито-данных (ref_items).
    Если avito_only_median=True — медиана строится ТОЛЬКО по ref_items (Авито),
    а не смешивается с ценами других площадок.
    """
    from statistics import median

    # Авито остаётся приоритетным эталоном. Если он заблокирован, используем
    # только автомобильные площадки с явными ценами (Дром/Auto.ru/Юла).
    # Соцсети VK/TG в эталон не входят: там часто встречаются кредитные платежи,
    # цены за запчасти и неполные объявления.
    def _is_avito_ref(it: dict) -> bool:
        return (it.get("source", "") or "").lower() == "avito"

    if ref_items:
        all_for_median = [i for i in ref_items if _is_avito_ref(i)]
    else:
        all_for_median = [i for i in items if _is_avito_ref(i)]
    if not all_for_median and not avito_only_median:
        _fallback_pool = ref_items if ref_items is not None else items
        all_for_median = [
            i for i in (_fallback_pool or [])
            if (i.get("source", "") or "").lower() in {"drom", "autoru", "youla", "yula"}
            and int(i.get("_price_int") or 0) > 0
        ]
    # Чистим эталон: дилеры завышают цену (→ фейковые скидки), битые занижают.
    # Также исключаем машины с запредельным пробегом (>500k км) — они не репрезентативны.
    # Медиана должна отражать РЕАЛЬНЫЙ рынок частников. Если после чистки данных
    # мало (<5) — откатываемся к исходному набору, чтобы не потерять оценку.
    try:
        _clean = [it for it in all_for_median 
                  if not is_dealer(it) 
                  and not is_not_running(it)
                  and not has_extreme_mileage(it)]
        if len(_clean) >= 5:
            all_for_median = _clean
    except Exception:
        pass
    def _trimmed_median(prices: list) -> float:
        """Медиана с отсечением выбросов: дилерские/восстановленные экземпляры и
        ошибки парсинга не задирают/не занижают рыночную цену.
        1) отбрасываем ~15% с каждого хвоста (было 20% - теперь чувствительнее), 
        2) убираем всё, что вне 0.35×–2.8× медианы (было 0.4-2.5, расширили диапазон).
        3) Для больших выборок (>15) используем взвешенную медиану."""
        s = sorted(prices)
        n = len(s)
        if n < 2:
            return float(s[0]) if s else 0.0
        
        # Более щадящее обрезание для больших выборок
        if n >= 8:
            k = max(1, n // 7)  # 15% вместо 20%
            s = s[k:n - k] or s
        
        m = median(s)
        if m <= 0:
            return 0.0
        
        # Расширенный диапазон допуска: 35%-280% от медианы (было 40%-250%)
        s2 = [x for x in s if 0.35 * m <= x <= 2.8 * m]
        
        # Если отсечение вырезало слишком много — откатываемся
        if len(s2) < 2:
            s2 = s
        
        return float(median(s2)) if len(s2) >= 2 else float(m)

    def _market_key_text(it: dict) -> str:
        """Текст для извлечения модели: у VK/TG/Юлы модель часто в описании."""
        title = it.get("title", "") or ""
        desc = it.get("description", "") or ""
        source = (it.get("source", "") or "").lower()
        title_key = _car_group_key(title)
        weak_title = len(title_key.split()) < 2 or title.strip().lower() in {
            "продам", "продажа", "продам авто", "продам машину", "обмен",
        }
        if source in ("vk", "tg", "tg_channel", "youla", "yula") or weak_title:
            return f"{title} {desc[:260]}"
        return title

    # Цены строго по модели И году: model -> {year -> [prices]}. Рынок считаем
    # ТОЛЬКО по той же модели в близких годах — никаких «все годы»/«вся марка»,
    # иначе 2001 Corolla сравнивается с 2018 и даёт фейковую «скидку».
    model_year: dict[str, dict] = {}
    model_all: dict[str, list] = {}
    for it in all_for_median:
        p = it.get("_price_int", 0)
        if p <= 0:
            continue
        key = _car_group_key(_market_key_text(it))
        parts = key.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
            model, yr = parts[0], int(parts[1])
            if model:
                model_year.setdefault(model, {}).setdefault(yr, []).append(p)
                model_all.setdefault(model, []).append(p)

    def _est_price(prices: list, lvl: str, cand_p):
        """Медиана цен той же модели/года с отсечением выбросов. Leave-one-out:
        убираем ОДНУ цену самого кандидата, чтобы дешёвая находка не занижала свой
        же «рынок». Возвращает (медиана, уровень, число_образцов)."""
        pr = list(prices)
        if cand_p is not None and len(pr) > 1 and cand_p in pr:
            pr.remove(cand_p)
        return _trimmed_median(pr), lvl, len(pr)

    def _market_for(model: str, yr: int, cand_p=None):
        """Рыночная цена по той же модели: окно ±1 год (точно, ≥3), затем ±2 (≥4),
        затем ±3 (≥5), затем ±5 (грубее, ≥6).
        Возвращает (медиана, уровень, N)|(0,'',0)."""
        yrs = model_year.get(model)
        if not yrs:
            return 0.0, "", 0
        
        # Стараемся собрать как можно больше релевантных данных для точной медианы
        near = []
        for y in (yr - 1, yr, yr + 1):
            near += yrs.get(y, [])
        if len(near) >= 3:
            return _est_price(near, "near", cand_p)
        
        wide = list(near)
        for y in (yr - 2, yr + 2):
            wide += yrs.get(y, [])
        if len(wide) >= 4:
            return _est_price(wide, "bracket", cand_p)
        
        # Расширяем ещё шире: ±3 года — всё ещё та же модель
        wider = list(wide)
        for y in (yr - 3, yr + 3):
            wider += yrs.get(y, [])
        if len(wider) >= 5:
            return _est_price(wider, "medium", cand_p)
        
        # Последний шанс: окно ±5 лет (уже грубее, но лучше чем ничего)
        widest = list(wider)
        for y in (yr - 5, yr - 4, yr + 4, yr + 5):
            widest += yrs.get(y, [])
        if len(widest) >= 6:
            return _est_price(widest, "wide", cand_p)
        
        return 0.0, "", 0

    def _market_for_model(model: str, cand_p=None):
        prices = model_all.get(model, [])
        if len(prices) >= 2:
            return _est_price(prices, "model", cand_p)
        return 0.0, "", 0

    for it in items:
        p = it.get("_price_int", 0)
        deal_score = 0.0
        savings_pct = 0.0

        if p > 0:
            key = _car_group_key(_market_key_text(it))
            parts = key.rsplit(" ", 1)
            med = 0.0
            _lvl = ""
            _n = 0
            # A platform's own page estimate is more precise than a cross-listing
            # median. Prefer it for Avito/Drom/Auto.ru, then use similar cars.
            _avm = it.get("_avito_market", 0) or 0
            _drm = it.get("_drom_market", 0) or 0
            _arm = it.get("_autoru_market", 0) or 0
            if _avm and 30_000 < _avm < 50_000_000:
                med, _lvl, _n = float(_avm), "avito", 30
            elif _drm and 30_000 < _drm < 50_000_000:
                med, _lvl, _n = float(_drm), "drom", 30
            elif _arm and 30_000 < _arm < 50_000_000:
                med, _lvl, _n = float(_arm), "autoru", 30
            elif len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
                med, _lvl, _n = _market_for(parts[0], int(parts[1]), p)
                if med <= 0:
                    med, _lvl, _n = _market_for_model(parts[0], p)
            elif key:
                med, _lvl, _n = _market_for_model(key, p)
            if med > 0:
                if _lvl not in ("avito", "drom"):
                    _mileage_factor = _market_mileage_factor(it)
                    if _mileage_factor < 1.0:
                        med *= _mileage_factor
                        it["_market_mileage_factor"] = _mileage_factor
                savings_pct = round((1 - p / med) * 100, 1)
                # Показываем даже очень большие скидки (−80% и глубже). Отсекаем
                # только явные ошибки парсинга: >85% (было 92% - теперь жестче).
                # Для "near" (±1 год, точная оценка) — капс 85%. 
                # Для "bracket" (±2 года) — 80%. 
                # Для "medium" (±3 года) — 75%.
                # Для "wide" (±5 лет, грубая оценка) — 70%.
                # Для Авито собственной оценки — 85% (высокая точность).
                if _lvl.startswith(("avito", "drom", "autoru")):
                    _cap = 85
                elif _lvl == "near":
                    _cap = 85
                elif _lvl == "bracket":
                    _cap = 80
                elif _lvl == "medium":
                    _cap = 75
                elif _lvl == "model":
                    _cap = 55
                else:  # "wide"
                    _cap = 70
                
                if savings_pct > _cap:
                    med = 0
                    savings_pct = 0.0
            if med > 0:
                it["_savings_pct"] = savings_pct
                it["_market_price"] = int(med)
                it["_below_market"] = _is_strong_below_market(it)
                it["_market_n"] = _n          # число аналогов — для доверия в сортировке
                it["_market_lvl"] = _lvl

                # Скидка — главный фактор, но вес зависит от уровня точности
                if _lvl.startswith(("avito", "drom", "autoru")):
                    deal_score += savings_pct * 4.5  # ↑ было 3.0, Авито самая точная
                elif _lvl == "near":
                    deal_score += savings_pct * 4.0  # ↑ было 3.0, ±1 год — точно
                elif _lvl == "bracket":
                    deal_score += savings_pct * 3.5  # ±2 года
                elif _lvl == "medium":
                    deal_score += savings_pct * 3.0  # ±3 года
                elif _lvl == "model":
                    deal_score += savings_pct * 1.5
                else:  # "wide"
                    deal_score += savings_pct * 2.5  # ↓ ±5 лет — грубо, вес ниже

                # БОНУС за размер выборки — много аналогов = точнее рынок = надёжнее скидка
                if _n >= 15:
                    deal_score += 20.0
                elif _n >= 10:
                    deal_score += 12.0
                elif _n >= 7:
                    deal_score += 6.0
                elif _n >= 5:
                    deal_score += 3.0

        # Оценка самого Авито: если он пометил цену «хорошая»/«ниже рынка» —
        # это сильное подтверждение выгоды, поднимаем; «выше рынка» — штраф.
        _ars = it.get("_avito_rating_score")
        if _ars is not None:
            deal_score += _ars * 12.0

        # Снижение цены: продавец скинул → мотивирован. Помечаем и поднимаем в топе.
        if p > 0:
            _drop = _note_price_drop(it.get("url", ""), p)
            if _drop > 0:
                it["_price_drop"] = _drop
                deal_score += 12.0

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

        # Штраф за битые/не на ходу — сильный (уходят в конец)
        if _text_is_junk(it.get("title", ""), it.get("description", "")):
            deal_score -= 80.0
            it["_is_junk"] = True

        it["_deal_score"] = round(deal_score, 2)
        it["_hot_score"] = round(it.get("_hot_score", 0) + max(0, deal_score), 2)

    _save_price_history()
    return items


def _sort_by_deal(items: list[dict]) -> list[dict]:
    """
    Идеальная сортировка: сначала самые выгодные + висящие дольше.

    Логика:
      Tier 0 — реально ниже рынка (savings_pct >= MARKET_DEAL_MIN_PCT):
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
    def _tier(x) -> int:
        if x.get("_already_seen"):
            return 10
        if _is_strong_below_market(x):
            return 0
        price = int(x.get("_price_int", 0) or 0)
        market = int(x.get("_market_price", 0) or 0)
        if market and price:
            # Known at-market/above-market listings must never outrank unknown cheap finds.
            return 4
        if price > 0 and price <= 80_000 and not x.get("_is_junk"):
            return 1
        if price > 0:
            return 2
        return 3

    def _primary_savings(x) -> float:
        """Главный ключ Tier 0: % скидки от рынка, взвешенный ДОВЕРИЕМ к рынку.
        Скидка −30% из 2 аналогов ненадёжна и не должна бить −25% из 20 аналогов.
        Полное доверие при ~8+ аналогах; для широкого окна (±4 года) доверие ниже."""
        pct = x.get("_savings_pct", 0) or 0
        if x.get("_is_junk"):
            pct -= 100  # битые/не на ходу — в самый низ выгодных
        n = x.get("_market_n", 0) or 0
        conf = min(1.0, n / 8.0)
        if str(x.get("_market_lvl", "")).startswith("wide"):
            conf *= 0.7
        # Никогда не обнуляем скидку полностью (0.5 — минимум), но хорошо
        # подкреплённые сделки поднимаются выше шатких.
        return pct * (0.5 + 0.5 * conf)

    def _secondary(x) -> float:
        """Тайбрейкер при одинаковой скидке: дольше висит + срочность."""
        days = x.get("_days_on_site", 0) or 0
        pct = x.get("_savings_pct", 0) or 0
        hot = 10.0 if (x.get("_deal_score", 0) - pct * 3) > 10 else 0.0
        return min(days, 90) * 0.5 + hot

    def _deal_rank(x) -> float:
        """Композитный рейтинг сделки для Tier 0 (больше — выше):
        1) глубина скидки % (с учётом доверия к рынку) — основной сигнал;
        2) абсолютная выгода в рублях — −25% на дорогой машине ценнее −40% на дешёвой;
        3) свежесть — среди равных свежие объявления чуть выше (успеть первым)."""
        pct = float(x.get("_savings_pct", 0) or 0)
        base = pct * 10.0
        market = x.get("_market_price", 0) or 0
        price = x.get("_price_int", 0) or 0
        rub_bonus = 0.0
        if market and price and market > price:
            rub_bonus = min((market - price) / 25_000.0, 25.0)
        days = x.get("_days_on_site", 0) or 0
        fresh_bonus = 6.0 if days <= 1 else (3.0 if days <= 3 else 0.0)
        cheap_bonus = 0.0
        if price:
            cheap_bonus = max(0.0, min((120_000 - price) / 20_000.0, 6.0))
        if x.get("_is_junk"):
            rub_bonus = fresh_bonus = cheap_bonus = 0.0
        return base + rub_bonus + fresh_bonus + cheap_bonus

    def _no_photo(x) -> int:
        """0 — есть фото (выше), 1 — без фото (в конец своего тира)."""
        return 0 if (x.get("_photo_url") or x.get("photo_url") or x.get("_photos", 0) > 0) else 1

    # Порядок ключей:
    #   1) тир (ниже рынка → по рынку → без цены → просмотренные),
    #   2) наличие фото (объявления без фото падают вниз своего тира),
    #   3) внутри Tier 0 — СТРОГО по величине скидки от рынка (самые выгодные вверху),
    #      при почти равной скидке — кто дольше висит/срочная продажа.
    #   Никакого округления в «полки»: −30% всегда выше −5%.
    items.sort(key=lambda x: (
        1 if x.get("_already_seen") else 0,
        x.get("_days_on_site", 999),
        _tier(x),
        _no_photo(x),
        (x.get("_days_on_site", 999), -round(_deal_rank(x), 1), -_secondary(x))
        if _tier(x) == 0
        else (
            x.get("_days_on_site", 999),
            x.get("_price_int", 999_999_999),
        ),
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


def scrape_drom(region: str, pages: int = 15, price_min: int = 0, price_max: int = 99_000_000, brand: str = "") -> list[dict]:
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

    # Пробуем несколько маршрутов Дрома: на Railway городской субдомен иногда
    # отдаёт пустую/защитную страницу, а auto.drom.ru/<city>/ продолжает работать.
    base = f"https://{region}.drom.ru"
    base_auto = "https://auto.drom.ru"
    # Марка: Дром использует путь /lada/all/ вместо /auto/all/
    _brand_l = (brand or "").strip().lower()
    _DROM_SLUG = {"mercedes": "mercedes-benz", "land rover": "land_rover", "alfa": "alfa_romeo"}
    _drom_seg = _DROM_SLUG.get(_brand_l, _brand_l) if _brand_l and _brand_l != "any" else "auto"

    _drom_proxies = _avito_proxies() if AVITO_PROXIES else None
    _drom_params = {}
    if price_min > 0:
        _drom_params["minprice"] = price_min
    if price_max < 99_000_000:
        _drom_params["maxprice"] = price_max

    def _fetch_drom_html(p: int) -> str:
        """Скачивает HTML страницы Дрома (с прокси-фолбэком). Для параллельной загрузки."""
        city_url = f"{base}/{_drom_seg}/all/" if p == 1 else f"{base}/{_drom_seg}/all/page{p}/"
        auto_city_url = f"{base_auto}/{region}/{_drom_seg}/all/" if p == 1 else f"{base_auto}/{region}/{_drom_seg}/all/page{p}/"
        geo_url = f"{base_auto}/{_drom_seg}/all/" if p == 1 else f"{base_auto}/{_drom_seg}/all/page{p}/"
        candidates = [(city_url, dict(_drom_params)), (auto_city_url, dict(_drom_params))]
        if DROM_GEO.get(region):
            _geo_params = dict(_drom_params)
            _geo_params["geo"] = DROM_GEO[region]
            candidates.append((geo_url, _geo_params))
        best_html = ""
        try:
            for url, params in candidates:
                r = session.get(url, params=params, timeout=7)
                html = r.text
                if len(html) > len(best_html):
                    best_html = html
                if "bulls-list_bull" in html or "data-ftid=\"bull_title\"" in html:
                    return html
            if _drom_proxies:
                import requests as _rq_d
                for url, params in candidates:
                    _r2 = _rq_d.get(url, params=params, timeout=7, proxies=_drom_proxies,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                                 "Accept-Language": "ru-RU,ru;q=0.9"})
                    html = _r2.text
                    if len(html) > len(best_html):
                        best_html = html
                    if "bulls-list_bull" in html or "data-ftid=\"bull_title\"" in html:
                        return html
            return best_html
        except Exception as e:
            print(f"  [Дром {region}] стр.{p}: {str(e)[:50]}")
            return ""

    # Грузим все страницы ПАРАЛЛЕЛЬНО (раньше было последовательно — медленно)
    from concurrent.futures import ThreadPoolExecutor as _TPE_DROM
    with _TPE_DROM(max_workers=min(8, pages)) as _dex:
        _drom_htmls = list(_dex.map(_fetch_drom_html, range(1, pages + 1)))

    for p, _html in enumerate(_drom_htmls, 1):
        if not _html:
            continue
        try:
            soup = _BS(_html, "lxml")
            cards = soup.select("div[data-ftid='bulls-list_bull']")
            print(f"  [Дром {region}] стр.{p}: {len(cards)} карточек")
            if not cards:
                continue

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

                    desc_el = (
                        card.select_one("[data-ftid='bull_description']")
                        or card.select_one("p[class*='description']")
                        or card.select_one("div[class*='description']")
                        or card.select_one("span[class*='description']")
                    )
                    desc = desc_el.get_text(strip=True) if desc_el else ""
                    # Если нет описания — собираем из параметров карточки
                    if not desc:
                        params_els = card.select("[data-ftid='bull_tags'] span, [class*='attributes'] span, [class*='params'] span")
                        if params_els:
                            desc = " · ".join(el.get_text(strip=True) for el in params_els if el.get_text(strip=True))[:300]

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
        except Exception as e:
            print(f"  [Дром {region}] парс стр.{p}: {e}")
            continue

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
        # Новые форматы Auto.ru API 2025
        or (data.get("response", {}) or {}).get("offers", [])
        or (data.get("data", {}) or {}).get("offers", [])
        or (data.get("result", {}) or {}).get("offers", [])
        or (data.get("listing", {}) or {}).get("offers", [])
    )
    if isinstance(listing, dict):
        listing = (
            listing.get("offers", [])
            or listing.get("items", [])
            or listing.get("results", [])
        )
    # Auto.ru регулярно переносит offers глубже в JSON, не меняя сами объекты
    # объявлений. Не привязываемся только к семи известным путям: если они
    # пусты, находим offer-объекты по их устойчивым полям.
    if not listing:
        listing = []
        stack = [data]
        visited: set[int] = set()
        while stack and len(visited) < 100_000:
            node = stack.pop()
            if not isinstance(node, (dict, list)):
                continue
            node_id = id(node)
            if node_id in visited:
                continue
            visited.add(node_id)
            if isinstance(node, list):
                stack.extend(node)
                continue
            has_vehicle = "vehicle_info" in node or "vehicleInfo" in node
            has_price = "price_info" in node or "priceInfo" in node
            has_identity = bool(node.get("url") or node.get("sale_url") or node.get("id"))
            if has_vehicle and has_price and has_identity:
                listing.append(node)
                continue
            stack.extend(node.values())

    seen_urls: set[str] = set()
    for offer in listing:
        try:
            if not isinstance(offer, dict):
                continue
            vehicle = offer.get("vehicle_info", {}) or offer.get("vehicleInfo", {}) or {}
            mark_info = vehicle.get("mark_info", {}) or vehicle.get("markInfo", {}) or {}
            model_info = vehicle.get("model_info", {}) or vehicle.get("modelInfo", {}) or {}
            mark = mark_info.get("name", "") or mark_info.get("code", "")
            model = model_info.get("name", "") or model_info.get("code", "")
            documents = offer.get("documents", {}) or {}
            year = documents.get("year", "") or vehicle.get("year", "") or offer.get("year", "")
            title = f"{mark} {model} {year}".strip()
            price_info = offer.get("price_info", {}) or offer.get("priceInfo", {}) or {}
            price_val = (
                price_info.get("price", "")
                or price_info.get("value", "")
                or offer.get("price", "")
            )
            if isinstance(price_val, dict):
                price_val = price_val.get("value", "") or price_val.get("amount", "")
            price_str = f"{int(price_val):,} ₽".replace(",", " ") if price_val else ""
            item_url = offer.get("url", "") or offer.get("sale_url", "")
            if item_url and item_url.startswith("/"):
                item_url = "https://auto.ru" + item_url
            if not item_url and offer.get("id"):
                item_url = f"https://auto.ru/cars/used/sale/{offer.get('id', '')}"
            if item_url in seen_urls:
                continue
            seller_type = offer.get("seller_type", "") or offer.get("sellerType", "")
            if str(seller_type).upper() == "COMMERCIAL":
                continue
            photos_list = offer.get("photos", []) or offer.get("images", []) or []
            photo_url = ""
            if photos_list:
                first_photo = photos_list[0] if isinstance(photos_list[0], dict) else {}
                sizes = first_photo.get("sizes", {}) or {}
                photo_url = (
                    sizes.get("1200x900") or sizes.get("832x624")
                    or sizes.get("456x342") or first_photo.get("url", "") or ""
                )
            days = 0
            additional = offer.get("additional_info", {}) or offer.get("additionalInfo", {}) or {}
            date_str = additional.get("creation_date", "") or additional.get("creationDate", "")
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
            if title and item_url and price_val:
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
                seen_urls.add(item_url)
        except Exception:
            pass
    return results


def _autoru_is_captcha(text: str) -> bool:
    """True, если ответ Auto.ru — капча-заглушка Яндекса, а не страница с авто."""
    t = (text or "")[:4000].lower()
    return (
        "captcha" in t or "вы не робот" in t or "проверка, что вы не робот" in t
        or "smartcaptcha" in t or "доступ ограничен" in t or "too-many-requests" in t
    )


def _autoru_parse_html(text: str, today) -> list[dict]:
    """Извлекает объявления из HTML Auto.ru (__INITIAL_STATE__ или regex)."""
    results = []

    # Метод 1: window.__INITIAL_STATE__ и другие встроенные JSON-блоки
    for marker in ("window.__INITIAL_STATE__=", "window.__INITIAL_STATE__ =",
                   "window.AUTOCART_STATE=", "window.AUTOCART_STATE =",
                   "__NEXT_DATA__"):
        if marker == "__NEXT_DATA__":
            # Для Next.js страниц Auto.ru (новый формат 2025)
            nd_m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S)
            if nd_m:
                try:
                    data = json.loads(nd_m.group(1))
                    found = _autoru_parse_offers(data, today)
                    if found:
                        print(f"  [Auto.ru] __NEXT_DATA__: {len(found)} объявлений")
                        return found
                except Exception as e:
                    print(f"  [Auto.ru] __NEXT_DATA__ json error: {e}")
            continue
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
                print(f"  [Auto.ru] {marker}: {len(found)} объявлений")
                return found
        except Exception as e:
            print(f"  [Auto.ru] {marker} json error: {e}")

    # Метод 2: карточки уже отрисованной HTML-страницы. Auto.ru может убрать
    # JSON-state, но оставляет ссылки /cars/used/sale/ и видимые цену/название.
    try:
        from bs4 import BeautifulSoup as _AutoSoup

        soup = _AutoSoup(text, "lxml")
        dom_seen: set[str] = set()
        price_visible_re = re.compile(
            r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d{4,9})\s*₽"
        )
        year_visible_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")
        for link in soup.select(
            'a[href*="/cars/used/sale/"], a[href*="/cars/new/sale/"]'
        ):
            href = (link.get("href") or "").replace("\\/", "/")
            if href.startswith("/"):
                href = "https://auto.ru" + href
            if not href.startswith("http"):
                continue
            item_url = href.split("?")[0].rstrip("/") + "/"
            if item_url in dom_seen:
                continue

            # Поднимаемся от ссылки до самой маленькой оболочки, содержащей
            # видимую цену. Это переживает смену CSS-классов ListingItem.
            card = link
            card_text = ""
            price_match = None
            for _ in range(9):
                card = getattr(card, "parent", None)
                if card is None:
                    break
                candidate_text = card.get_text(" ", strip=True)
                if len(candidate_text) > 10_000:
                    break
                candidate_price = price_visible_re.search(candidate_text)
                if candidate_price:
                    card_text = candidate_text
                    price_match = candidate_price
                    break
            if not card or not price_match:
                continue
            price_val = int(re.sub(r"\D", "", price_match.group(1)) or 0)
            if not (10_000 <= price_val <= 99_000_000):
                continue

            title = ""
            title_el = card.select_one(
                '[class*="ListingItemTitle"], [data-ftid*="bull_title"], h3, h2'
            )
            if title_el:
                title = title_el.get_text(" ", strip=True)
            if not title:
                title = (link.get("aria-label") or link.get("title") or "").strip()
            image = card.select_one("img")
            if not title and image:
                title = (image.get("alt") or image.get("title") or "").strip()

            path = item_url.split("/sale/", 1)[-1].strip("/").split("/")
            if not title:
                path_mark = path[0].replace("_", " ").title() if path else "Авто"
                path_model = path[1].replace("_", " ").title() if len(path) > 1 else ""
                title = f"{path_mark} {path_model}".strip()
            year_m = year_visible_re.search(title) or year_visible_re.search(card_text)
            year = int(year_m.group(1)) if year_m else 0
            if year and str(year) not in title:
                title = f"{title}, {year}"

            photo_url = ""
            if image:
                photo_url = (
                    image.get("src") or image.get("data-src")
                    or image.get("data-lazy-src") or ""
                )
                if not photo_url and image.get("srcset"):
                    photo_url = image.get("srcset", "").split(",")[0].strip().split(" ")[0]
            if photo_url.startswith("//"):
                photo_url = "https:" + photo_url

            days = 0
            text_lower = card_text.lower()
            date_known = False
            if "сегодня" in text_lower:
                date_known = True
            elif "вчера" in text_lower:
                days, date_known = 1, True
            else:
                days_m = re.search(r"\b(\d{1,3})\s+(?:дн|день|дня|дней)\b", text_lower)
                if days_m:
                    days, date_known = int(days_m.group(1)), True

            item = {
                "source": "autoru", "title": title[:160],
                "price": f"{price_val:,} ₽".replace(",", " "),
                "url": item_url,
                "date": str(today - datetime.timedelta(days=days)),
                "_photos": 1 if photo_url else 0, "_days_on_site": days,
                "description": card_text[:400], "seller": "",
                "_photo_url": photo_url, "_price_int": price_val,
                "_year": year, "_date_known": date_known,
            }
            item["_hot_score"] = hot_score(item)
            results.append(item)
            dom_seen.add(item_url)
        if results:
            print(f"  [Auto.ru] DOM HTML: {len(results)} объявлений")
    except Exception as exc:
        print(f"  [Auto.ru] DOM parse: {str(exc)[:80]}")

    # Метод 3: устойчивый разбор URL-карточек в сыром HTML/JSON. В новом HTML
    # Auto.ru цена часто находится ПЕРЕД url, а ссылки бывают относительными и
    # с JSON-экранированием. Старый шаблон искал только url → price и поэтому
    # объединял всю страницу в один матч, возвращая ровно одно объявление.
    normalized = (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
    )
    seen_urls: set[str] = {item.get("url", "") for item in results if item.get("url")}
    url_re = re.compile(
        r'(?:(?:https?:)?//(?:www\.)?auto\.ru)?'
        r'/cars/(?:used|new)/sale/[a-z0-9_./%+-]{8,180}',
        re.I,
    )

    def _nearest_match(pattern: str, context: str, anchor: int):
        matches = list(re.finditer(pattern, context, re.I | re.S))
        return min(matches, key=lambda match: abs(match.start() - anchor)) if matches else None

    def _json_text(raw: str) -> str:
        try:
            return str(json.loads(f'"{raw}"'))
        except Exception:
            return raw.replace("\\n", " ").replace('\\"', '"').strip()

    brand_names = {
        "vaz": "ВАЗ (Lada)", "lada": "Lada", "gaz": "ГАЗ",
        "uaz": "УАЗ", "moskvich": "Москвич",
    }

    for m in url_re.finditer(normalized):
        raw_url = m.group(0)
        path_start = raw_url.find("/cars/")
        if path_start < 0:
            continue
        item_url = "https://auto.ru" + raw_url[path_start:]
        item_url = item_url.split("?")[0].rstrip("/.,") + "/"
        if item_url in seen_urls:
            continue

        ctx_start = max(0, m.start() - 7_000)
        ctx_end = min(len(normalized), m.end() + 7_000)
        ctx = normalized[ctx_start:ctx_end]
        anchor = m.start() - ctx_start

        _price_pattern = (
            r'"price"\s*:\s*(?:\{\s*"(?:value|amount)"\s*:\s*)?"?(\d{4,9})'
        )
        # В карточках Auto.ru price_info расположен перед url. Предпочитаем
        # последний price перед ссылкой: иначе цена следующей карточки, стоящая
        # сразу после текущего URL, ошибочно приклеивается к текущей машине.
        _price_matches = list(re.finditer(_price_pattern, ctx, re.I | re.S))
        _price_before = [match for match in _price_matches if match.start() < anchor]
        price_m = (
            max(_price_before, key=lambda match: match.start())
            if _price_before
            else (_nearest_match(_price_pattern, ctx, anchor) if _price_matches else None)
        )
        if not price_m:
            price_m = _nearest_match(
                r'"priceInfo"\s*:\s*\{.{0,500}?"(?:value|price)"\s*:\s*"?(\d{4,9})',
                ctx,
                anchor,
            )
        price_val = int(price_m.group(1)) if price_m else 0
        if not (10_000 <= price_val <= 99_000_000):
            continue

        mark_m = _nearest_match(
            r'"(?:mark_info|markInfo)"\s*:\s*\{.{0,600}?"(?:name|code)"\s*:\s*"([^"]+)"',
            ctx,
            anchor,
        )
        model_m = _nearest_match(
            r'"(?:model_info|modelInfo)"\s*:\s*\{.{0,600}?"(?:name|code)"\s*:\s*"([^"]+)"',
            ctx,
            anchor,
        )
        year_m = _nearest_match(r'"year"\s*:\s*"?(\d{4})', ctx, anchor)
        path = item_url.split("/sale/", 1)[-1].strip("/").split("/")
        path_mark = path[0].replace("_", " ") if path else ""
        path_model = path[1].replace("_", " ") if len(path) > 1 else ""
        mark = _json_text(mark_m.group(1)) if mark_m else brand_names.get(path_mark, path_mark.title())
        model = _json_text(model_m.group(1)) if model_m else path_model.title()
        year = year_m.group(1) if year_m else ""
        if not (mark or model or year):
            continue
        title = f"{mark} {model} {year}".strip()
        price_str = f"{price_val:,} ₽".replace(",", " ")
        # Фото и описание тоже берём ближайшие к URL, а не первые на странице.
        photo_m = _nearest_match(r'"1200x900"\s*:\s*"([^"]+)"', ctx, anchor)
        if not photo_m:
            photo_m = _nearest_match(
                r'"(?:832x624|456x342|320x240)"\s*:\s*"([^"]+)"',
                ctx,
                anchor,
            )
        if not photo_m:
            photo_m = _nearest_match(
                r'"((?:https?:)?//avatars\.mds\.yandex\.net/[^"]{10,})"',
                ctx,
                anchor,
            )
        photo_url = photo_m.group(1).replace("\\/", "/") if photo_m else ""
        if photo_url.startswith("//"):
            photo_url = "https:" + photo_url
        desc_m = _nearest_match(r'"description"\s*:\s*"([^"]{10,800})"', ctx, anchor)
        desc = _json_text(desc_m.group(1))[:400] if desc_m else ""
        date_m = _nearest_match(
            r'"(?:creation_date|creationDate)"\s*:\s*"(\d{4}-\d{2}-\d{2})',
            ctx,
            anchor,
        )
        days = 0
        if date_m:
            try:
                created = datetime.datetime.fromisoformat(date_m.group(1)).date()
                days = max(0, (today - created).days)
            except Exception:
                pass
        item = {
            "source": "autoru", "title": title, "price": price_str,
            "url": item_url, "date": str(today - datetime.timedelta(days=days)),
            "_photos": 1 if photo_url else 0, "_days_on_site": days,
            "description": desc, "seller": "", "_photo_url": photo_url,
            "_price_int": price_val, "_year": int(year) if year else 0,
            "_date_known": bool(date_m),
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)
        seen_urls.add(item_url)

    if results:
        print(f"  [Auto.ru] regex HTML: {len(results)} объявлений")
    return results


def _autoru_search_fallback(
    region: str,
    price_min: int,
    price_max: int,
    brand: str = "",
    limit: int = 12,
) -> list[dict]:
    """Быстрый резерв Auto.ru через уже проиндексированные страницы поиска.

    Используется только когда Auto.ru закрыл каталог капчей. Возвращает реальные
    ссылки auto.ru с ценой из сниппета, чтобы источник не становился полностью
    пустым из-за одного заблокированного IP.
    """
    try:
        import requests as _req
        import urllib.parse as _up
    except ImportError:
        return []

    region_name = REGIONS.get(region, region)
    brand_hint = f" {brand}" if brand and brand != "any" else ""
    price_hint = f" до {price_max} руб" if price_max < 99_000_000 else ""
    query = f"site:auto.ru/cars/used/sale/ {region_name}{brand_hint}{price_hint}"
    try:
        response = _req.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "ru-ru"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            timeout=7,
        )
        if response.status_code != 200 or len(response.text) < 1_000:
            return []
        text = response.text
        for _ in range(2):
            decoded = _up.unquote(text)
            if decoded == text:
                break
            text = decoded
    except Exception as exc:
        print(f"  [Auto.ru fallback] поиск: {str(exc)[:60]}")
        return []

    url_re = re.compile(
        r"https?://(?:www\.)?auto\.ru/cars/used/sale/"
        r"[a-z0-9_.%-]+/[a-z0-9_.%-]+/[a-z0-9_.%-]+/?",
        re.I,
    )
    price_re = re.compile(
        r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d{4,9})\s*(?:₽|руб\.?)",
        re.I,
    )
    year_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")
    today = datetime.date.today()
    out: list[dict] = []
    seen_urls: set[str] = set()

    for match in url_re.finditer(text):
        url = match.group(0).split("?")[0].rstrip("/") + "/"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # У DDG цена и описание идут после целевой ссылки. Не захватываем текст
        # предыдущего результата или сам поисковый запрос с верхней ценой.
        context = html.unescape(re.sub(
            r"\s+",
            " ",
            re.sub(r"<[^>]+>", " ", text[match.start():match.end() + 1_200]),
        )).strip()
        price = 0
        for price_match in price_re.finditer(context):
            value = int(re.sub(r"\D", "", price_match.group(1)) or 0)
            if 10_000 <= value <= 99_000_000:
                price = value
                break
        if not price or not (price_min <= price <= price_max):
            continue

        path = url.split("/sale/", 1)[-1].strip("/").split("/")
        brand_name = path[0].replace("_", " ").title() if path else "Авто"
        model_name = path[1].replace("_", " ").title() if len(path) > 1 else ""
        year_match = year_re.search(context)
        year = int(year_match.group(1)) if year_match else 0
        title = " ".join(x for x in (brand_name, model_name, str(year or "")) if x).strip()
        item = {
            "source": "autoru",
            "title": title or "Автомобиль с Auto.ru",
            "price": f"{price:,} ₽".replace(",", " "),
            "_price_int": price,
            "url": url,
            "_photo_url": "",
            "description": context[:400],
            "seller": "Auto.ru",
            "_year": year,
            "_days_on_site": 0,
            "_photos": 0,
            "mileage": 0,
            "date": str(today),
        }
        item["_hot_score"] = hot_score(item)
        out.append(item)
        if len(out) >= limit:
            break

    if out:
        print(f"  [Auto.ru fallback] {len(out)} объявлений из поискового индекса")
    return out


def scrape_autoru(
    region: str,
    pages: int = 10,
    price_min: int = 0,
    price_max: int = 99_000_000,
    brand: str = "",
    deadline_sec: int | None = None,
) -> list[dict]:
    slug = AUTORU_SLUGS.get(region, region)
    geo_ids = AUTORU_GEO_IDS.get(region, [])
    try:
        import requests as _req
    except ImportError:
        return []

    results = []
    today = datetime.date.today()
    # Жёсткий дедлайн: Auto.ru капча-защищён и часто виснет — не даём тормозить весь
    # поиск. Держим короткий бюджет: если IP чистый — успеваем, если капча — быстро выходим.
    _ar_deadline = time.time() + (
        max(8, int(deadline_sec)) if deadline_sec is not None else SEARCH_AUTORU_DEADLINE_SEC
    )
    _ar_empty_streak = 0
    # Марка для Auto.ru: путь /cars/lada/used/ и catalog_filter mark=LADA
    _brand_l = (brand or "").strip().lower()
    _AR_SLUG = {"land rover": "land_rover", "alfa": "alfa_romeo"}
    _brand_slug = _AR_SLUG.get(_brand_l, _brand_l)
    _brand_path = f"{_brand_slug}/" if _brand_l and _brand_l != "any" else ""

    # Создаём сессию и прогреваем куки через GET запрос страницы листинга
    # Auto.ru требует куки сессии для AJAX — без них возвращает пустой ответ
    _ar_session = _req.Session()
    _ar_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    _ar_base_url = f"https://auto.ru/{slug}/cars/{_brand_path}used/?seller_group=PRIVATE"
    if price_min > 0:
        _ar_base_url += f"&price_from={price_min}"
    if price_max < 99_000_000:
        _ar_base_url += f"&price_to={price_max}"
    _warm_html = ""
    _warm_status = 0
    # Если задан выделенный РФ-пул (AUTORU_PROXIES) — прогрев через общий мобильный
    # прокси НЕ делаем: он всё равно ловит капчу и лишь тратит 6-11с, замедляя весь
    # поиск. Сразу идём в цикл, где Метод 0* берёт объявления через РФ-прокси.
    # 1) curl_cffi (Chrome TLS-отпечаток) через прокси — ЛУЧШИЙ обход анти-бота
    #    Яндекса, который проверяет TLS-fingerprint. Обычный requests почти всегда
    #    ловит капчу, а curl_cffi проходит чаще.
    if not AUTORU_PROXIES:
        try:
            from curl_cffi import requests as _cffi_ar
            _rc = _cffi_ar.get(
                _ar_base_url, impersonate="chrome124", timeout=6,
                headers={"Accept-Language": "ru-RU,ru;q=0.9",
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                         "Referer": "https://auto.ru/", "Upgrade-Insecure-Requests": "1"},
                proxies=_avito_proxies() or {},
            )
            _warm_html, _warm_status = _rc.text, _rc.status_code
            # Переносим куки (spravka и т.п.) в requests-сессию для AJAX-фолбэка
            try:
                for _k, _v in _rc.cookies.get_dict().items():
                    _ar_session.cookies.set(_k, _v)
            except Exception:
                pass
            print(f"  [Auto.ru] curl_cffi прогрев: HTTP {_warm_status}, {len(_warm_html):,}б")
        except Exception as _ec:
            print(f"  [Auto.ru] curl_cffi прогрев: {str(_ec)[:60]}")
        # 2) обычный requests — запасной, если curl_cffi не дал страницу
        if len(_warm_html) < 5_000:
            try:
                _warm = _ar_session.get(_ar_base_url, headers={
                    "User-Agent": _ar_ua,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                }, proxies=_avito_proxies(), timeout=5)
                _warm_html, _warm_status = _warm.text, _warm.status_code
            except Exception as _e:
                print(f"  [Auto.ru] requests прогрев: {str(_e)[:60]}")
    _wl = _warm_html.lower()
    # Определяем, капча ли прогрев (для решения, парсить ли HTML-страницу).
    # НО не выходим — AJAX-методы могут сработать даже при капче на HTML.
    _warm_blocked = bool(_wl) and (
        "captcha" in _wl or "проверка, что вы не робот" in _wl
        or "too-many-requests" in _wl or "доступ ограничен" in _wl
        or _warm_status in (429, 403)
    )
    # Парсим прогрев, если это не капча (мобильная страница бывает и <50К)
    if not _warm_blocked and len(_warm_html) > 3_000:
        _warm_items = _autoru_parse_html(_warm_html, today)
        if _warm_items:
            print(f"  [Auto.ru] прогрев дал {len(_warm_items)} объявлений")
            results.extend(_warm_items)

    if results:
        _remember_source_results("autoru", region, results)
        return results
    # ВАЖНО: даже если прогрев поймал капчу — НЕ выходим. AJAX-методы (мобильный
    # API + desktop AJAX через прокси) часто работают, когда HTML-страница
    # отдаёт капчу. Раньше ранний return убивал их — Auto.ru искал мало.
    # Если прогрев поймал капчу Яндекса и НЕТ выделенного РФ-пула для Auto.ru —
    # как крайняя мера меняем IP общего мобильного прокси. ВАЖНО: не форсируем и
    # только при отсутствии AUTORU_PROXIES, иначе ротация общего IP ломает Авито
    # (спам «Already change IP» и смена IP у Авито в середине поиска).
    if _warm_blocked and AVITO_PROXIES and not AUTORU_PROXIES:
        if _rotate_proxy_ip():
            try:
                from curl_cffi import requests as _cffi_ar2
                _rc2 = _cffi_ar2.get(
                    _ar_base_url, impersonate="chrome124", timeout=6,
                    headers={"Accept-Language": "ru-RU,ru;q=0.9",
                             "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                             "Referer": "https://auto.ru/", "Upgrade-Insecure-Requests": "1"},
                    proxies=_avito_proxies() or {},
                )
                _warm_html, _warm_status = _rc2.text, _rc2.status_code
                try:
                    for _k, _v in _rc2.cookies.get_dict().items():
                        _ar_session.cookies.set(_k, _v)
                except Exception:
                    pass
                print(f"  [Auto.ru] прогрев после ротации IP: HTTP {_warm_status}, {len(_warm_html):,}б")
                if len(_warm_html) > 50_000:
                    _warm_items2 = _autoru_parse_html(_warm_html, today)
                    if _warm_items2:
                        print(f"  [Auto.ru] прогрев (ротация) дал {len(_warm_items2)} объявлений")
                        results.extend(_warm_items2)
                        _remember_source_results("autoru", region, results)
                        return results
            except Exception as _ec2:
                print(f"  [Auto.ru] прогрев после ротации: {str(_ec2)[:60]}")

    # Метод 1: AJAX API Auto.ru с прогретой сессией (возвращает JSON)
    headers_ajax = {
        "User-Agent": _ar_ua,
        "Accept": "application/json,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://auto.ru",
        "Referer": f"https://auto.ru/{slug}/cars/used/",
        "x-client-app": "autoru-frontend-application",
        "x-page-request-id": "ajax",
    }

    for p in range(1, pages + 1):
        if time.time() > _ar_deadline:  # не превышаем общий лимит Auto.ru
            print(f"  [Auto.ru] дедлайн {p-1} стр. — выходим")
            break
        body: dict = {
            "category": "cars", "section": "used",
            "seller_type": ["PRIVATE"], "page": p, "page_size": 50,
            "sort": "fresh_relevance_1-desc",
            "output_type": "list",
        }
        if geo_ids:
            body["geo_id"] = geo_ids
        if price_min > 0:
            body["price_from"] = price_min
        if price_max < 99_000_000:
            body["price_to"] = price_max
        if _brand_l and _brand_l != "any":
            body["catalog_filter"] = [{"mark": _brand_slug.upper()}]

        batch = []
        html_url = f"https://auto.ru/{slug}/cars/{_brand_path}used/?seller_group=PRIVATE&page={p}&sort=fresh_relevance_1-desc"
        if price_min > 0:
            html_url += f"&price_from={price_min}"
        if price_max < 99_000_000:
            html_url += f"&price_to={price_max}"

        # Метод 00: Официальный API приложения (apiauto.ru) — отдаёт ЧИСТЫЙ JSON
        # без капчи Яндекса. Работает, только если задан AUTORU_API_TOKEN
        # (заголовок x-authorization из приложения ru.auto.ara). Самый надёжный
        # путь: desktop-версия капча-стеной режет всё, а этот API — нет.
        if not batch and AUTORU_API_TOKEN:
            try:
                _api_body: dict = {
                    "category": "cars", "section": "USED",
                    "seller_group": ["PRIVATE"],
                }
                if geo_ids:
                    _api_body["geo_id"] = geo_ids
                if price_min > 0:
                    _api_body["price_from"] = price_min
                if price_max < 99_000_000:
                    _api_body["price_to"] = price_max
                if _brand_l and _brand_l != "any":
                    _api_body["catalog_filter"] = [{"mark": _brand_slug.upper()}]
                _api_r = _req.post(
                    "https://apiauto.ru/1.0/search/cars",
                    params={"context": "listing", "sort": "fresh_relevance_1-desc",
                            "page": p, "page_size": 50},
                    json=_api_body,
                    headers={
                        "x-authorization": AUTORU_API_TOKEN,
                        "User-Agent": "ru.auto.ara/11.6.0 (Android)",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "x-client-app": "ru.auto.ara",
                    },
                    proxies=_avito_proxies(),
                    timeout=8,
                )
                print(f"  [Auto.ru] apiauto стр.{p}: HTTP {_api_r.status_code}, {len(_api_r.text):,}б")
                if _api_r.status_code == 200:
                    try:
                        batch = _autoru_parse_offers(_api_r.json(), today)
                    except Exception:
                        pass
            except Exception as e:
                print(f"  [Auto.ru] apiauto API: {str(e)[:80]}")

        # Метод 0*: пул РФ-прокси через curl_cffi (Chrome TLS-отпечаток).
        # Ключевой момент: Яндекс режет капчей по ДВУМ признакам — «грязный» IP
        # И TLS-отпечаток. Чистый РФ IP + обычный python-requests всё равно ловит
        # капчу, потому что отпечаток не браузерный. curl_cffi (impersonate chrome)
        # даёт браузерный TLS → чистый РФ IP + браузерный отпечаток = проходит.
        # Сначала GET страницы листинга (греет cookie spravka), затем берём
        # объявления прямо из HTML (__NEXT_DATA__) или добиваем AJAX-ом.
        if not batch and AUTORU_PROXIES:
            try:
                from curl_cffi import requests as _cffi_ru
            except Exception:
                _cffi_ru = None
            for _arp in _autoru_proxy_dicts():
                if time.time() > _ar_deadline:
                    break
                _phost = _arp.get("https", "").split("@")[-1]
                # 1) curl_cffi (браузерный TLS) — основной путь
                if _cffi_ru is not None:
                    try:
                        _sess_ru = _cffi_ru.Session()
                        _gh = _sess_ru.get(
                            html_url, impersonate="chrome124", timeout=6,
                            headers={"Accept-Language": "ru-RU,ru;q=0.9",
                                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                     "Referer": f"https://auto.ru/{slug}/cars/used/",
                                     "Upgrade-Insecure-Requests": "1"},
                            proxies=_arp,
                        )
                        print(f"  [Auto.ru] РФ-прокси(cffi) {_phost} стр.{p}: HTTP {_gh.status_code}, {len(_gh.text):,}б")
                        if _gh.status_code == 200 and not _autoru_is_captcha(_gh.text):
                            batch = _autoru_parse_html(_gh.text, today)
                        # добиваем AJAX-ом через ту же прогретую сессию (если есть время)
                        if not batch and not _autoru_is_captcha(_gh.text) and time.time() < _ar_deadline:
                            _aj = _sess_ru.post(
                                "https://auto.ru/-/ajax/desktop/listing/",
                                json=body, impersonate="chrome124", timeout=5,
                                headers={**headers_ajax, "x-requested-with": "fetch"},
                                proxies=_arp,
                            )
                            if _aj.status_code == 200 and not _autoru_is_captcha(_aj.text):
                                try:
                                    batch = _autoru_parse_offers(_aj.json(), today)
                                except Exception:
                                    batch = _autoru_parse_html(_aj.text, today)
                        if batch:
                            print(f"  [Auto.ru] РФ-прокси {_phost}: {len(batch)} объявлений ✅")
                            break
                    except Exception as e:
                        print(f"  [Auto.ru] РФ-прокси(cffi) {_phost}: {str(e)[:60]}")
                # 2) запасной путь — обычный requests (если curl_cffi недоступен)
                if not batch and _cffi_ru is None:
                    try:
                        _rp = _req.post(
                            "https://auto.ru/-/ajax/desktop/listing/",
                            json=body,
                            headers={**headers_ajax, "x-requested-with": "fetch"},
                            proxies=_arp, timeout=6,
                        )
                        print(f"  [Auto.ru] РФ-прокси(req) {_phost} стр.{p}: HTTP {_rp.status_code}, {len(_rp.text):,}б")
                        if _rp.status_code == 200 and not _autoru_is_captcha(_rp.text):
                            try:
                                batch = _autoru_parse_offers(_rp.json(), today)
                            except Exception:
                                batch = _autoru_parse_html(_rp.text, today)
                            if batch:
                                break
                    except Exception as e:
                        print(f"  [Auto.ru] РФ-прокси(req) {_phost}: {str(e)[:60]}")

        # Метод 0а: Прямой AJAX API с общим мобильным прокси. Пропускаем, если
        # есть выделенный РФ-пул (он уже отработал выше и не ловит капчу).
        if not batch and AVITO_PROXIES and not AUTORU_PROXIES:
            try:
                r_ajax = _req.post(
                    "https://auto.ru/-/ajax/desktop/listing/",
                    json=body,
                    headers={**headers_ajax, "x-requested-with": "fetch"},
                    proxies=_avito_proxies(),
                    timeout=4,
                )
                print(f"  [Auto.ru] прокси AJAX стр.{p}: HTTP {r_ajax.status_code}, {len(r_ajax.text):,}б")
                if r_ajax.status_code == 200:
                    try:
                        batch = _autoru_parse_offers(r_ajax.json(), today)
                    except Exception:
                        batch = _autoru_parse_html(r_ajax.text, today)
            except Exception as e:
                print(f"  [Auto.ru] прокси AJAX: {str(e)[:80]}")

        # Метод 0b: Прямой HTML через общий мобильный прокси (пропускаем при РФ-пуле)
        if not batch and AVITO_PROXIES and not AUTORU_PROXIES:
            try:
                r0 = _req.get(html_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": f"https://auto.ru/{slug}/cars/used/",
                }, timeout=4, proxies=_avito_proxies())
                print(f"  [Auto.ru] прокси HTML стр.{p}: HTTP {r0.status_code}, {len(r0.text):,}б")
                # Парсим любой не-капча ответ (мобильная страница может быть <50К)
                if r0.status_code == 200 and not _autoru_is_captcha(r0.text):
                    batch = _autoru_parse_html(r0.text, today)
            except Exception as e:
                print(f"  [Auto.ru] прокси HTML: {str(e)[:50]}")

        # Метод 0c: curl_cffi через мобильный прокси (пропускаем при РФ-пуле)
        if not batch and not AUTORU_PROXIES:
            try:
                from curl_cffi import requests as _cffi
                _cffi_hdrs0 = {
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": f"https://auto.ru/{slug}/cars/used/",
                }
                rc0 = _cffi.get(html_url, impersonate="chrome124", timeout=4, headers=_cffi_hdrs0,
                                proxies=_avito_proxies())
                print(f"  [Auto.ru] curl_cffi стр.{p}: HTTP {rc0.status_code}, {len(rc0.text):,}б")
                if rc0.status_code == 200 and not _autoru_is_captcha(rc0.text):
                    batch = _autoru_parse_html(rc0.text, today)
            except Exception as e:
                print(f"  [Auto.ru] curl_cffi: {str(e)[:80]}")

        # Метод 0d: бесплатные РФ-прокси — не требует настроек. Часть РФ ISP-IP
        # Яндекс НЕ режет капчей (в отличие от дата-центра). Пробуем AJAX (JSON)
        # через несколько таких прокси. Работает даже без мобильного прокси.
        if not batch and _working_free_proxies and time.time() < _ar_deadline:
            for _fp in list(_working_free_proxies)[:2]:
                if time.time() > _ar_deadline:
                    break
                try:
                    _fp_prx = {"http": f"http://{_fp}", "https": f"http://{_fp}"}
                    _rf = _req.post(
                        "https://auto.ru/-/ajax/desktop/listing/",
                        json=body,
                        headers={**headers_ajax, "x-requested-with": "fetch"},
                        proxies=_fp_prx, timeout=3,
                    )
                    if _rf.status_code == 200 and not _autoru_is_captcha(_rf.text):
                        try:
                            batch = _autoru_parse_offers(_rf.json(), today)
                        except Exception:
                            batch = _autoru_parse_html(_rf.text, today)
                        if batch:
                            print(f"  [Auto.ru] free-proxy {_fp}: {len(batch)} объявлений")
                            break
                except Exception:
                    continue

        # Метод 1: ScraperAPI render=true — JS выполняется, __INITIAL_STATE__ заполняется
        if not batch and SCRAPER_API_KEY:
            try:
                r3 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": html_url,
                    "country_code": "ru", "render": "true", "wait": "1500",
                }, timeout=20)
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
                }, timeout=25)
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
                    data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=20
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
            # Одна пустая страница может быть временным сбоем/капчей —
            # прерываемся только после двух пустых подряд. IP общего мобильного
            # прокси НЕ трогаем (это ломает Авито) — для Auto.ru есть свой РФ-пул.
            _ar_empty_streak += 1
            if _ar_empty_streak >= 2:
                break
            time.sleep(0.05)
            continue
        _ar_empty_streak = 0
        results.extend(batch)
        time.sleep(0.05)

    if not results:
        results = _autoru_search_fallback(region, price_min, price_max, brand=brand)
    if not results:
        results = _cached_source_results("autoru", region, price_min, price_max, limit=30)
        if results:
            print(f"  [Auto.ru] восстановлено {len(results)} объявлений из общего кэша")
    _remember_source_results("autoru", region, results)
    print(f"  [Auto.ru] итого {len(results)} объявлений")
    return results


def _scrape_autoru_background(
    region: str,
    pages: int = 2,
    price_min: int = 0,
    price_max: int = 99_000_000,
) -> list[dict]:
    """Do not let monitor regions attack Auto.ru concurrently through one IP."""
    if not _AUTORU_BACKGROUND_LOCK.acquire(blocking=False):
        return _cached_source_results("autoru", region, price_min, price_max, limit=40)
    try:
        return scrape_autoru(
            region,
            pages=pages,
            price_min=price_min,
            price_max=price_max,
            deadline_sec=20,
        )
    finally:
        _AUTORU_BACKGROUND_LOCK.release()


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
        "avtorynok_ekaterinburga", "perek96_965", "ekaterinbyrg_avtorynok",
        "buy_car66", "autorinok_66", "mashiny_v_ekaterinburge",
        "baraholkaekb", "avto196ekb",
    ],
    "moskva": [
        "dnrtnf", "Moscow_AutoTrade", "prodat_kupite",
        "mashiny_v_moskve", "mospodbor", "saleautomsk",
        "AVTOBAZAR_RF",
    ],
    "spb": [
        "mashiny_v_spb", "mashiny_v_spbq", "NizheRynkaSpbfast",
        "mashiny_v_peterburge",
    ],
    "novosibirsk": [
        "mashiny_v_novosibirske", "avtorynok_Novosibirsk", "avtonovosib",
    ],
    "kazan": [
        "avtorynok_Kazan", "mashiny_v_kazani", "kazanplusauto",
        "kazan_avto_100t",
    ],
    "krasnodar": [
        "mashiny_v_krasnodare", "avtok23", "bukkrai",
    ],
    "chelyabinsk": [
        "AtvoChelyabinsk", "mashiny_v_chelyabinske", "Avtorinok_74",
    ],
    "ufa": [
        "avtorynok_ufa_rb", "BashAutoPrice", "mashiny_v_ufe",
        "autorynok_ufa", "avtorynok_ufa_perekup",
    ],
    "omsk": [
        "avtorinokomsk", "mashiny_v_omske", "avtorynok_omsk_55",
    ],
    "rostov": [
        "mashiny_v_rostove", "auto61rus", "mashiny_v_rostove_nd",
    ],
    "tyumen": [
        "autob72", "avto_tumen_72", "mashiny_v_tyumeni",
    ],
    "samara": [
        "timecars_max", "AVTO_ZZ2", "AvtoTolaytti",
        "mashiny_v_samare", "AUTOTORG63",
    ],
    "volgograd": [
        "AvtomobiliVolgograd", "mashiny_v_volgograde",
    ],
    "perm": [
        "slava_alekseev_999", "permavtorinok", "mashiny_v_permi",
        "automarket_59",
    ],
    "voronezh": [
        "auto_voronezh36", "vrnCars", "Voronezh_AvtoRynok",
    ],
    "saratov": [
        "mashiny_v_saratove", "autobazar13",
    ],
    "krasnoyarsk": [
        "avto_prodazha_krsk", "krasnoyarsk_24avto", "mashiny_v_krasnoyarske",
        "AUTO_24RU", "avto_krsk", "krasnoyarsk_avto",
        "krsk_auto_baraholka", "avto_baraholka_krsk24",
    ],
    "irkutsk": [
        "AUTO_38RU", "mashiny_v_irkutske",
    ],
    "vladivostok": [
        "mashiny_v_vladivostoke", "VelesAutoDV_salecar",
    ],
    "habarovsk": [
        "auto_khv", "mashiny_v_habarovske",
    ],
    "nn": [
        "mashiny_v_nizhnem", "mashiny_v_nn", "rynok_nizhniy",
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
    r"(\d[\d\s]{1,10})\s*(?:₽|тыс\.?\s*р(?:уб)?|тыс\.?|т\.?\s*р?\.?|тр\.?|k\b|к\b|руб|р\.)",
    re.IGNORECASE,
)
_TG_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")
_TG_MILEAGE_RE = re.compile(r"(\d[\d\s]{2,6})\s*(?:тыс\.?\s*км|км)", re.IGNORECASE)


def _tg_parse_price(text: str) -> int:
    """Извлекает цену из текста Telegram-объявления."""
    low = str(text or "").lower()
    ctx_re = re.compile(
        r"(?:цен[аеу]|стоимост[ьи]|прошу|продам за|отдам за)\s*[:\-]?\s*(\d[\d\s]{1,9})(?:\s*(?:тыс|т\.?\s*р?|тр|k|к))?",
        re.IGNORECASE,
    )
    for m in ctx_re.finditer(text):
        raw = re.sub(r"\D", "", m.group(1))
        if not raw:
            continue
        val = int(raw)
        full = m.group(0).lower()
        if any(s in full for s in ("тыс", "т.р", "тр")) or re.search(r"\bт\b|\bk\b|\bк\b", full):
            val *= 1000
        if val < 1000:
            val *= 1000
        if 50_000 <= val <= 50_000_000:
            return val
    for m in _TG_PRICE_RE.finditer(text):
        raw = re.sub(r"\D", "", m.group(1))
        if not raw:
            continue
        val = int(raw)
        # «тыс.» суффикс — умножаем
        suffix = m.group(0)[len(m.group(1)):].strip().lower()
        if any(s in suffix for s in ("тыс", "тр", "т", "k", "к")):
            nearby = low[max(0, m.start() - 45):m.end() + 12]
            if any(x in nearby for x in ("влож", "ходов", "сцеп", "ремонт", "поменян", "замен", "капиталк", "грм")):
                continue
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
    # Старые советские/российские марки и народные названия — часто в бюджете до 100к
    "жигул", "жига", "копейка", "шестёрка", "шестерка", "семёрка", "семерка",
    "восьмёрка", "восьмерка", "девятка", "десятка", "одиннадцатая", "пятнашка",
    "классик", "самара", "зубило",  # ВАЗ 2108/2109/2110
    "ока ", "оку ", "оке ", "volga", "волга", "газель", "соболь",
    "2101", "2102", "2103", "2104", "2105", "2106", "2107", "2108", "2109",
    "2110", "2111", "2112", "2113", "2114", "2115", "21099", "2170", "2171",
    "datsun on-do", "on do", "datsun mi-do",
    # Китайские марки (новые популярные)
    "chery tiggo", "tiggo", "haval jolion", "jolion", "geely atlas", "coolray",
    "omoda c5", "jaecoo 7", "tank 300", "tank 500",
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
    "скутер", "мопед", "мотороллер", "мотоцикл", "питбайк", "квадроцикл",
    "вятка электрон", "продам вятку", "вятка электрон", "vespa", "yamaha ybr",
    "запчаст", "автозапчаст", "разбор", "на разбор", "на запчаст",
    # Салонные/кузовные детали: «дверные карты ВАЗ 2106» не автомобиль.
    "дверные карты", "карты двер", "карта двери", "карты ваз", "карты 210",
    "обшивка двер", "обшивки двер", "обшивку двер", "обшивк салона",
    "салон ваз", "салон на ваз", "комплект салона", "комплект карт",
    # НЕ добавляем "шин" — оно содержится в "машина", "машины", "машину" → ложное срабатывание
    "шины б/у", "б/у шин", "продам шин", "зимние шин", "летние шин", "комплект шин",
    "покрышк", "резина б/у", "б/у резин",
    "колёса б/у", "колеса б/у", "б/у колёс", "б/у колес",
    "диски r", "диски р", "диск r", "диск р", "диск на",
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
    "обратная связь бота", "бот поддержки",
    "вступить в группу", "вступить в чат",
    # Новости о ценах на топливо/бензин — не продажа авто
    "цены на бензин", "стоимость бензина", "цена бензина", "бензин подорожа",
    "бензин подешев", "цены на топливо", "цены на азс", "заправка дорожает",
    "лихорадит цены", "горожане обсуждают", "цены на нефть",
    "аи-92", "аи-95", "аи-98", "дизельное топлив",
    # Другие новости и нерелевантный контент
    "читайте также", "подробнее на сайте", "источник:", "по данным",
    "сообщает корреспондент", "по информации", "как сообщает",
    # Промышленное оборудование, инструменты, стройматериалы
    "клапан", "вентил", "насос", "компрессор", "редуктор", "котёл", "котел",
    "труба", "трубопровод", "задвижка", "фланец", "кран шаровый",
    "электродвигател", "генератор", "трансформатор",
    "пиломатериал", "доска", "брус", "кирпич", "цемент", "стройматери",
    "инструмент", "дрель", "болгарк", "перфоратор", "сварочн",
    # Одежда, обувь, техника
    "куртк", "пальто", "платье", "туфл", "ботинк", "кроссовк",
    "телефон продам", "смартфон", "айфон", "samsung продам", "ноутбук продам",
    "холодильник", "стиральн", "посудомоечн",
    # Сельхоз / животноводство
    "трактор", "комбайн", "сенокосилк", "культиватор",
    "корова", "свинья", "поросята", "птица", "куриц",
    # Статьи/реклама/оценочные сервисы — не объявления (часто у конкурентов)
    "ликвидност", "что влияет", "ключевые фактор", "ключевых фактор", "разбираем",
    "экспресс-анализ", "экспресс анализ", "нижегородец", "факторы оценки",
    "бесплатный экспресс", "подписывайтес", "подпишитес", "наш канал", "наш чат",
    "оцени авто", "оценка автомобиля", "узнать стоимость", "рубрика", "полезный пост",
    "почему одни", "разбор:", "инструкция", "лайфхак", "топ-", "топ ",
    # Еда/личное/прочее (просачивается из newsfeed)
    "ягод", "хлебуш", "грибы", "урожай", "рецепт", "магазинчик",

    "карабин", "сайга", "ружьё", "ружье", "ружья", "винтовк", "оружие", "оружия",
    "патрон", "калибр", "кал.", "нарез", "ствол", "охотнич", "отстрел",
    "пистолет", "травматик", "пневматик", "глушител", "дтк ", "олрр",
    "7,62", "5,45", "12х76", "16 калибр", "охотбилет", "разрешение на оружие",
    # Страйкбол / airsoft / пневматика-копии (НЕ добавляем "автомат" —
    # это коробка-автомат у авто; и "кольт" — это Mitsubishi Colt)
    "страйкбол", "airsoft", "глок", "glock", "9x19", "9х19", "#сбм", "продам_сбм",
    "обойма", "гбб", "gbb", "м4а1", "ак-47", "ак47", "автомат калашников",
    # Пейнтбол и снаряжение
    "пейнтбол", "paintball", "пейнтбольн", "маркер пейнтбол", "hk army", "hkarmy",
    "bunkerkings", "dye paintball", "virtue paintball", "пейнтбольный",
]

def _is_car_sale_social(text: str) -> bool:
    """Возвращает True только если текст — объявление о продаже/покупке именно автомобиля."""
    tl = text.lower()
    if any(rk in tl for rk in _SOCIAL_REJECT_KEYWORDS):
        return False
    if _is_moto(tl[:700]):
        return False
    has_sale = any(sk in tl for sk in _SOCIAL_SALE_KEYWORDS)
    # Считаем сколько «сильных» авто-признаков (марка/пробег/двигатель/год и т.п.)
    car_hits = sum(1 for ck in _SOCIAL_CAR_STRONG if ck in tl)
    if not (has_sale and car_hits >= 1):
        return False
    # Конкретика объявления: марка (лат/кир), ИЛИ год, ИЛИ цена, ИЛИ ≥2 авто-признака
    # (реальное объявление почти всегда: марка+пробег+год; статья — 1 общее «авто»).
    has_brand = bool(_SOCIAL_TITLE_RE.search(tl))
    has_year = bool(_SOCIAL_YEAR_RE.search(tl))
    has_price_hint = bool(re.search(r"\d{2,3}\s*(?:тыс|т\.?\s*р|к\b|₽|руб|млн)", tl)) or \
                     bool(re.search(r"\d[\d\s.,]{4,}\s*(?:₽|руб|р\b|р\.)", tl))
    # Цена сама по себе не доказывает, что это авто: так проходили мопеды,
    # коляски и запчасти. Нужна марка/модель, год или несколько авто-признаков.
    return has_brand or has_year or car_hits >= 2

_SOCIAL_TITLE_RE = re.compile(
    r"(toyota|honda|kia|hyundai|nissan|mazda|bmw|audi|mercedes|lada|ваз|haval|geely|chery|skoda|volkswagen|vw|renault|peugeot|ford|opel|chevrolet|mitsubishi|subaru|lexus|infiniti|volvo|jeep|suzuki|datsun|changan|exeed|omoda|tank|jaecoo|byd|нива|приора|гранта|калина|vesta|largus|xray|москвич|\b210[1-9]\b|\b211[0-5]\b|\b21099\b|\b217[0-2]\b|\b219[0-4]\b|\b2121\b|\b2131\b)",
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


def scrape_tg_channels(region: str, price_min: int, price_max: int, fast: bool = False) -> list[dict]:
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

    # Названия городов и областей на русском для поисковых запросов
    _tg_region_names = {
        "ekaterinburg": "Екатеринбург", "moskva": "Москва", "spb": "Санкт-Петербург",
        "novosibirsk": "Новосибирск", "kazan": "Казань", "chelyabinsk": "Челябинск",
        "ufa": "Уфа", "krasnodar": "Краснодар", "omsk": "Омск", "rostov": "Ростов-на-Дону",
        "tyumen": "Тюмень", "samara": "Самара", "volgograd": "Волгоград",
        "perm": "Пермь", "voronezh": "Воронеж", "saratov": "Саратов",
        "krasnoyarsk": "Красноярск", "irkutsk": "Иркутск",
        "vladivostok": "Владивосток", "habarovsk": "Хабаровск", "nn": "Нижний Новгород",
    }
    _tg_oblast_names = {
        "ekaterinburg": "Свердловская область", "moskva": "Московская область",
        "spb": "Ленинградская область", "novosibirsk": "Новосибирская область",
        "kazan": "Татарстан", "chelyabinsk": "Челябинская область",
        "ufa": "Башкортостан", "krasnodar": "Краснодарский край",
        "omsk": "Омская область", "rostov": "Ростовская область",
        "tyumen": "Тюменская область", "samara": "Самарская область",
        "perm": "Пермский край", "voronezh": "Воронежская область",
        "krasnoyarsk": "Красноярский край", "nn": "Нижегородская область",
        "irkutsk": "Иркутская область", "saratov": "Саратовская область",
        "vladivostok": "Приморский край", "habarovsk": "Хабаровский край",
        "volgograd": "Волгоградская область",
    }
    region_name_ru = _tg_region_names.get(city_key, city_key or region)
    oblast_name_ru = _tg_oblast_names.get(city_key, region_name_ru)
    search_locations = list(dict.fromkeys([region_name_ru, oblast_name_ru]))

    # Создаём сессию с русским прокси (тот же что и для Авито)
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    _tg_proxy_url = _active_proxy_url()
    if _tg_proxy_url and "__agentproxy" not in _tg_proxy_url:
        session.proxies.update({"http": _tg_proxy_url, "https": _tg_proxy_url})

    # Реальные публичные TG каналы продажи авто по городам
    # Источник: t.me/s/ — только каналы с открытым веб-просмотром
    TG_REAL_CHANNELS: dict[str, list[str]] = TG_AUTO_CHANNELS

    # ── Шаг 1: Discovery реальных каналов через tgstat/Yandex/DDG ───
    import urllib.parse as _upq_tg
    from concurrent.futures import ThreadPoolExecutor as _TPE_TG, as_completed as _ac_TG

    _tme_re = re.compile(r'(?:t\.me|telegram\.me)/([a-zA-Z][a-zA-Z0-9_]{3,31})(?![/\d])')
    _skip_tg = {"telegram","durov","tgstat","tlgrm","joinchat","share","addstickers",
                "robocop","BotFather","gif","stickers","contest","c","bot","notifications",
                "SpamBot","vote","channel","group"}

    # Федеральные автоканалы — только проверенные (t.me/s/ работает только с каналами, не чатами)
    _TG_FEDERAL_CHANNELS = [
        "perekupskiydvig",
        "avtorynokby196",
    ]
    # Уже известные каналы из справочника (могут быть фейками — проверим t.me/s/)
    _seed_channels = list(dict.fromkeys(
        TG_REAL_CHANNELS.get(city_key, []) + TG_AUTO_CHANNELS.get(city_key, []) + _TG_FEDERAL_CHANNELS
    ))
    if fast:
        _seed_channels = _seed_channels[:14]

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

        # 5. Bing — менее строгий к ботам, хорошо находит t.me
        for _loc in search_locations:
            for _q in [
                f"site:t.me автобарахолка {_loc}",
                f"site:t.me продам авто {_loc}",
            ]:
                try:
                    r = session.get("https://www.bing.com/search",
                        params={"q": _q, "setlang": "ru", "count": "20"},
                        timeout=9,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                 "Accept-Language": "ru-RU,ru;q=0.9"})
                    if r.status_code == 200:
                        found += _tme_re.findall(_upq_tg.unquote(r.text))
                except Exception:
                    pass

        # 6. Google — ищет t.me ссылки лучше всего с российского IP
        for _loc in search_locations:
            for _q in [
                f"site:t.me автобарахолка {_loc}",
                f"site:t.me продам авто {_loc}",
                f"telegram автобарахолка {_loc} канал",
            ]:
                try:
                    r = session.get("https://www.google.com/search",
                        params={"q": _q, "hl": "ru", "num": "20"},
                        timeout=9,
                        headers={
                            "User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
                            "Accept-Language": "ru-RU,ru;q=0.9",
                        })
                    if r.status_code == 200:
                        found += _tme_re.findall(_upq_tg.unquote(r.text))
                except Exception:
                    pass

        # 6. Telegram поиск через web.telegram.org (публичные каналы)
        try:
            r = session.get("https://telegram.me/s/",
                params={"q": f"автобарахолка {region_name_ru}"},
                timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                found += _tme_re.findall(r.text)
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
        """Парсит публичный TG канал через t.me/s/."""
        import requests as _req_tme
        _tme_session = _req_tme.Session()  # t.me не блокирует Railway — прокси не нужен
        try:
            batch = []
            seen_urls_ch: set[str] = set()
            today_d = datetime.date.today()
            before_id = None
            for _page in range(1 if fast else 3):
                url_t = f"https://t.me/s/{channel}" if before_id is None else f"https://t.me/s/{channel}?before={before_id}"
                try:
                    r = _tme_session.get(
                        url_t,
                        timeout=4 if fast else 7,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    )
                    if r.status_code != 200:
                        print(f"  [TG] @{channel} HTTP {r.status_code}")
                        break
                    if "tgme_widget_message" not in r.text:
                        _why = "канал приватный" if "tgme_page_status" in r.text else ("не найден" if "tgme_page_error" in r.text else f"нет виджета, размер={len(r.text)}")
                        print(f"  [TG] @{channel} — пропускаем: {_why}")
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
                        if len(text) < 20:
                            continue
                        if _is_moto(text[:200]):
                            continue
                        if not _is_car_sale_social(text):
                            print(f"  [TG] @{channel} отфильтрован пост: {text[:80]!r}")
                            continue
                        year_m = _TG_YEAR_RE.search(text)
                        year_num = int(year_m.group(1)) if year_m else 0
                        price = _tg_parse_price(text)
                        if price and (
                            not _social_price_is_plausible(price, year_num)
                            or _social_price_is_credit_payment(price, text)
                        ):
                            print(f"  [TG] ignore suspicious price {price} for year {year_num}: {text[:80]!r}")
                            price = 0
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
                            "_year": year_num,
                            "_days_on_site": days,
                            "_no_price": price == 0,
                        })
                    before_id = min_id
                    if not found_new or not before_id:
                        break
                except Exception:
                    break
            if batch:
                print(f"  [TG] @{channel}: {len(batch)} объявлений")
            return batch
        except Exception as e:
            print(f"  [TG] @{channel} исключение: {e}")
            return []

    # ── Шаг 3: DDG поиск постов из TG напрямую ───────────────────────
    def _ddg_tg_posts() -> list[dict]:
        import urllib.parse as _upq2
        _tg_post_re = re.compile(r'https?://t\.me/[a-zA-Z0-9_]+/\d+', re.I)
        all_locs = list(dict.fromkeys([region_name_ru, oblast_name_ru]))
        queries = [f"site:t.me продам авто {_loc}" for _loc in all_locs]
        queries += [f"site:t.me автобарахолка {_loc}" for _loc in all_locs[:1]]
        if fast:
            queries = queries[:1]
        batch = []
        seen_u: set[str] = set()
        for q in queries:
            try:
                r = session.get("https://html.duckduckgo.com/html/",
                    params={"q": q, "kl": "ru-ru"}, timeout=4 if fast else 9)
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
                    year_m = _TG_YEAR_RE.search(ctx)
                    year_num = int(year_m.group(1)) if year_m else 0
                    price = _tg_parse_price(ctx)
                    if price and (
                        not _social_price_is_plausible(price, year_num)
                        or _social_price_is_credit_payment(price, ctx)
                    ):
                        print(f"  [TG] ignore suspicious price {price} for year {year_num}: {ctx[:80]!r}")
                        price = 0
                    if price > 0 and not (price_min <= price <= price_max):
                        continue
                    ch_m = re.match(r'https?://t\.me/([a-zA-Z0-9_]+)/', href)
                    ch_name = ch_m.group(1) if ch_m else "tg"
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
                        "_year": year_num,
                        "_days_on_site": 0,
                        "_no_price": price == 0,
                    })
            except Exception:
                pass
        return batch

    # ── Сначала БЫСТРО парсим seed-каналы (они дают основную массу) ─────
    # Дискавери через tgstat/bing/google медленный и часто блокируется, из-за
    # него весь TG не успевал в общий дедлайн поиска и обнулялся. Поэтому:
    # 1) сперва seed-каналы (параллельно, коротко), 2) дискавери — ТОЛЬКО если
    # seed дал мало результатов, и с жёстким лимитом времени.
    results: list[dict] = []
    try:
        with _TPE_TG(max_workers=max(1, min(12, len(_seed_channels)))) as _ch_ex:
            for ch_batch in _ch_ex.map(
                _parse_channel,
                _seed_channels,
                timeout=7 if fast else 11,
            ):
                if ch_batch:
                    results.extend(ch_batch)
                    print(f"  [TG seed] {len(ch_batch)} объявлений")
    except Exception as _tg_seed_error:
        print(f"  [TG seed] лимит времени: {str(_tg_seed_error)[:60]}")

    # В быстром режиме разрешён один короткий индексный запрос; полный обход
    # каталогов/поисковиков остаётся только фоновому режиму.
    if fast and len(results) < 5:
        try:
            ddg_batch = _ddg_tg_posts()
        except Exception:
            ddg_batch = []
        if ddg_batch:
            results.extend(ddg_batch)
            print(f"  [TG DDG fast] {len(ddg_batch)} постов")
    elif len(results) < 5:
        with _TPE_TG(max_workers=2) as _dis_ex:
            _disc_fut = _dis_ex.submit(_discover_channels)
            _ddg_fut = _dis_ex.submit(_ddg_tg_posts)
            try:
                discovered = _disc_fut.result(timeout=6)
            except Exception:
                discovered = []
            try:
                ddg_batch = _ddg_fut.result(timeout=4)
                if ddg_batch:
                    results.extend(ddg_batch)
                    print(f"  [TG DDG] {len(ddg_batch)} постов")
            except Exception:
                pass
        new_chs = [c for c in discovered if c not in _seed_channels and c not in _skip_tg][:12]
        if new_chs:
            with _TPE_TG(max_workers=12) as _ch_ex2:
                for ch_batch in _ch_ex2.map(_parse_channel, new_chs, timeout=8):
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
    if not deduped_tg:
        deduped_tg = _cached_source_results("tg", region, price_min, price_max, limit=30)
        if deduped_tg:
            print(f"  [TG] восстановлено {len(deduped_tg)} объявлений из кэша")
    _remember_source_results("tg", region, deduped_tg)
    return deduped_tg


# ── Парсер Юлы (youla.ru) ────────────────────────────────────────
# Координаты городов для гео-поиска Юлы (её API фильтрует по lat/lng+radius).
YOULA_COORDS = {
    "ekaterinburg": (56.838, 60.605), "moscow": (55.755, 37.617),
    "spb": (59.939, 30.315), "novosibirsk": (55.008, 82.935),
    "kazan": (55.796, 49.108), "chelyabinsk": (55.159, 61.402),
    "ufa": (54.735, 55.958), "krasnodar": (45.035, 38.975),
    "omsk": (54.989, 73.368), "tyumen": (57.153, 65.534),
    "perm": (58.010, 56.229), "krasnoyarsk": (56.010, 92.852),
    "voronezh": (51.660, 39.200), "samara": (53.195, 50.100),
    "rostov": (47.222, 39.718),
}

def scrape_youla(region: str, pages: int = 4, price_min: int = 0,
                 price_max: int = 99_000_000, brand: str = "") -> list[dict]:
    """Ищет б/у авто на Юле через её публичный JSON-API (category=23 — легковые).
    Гео — по координатам города + радиус. Цена приходит в КОПЕЙКАХ."""
    try:
        import requests as _req
    except ImportError:
        return []
    coords = YOULA_COORDS.get(region)
    if not coords:
        return []
    lat, lng = coords
    results: list[dict] = []
    today = datetime.date.today()
    now_ts = time.time()
    _brand_l = (brand or "").strip().lower()
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    seen_ids: set[str] = set()
    for p in range(1, pages + 1):
        url = (f"https://youla.ru/api/v1/products?category=23"
               f"&latitude={lat}&longitude={lng}&radius=250&page={p}&per_page=50")
        try:
            r = _req.get(url, headers=hdrs, timeout=10)
            if r.status_code != 200:
                print(f"  [Юла] стр.{p}: HTTP {r.status_code}")
                break
            data = r.json().get("data", [])
        except Exception as e:
            print(f"  [Юла] стр.{p}: {str(e)[:70]}")
            break
        if not data:
            break
        _added = 0
        for it in data:
            try:
                if it.get("type") != "product" or it.get("is_sold") or it.get("is_blocked"):
                    continue
                _id = it.get("id", "")
                if not _id or _id in seen_ids:
                    continue
                seen_ids.add(_id)
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                if _brand_l and _brand_l != "any" and _brand_l not in name.lower():
                    continue
                price_kop = it.get("price") or 0
                price_rub = int(price_kop) // 100 if price_kop else 0
                if price_rub and not (price_min <= price_rub <= price_max):
                    continue
                _u = it.get("url", "")
                item_url = ("https://youla.ru" + _u) if _u.startswith("/") else (_u or it.get("short_url", ""))
                imgs = it.get("images") or []
                photo_url = imgs[0].get("url", "") if imgs else ""
                dp = it.get("date_published") or 0
                days = max(0, int((now_ts - dp) // 86400)) if dp else 0
                loc = it.get("location") or {}
                item = {
                    "source": "youla", "title": name,
                    "price": f"{price_rub:,} ₽".replace(",", " ") if price_rub else "цена не указана",
                    "_price_int": price_rub,
                    "url": item_url, "date": str(today - datetime.timedelta(days=days)),
                    "_days_on_site": days, "_date_known": True,
                    "_photo_url": photo_url, "_photos": len(imgs),
                    "description": (it.get("description") or "")[:400],
                    "seller": loc.get("city_name", ""), "mileage": 0,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
                _added += 1
            except Exception:
                continue
        print(f"  [Юла] стр.{p}: +{_added} (всего {len(results)})")
        if _added == 0:
            continue
        time.sleep(0.1)
    if len(results) < 8:
        try:
            import urllib.parse as _upq_y
            region_name = REGIONS.get(region, region)
            queries = [
                f"site:youla.ru {region_name} автомобиль продажа",
                f"site:youla.ru {region_name} авто {brand}" if _brand_l and _brand_l != "any" else f"site:youla.ru {region_name} авто",
            ]
            seen_urls = {_norm_url(i.get("url", "")) for i in results}
            for q in queries:
                try:
                    rr = _req.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": q, "kl": "ru-ru"},
                        headers={"User-Agent": hdrs["User-Agent"], "Accept-Language": "ru-RU,ru;q=0.9"},
                        timeout=8,
                    )
                    if rr.status_code != 200:
                        continue
                    html_text = _upq_y.unquote(rr.text)
                    for m in re.finditer(r'https?://(?:www\.)?youla\.ru/[^\s"<>]+', html_text, re.I):
                        u = m.group(0).split("&")[0].rstrip(".,)'\"")
                        u_norm = _norm_url(u)
                        if not u_norm or u_norm in seen_urls:
                            continue
                        pos = html_text.find(m.group(0))
                        ctx = re.sub(r"<[^>]+>", " ", html_text[max(0, pos - 280):pos + 500])
                        ctx = re.sub(r"\s+", " ", ctx).strip()
                        if _is_moto(ctx[:700]):
                            continue
                        price = parse_price(ctx)
                        if price and not (price_min <= price <= price_max):
                            continue
                        title = _social_make_title(ctx) if _SOCIAL_TITLE_RE.search(ctx) else ctx[:100]
                        probe = {"title": title, "description": ctx, "url": u_norm}
                        if _is_non_car_goods(probe):
                            continue
                        key = _car_group_key(title)
                        if not key or len(key.split()) < 2:
                            continue
                        if _brand_l and _brand_l != "any" and not _match_brand_item(probe, _brand_l):
                            continue
                        seen_urls.add(u_norm)
                        results.append({
                            "source": "youla",
                            "title": title,
                            "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
                            "_price_int": price,
                            "url": u_norm,
                            "date": str(today),
                            "_days_on_site": 0,
                            "_date_known": False,
                            "_photo_url": "",
                            "_photos": 0,
                            "description": ctx[:400],
                            "seller": region_name,
                            "mileage": extract_mileage(ctx),
                        })
                except Exception:
                    continue
            if len(results) >= 8:
                print(f"  [Юла fallback] всего {len(results)} объявлений")
        except Exception as e:
            print(f"  [Юла fallback] ошибка: {str(e)[:80]}")
    print(f"  [Юла] итого {len(results)} объявлений")
    return results


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

    vk_token = os.getenv("VK_TOKEN", "47e33a5247e33a5247e33a526a44a2d4af447e347e33a522ddfc471e56efb853533d23c")
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
        r"(\d[\d\s]{1,8})\s*(?:₽|тыс\.?\s*(?:р(?:уб(?:лей?|ля)?)?\.?)?|т\.?\s*р?\.?|тр\.?|k\b|к\b|руб(?:лей?|ля)?\.?|р\b\.?)",
        re.IGNORECASE,
    )
    # Число рядом с ценовым словом: "цена 150000", "прошу 95 000", "стоимость 80тыс"
    _vk_price_ctx_re = re.compile(
        r"(?:цен[аеу]|стоимост[ьи]|прошу|продам за|отдам за)\s*[:\-]?\s*(\d[\d\s]{1,7})(?:\s*(?:тыс|т\.?\s*р?|тр|k|к))?",
        re.IGNORECASE,
    )
    _vk_year_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")


    def _parse_price(text: str) -> int:
        _tl = text.lower()
        # 0. Миллионы: "1.2 млн", "1 млн 200", "2 миллиона"
        _mln = re.search(r"(\d[.,]?\d?)\s*(?:млн|миллион)", _tl)
        if _mln:
            try:
                val = int(float(_mln.group(1).replace(",", ".")) * 1_000_000)
                if 5_000 <= val <= 50_000_000:
                    return val
            except Exception:
                pass
        # 0a. 🍋 / "лимон" = миллион (сленг): "6 100 🍋"→6.1млн, "6.1 лимон"→6.1млн
        _lem = re.search(r"(\d+(?:[.,]\d+)?|\d[\d\s]*\d)\s*(?:🍋|лимон)", _tl)
        if _lem:
            try:
                g = _lem.group(1).strip()
                if "." in g or "," in g:
                    val = int(float(g.replace(",", ".").replace(" ", "")) * 1_000_000)
                else:
                    d = int(re.sub(r"\D", "", g))
                    val = d * 1000 if d >= 1000 else d * 1_000_000  # "6100"→6.1млн, "6"→6млн
                if 100_000 <= val <= 50_000_000:
                    return val
            except Exception:
                pass
        # 0b. Цена с разделителями тысяч точка/запятая: "Цена:1.800.000", "55,000₽".
        #     Группы по 3 цифры через . или , (не путать с "1.6" — там 1 цифра).
        _sep_re = r'(\d{1,3}(?:[.,]\d{3})+)'
        for m in re.finditer(rf'(?:цен[аеуы]|стоимост|прошу|за)\s*[:\-]?\s*{_sep_re}', _tl):
            val = int(re.sub(r'\D', '', m.group(1)))
            if 5_000 <= val <= 50_000_000:
                return val
        for m in re.finditer(rf'{_sep_re}\s*(?:₽|руб|р\.)', text):
            val = int(re.sub(r'\D', '', m.group(1)))
            if 5_000 <= val <= 50_000_000:
                return val
        # Явное ценовое поле должно быть приоритетнее последующих сумм ремонта/вложений.
        for m in _vk_price_ctx_re.finditer(text):
            raw = re.sub(r"\D", "", m.group(1))
            if not raw:
                continue
            val = int(raw)
            full = m.group(0).lower()
            if any(s in full for s in ("тыс", "т.р", "тр")) or re.search(r"\bт\b|\bk\b|\bк\b", full):
                val *= 1000
            if val < 1000:
                val *= 1000
            if 5_000 <= val <= 50_000_000:
                return val
        # Сначала ищем с явным символом валюты
        for m in _vk_price_re.finditer(text):
            _g = m.group(1).strip()
            # Если число содержит пробел и это НЕ группировка тысяч (110 000),
            # значит склеились модель+цена ("2107 12 тыс") — берём число у суффикса
            if " " in _g and not re.fullmatch(r"\d{1,3}(?: \d{3})+", _g):
                _g = _g.split()[-1]
            raw = re.sub(r"\D", "", _g)
            if not raw:
                continue
            val = int(raw)
            suffix = m.group(0)[len(m.group(1)):].strip().lower()
            _is_k = "тыс" in suffix or "тр" in suffix or re.search(r"\bт\.?\s*р?\.?\b", suffix) or re.search(r"\bk\b|\bк\b", suffix)
            if _is_k:
                # "110к пробег" / "107 тыс км" — это ПРОБЕГ, а не цена. Суффикс ₽/руб —
                # всегда цена, а к/тыс рядом с "пробег"/"км" — почти всегда пробег.
                _after = _tl[m.end():m.end() + 12]
                _before = _tl[max(0, m.start() - 45):m.start()]
                if ("пробег" in _after or "пробег" in _before
                        or _after.lstrip(" .,:") .startswith("км")
                        or "тыс.км" in _after or "тыс км" in _after):
                    continue
                if any(x in (_before + _after) for x in (
                    "влож", "ходов", "сцеп", "ремонт", "поменян", "замен", "капиталк", "грм"
                )):
                    continue
                if val < 1000:
                    val *= 1000
            if 5_000 <= val <= 50_000_000:
                return val
        # Затем ищем число рядом с ценовым словом
        for m in _vk_price_ctx_re.finditer(text):
            raw = re.sub(r"\D", "", m.group(1))
            if not raw:
                continue
            val = int(raw)
            full = m.group(0).lower()
            if any(s in full for s in ("тыс", "т.р", "тр")) or re.search(r"\bт\b|\bk\b|\bк\b", full):
                val *= 1000
            if val < 1000:  # вероятно тысячи без суффикса: "цена 95" → 95000
                val *= 1000
            if 5_000 <= val <= 50_000_000:
                return val
        # Третий проход: число с разделителями-пробелами ("1 200 000") или голое
        # ("950000"). Исключаем пробег/год/мощность/телефоны по контексту.
        _SKIP_CTX = ('пробег', 'км', 'год', 'г.в', 'г/в', 'тыс.км', 'л.с', 'лс',
                     'тел', 'phone', 'whats', 'viber', '+7', 'звон', 'налог', 'каждые')
        def _looks_like_phone(s: str) -> bool:
            d = re.sub(r'\D', '', s)
            return len(d) >= 10 and (d.startswith('89') or d.startswith('79') or d.startswith('7') or d.startswith('8'))
        # Сначала числа с пробелами-разделителями тысяч
        for m in re.finditer(r'\b(\d{1,3}(?:\s\d{3})+)\b', text):
            raw = m.group(1)
            if _looks_like_phone(raw):
                continue
            val = int(re.sub(r'\s', '', raw))
            ctx = _tl[max(0, m.start() - 25):m.end() + 12]
            if any(skip in ctx for skip in _SKIP_CTX):
                continue
            if 5_000 <= val <= 9_999_999:
                return val
        # Затем голое число
        for m in re.finditer(r'\b(\d{5,7})\b', text):
            val = int(m.group(1))
            if 5_000 <= val <= 9_999_999:
                ctx = _tl[max(0, m.start() - 30):m.end() + 30]
                if any(skip in ctx for skip in _SKIP_CTX):
                    continue
                return val
        return 0

    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    # Русский резидентный прокси — тот же единый актуальный конфиг, что у Авито.
    _vk_proxy_url = _active_proxy_url()
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
        owner_id = post.get("owner_id") or post.get("from_id", 0)
        post_id = post.get("id", "")
        url = f"https://vk.com/wall{owner_id}_{post_id}"
        # Личная страница продавца. Приоритет:
        #  1) signer_id — кто подписал пост в группе (реальный автор)
        #  2) автор репоста (copy_history) если это пользователь
        #  3) ссылка на vk.com/<профиль> прямо в тексте
        #  4) from_id/owner_id если это пользователь (положительный id)
        #  5) иначе — группа (club), помечаем как НЕ личную страницу
        _signer = post.get("signer_id") or 0
        _ch = post.get("copy_history") or []
        _orig_from = (_ch[0].get("from_id") or _ch[0].get("owner_id") or 0) if _ch else 0
        # Ссылка на профиль в тексте, НО не на сообщество/служебные пути.
        # club777/public555/event123/wall.../id0 — это НЕ личная страница продавца.
        _txt_link = re.search(
            r'vk\.com/(?!club\d|public\d|event\d|wall|feed\b|im\b|id0\b)([a-zA-Z][\w.]{2,30})',
            text,
        )
        _link_slug = _txt_link.group(1) if _txt_link else ""
        _base = post.get("from_id") or post.get("owner_id") or 0
        _is_personal = True
        if _signer > 0:
            _author_page = f"https://vk.com/id{_signer}"
        elif _orig_from > 0:
            _author_page = f"https://vk.com/id{_orig_from}"
        elif _link_slug:
            _author_page = f"https://vk.com/{_link_slug}"
        elif _base > 0:
            _author_page = f"https://vk.com/id{_base}"
        elif _base < 0:
            _author_page = f"https://vk.com/club{-_base}"  # только группа — личной страницы нет
            _is_personal = False
        else:
            _author_page = url
            _is_personal = False
        # Телефон из текста — самый надёжный контакт, если личной страницы нет
        _ph_m = re.search(r'(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}', text)
        _phone = _ph_m.group(0).strip() if _ph_m else ""
        year_m = _vk_year_re.search(text)
        year_num = int(year_m.group(1)) if year_m else 0
        price = _parse_price(text)
        if price and (
            not _social_price_is_plausible(price, year_num)
            or _social_price_is_credit_payment(price, text)
        ):
            print(f"  [VK] ignore suspicious price {price} for year {year_num}: {text[:80]!r}")
            price = 0
        if price > 0 and not (price_min <= price <= price_max):
            return None
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
        # Имя продавца: личная страница → ссылка на профиль; иначе телефон/группа.
        if _is_personal:
            seller = _author_page.replace("https://", "")
        elif _phone:
            seller = f"тел. {_phone}"
        elif source_label and source_label.lower() != "newsfeed":
            seller = source_label  # название группы
        else:
            seller = _author_page.replace("https://", "")
        return {
            "title": _social_make_title(text),
            "price": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
            "_price_int": price,
            "url": url,
            "_photo_url": photo_url,
            "description": text[:500],
            "source": "vk",
            "seller": seller,
            "_seller_url": _author_page if _is_personal else "",
            "_seller_is_personal": _is_personal,
            "_phone": _phone,
            "_year": year_num,
            "_days_on_site": days,
            "_no_price": price == 0,
        }

    from concurrent.futures import ThreadPoolExecutor as _TPE_VK, as_completed as _ac_VK
    import urllib.parse as _upq_vk

    VK_API_URL = "https://api.vk.com/method"

    # VK API НЕ должен идти через прокси — создаём отдельную сессию без прокси
    import requests as _req_vk
    _vk_api = _req_vk.Session()
    _vk_api.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })

    # ── Шаг 1: Проверяем токен и ищем группы через API ──────────────
    _found_group_ids: dict[int, str] = {}
    _vk_token_ok = False
    _vk_phase_newsfeed = None  # newsfeed-фаза (заполняется ниже, если есть токен)

    if vk_token:
        try:
            # Проверяем токен через users.get (работает с любым типом токена)
            _tr = _vk_api.get(f"{VK_API_URL}/users.get",
                params={"access_token": vk_token, "v": "5.131"}, timeout=10)
            _tr_json = _tr.json()
            if "error" in _tr_json:
                _ec = _tr_json["error"].get("error_code", 0)
                _em = _tr_json["error"].get("error_msg", "")
                print(f"  [VK] токен невалиден: код {_ec} {_em}")
                if _ec == 15:
                    print(f"  [VK] ПОДСКАЗКА: нужен standalone user_token, а не сервисный токен сообщества!")
                vk_token = ""  # не используем невалидный токен
            else:
                _vk_token_ok = True
                print(f"  [VK] токен OK")
        except Exception as e:
            print(f"  [VK] ошибка проверки токена: {e}")

    if _vk_token_ok:
        # newsfeed.search — единственный метод работающий с сервисным токеном плагина
        # groups.search требует standalone-токен → не используем
        _nf_seen: set[str] = set()
        _nf_queries = [
            f"продам авто {region_name_ru}",
            f"продам {region_name_ru} пробег",
            f"продаю авто {region_name_ru}",
            f"авто {region_name_ru} год двигатель",
            f"продам {oblast_name_ru} тыс.км",
            f"продаю {oblast_name_ru} пробег",
            f"авто барахолка {region_name_ru}",
            f"авторынок {region_name_ru}",
            f"куплю авто {region_name_ru}",
        ]
        def _nf_one(q: str) -> list:
            local = []
            try:
                r = _vk_api.get(f"{VK_API_URL}/newsfeed.search",
                    params={"q": q, "count": 200, "extended": 1,
                            "access_token": vk_token, "v": "5.131"}, timeout=12)
                resp = r.json()
                if resp.get("error"):
                    return []
                for post in resp.get("response", {}).get("items", []):
                    item = _vk_make_item(post, "newsfeed")
                    if item:
                        local.append(item)
            except Exception:
                pass
            return local

        def _exec_newsfeed():
            with _TPE_VK(max_workers=8) as _nfex:
                for batch_nf in _nfex.map(_nf_one, _nf_queries, timeout=14):
                    for item in (batch_nf or []):
                        if item["url"] not in _nf_seen:
                            _nf_seen.add(item["url"])
                            results.append(item)
            if _nf_seen:
                print(f"  [VK newsfeed] {len(_nf_seen)} постов из {len(_nf_queries)} запросов")
        _vk_phase_newsfeed = _exec_newsfeed

    # ── Шаг 2: Скрейпим стены найденных групп ────────────────────────
    def _scrape_wall(gid_name: tuple) -> list:
        gid, gname = gid_name
        local = []
        base = {"owner_id": f"-{gid}", "count": 100, "filter": "owner", "v": "5.131"}
        search_p = {"owner_id": f"-{gid}", "query": "продам", "count": 100, "v": "5.131"}
        if _vk_token_ok:
            base["access_token"] = vk_token
            search_p["access_token"] = vk_token
        try:
            rg = _vk_api.get(f"{VK_API_URL}/wall.get", params=base, timeout=6)
            resp = rg.json().get("response", {})
            for post in (resp.get("items", []) if isinstance(resp, dict) else []):
                item = _vk_make_item(post, gname)
                if item:
                    local.append(item)
        except Exception:
            pass
        try:
            rw = _vk_api.get(f"{VK_API_URL}/wall.search", params=search_p, timeout=6)
            resp2 = rw.json().get("response", {})
            for post in (resp2.get("items", []) if isinstance(resp2, dict) else []):
                item = _vk_make_item(post, gname)
                if item and item["url"] not in {x["url"] for x in local}:
                    local.append(item)
        except Exception:
            pass
        return local

    wall_groups = list(_found_group_ids.items())[:40]
    _wall_seen: set[str] = {it["url"] for it in results}  # уже найденные через newsfeed
    if wall_groups:
        with _TPE_VK(max_workers=12) as _wex:
            for posts in _wex.map(_scrape_wall, wall_groups, timeout=16):
                for item in (posts or []):
                    if item["url"] not in _wall_seen:
                        _wall_seen.add(item["url"])
                        results.append(item)
        print(f"  [VK wall] {len(wall_groups)} групп")

    # ── Шаг 3: Seed slugs через utils.resolveScreenName (без токена) ──
    # Реальные VK-слаги групп авто барахолок по регионам
    # Паттерны: avtobaraholka_[город], [город]_avto и т.д.
    _VK_SEED_SLUGS = {
            "ekaterinburg": [
                "buy_car66", "autosverdlovskayaoblast", "cars_market_cars",
                "fvs196", "avto_baraholka_tagila", "kareta96",
                "fvs__196", "ekb_auto196", "club176651739",
                "car_ekb", "podjopnik96", "club185888917",
                "autodo_50", "ekb_autom", "avtobaza96",
                "baraholkalesnoy", "f_v_s_1_9_6", "avtobaraholka_ekb",
                "avtobaraholka_sverdlovsk", "avto_baraholka_ural", "ekb_avto_baraholka",
                "avto_sverdlovsk_oblast", "avtorynok_ekb", "avto_baraholka96",
                "avtobaraholka96", "sverdlovsk_avto", "avto_do200_sverdlovsk",
                "avto_ural_baraholka", "prodamavto_ekb", "avtomoto_rynok_sverdlovsk",
                "avto_do300_sverdlovsk",
            ],
            "moskva": [
                "automoto_ua", "tachka77", "avto_podbor_moskva",
                "moscow_autorynok", "avtoobmen99", "club135400383",
                "berlroga", "moskva_avtorynok7", "avtobaraholka_msk",
                "avtobaraholka_moskva", "avto_baraholka_msk", "msk_avto_baraholka",
                "avtorynok_moskva", "avto_do200_msk", "avto_do300_msk",
                "prodamavto_msk77", "avtomoto_rynok_msk",
            ],
            "spb": [
                "autosalon_bit", "avtoshym", "autobaraholka_178",
                "automarket_piter", "spb78auto", "spb_autom",
                "opt_tool_sale", "club221866393", "probeg_auto",
                "avtobaraholka_spb", "avtobaraholka_piter", "avto_baraholka_spb",
                "spb_avto_baraholka", "avtorynok_spb", "avto_do200_spb",
                "prodamavto_spb78", "avtomoto_rynok_spb",
            ],
            "novosibirsk": [
                "avtorynok_nsk", "prodazha_avto_novosibirsk", "nsk_autom",
                "buy_sell_car_novosibirsk", "avtorynok_54", "auto_54rus",
                "avtorynok_novosibirsk", "vaz_novosibirsk", "auto_novosibirsk_no",
                "car_nsk54", "avtobaraholka_nsk", "avtobaraholka_novosibirsk",
                "avto_baraholka_nsk", "nsk_avto_baraholka", "avtorynok_nsk54",
                "avto_do200_nsk", "prodamavto_nsk54", "avtomoto_rynok_nsk",
            ],
            "kazan": [
                "kzn_autom", "rusauto116", "autorynok116",
                "auto_kn", "kznavto16", "nomera16rus",
                "avtorynok16", "avtovkazani", "auto_16rus",
                "avtokzn116", "auto_baracholka_kazan", "car_kazan",
                "kazan_avto116", "clubavto2222", "kamaavto12",
                "avtobazar_tatar", "tatarbazar_vk", "avtobaraholka_kazan",
                "avtobaraholka_tatarstan", "avto_baraholka_kazan", "kazan_avto_baraholka",
                "avtorynok_kazan16", "avto_do200_kazan",
            ],
            "chelyabinsk": [
                "autorinok_74", "avtorinok_che74", "automoto74",
                "avtobaza74", "chel_autom", "autoformatbox",
                "avtochelik174", "auto_chl", "avtorynok774",
                "automobile_market", "dmauto174", "chl.autosalon",
                "autorinok_chel", "buy_sell_car_chelyabinsk", "chelyabinskavtorinok",
                "kupauto174", "avto_do_200_chel", "autorynok_che",
                "avtobaraholka_chel", "avtobaraholka_chelyabinsk", "avto_baraholka_chel",
                "chel_avto_baraholka", "avtorynok_chel74", "avto_do200_chel",
                "avtomoto_rynok_chelyabinsk",
            ],
            "ufa": [
                "avto_prodaja_rb", "avto_rb_102", "ufamarket02",
                "cheapauto02", "mashin_rb", "avto__102",
                "automarket_ufa_rb", "tachkirb", "ufa_autom",
                "club200396770", "ufa_novocti", "avtovykup102",
                "avtoavtoufa", "auto_uf", "autobaraxolka_ufa",
                "buy_sell_car_ufa", "club144115503", "tut_auto102",
                "avtorynokyfa", "boomcar_ufa", "avtobaraholka_ufa",
                "avtobaraholka_bashkortostan", "avto_baraholka_ufa", "ufa_avto_baraholka",
                "avtorynok_ufa02", "avto_do200_ufa",
            ],
            "krasnodar": [
                "avtorinok.krasnodar", "autobaraholka_23", "a_k123",
                "autorynok.krasnodar", "auto.krasnodar123", "baraholka_93",
                "avtodromkrd", "ar_93", "auto_krasnodara",
                "kdr_auto", "avtorazborkrasnodar", "krd_autom",
                "avtorinokkracnodar", "buy_sell_auto_krasnodar", "avto193avto",
                "avtobaraholka_93", "avtokrasnodara", "adigshop",
                "autosale_23", "club1154", "avtobaraholka_krd",
                "avtobaraholka_kuban", "avto_baraholka_krd", "krd_avto_baraholka",
                "avtorynok_krasnodar", "avto_do200_krd", "avtomoto_rynok_kuban",
            ],
            "omsk": [
                "avtotrade55", "autorynok_omsk", "autotradeomsk",
                "avtoomsk1", "voditel55auto", "za100k_omsk",
                "omsk_autom", "tvoyavtorynok55", "avtoomsk55ru",
                "vykup.avto.omsk", "autobazar55rus", "auto_oms",
                "auto_resale", "club124601260", "auto_dealers55",
                "auto_omsk55", "autobaraholka55", "club139740852",
                "club172414104", "avtobaraholka_omsk", "avto_baraholka_omsk",
                "omsk_avto_baraholka", "avtorynok_omsk55", "avto_do200_omsk",
            ],
            "rostov": [
                "ar_61", "avto_do_100", "rostov_avtobazar",
                "auto_rostov_fortuna", "autobaraholka_61", "rostovondon_auto",
                "rnd_auto", "avtorostov1", "rnd_autom",
                "avtorinokrostov761", "avtoyug61", "club164863032",
                "avto_tac", "saleauto.rostov", "avtorynok_rostov",
                "avtobaraholka_rostov", "avtobaraholka_don", "avto_baraholka_rostov",
                "rostov_avto_baraholka", "avtorynok_rostov61",
            ],
            "tyumen": [
                "car_72", "autotmn", "autobaraholka172",
                "tmn.auto72", "autocar_72", "autob72",
                "avtobaza72", "mossauto", "avangard7777",
                "buy_sell_car_tyumen", "avtolavka72", "tob147",
                "autorynok72", "avtorynok_tyumen", "autorasprodasha72",
                "avto72l86", "autogranat", "avtobaraholka_tyumen",
                "avto_baraholka_tyumen", "tyumen_avto_baraholka", "avtorynok_tyumen72",
                "avto_do200_tyumen",
            ],
            "samara": [
                "cars_samara", "avtobuy163", "avtosamara63ru",
                "avto_163", "besplatno_sam_auto", "auto_smr",
                "rebuycars63", "avto_samara_oblast", "samaraavto1",
                "avto_baraholka_63", "avtozasto163", "buy_sell_car_samara",
                "avtorinok163", "avtomagazin63", "auto163auto",
                "avtobaraholka_samara", "avto_baraholka_samara", "samara_avto_baraholka",
                "avtorynok_samara63",
            ],
            "perm": [
                "59avtoo", "avtorynokpermskiy", "59cars59",
                "59pagoradu", "car159", "auto_159",
                "avtorynokperm", "perm_autom", "carperm",
                "pr_auto", "automarketprm", "clubavto159rus",
                "permautoperm", "59avtoperm", "kupiprodaiperm",
                "avtorinok.perm59ars", "avto_vykup_perm159", "avtovukyp_perm",
                "avtobaraholka_perm", "avto_baraholka_perm", "perm_avto_baraholka",
                "avtorynok_perm59", "avto_do200_perm",
            ],
            "voronezh": [
                "avtorinokvrn136", "autoskar", "vrn_m",
                "auto_vr", "vrn_am", "auto_voronezha",
                "clubvrh136", "vrn36buytosell", "car_vrn",
                "avtorinok_36rus", "buy_sell_car_voronezh", "auto_moto_market136",
                "31auto36bazar46", "avtorynok_36", "avto036",
                "auto_voronezh36", "autorinok_voronezh", "avtobaraholka_vrn",
                "avtobaraholka_voronezh", "avto_baraholka_vrn", "vrn_avto_baraholka",
                "avtorynok_voronezh36",
            ],
            "volgograd": [
                "avtobaraholka_vgd", "avtobaraholka_volgograd", "avto_baraholka_vgd",
                "vgd_avto_baraholka", "avtorynok_volgograd34",
            ],
            "krasnoyarsk": [
                "cars_24", "club134936417", "avtorinok24",
                "avtorinok_krsk", "auto24_krsk", "bmtkrsk",
                "avtorynokkrsk124", "auto__krsk", "car_market24",
                "krsk_autom", "buy_sell_car_krasnoyarsk", "club176240919",
                "auto_krs", "auto.car24", "dromkras",
                "avtocar24rus", "24buauto", "avtovkrsk124",
                "autobaraholka_124", "avtobaraholka_krsk", "avtobaraholka_krasnoyarsk",
                "avto_baraholka_krs", "krsk_avto_baraholka", "avtorynok_krasnoyarsk24",
            ],
            "nn": [
                "avtobaraholka_nn", "avtobaraholka_nizhniy", "avto_baraholka_nn",
                "nn_avto_baraholka", "avtorynok_nn52",
            ],
            "saratov": [
                "avtobaraholka_saratov", "avto_baraholka_saratov", "saratov_avto_baraholka",
            ],
            "irkutsk": [
                "avtobaraholka_irkutsk", "avto_baraholka_irk", "irkutsk_avto_baraholka",
            ],
            "vladivostok": [
                "avtobaraholka_vlad", "avtobaraholka_vladivostok", "japancars_vlad",
                "vlad_avto_baraholka",
            ],
            "habarovsk": [
                "avtobaraholka_hab", "avtobaraholka_habarovsk", "hab_avto_baraholka",
            ],
        }
    _seed_slugs = _VK_SEED_SLUGS.get(city_key, [])
    # Генерируем доп паттерны из названия города
    _cname_e = city_key or ""
    for _pat in [f"avto_{_cname_e}", f"avtobaraholka_{_cname_e}", f"avtobazar_{_cname_e}",
                 f"prodamavto_{_cname_e}", f"avtorynok_{_cname_e}", f"{_cname_e}_avto",
                 f"avto{_cname_e}"]:
        if _pat and _pat not in _seed_slugs and len(_pat) >= 4:
            _seed_slugs.append(_pat)

    def _resolve_and_scrape(slug: str) -> list:
        """Резолвит slug → group_id через utils.resolveScreenName, затем скрейпит стену."""
        gid = None
        # utils.resolveScreenName работает БЕЗ токена — используем прямую сессию
        try:
            r = _vk_api.get(f"{VK_API_URL}/utils.resolveScreenName",
                params={"screen_name": slug, "v": "5.131"}, timeout=4)
            obj = r.json().get("response", False)
            if obj and isinstance(obj, dict) and obj.get("type") in ("group", "page", "public"):
                gid = obj.get("object_id")
                if gid and gid not in _found_group_ids:
                    _found_group_ids[gid] = slug
        except Exception:
            pass
        # resolveScreenName не дал ID — пропускаем (m.vk.com-фолбэк убран ради скорости)
        if not gid:
            return []
        return _scrape_wall((gid, slug))

    def _exec_seeds():
        if not _seed_slugs:
            return
        with _TPE_VK(max_workers=15) as _sex:
            for batch_s in _sex.map(_resolve_and_scrape, _seed_slugs[:20], timeout=12):
                for item in (batch_s or []):
                    if item["url"] not in _wall_seen:
                        _wall_seen.add(item["url"])
                        results.append(item)
        print(f"  [VK slugs] {len(_wall_seen)} итого после resolve")

    # newsfeed и seed-резолв НЕЗАВИСИМЫ → выполняем их ПАРАЛЛЕЛЬНО
    # (раньше последовательно ~38с, теперь ~max(фаз) ~14с). Шаг 2 — пустой no-op.
    _vk_phases = [f for f in (_vk_phase_newsfeed, _exec_seeds) if f]
    if _vk_phases:
        with _TPE_VK(max_workers=len(_vk_phases)) as _pex:
            list(_pex.map(lambda f: f(), _vk_phases))

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
# ВНИМАНИЕ: ID проверены через www.avito.ru/web/1/slocations (2026-06).
# Старые значения были почти все неверными → Авито возвращал 0 по этим городам.
AVITO_LOCATION_IDS = {
    "ekaterinburg": 654070,
    "moscow":       637640,
    "spb":          653240,
    "novosibirsk":  641780,
    "kazan":        650400,
    "chelyabinsk":  661420,
    "ufa":          646600,
    "krasnodar":    633540,
    "omsk":         642320,
    "tyumen":       659020,
    "perm":         644200,
    "krasnoyarsk":  635320,
    "voronezh":     625810,
    "samara":       653040,
    "rostov":       652000,
}

# ID областей/краёв/республик — поиск по ВСЕМУ региону, а не только городу
# (Екатеринбург → вся Свердловская область и т.д.). Проверено через slocations.
AVITO_OBLAST_IDS = {
    "ekaterinburg": 653700,  # Свердловская область
    "spb":          636370,  # Ленинградская область
    "novosibirsk":  641470,  # Новосибирская область
    "kazan":        650130,  # Республика Татарстан
    "chelyabinsk":  660710,  # Челябинская область
    "ufa":          645790,  # Республика Башкортостан
    "krasnodar":    632660,  # Краснодарский край
    "omsk":         642020,  # Омская область
    "tyumen":       658170,  # Тюменская область
    "perm":         643700,  # Пермский край
    "krasnoyarsk":  634930,  # Красноярский край
    "voronezh":     625670,  # Воронежская область
    "samara":       652560,  # Самарская область
    "rostov":       651110,  # Ростовская область
    # moscow — федеральный город, остаётся как есть (637640)
}


def _avito_region_loc(region: str) -> int:
    """locationId для поиска: предпочитаем область (весь регион), иначе город."""
    return AVITO_OBLAST_IDS.get(region) or AVITO_LOCATION_IDS.get(region, 637640)



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
        # string/fullString — формат веб-JSON API Авито (/web/1/js/items)
        for text_key in ("valueText", "text", "label", "displayValue", "fullString", "string"):
            t = obj.get(text_key)
            if t and isinstance(t, str):
                digits = re.sub(r"[^\d]", "", t)
                if digits and 5_000 < int(digits) < 99_000_000:
                    return t, int(digits)
        # Прямое числовое значение
        for val_key in ("value", "number", "amount", "price", "sum"):
            v = obj.get(val_key)
            if v and isinstance(v, (int, float)) and 5_000 < v < 99_000_000:
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
        if isinstance(info, (int, float)) and 5_000 < info < 99_000_000:
            return f"{int(info):,} ₽".replace(",", " "), int(info)
        if isinstance(info, dict):
            # Сначала ищем valueText — самый надёжный источник цены.
            # fullString/string — формат веб-JSON API Авито (/web/1/js/items),
            # напр. priceDetailed={"string":"191 000","fullString":"191 000 ₽"}
            vt = (info.get("valueText") or info.get("text") or info.get("displayValue")
                  or info.get("fullString") or info.get("string") or "")
            if vt and isinstance(vt, str):
                digits = re.sub(r"[^\d]", "", vt)
                if digits and 5_000 < int(digits) < 99_000_000:
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
                if isinstance(v, (int, float)) and 5_000 < v < 99_000_000:
                    return f"{int(v):,} ₽".replace(",", " "), int(v)
                if isinstance(v, str):
                    d = re.sub(r"[^\d]", "", v)
                    if d and 5_000 < int(d) < 99_000_000:
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


# Оценка цены Авито («хорошая цена», «ниже рынка» и т.п.) → числовой балл.
_AVITO_RATING_MAP = [
    ("отличная цена", 2), ("очень хорошая цена", 2),
    ("ниже рыночной", 2), ("ниже рынка", 2),
    ("дешевле оценки", 2), ("дешевле рыночной оценки", 2),
    ("хорошая цена", 1), ("рыночная цена", 0), ("соответствует оценке", 0),
    ("по рынку", 0), ("дороже оценки", -1), ("дороже рыночной оценки", -1),
    ("выше рыночной", -1), ("выше рынка", -1),
    ("завышенная цена", -1), ("завышена", -1), ("дорого", -1),
]

def _avito_price_rating(it: dict) -> tuple:
    """Возвращает (текст_оценки, балл, оценка_рынка_₽) из данных объявления Авито.
    Авито сам оценивает цену авто («хорошая цена» / «ниже рынка»…). Если оценка и/или
    числовая рыночная стоимость есть в JSON — берём их (это точнее нашей медианы).
    Всё пусто — ('', None, 0)."""
    try:
        blob = json.dumps(it, ensure_ascii=False).lower()
    except Exception:
        return "", None, 0
    text, score = "", None
    for phrase, sc in _AVITO_RATING_MAP:
        if phrase in blob:
            text, score = phrase, sc
            break
    # Числовая рыночная оценка Авито, если попалась в JSON
    market = 0
    m = re.search(
        r'"(?:marketprice|averageprice|avgprice|estimateprice|marketvalue|priceestimate)"\s*:\s*\{?[^}]*?(\d{5,9})',
        blob)
    if m:
        v = int(m.group(1))
        if 30_000 < v < 50_000_000:
            market = v
    if not market:
        market = _extract_market_estimate_from_obj(it, "avito")
    return text, score, market


def _avito_rating_from_text(text: str) -> tuple[str, int | None]:
    low = (text or "").lower()
    for phrase, score in _AVITO_RATING_MAP:
        if phrase in low:
            return phrase, score
    return "", None


def _parse_rub_amount(text: str) -> int:
    raw = re.sub(r"\D", "", text or "")
    if not raw:
        return 0
    try:
        value = int(raw)
        return value if 10_000 <= value <= 50_000_000 else 0
    except Exception:
        return 0


def _extract_market_estimate_from_text(text: str, source: str = "") -> int:
    """Best-effort extraction of marketplace market/AI estimate from HTML text."""
    if not text:
        return 0
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()
    source = (source or "").lower()
    marker_groups = [
        "оценка нейросети", "нейросеть", "дешевле оценки", "дороже оценки",
        "дром оценил", "оценка дрома", "оценка дром",
        "рыночная цена", "рыночная стоимость", "средняя цена", "средняя стоимость",
        "market price", "marketprice", "average price", "avgprice",
        "estimated price", "estimateprice", "price estimate", "valuation",
    ]
    starts = [low.find(m) for m in marker_groups if low.find(m) >= 0]
    if not starts:
        return 0
    for start in sorted(starts)[:6]:
        window = flat[max(0, start - 80):start + 520]
        amounts = re.findall(r"(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*(?:₽|руб|р\b)?", window, re.I)
        values = []
        for amount in amounts:
            value = _parse_rub_amount(amount)
            if value:
                values.append(value)
        if not values:
            continue
        # In Avito's AI block first amount is usually estimate, second is listing price.
        if source == "avito":
            return values[0]
        # Drom blocks can contain listing price first; prefer value after "оценил".
        for v in values:
            return v
    return 0


def _extract_market_estimate_from_obj(obj, source: str = "") -> int:
    """Deep JSON/object scan for explicit marketplace market estimate fields."""
    market_key_re = re.compile(
        r"(market|average|avg|estimate|estimated|valuation|fair|recommended|"
        r"рыноч|средн|оценк)",
        re.I,
    )
    price_key_re = re.compile(r"(price|cost|value|amount|sum|стоим|цен)", re.I)

    def _walk(x, depth=0, parent_key=""):
        if depth > 9:
            return 0
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                key_is_market = bool(market_key_re.search(lk) or market_key_re.search(parent_key))
                if key_is_market:
                    if isinstance(v, (int, float)):
                        iv = int(v)
                        if 30_000 < iv < 50_000_000:
                            return iv
                    if isinstance(v, str):
                        iv = _parse_rub_amount(v)
                        if iv:
                            return iv
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if price_key_re.search(str(kk)):
                                iv = _walk(vv, depth + 1, str(kk))
                                if iv:
                                    return iv
                        iv = _walk(v, depth + 1, lk)
                        if iv:
                            return iv
                    if isinstance(v, list):
                        iv = _walk(v, depth + 1, lk)
                        if iv:
                            return iv
                elif isinstance(v, (dict, list)):
                    iv = _walk(v, depth + 1, lk)
                    if iv:
                        return iv
        elif isinstance(x, list):
            for v in x:
                iv = _walk(v, depth + 1, parent_key)
                if iv:
                    return iv
        return 0

    try:
        return _walk(obj)
    except Exception:
        return 0


def _avito_ai_estimate_from_text(text: str) -> int:
    """Extracts Avito neural-network estimate from an opened listing page."""
    if not text:
        return 0
    flat = re.sub(r"\s+", " ", text)
    exact_patterns = (
        r"Оценка\s+нейросети[^0-9]{0,120}(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*₽",
        r"Нейросеть[^0-9]{0,160}(?:определила|оценила|изучила)[^0-9]{0,160}(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*₽",
        r"Дешевле\s+оценки[^0-9]{0,160}Оценка\s+нейросети[^0-9]{0,120}(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*₽",
        r"Дороже\s+оценки[^0-9]{0,160}Оценка\s+нейросети[^0-9]{0,120}(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*₽",
    )
    for pat in exact_patterns:
        m = re.search(pat, flat, re.IGNORECASE)
        if m:
            value = _parse_rub_amount(m.group(1))
            if value:
                return value
    # Cut the block before listing price so "Стоимость в объявлении" cannot win.
    low = flat.lower()
    start = low.find("оценка нейросети")
    if start >= 0:
        end = low.find("стоимость в объявлении", start)
        block = flat[start:end if end > start else start + 320]
        amounts = re.findall(r"(\d{1,3}(?:[\s.,]\d{3})+|\d{5,9})\s*₽", block)
        for amount in amounts:
            value = _parse_rub_amount(amount)
            if value:
                return value
    return _extract_market_estimate_from_text(text, "avito")


def _drom_estimate_from_text(text: str) -> int:
    """Extracts Drom's own price estimate from an opened listing page."""
    if not text:
        return 0
    flat = re.sub(r"\s+", " ", text)
    patterns = (
        r"Дром\s+оценил\s+(\d[\d\s]{1,12})\s*₽",
        r"Оценк[аи]\s+Дрома.{0,160}?Дром\s+оценил\s+(\d[\d\s]{1,12})\s*₽",
        r"drom[^0-9]{0,80}(?:estimate|estimated|valuation|market)[^0-9]{0,80}(\d{5,9})",
    )
    for pat in patterns:
        m = re.search(pat, flat, re.IGNORECASE)
        if not m:
            continue
        value = _parse_rub_amount(m.group(1))
        if value:
            return value
    return 0


def _apply_page_market(item: dict, market: int, lvl: str) -> None:
    price = int(item.get("_price_int") or 0)
    if not price or not market or not (30_000 < market < 50_000_000):
        return
    item[f"_{lvl}_market"] = market
    if lvl not in ("avito", "drom", "autoru"):
        return
    item["_market_price"] = market
    item["_market_lvl"] = lvl
    item["_market_n"] = 30
    item.pop("_market_mileage_factor", None)
    item["_savings_pct"] = round((1 - price / market) * 100, 1)
    item["_below_market"] = _is_strong_below_market(item)


def _avito_item_from_json(it: dict, today) -> dict | None:
    """Преобразует объект Авито JSON в dict объявления. Возвращает None для дилеров."""
    try:
        title = it.get("title", "")
        url_path = (
            it.get("urlPath") or it.get("url") or
            it.get("canonicalUrl") or it.get("shortUrl") or
            it.get("slug") or ""
        )
        if not url_path:
            return None
        if url_path.startswith("/"):
            item_url = "https://www.avito.ru" + url_path
        elif url_path.startswith("http"):
            item_url = url_path
        else:
            item_url = "https://www.avito.ru/" + url_path.lstrip("/")
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
        # Оценка цены самим Авито (если есть) — «хорошая цена»/«ниже рынка» и
        # числовая рыночная стоимость. От неё отталкиваемся в ранжировании.
        _ar_text, _ar_score, _ar_market = _avito_price_rating(it)
        if _ar_text:
            item["_avito_rating"] = _ar_text
            item["_avito_rating_score"] = _ar_score
        if _ar_market:
            item["_avito_market"] = _ar_market
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
    def _looks_like_listing(sample: dict) -> bool:
        """Проверяет, похож ли dict на объявление Авито."""
        # Старый формат: urlPath начинается с /
        url_path = sample.get("urlPath", "")
        if isinstance(url_path, str) and url_path.startswith("/"):
            if any(k in sample for k in ("priceDetailed", "price", "images", "gallery", "photos")):
                return True
            if "id" in sample and "title" in sample and any(s in url_path for s in ("avto", "auto", "avtomobili")):
                return True
        # Новый формат 2025+: поле "slug" или "canonicalUrl" вместо urlPath
        slug_val = sample.get("slug") or sample.get("canonicalUrl") or sample.get("shortUrl") or ""
        if isinstance(slug_val, str) and slug_val and any(k in sample for k in ("priceDetailed", "price", "images", "gallery", "photos")):
            return True
        # url + price/images (точные признаки листинга)
        url_val = sample.get("url", "")
        if isinstance(url_val, str) and ("avito.ru" in url_val or url_val.startswith("/")):
            if any(k in sample for k in ("priceDetailed", "images", "gallery")):
                return True
        # Только по признакам листинга (id + title + price-like + не навигация)
        if "id" in sample and "title" in sample:
            if any(k in sample for k in ("priceDetailed", "price", "images", "gallery", "photos", "stats")):
                # Убеждаемся, что это не навигационный элемент (categories/breadcrumbs)
                if not any(k in sample for k in ("children", "categoryId", "type")) or any(k in sample for k in ("priceDetailed", "images", "gallery")):
                    return True
        return False

    if isinstance(obj, list):
        if len(obj) >= 1 and isinstance(obj[0], dict):
            sample = obj[0]
            if _looks_like_listing(sample):
                url_path = sample.get("urlPath", sample.get("url", ""))
                print(f"  [findItems] найден массив len={len(obj)}, sample_url={url_path!r}")
                return obj
        for x in obj:
            r = _avito_find_items_in_json(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for key in ("items", "catalog", "listing", "offers", "ads", "cars",
                    "search", "results", "snippets", "adverts", "data", "list",
                    "hits", "content", "advertisements", "announcements"):
            val = obj.get(key)
            if isinstance(val, list) and len(val) >= 1 and isinstance(val[0], dict):
                sample = val[0]
                if _looks_like_listing(sample):
                    url_path = sample.get("urlPath", sample.get("url", ""))
                    print(f"  [findItems] найден массив [{key}] len={len(val)}, sample_url={url_path!r}")
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
    # Новые пути 2025-2026 (Авито меняет структуру с каждым Next.js обновлением)
    _di6 = _deep_get(nd, "props.pageProps.initialData.catalog.items")
    _di7 = _deep_get(nd, "props.pageProps.data.items")
    _di8 = _deep_get(nd, "props.pageProps.items")
    _di9 = _deep_get(nd, "props.initialData.catalog.items")
    _di10 = _deep_get(nd, "props.pageProps.initialState.listing.items")
    # Дополнительные пути 2025-2026 для нового формата Авито
    _di11 = _deep_get(nd, "props.pageProps.ssrData.catalog.items")
    _di12 = _deep_get(nd, "props.pageProps.ssrData.items")
    _di13 = _deep_get(nd, "props.pageProps.dehydratedState.queries") and None  # сложная структура — handled below
    _di14 = _deep_get(nd, "props.pageProps.initialState.search.items")
    _di15 = _deep_get(nd, "props.pageProps.initialState.catalog.catalog.items")
    # Попытка найти items в dehydratedState (React Query, новый формат 2025)
    _dehydrated = _deep_get(nd, "props.pageProps.dehydratedState.queries") or []
    _di_dehydrated = None
    if isinstance(_dehydrated, list):
        for _q in _dehydrated:
            _state_data = _deep_get(_q, "state.data") if isinstance(_q, dict) else None
            if isinstance(_state_data, dict):
                _cand = (_state_data.get("items") or _deep_get(_state_data, "catalog.items")
                         or _deep_get(_state_data, "result.items") or _deep_get(_state_data, "data.items"))
                if isinstance(_cand, list) and len(_cand) >= 1 and isinstance(_cand[0], dict):
                    _di_dehydrated = _cand
                    break
    print(f"  [parse] nd found={bool(nd)}, paths: {len(_di1) if _di1 else 0}/{len(_di2) if _di2 else 0}/{len(_di3) if _di3 else 0}/{len(_di4) if _di4 else 0}/{len(_di5) if _di5 else 0}/{len(_di6) if _di6 else 0}/{len(_di7) if _di7 else 0}/{len(_di8) if _di8 else 0}/{len(_di9) if _di9 else 0}/{len(_di10) if _di10 else 0}/{len(_di11) if _di11 else 0}/{len(_di14) if _di14 else 0}/{len(_di15) if _di15 else 0}/{len(_di_dehydrated) if _di_dehydrated else 0}")
    items_raw = (_di1 or _di2 or _di3 or _di4 or _di5 or _di6 or _di7 or _di8 or _di9 or _di10
                 or _di11 or _di12 or _di14 or _di15 or _di_dehydrated or _avito_find_items_in_json(nd))
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


def _avito_api_fetch(
    region: str,
    pages: int,
    price_min: int,
    price_max: int,
    today,
    sort_by_date: bool = False,
    brand: str = "",
    fast: bool = False,
) -> list[dict]:
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
    location_id = _avito_region_loc(region)
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

    _api_start = time.time()

    # ── Метод 0: Официальный мобильный JSON API (m.avito.ru/api/13/items) ──────
    # С российским мобильным IP (Megafone/MTS) работает без авторизации и OAuth.
    # Возвращает структурированный JSON — не нужно парсить HTML.
    if AVITO_PROXIES:
        _key = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
        _mob_params: dict = {
            "key": _key,
            "locationId": location_id,
            "categoryId": 9,
            "params[109][]": 106,
            "page": 1,
            "limit": 100,
            "display": "list",
        }
        if price_min > 0:
            _mob_params["priceMin"] = price_min
        if price_max < 99_000_000:
            _mob_params["priceMax"] = price_max
        _mob_hdrs = {
            "User-Agent": "ru.avito.avitomobile/18.0 (Android 13; ru_RU)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "x-avito-app-version": "18.0.0",
        }
        try:
            # Сначала НАПРЯМУЮ (чистый Railway IP работает), потом через прокси
            try:
                _r_mob = session.get(
                    "https://m.avito.ru/api/13/items",
                    params=_mob_params, headers=_mob_hdrs, timeout=4 if fast else 8,
                )
                print(f"  [Авито mobileAPI0 напрямую] HTTP {_r_mob.status_code}, {len(_r_mob.text):,}б")
                if _r_mob.status_code != 200:
                    raise ValueError("direct non-200")
            except Exception:
                _r_mob = session.get(
                    "https://m.avito.ru/api/13/items",
                    params=_mob_params, headers=_mob_hdrs, timeout=4 if fast else 8,
                    proxies=_avito_proxies(),
                )
                print(f"  [Авито mobileAPI0 прокси] HTTP {_r_mob.status_code}, {len(_r_mob.text):,}б")
            if _r_mob.status_code == 200:
                try:
                    _mob_data = _r_mob.json()
                    _mob_raw = (
                        _deep_get(_mob_data, "result.items")
                        or _deep_get(_mob_data, "result.hits")
                        or _deep_get(_mob_data, "result.catalog.items")
                        or _deep_get(_mob_data, "result.catalog")
                        or _deep_get(_mob_data, "data.items")
                        or _deep_get(_mob_data, "data.catalog.items")
                        or _mob_data.get("items")
                        or _mob_data.get("hits")
                        or _avito_find_items_in_json(_mob_data)
                    )
                    if _mob_raw:
                        _mob_out = [_avito_item_from_json(it, today) for it in _mob_raw]
                        _mob_out = [x for x in _mob_out if x]
                        if _mob_out:
                            print(f"  [Авито mobileAPI0] {len(_mob_out)} объявлений")
                            results.extend(_mob_out)
                    else:
                        _keys = list(_mob_data.keys())[:8] if isinstance(_mob_data, dict) else type(_mob_data).__name__
                        print(f"  [Авито mobileAPI0] нет items, ключи: {_keys}")
                        print(f"  [Авито mobileAPI0] ответ: {_r_mob.text[:500]}")
                except Exception as _e:
                    print(f"  [Авито mobileAPI0] json: {_e}")
        except Exception as _e:
            print(f"  [Авито mobileAPI0] {str(_e)[:60]}")

    # ── Метод 0b: Альтернативные эндпоинты мобильного API ─────────────────────
    # Пробуем новые версии API (v14, v15, v16) которые Авито использует сейчас
    # Пропускаем если уже потратили >10с на метод 0 (чтобы не превысить 70с таймаут бота)
    if not fast and not results and AVITO_PROXIES and (time.time() - _api_start) < 10:
        for _alt_url, _alt_ver in [
            ("https://m.avito.ru/api/16/items", "api16"),
            ("https://m.avito.ru/api/15/items", "api15"),
            ("https://m.avito.ru/api/14/items", "api14"),
            ("https://www.avito.ru/web/1/map/items", "mapItems"),
        ]:
            try:
                _alt_params: dict = {
                    "locationId": location_id,
                    "categoryId": 9,
                    "params[109][]": 106,
                    "page": 1,
                    "limit": 50,
                    "display": "list",
                }
                if price_min > 0:
                    _alt_params["priceMin"] = price_min
                if price_max < 99_000_000:
                    _alt_params["priceMax"] = price_max
                _alt_r = session.get(
                    _alt_url,
                    params=_alt_params,
                    headers={"User-Agent": "ru.avito.avitomobile/18.0 (Android 13; ru_RU)",
                             "Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9"},
                    timeout=8,
                    proxies=_avito_proxies(),
                )
                print(f"  [Авито {_alt_ver}] HTTP {_alt_r.status_code}, {len(_alt_r.text):,}б")
                if _alt_r.status_code == 200:
                    try:
                        _alt_data = _alt_r.json()
                        _alt_raw = (
                            _deep_get(_alt_data, "result.items")
                            or _deep_get(_alt_data, "result.hits")
                            or _deep_get(_alt_data, "result.catalog.items")
                            or _deep_get(_alt_data, "data.items")
                            or _alt_data.get("items")
                            or _avito_find_items_in_json(_alt_data)
                        )
                        if _alt_raw:
                            _alt_out = [_avito_item_from_json(it, today) for it in _alt_raw]
                            _alt_out = [x for x in _alt_out if x]
                            if _alt_out:
                                print(f"  [Авито {_alt_ver}] {len(_alt_out)} объявлений")
                                results.extend(_alt_out)
                                break
                        else:
                            print(f"  [Авито {_alt_ver}] нет items, ответ: {_alt_r.text[:300]}")
                    except Exception as _e:
                        print(f"  [Авито {_alt_ver}] json: {_e}")
            except Exception as _e:
                print(f"  [Авито {_alt_ver}] {str(_e)[:60]}")

    if results:
        print(f"  [Авито API] мобильный API дал {len(results)} объявлений")
        return results

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
                r = session.get(url, params=params, headers=mobile_headers, timeout=8, proxies=_avito_proxies())
                print(f"  [Авито m.] {url} стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200 and ('"urlPath"' in r.text or 'data-marker="item"' in r.text or '__NEXT_DATA__' in r.text):
                    result = _parse_avito_html(r.text, slug, today)
                    if result:
                        return result
            except Exception as e:
                print(f"  [Авито m.] {url} стр.{p}: {e}")
        return []

    def _try_cffi_web(p: int) -> list[dict]:
        """curl_cffi Chrome impersonation с прогревом сессии (куки) — главный метод Авито.

        ВАЖНО (проверено /avito_debug 2026-06): прямой запрос без сессии отдаёт
        200, но скелет-страницу 339КБ без данных (антибот-challenge). Решение:
        curl_cffi Session с прогревом — сначала заходим на главную (получаем куки
        __cf_bm/cookies), потом запрашиваем каталог — тогда отдаёт полную SSR-страницу."""
        try:
            from curl_cffi import requests as _cffi
        except ImportError:
            return []
        _brand_path = f"/{brand}" if brand and brand != "any" else ""
        url = f"https://www.avito.ru/{slug}/avtomobili{_brand_path}"
        params: dict = {"seller_type": "1"}
        if p > 1:
            params["p"] = p
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        _hdrs = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Referer": f"https://www.avito.ru/{slug}",
        }
        # Если прокси работает — идём через прокси первым (прямой IP даёт 339KB скелет-страницу)
        _attempts = []
        if AVITO_PROXIES and not _proxy_auth_failed:
            _attempts.append(_avito_proxies())
        _attempts.append(None)  # прямой как резерв
        for _proxies in _attempts:
            _tag = "напрямую" if _proxies is None else "через прокси"
            for _imp in ("chrome124", "chrome120", "chrome116"):
                try:
                    _sess = _cffi.Session(impersonate=_imp)
                    if _proxies:
                        _sess.proxies = _proxies
                    # Прогрев: заходим на страницу города → получаем куки антибота
                    if p == 1:
                        try:
                            _w = _sess.get(f"https://www.avito.ru/{slug}", timeout=10,
                                           headers={"Accept-Language": "ru-RU,ru;q=0.9",
                                                    "Upgrade-Insecure-Requests": "1"})
                            print(f"  [Авито cffi {_tag}] прогрев {slug}: HTTP {_w.status_code}, куки={len(_sess.cookies)}")
                            time.sleep(0.5)
                        except Exception:
                            pass
                    r = _sess.get(url, params=params, timeout=14, headers=_hdrs)
                    print(f"  [Авито cffi {_tag}] стр.{p} {_imp}: HTTP {r.status_code}, {len(r.text):,}б, куки={len(_sess.cookies)}")
                    if r.status_code == 200 and ('"urlPath"' in r.text or '"canonicalUrl"' in r.text
                                                 or 'data-marker="item"' in r.text or '__NEXT_DATA__' in r.text):
                        res = _parse_avito_html(r.text, slug, today)
                        if res:
                            print(f"  [Авито cffi {_tag}] стр.{p}: {len(res)} объявлений ✅")
                            return res
                        print(f"  [Авито cffi {_tag}] стр.{p}: 200, но парсер 0")
                    elif r.status_code == 200:
                        # 200 но скелет-страница без данных — пробуем следующий профиль/сессию
                        print(f"  [Авито cffi {_tag}] стр.{p} {_imp}: 200 скелет ({len(r.text):,}б), след. профиль")
                        continue
                    elif r.status_code == 429:
                        print(f"  [Авито cffi {_tag}] стр.{p}: 429 rate limit, ждём 3с...")
                        time.sleep(3)
                        break  # этот канал забанен — следующий
                    elif r.status_code in (403, 503):
                        break  # этот канал забанен — следующий
                except Exception as e:
                    print(f"  [Авито cffi {_tag}] стр.{p}: {str(e)[:80]}")
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
            r = cs_session.get(url, params=params, timeout=8, proxies=_avito_proxies())
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
                r = session.get(url, params=params, headers=_hdrs, timeout=8, proxies=_avito_proxies())
                print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: HTTP {r.status_code}, {len(r.text):,}б")
                _has_listing_data = ('"urlPath"' in r.text or '"canonicalUrl"' in r.text or
                                     'data-marker="item"' in r.text or '"shortUrl"' in r.text)
                if r.status_code == 200 and _has_listing_data:
                    res = _parse_avito_html(r.text, slug, today)
                    if res:
                        return res
                    # Страница получена, но парсер вернул 0 — диагностика
                    _has_nd = "__NEXT_DATA__" in r.text
                    _has_items = '"urlPath"' in r.text or '"canonicalUrl"' in r.text
                    _snippet = r.text[r.text.find("__NEXT_DATA__"):r.text.find("__NEXT_DATA__")+300] if _has_nd else r.text[:300]
                    print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: парсер 0, NEXT_DATA={_has_nd}, listing_data={_has_items}")
                    print(f"  [Авито webHTML] snippet: {_snippet[:300]!r}")
                elif r.status_code == 200:
                    print(f"  [Авито webHTML] стр.{p} попытка {attempt+1}: 200 но нет данных объявлений, первые 300б: {r.text[:300]!r}")
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
                timeout=8,
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

    def _try_avito_mobile_api(p: int) -> list[dict]:
        """Официальный мобильный API Авито (m.avito.ru/api/13/items).
        С российским мобильным IP (Megafone/MTS/Beeline) работает без авторизации.
        Возвращает чистый JSON без необходимости парсить HTML."""
        _base_params: dict = {
            "locationId": location_id,
            "categoryId": 9,
            "params[109][]": 106,
            "page": p,
            "limit": 50,
            "display": "list",
            "sortType": "101",  # по дате
        }
        if price_min > 0:
            _base_params["priceMin"] = price_min
        if price_max < 99_000_000:
            _base_params["priceMax"] = price_max

        # Пробуем разные варианты API: новые версии первыми
        _key = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
        _variants = [
            ("https://m.avito.ru/api/16/items", {**_base_params, "key": _key}),
            ("https://m.avito.ru/api/16/items", _base_params),
            ("https://m.avito.ru/api/15/items", {**_base_params, "key": _key}),
            ("https://m.avito.ru/api/14/items", {**_base_params, "key": _key}),
            ("https://m.avito.ru/api/13/items", {**_base_params, "key": _key}),
            ("https://m.avito.ru/api/13/items", _base_params),
            ("https://m.avito.ru/api/9/items",  {**_base_params, "key": _key}),
        ]
        hdrs = {
            "User-Agent": "ru.avito.avitomobile/18.0 (Android 13; ru_RU)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "x-avito-app-version": "18.0.0",
        }
        # Пробуем сначала напрямую (Railway IP), потом через прокси если есть
        _proxy_candidates = [None]
        if AVITO_PROXIES and not _proxy_auth_failed:
            _proxy_candidates.append(_avito_proxies())
        for _url, _params in _variants:
            for _px in _proxy_candidates:
                _tag = "напрямую" if _px is None else "прокси"
                for attempt in range(2):
                    try:
                        r = session.get(
                            _url, params=_params, headers=hdrs, timeout=8,
                            proxies=_px,
                        )
                        print(f"  [Авито mobileAPI {_tag}] стр.{p} {_url.split('/')[-2]}: HTTP {r.status_code}, {len(r.text):,}б")
                        if r.status_code == 200:
                            try:
                                data = r.json()
                            except Exception:
                                print(f"  [Авито mobileAPI] не JSON: {r.text[:100]!r}")
                                break
                            raw = (
                                _deep_get(data, "result.items")
                                or _deep_get(data, "result.catalog.items")
                                or _deep_get(data, "result.hits")
                                or _deep_get(data, "data.items")
                                or data.get("items")
                                or _avito_find_items_in_json(data)
                            )
                            if raw:
                                out = []
                                for it in raw:
                                    item = _avito_item_from_json(it, today)
                                    if item:
                                        out.append(item)
                                if out:
                                    print(f"  [Авито mobileAPI {_tag}] стр.{p}: {len(out)} объявлений ✅")
                                    return out
                                print(f"  [Авито mobileAPI {_tag}] стр.{p}: raw={len(raw)}, после фильтра=0 — sample: {list(raw[0].keys())[:8] if raw else '[]'}")
                            else:
                                _keys = list(data.keys())[:8] if isinstance(data, dict) else type(data).__name__
                                print(f"  [Авито mobileAPI {_tag}] стр.{p}: нет items, ключи: {_keys}")
                                print(f"  [Авито mobileAPI {_tag}] ответ: {r.text[:400]!r}")
                            break  # Ответ получен но items=0 — пробуем следующий вариант
                        elif r.status_code in (403, 429, 503):
                            time.sleep(2)
                            continue
                        else:
                            break
                    except Exception as e:
                        print(f"  [Авито mobileAPI {_tag}] стр.{p}: {str(e)[:60]}")
                        time.sleep(1)
        return []

    def _try_playwright(p: int) -> list[dict]:
        """Playwright (реальный Chromium) — обходит Cloudflare JS-challenge полностью.
        Chromium предустановлен на Railway (/opt/pw-browsers/chromium).
        Только стр.1 — headless слишком медленный для многих страниц."""
        if p > 1:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []
        _brand_path = f"/{brand}" if brand and brand != "any" else ""
        url = f"https://www.avito.ru/{slug}/avtomobili{_brand_path}"
        _params: list[tuple] = [("seller_type", "1")]
        if price_min > 0:
            _params.append(("pmin", str(price_min)))
        if price_max < 99_000_000:
            _params.append(("pmax", str(price_max)))
        qs = "&".join(f"{k}={v}" for k, v in _params)
        full_url = f"{url}?{qs}"
        import glob as _gl, os as _os2
        def _find_chromium() -> str | None:
            for _pat in [
                "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                "/opt/pw-browsers/chromium",
                "/usr/bin/chromium-browser", "/usr/bin/chromium",
                "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
            ]:
                _found = _gl.glob(_pat)
                if _found:
                    return _found[0]
                if _os2.path.exists(_pat):
                    return _pat
            return None
        _exe = _find_chromium()
        try:
            with sync_playwright() as pw:
                launch_opts = {
                    "headless": True,
                    "args": [
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage", "--disable-gpu",
                        "--no-zygote", "--single-process",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--window-size=1280,900",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--mute-audio",
                    ],
                }
                if _exe:
                    launch_opts["executable_path"] = _exe
                browser = pw.chromium.launch(**launch_opts)
                # Playwright ТОЛЬКО через прокси — Railway IP жёстко заблокирован Авито
                _proxy_attempts = []
                if AVITO_PROXIES and not _proxy_auth_failed and AVITO_PROXY_USER:
                    _proxy_attempts.append({
                        "server": f"http://{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}",
                        "username": AVITO_PROXY_USER,
                        "password": AVITO_PROXY_PASS,
                    })
                # НЕ добавляем None (прямой) — Railway IP заблокирован Авито навсегда
                html = ""
                for _proxy_cfg in _proxy_attempts:
                    try:
                        ctx_opts = {
                            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "locale": "ru-RU",
                            "viewport": {"width": 1280, "height": 900},
                            "extra_http_headers": {
                                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                                "sec-ch-ua-mobile": "?0",
                                "sec-ch-ua-platform": '"Windows"',
                            },
                        }
                        if _proxy_cfg:
                            ctx_opts["proxy"] = _proxy_cfg
                        ctx = browser.new_context(**ctx_opts)
                        # Stealth — скрываем признаки headless/automation
                        try:
                            from playwright_stealth import stealth_sync
                            page = ctx.new_page()
                            stealth_sync(page)
                        except ImportError:
                            page = ctx.new_page()
                        # Скрываем webdriver через CDP
                        try:
                            ctx.add_init_script("""
                                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                                Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
                                window.chrome = {runtime: {}};
                            """)
                        except Exception:
                            pass
                        _tag_pw = "прокси" if _proxy_cfg else "напрямую"
                        try:
                            # Прогрев через главную Авито (НЕ через город — ред. по IP-гео ломает регион)
                            page.goto("https://www.avito.ru/", timeout=15000, wait_until="domcontentloaded")
                            page.wait_for_timeout(1500)
                        except Exception:
                            pass
                        page.goto(full_url, timeout=30000, wait_until="networkidle")
                        page.wait_for_timeout(2000)
                        html = page.content()
                        ctx.close()
                        _has_data = '__NEXT_DATA__' in html or '"urlPath"' in html or '"canonicalUrl"' in html
                        _is_banned = "проблема с IP" in html
                        _is_429 = len(html) < 50_000 and ("429" in html or "Too Many" in html)
                        print(f"  [Playwright {_tag_pw}] стр.{p}: {len(html):,}б {'✅' if _has_data else '❌'}{' [бан-IP]' if _is_banned else ''}{' [429]' if _is_429 else ''}")
                        if _has_data:
                            break
                        if _is_429 or _is_banned:
                            break
                    except Exception as _epw:
                        print(f"  [Playwright {_tag_pw if '_tag_pw' in dir() else '?'}] стр.{p}: {str(_epw)[:80]}")
                browser.close()
            print(f"  [Playwright] стр.{p}: {len(html):,}б, данные={'✅' if '__NEXT_DATA__' in html or 'canonicalUrl' in html else '❌'}")
            if '__NEXT_DATA__' in html or '"urlPath"' in html or '"canonicalUrl"' in html:
                res = _parse_avito_html(html, slug, today)
                if res:
                    print(f"  [Playwright] стр.{p}: {len(res)} объявлений ✅")
                return res
        except Exception as e:
            print(f"  [Playwright] стр.{p}: {str(e)[:120]}")
        return []

    def _try_avito_web_json(p: int) -> list[dict]:
        """ГЛАВНЫЙ метод: веб-JSON API Авито (www.avito.ru/web/1/js/items).

        Возвращает структурированный JSON каталога (catalog.items) БЕЗ Cloudflare
        и БЕЗ JS-рендеринга. Проверено эмпирически (2026-06): отдаёт полные данные
        объявлений (title, priceDetailed, urlPath, images, location) даже с
        датацентровых IP. Один запрос на страницу — не сжигает прокси-IP.

        Параметры фильтра (проверено вживую):
          categoryId=9 — автомобили
          locationId   — числовой ID региона
          owner=1      — только частные продавцы
          pmin/pmax    — диапазон цены
          s=104        — сортировка по дате (при sort_by_date)
        """
        try:
            import requests as _req
        except ImportError:
            return []
        _loc = _avito_region_loc(region)
        _params: dict = {
            "categoryId": 9,
            "locationId": _loc,
            "page": p,
            "owner": 1,  # только частники
        }
        if price_min > 0:
            _params["pmin"] = price_min
        if price_max < 99_000_000:
            _params["pmax"] = price_max
        if sort_by_date:
            _params["s"] = 104  # по дате (свежие первыми)
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "x-requested-with": "XMLHttpRequest",
            "Referer": f"https://www.avito.ru/{slug}/avtomobili",
        }
        # Прокси первым (свежий IP), затем напрямую (для js/items датацентр-IP часто проходит).
        # При firewall/429 на прокси — меняем IP и пробуем прокси ещё раз (до 2 ротаций).
        _proxy_order = []
        if AVITO_PROXIES and not _proxy_auth_failed:
            _proxy_order.append(("прокси", _avito_proxies()))
            _proxy_order.append(("прокси-rot1", "ROTATE"))  # сменить IP и повторить
            _proxy_order.append(("прокси-rot2", "ROTATE"))
        _proxy_order.append(("напрямую", None))  # напрямую (датацентр-IP)
        for _tag, _px in _proxy_order:
            # Маркер ROTATE — сменить IP прокси и использовать его же
            if _px == "ROTATE":
                if not _rotate_proxy_ip(min_interval=0):
                    continue  # ротация недоступна — пропускаем
                _px = _avito_proxies()
            try:
                r = _req.get(
                    "https://www.avito.ru/web/1/js/items",
                    params=_params, headers=_hdrs, timeout=20,
                    proxies=_px or {},
                )
                if r.status_code != 200 and r.status_code not in (403, 429):
                    print(f"  [Авито webJSON {_tag}] стр.{p}: HTTP {r.status_code}")
                    continue
                try:
                    data = r.json()
                except Exception:
                    print(f"  [Авито webJSON {_tag}] стр.{p}: HTTP {r.status_code}, не JSON ({len(r.text):,}б)")
                    continue
                # too-many-requests / firewall — IP в лимите, пробуем следующий (ротацию)
                if isinstance(data, dict) and ("too-many-requests" in data or "firewall" in str(data)[:200]):
                    print(f"  [Авито webJSON {_tag}] стр.{p}: firewall (IP лимит) → смена IP")
                    continue
                raw = (data.get("catalog", {}) or {}).get("items", [])
                if not raw:
                    raw = _avito_find_items_in_json(data)
                out: list[dict] = []
                for it in raw:
                    if not isinstance(it, dict) or not it.get("id"):
                        continue
                    parsed = _avito_item_from_json(it, today)
                    if parsed:
                        out.append(parsed)
                if out:
                    print(f"  [Авито webJSON {_tag}] стр.{p}: {len(out)} объявлений ✅ (в каталоге {data.get('count','?')})")
                    return out
                print(f"  [Авито webJSON {_tag}] стр.{p}: raw={len(raw)}, после фильтра=0")
            except Exception as e:
                print(f"  [Авито webJSON {_tag}] стр.{p}: {str(e)[:70]}")
        return []

    def _try_avito_json_api(p: int) -> list[dict]:
        """Avito internal JSON listing endpoint — returns structured data without HTML parsing."""
        try:
            import requests as _req
        except ImportError:
            return []
        location_id = _avito_region_loc(region)
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
                proxies=_avito_proxies() or {},
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

    def _try_avito_xhr(p: int) -> list[dict]:
        """Авито XHR API — внутренний эндпоинт, который сайт использует при AJAX-пагинации.
        Работает с российским IP (прокси). Возвращает JSON с полными объявлениями."""
        if not AVITO_PROXIES:
            return []
        _brand_path = f"/{brand}" if brand and brand != "any" else ""
        _xhr_url = f"https://www.avito.ru/{slug}/avtomobili{_brand_path}"
        _params: dict = {"seller_type": "1", "forceLocal": "1", "output": "json"}
        if p > 1:
            _params["p"] = p
        if price_min > 0:
            _params["pmin"] = price_min
        if price_max < 99_000_000:
            _params["pmax"] = price_max
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.avito.ru/{slug}/avtomobili",
        }
        for _xurl in [
            f"https://www.avito.ru/{slug}/avtomobili{_brand_path}",
            f"https://www.avito.ru/api/11/items?locationId={location_id}&categoryId=9&page={p}",
        ]:
            try:
                r = session.get(_xurl, params=_params, headers=_hdrs, timeout=8, proxies=_avito_proxies())
                print(f"  [Авито XHR] {_xurl.split('?')[0].split('/')[-1]} стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200:
                    ct = r.headers.get("Content-Type", "")
                    if "json" in ct:
                        try:
                            data = r.json()
                            raw = (data.get("items") or _deep_get(data, "result.items")
                                   or _deep_get(data, "data.items") or _avito_find_items_in_json(data))
                            if raw:
                                out = [_avito_item_from_json(it, today) for it in raw]
                                out = [x for x in out if x]
                                if out:
                                    print(f"  [Авито XHR] {len(out)} объявлений")
                                    return out
                        except Exception:
                            pass
                    elif '"urlPath"' in r.text or '"canonicalUrl"' in r.text:
                        res = _parse_avito_html(r.text, slug, today)
                        if res:
                            print(f"  [Авито XHR] HTML fallback: {len(res)} объявлений")
                            return res
            except Exception as e:
                print(f"  [Авито XHR] ошибка: {str(e)[:60]}")
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

    def _try_yandex_snippets(
        p: int,
        max_seconds: int = 45,
        max_results: int = 40,
    ) -> list[dict]:
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
        _price_re = re.compile(
            r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d{3,9})"
            r"\s*(?:₽|тыс\.?\s*р(?:уб)?\.?|руб\.?)",
            re.I,
        )
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

        def _parse_serp(serp_html: str) -> list[dict]:
            import html as _html_module
            import urllib.parse as _upq
            out: list[dict] = []
            ctx_html = serp_html
            for _ in range(2):
                try:
                    ctx_html = _upq.unquote(ctx_html)
                except Exception:
                    break
            found_urls = _extract_avito_urls(ctx_html)
            for url in found_urls:
                clean_url = url.split("?")[0]
                pos = ctx_html.find(url)
                # Сниппет результата находится после ссылки. Текст до неё может
                # содержать цену предыдущей машины или верхнюю границу запроса.
                context = ctx_html[pos:pos+1_200] if pos >= 0 else ""
                context_clean = _html_module.unescape(re.sub(r"<[^>]+>", " ", context))
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

        # Полный режим собирает широкий рынок, быстрый fallback ограничивается
        # несколькими секундами, чтобы не задерживать ответ пользователю.
        _ddg_deadline = time.time() + max_seconds

        for brand in _all_brands:
            if len(results_out) >= max_results:
                break
            if time.time() > _ddg_deadline:
                print(f"  [ddg] тайм-лимит {max_seconds}с, остановка на {brand}")
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

    # Пользовательский поиск не должен зависать на десятках антибот-методов.
    # После двух быстрых запросов берём реальные проиндексированные ссылки и
    # возвращаем управление; полный медленный режим остаётся для мониторинга.
    if fast:
        try:
            indexed = _try_yandex_snippets(1, max_seconds=6, max_results=12)
        except Exception as exc:
            indexed = []
            print(f"  [Авито fast fallback] {str(exc)[:60]}")
        if indexed:
            print(f"  [Авито fast fallback] {len(indexed)} объявлений из поискового индекса")
        return indexed

    # free_proxies даёт настоящую страницу Авито (десятки объявлений), DuckDuckGo —
    # ещё несколько. Запускаем ВСЁ параллельно и СЛИВАЕМ результаты, а не берём
    # первый ответивший метод (иначе теряем большие пачки, что приходят чуть позже).
    # webJSON (/web/1/js/items) первым ВЕЗДЕ — главный рабочий метод (JSON, без JS/Cloudflare).
    _no_proxy_methods = [_try_avito_web_json, _try_playwright, _try_scraperapi_fast, _try_free_proxies, _try_yandex_snippets, _try_cffi_web, _try_curl_cffi, _try_cs_web, _try_mobile_site, _try_web_html, _try_avito_mobile_api, _try_avito_public_api, _try_avito_rss, _try_googlebot_ua, _try_avito_lite, _try_scraperapi, _try_avito_json_api]
    # При наличии рабочего ScraperAPI не тратим пользовательский поиск на
    # мобильный IP, уже попавший под 429/капчу: резидентный API становится
    # основным маршрутом, а остальные методы остаются резервом.
    _use_proxy = AVITO_PROXIES and not _proxy_auth_failed and not SCRAPER_API_KEY
    if _use_proxy:
        # Платный прокси (московский мобильный IP, Megafone/MTS).
        all_methods = [_try_avito_web_json, _try_avito_mobile_api, _try_web_html, _try_mobile_site, _try_avito_rss, _try_avito_json_api, _try_avito_xhr, _try_playwright, _try_googlebot_ua, _try_avito_lite, _try_avito_public_api]
    else:
        all_methods = _no_proxy_methods
    # При наличии прокси — пробуем методы ПОСЛЕДОВАТЕЛЬНО (не параллельно).
    # Параллельные запросы через один IP = мгновенный 429.
    # Остановиться при первом методе давшем объявления.
    if _use_proxy:
        # ТОЛЬКО быстрые JSON-методы. Каждый = 1 запрос, ~1-2с.
        # HTML/Playwright методы выброшены: они медленные (25-30с), требуют JS
        # (отдают Cloudflare-скелет) и детектятся Авито как бот. Через прокси
        # они только жгут IP и время. Если JSON-методы не прошли (IP в 429) —
        # быстро выходим и показываем Дром/ВК/TG, а не ждём 55с впустую.
        _p1_methods = [_try_avito_web_json, _try_avito_mobile_api,
                       _try_avito_json_api, _try_avito_xhr]
        # Доп. страницы добавим тем же методом что сработал
        tasks = [(m, 1) for m in _p1_methods]
        _cap = 300
        _deadline_s = 20
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

    merged: dict[str, dict] = {}
    _seen_keys: set = set()
    _soft_deadline = time.time() + _deadline_s

    if _use_proxy:
        # Последовательный перебор методов — берём первый давший результат
        _working_proxy_method = None
        for _m_seq, _pg_seq in tasks:
            if time.time() > _soft_deadline:
                break
            try:
                _seq_res = _m_seq(_pg_seq)
            except Exception:
                _seq_res = []
            if _seq_res:
                _working_proxy_method = _m_seq
                for it in _seq_res:
                    u = it.get("url")
                    key = _listing_key(u)
                    if u and key and key not in _seen_keys:
                        _seen_keys.add(key)
                        merged[u] = it
                print(f"  [Авито] {_m_seq.__name__} стр.1: {len(_seq_res)} объявлений ✅")
                break
        # Если нашли рабочий метод — докачиваем стр. 2-4 через него же
        if _working_proxy_method and _working_proxy_method not in (_try_yandex_snippets, _try_free_proxies):
            for _extra_p in [2, 3, 4]:
                if len(merged) >= _cap or time.time() > _soft_deadline:
                    break
                try:
                    _extra = _working_proxy_method(_extra_p)
                except Exception:
                    _extra = []
                if not _extra:
                    break
                added = 0
                for it in _extra:
                    u = it.get("url")
                    key = _listing_key(u)
                    if u and key and key not in _seen_keys:
                        _seen_keys.add(key)
                        merged[u] = it
                        added += 1
                print(f"  [Авито] {_working_proxy_method.__name__} стр.{_extra_p}: +{added}")
                if added == 0:
                    break
        _ex = None  # нет пула потоков в proxy-режиме
    else:
        _ex = _TPE(max_workers=min(6, len(tasks)))
    try:
      if not _use_proxy and _ex:
        fut_map = {_ex.submit(m, pg): (m, pg) for (m, pg) in tasks}
        for fut in _as_completed(fut_map, timeout=45):
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
            if len(merged) >= _cap or (merged and time.time() > _soft_deadline):
                break
    except Exception as e:
        print(f"  [Авито API] пул: {str(e)[:60]}")
    finally:
        if _ex:
            _ex.shutdown(wait=False)

    results = list(merged.values())

    # Мобильный прокси может быть корректно авторизован, но его текущий IP уже
    # заблокирован Авито кодами 403/429. В этом случае не возвращаем пустоту:
    # берём короткий срез реальных ссылок Авито из поискового индекса.
    if not results and _use_proxy and not _proxy_auth_failed:
        try:
            indexed = _try_yandex_snippets(1, max_seconds=10, max_results=12)
        except Exception as exc:
            indexed = []
            print(f"  [Авито fallback] поиск: {str(exc)[:60]}")
        for item in indexed:
            url = item.get("url", "")
            key = _listing_key(url)
            if url and key and key not in _seen_keys:
                _seen_keys.add(key)
                merged[url] = item
        results = list(merged.values())
        if results:
            print(f"  [Авито fallback] {len(results)} объявлений из поискового индекса")

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
_AVITO_NETWORK_LOCK = _threading.Lock()
# Один мобильный прокси обслуживает обе защищённые площадки. Фоновые Авито и
# Auto.ru не должны конкурировать ни внутри источника, ни друг с другом.
_AVITO_BACKGROUND_LOCK = _AUTORU_BACKGROUND_LOCK


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


def scrape_avito(
    region: str,
    pages: int = 5,
    price_min: int = 0,
    price_max: int = 99_000_000,
    sort_by_date: bool = False,
    brand: str = "",
    fast: bool = False,
) -> list[dict]:
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
        # ⚡ Ранний доступ: в режиме мониторинга (sort_by_date) кэш всего 8 мин,
        # чтобы новые объявления находились почти сразу. Обычный поиск — 90 мин.
        _cache_ttl = 8 * 60 if sort_by_date else 90 * 60
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
        # Все запросы Авито проходят через один мобильный IP. Параллельные
        # обращения из поиска, прогрева и мониторинга мгновенно дают 429.
        _got_avito_slot = _AVITO_NETWORK_LOCK.acquire(timeout=5 if fast else 1)
        if _got_avito_slot:
            try:
                items = _scrape_avito_raw(
                    region,
                    pages=pages,
                    price_min=_scrape_pmin,
                    price_max=_scrape_pmax,
                    sort_by_date=sort_by_date,
                    brand=brand,
                    fast=fast,
                )
            finally:
                _AVITO_NETWORK_LOCK.release()
        else:
            items = []
            print(f"  [Авито] сеть занята другим запросом — использую кэш {region}")
        if items:
            _AVITO_REGION_CACHE[cache_key] = (now, items)
            # Запись кэша на диск — в фоне, чтобы не держать пользователя. Делаем
            # дешёвый shallow-снимок СИНХРОННО (защита от dict-changed-during-iter),
            # а тяжёлые json.dumps + write выносим в фоновый поток.
            try:
                _snap = dict(_AVITO_REGION_CACHE)
                def _flush_cache(_data=_snap):
                    try:
                        _AVITO_CACHE_FILE.write_text(
                            json.dumps({"version": 4, "data": _data}, ensure_ascii=False),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                _threading.Thread(target=_flush_cache, daemon=True).start()
            except Exception:
                pass
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


def _scrape_avito_background(
    region: str,
    pages: int = 1,
    price_min: int = 0,
    price_max: int = 99_000_000,
    sort_by_date: bool = True,
) -> list[dict]:
    """One background Avito request at a time; user searches keep priority."""
    if not _AVITO_BACKGROUND_LOCK.acquire(blocking=False):
        best: list[dict] = []
        for key, (_ts, cached_items) in list(_AVITO_REGION_CACHE.items()):
            if key == region or key.startswith(region + "_"):
                if len(cached_items or []) > len(best):
                    best = cached_items or []
        return [
            dict(it) for it in best
            if (not it.get("_price_int"))
            or (price_min <= int(it.get("_price_int") or 0) <= price_max)
        ]
    try:
        return scrape_avito(
            region,
            pages=pages,
            price_min=price_min,
            price_max=price_max,
            sort_by_date=sort_by_date,
            fast=True,
        )
    finally:
        _AVITO_BACKGROUND_LOCK.release()


def _scrape_avito_raw(
    region: str,
    pages: int = 5,
    price_min: int = 0,
    price_max: int = 99_000_000,
    sort_by_date: bool = False,
    brand: str = "",
    fast: bool = False,
) -> list[dict]:
    """
    Бесплатный парсер Авито. Стратегия (порядок попыток):
    1. _avito_api_fetch: cloudscraper+Android UA, m.avito.ru, публичный API, веб-API —
       всё это легче проходит с датацентровых IP, чем десктопный скрейпинг.
    2. Прямой HTTP-запрос с Desktop UA (иногда работает в определённых регионах).
    3. Headless Playwright + stealth — последний резерв, требует больше времени.
    """
    slug = AVITO_SLUGS.get(region, region)
    today = datetime.date.today()

    # Перед сетевым скрейпом (кэш-промах) меняем IP прокси на свежий, чтобы
    # обойти rate-limit Авито (429). min_interval=8с — каждый поиск стартует
    # со свежим IP, но защита от слишком частой ротации (лимиты провайдера).
    if AVITO_PROXIES and not _proxy_auth_failed:
        _rotate_proxy_ip(min_interval=8)

    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except ImportError:
        return []

    # ── Метод 1: API / мобильный сайт / cloudscraper ─────────────
    print(f"  [Авито] пробуем API-методы для {region}…")
    api_results = _avito_api_fetch(
        region,
        pages,
        price_min,
        price_max,
        today,
        sort_by_date=sort_by_date,
        brand=brand,
        fast=fast,
    )
    if api_results:
        print(f"  [Авито] API-метод дал {len(api_results)} объявлений")
        return api_results
    if fast:
        print("  [Авито] быстрый режим: API/индекс пусты, HTML-ветку не запускаем")
        return []
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


class MyDeals(StatesGroup):
    add_title = State()
    add_buy = State()
    add_expenses = State()
    sell_price = State()


# ── 🚗 Мои сделки (аналитика перекупа) ───────────────────────────
def _load_deals(uid: int) -> list[dict]:
    f = user_dir(uid) / "my_deals.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_deals(uid: int, deals: list[dict]):
    f = user_dir(uid) / "my_deals.json"
    f.write_text(json.dumps(deals, ensure_ascii=False, indent=2), encoding="utf-8")


def _deals_summary(deals: list[dict]) -> str:
    """Сводка по сделкам: вложено, в работе, продано, прибыль, средний срок, ROI."""
    in_work = [d for d in deals if d.get("status") == "active"]
    sold = [d for d in deals if d.get("status") == "sold"]
    invested = sum(d.get("buy", 0) + d.get("expenses", 0) for d in in_work)
    total_profit = sum(d.get("profit", 0) for d in sold)
    total_cost_sold = sum(d.get("buy", 0) + d.get("expenses", 0) for d in sold)
    roi = (total_profit / total_cost_sold * 100) if total_cost_sold else 0
    # Средний срок продажи (дней между buy_ts и sell_ts)
    _days = [
        int((d["sell_ts"] - d["buy_ts"]) / 86400)
        for d in sold if d.get("sell_ts") and d.get("buy_ts") and d["sell_ts"] >= d["buy_ts"]
    ]
    avg_days = int(sum(_days) / len(_days)) if _days else 0
    lines = [
        "💼 *Мои сделки — аналитика*",
        "",
        f"🔧 В работе: *{len(in_work)}* (вложено {invested:,} ₽)".replace(",", " "),
        f"✅ Продано: *{len(sold)}*",
        f"💰 Прибыль: *{total_profit:+,} ₽*".replace(",", " "),
    ]
    if sold:
        lines.append(f"📈 ROI: *{roi:+.1f}%*")
        if avg_days:
            lines.append(f"⏱ Средний срок продажи: *{avg_days} дн.*")
    return "\n".join(lines)


def _deals_keyboard(deals: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить авто", callback_data="deal_add")]]
    for d in deals:
        if d.get("status") == "active":
            did = d.get("id", "")
            rows.append([
                InlineKeyboardButton(text=f"✅ Продал: {d.get('title','')[:22]}", callback_data=f"deal_sell|{did}"),
                InlineKeyboardButton(text="🗑", callback_data=f"deal_del|{did}"),
            ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    """Подписка отключена — пропускаем всех. Заодно регистрируем пользователя
    в надёжном реестре PG (счётчик статистики, переживает деплой)."""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        try:
            u = getattr(event, "from_user", None)
            if u and u.id:
                # Только регистрация/last_seen; поиск считается в do_search
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _register_user(u.id, u.username, False)
                )
        except Exception:
            pass
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
        [KeyboardButton(text="💼 Мои сделки"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💎 Купить подписку"), KeyboardButton(text="🛟 Поддержка")],
        [KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="🤝 Пригласить друга"), KeyboardButton(text="♻️ Сбросить историю")],
    ],
    resize_keyboard=True,
    persistent=True,
)

# Клавиатура админа = обычная + строка «📊 Статистика»
_ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Найти авто"), KeyboardButton(text="🌐 Глобальный поиск")],
        [KeyboardButton(text="🆕 Новые сегодня"), KeyboardButton(text="🎯 Следить за маркой")],
        [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🚗 Мой гараж")],
        [KeyboardButton(text="💼 Мои сделки"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="💎 Купить подписку"), KeyboardButton(text="🛟 Поддержка")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="❓ Помощь")],
        [KeyboardButton(text="🤝 Пригласить друга"), KeyboardButton(text="♻️ Сбросить историю")],
    ],
    resize_keyboard=True,
    persistent=True,
)


def kb_for(uid: int) -> ReplyKeyboardMarkup:
    """Клавиатура с учётом прав: админ видит кнопку «📊 Статистика»."""
    return _ADMIN_KEYBOARD if uid in ADMIN_IDS else MAIN_KEYBOARD


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
    "питбайк", "мотик", "мотороллер", "вятка электрон", "вятка", "vespa", "yamaha ybr", "honda cbr",
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


_PROMO_PHRASES = (
    "способ продвижения", "наверху списка", "наверх списка", "просматривают чаще",
    "поднять объявление", "продвижение объявления", "vip-размещение", "vip размещение",
    "платное размещение", "поднятие в поиске", "выделить объявление",
)
_PLACEHOLDER_TITLES = ("авто на auto.ru", "авто на авито", "авто на дром", "автомобиль")
_NON_CAR_GOODS_KEYWORDS = (
    "коляск", "carrello", "люльк", "автолюльк", "детск", "ребенк", "ребёнк",
    "пк", "компьютер", "монитор", "клавиатур", "клава", "мышк", "наушник",
    "видеокарт", "процессор", "материнск", "оперативн", "ssd", "hdd",
    "телефон", "смартфон", "iphone", "айфон", "samsung", "ноутбук",
    "холодильник", "стиральн", "диван", "кровать", "шкаф",
)
_NON_CAR_GOODS_RE = re.compile(
    r"(?<!маш)(?:\bшин(?:ы|а|у|ами|ах)?\b|\bрезин(?:а|у|ы|ой)?\b|"
    r"\bпокрышк\w*\b|\bкол[её]с(?:а|о|ный|ные)?\b|"
    r"\bдиск(?:и|ов)?\s*r?\d{0,2}\b|\bкомплект\s+(?:шин|резин|кол[её]с|диск))",
    re.IGNORECASE,
)


def _is_non_car_goods(it: dict) -> bool:
    blob = f'{it.get("title", "")} {it.get("description", "")}'.lower()
    if not blob.strip():
        return False
    if any(k in blob for k in _NON_CAR_GOODS_KEYWORDS):
        return True
    if _NON_CAR_GOODS_RE.search(blob):
        return True
    return False


def _is_promo_listing(it: dict) -> bool:
    """True для рекламы площадки / промо-блоков, а не реальных объявлений."""
    title = (it.get("title", "") or "").strip().lower()
    if title in _PLACEHOLDER_TITLES:
        return True
    blob = (title + " " + (it.get("description", "") or "")).lower()
    return any(p in blob for p in _PROMO_PHRASES)


def _filter_by_category(items: list[dict], category: str, brand: str) -> list[dict]:
    """Фильтрует список объявлений по категории и марке. Всегда исключает мото/скутеры."""
    # Убираем рекламу площадки / промо-блоки и мото/скутеры.
    # Для VK/TG обязательно смотрим не только title, но и описание: заголовок часто
    # короткий («Продам Вятку электрон.»), а признаки мото/деталей лежат в тексте.
    items = [it for it in items if not _is_promo_listing(it) and not _is_non_car_goods(it)]
    items = [
        it for it in items
        if not _is_moto(f'{it.get("title", "")} {it.get("description", "")}'[:700])
    ]

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
        # Принимаем оба формата: "ref_123" и "ref123" (на случай старых ссылок)
        _ref_digits = ""
        if param.startswith("ref_"):
            _ref_digits = param[4:]
        elif param.startswith("ref") and param[3:].isdigit():
            _ref_digits = param[3:]
        if _ref_digits.isdigit():
            try:
                inviter_uid = int(_ref_digits)
                if inviter_uid == msg.from_user.id:
                    # Пользователь открыл СВОЮ ссылку (тестирует) — подтверждаем, что
                    # ссылка рабочая, но себя пригласить нельзя.
                    await msg.answer(
                        "✅ *Ссылка работает!*\n\n"
                        "Это твоя реферальная ссылка — себя пригласить нельзя.\n"
                        "Отправь её другу: когда он перейдёт и запустит бота, "
                        "я сразу пришлю тебе уведомление 🔔",
                        parse_mode="Markdown",
                    )
                elif inviter_uid != msg.from_user.id:
                    _ref_res = _record_referral(msg.from_user.id, inviter_uid)
                    # Уведомляем пригласившего, что друг перешёл по его ссылке
                    if _ref_res and _ref_res.get("is_new"):
                        _fname = msg.from_user.first_name or "Друг"
                        _un = f" (@{msg.from_user.username})" if msg.from_user.username else ""
                        _cnt = _ref_res.get("count", 0)
                        _txt = (
                            f"🎉 *По твоей ссылке перешёл друг!*\n\n"
                            f"👤 {_fname}{_un}\n"
                            f"👥 Всего приглашено: *{_cnt}*\n"
                            f"🎁 +3 дня доступа (всего бонусом: {_ref_res.get('bonus_days', 0)} дн.)"
                        )
                        if _ref_res.get("milestone"):
                            _txt += "\n\n🏆 *10 друзей — +30 дней сверху!*"
                        try:
                            await bot.send_message(inviter_uid, _txt, parse_mode="Markdown")
                        except Exception:
                            pass
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
            reply_markup=kb_for(msg.from_user.id),
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
            reply_markup=kb_for(msg.from_user.id),
        )


@dp.message(Command("avito_debug"))
async def cmd_avito_debug(msg: Message):
    """Диагностика прокси и Авито — только для админов.
    ВАЖНО: делает минимум запросов чтобы не сжигать прокси IP."""
    if msg.from_user.id not in ADMIN_IDS:
        return
    await msg.answer("🔍 Тестирую прокси и Авито (минимум запросов)...")
    loop = asyncio.get_running_loop()

    def _run_test():
        import requests as _rq, datetime as _dt, re as _re, glob as _gl, os as _os_d
        out = []

        # 1. IP-адреса (безопасно — не трогают Авито)
        try:
            ip = _rq.get("https://api.ipify.org", timeout=5).text.strip()
            out.append(f"🌐 Railway IP: {ip}")
        except Exception as e:
            out.append(f"🌐 Railway IP: ошибка {e}")

        if AVITO_PROXIES:
            # Ротация IP перед тестом — свежий IP не зарейтлимичен
            if AVITO_PROXY_ROTATE_URL:
                _rot_ok = _rotate_proxy_ip(min_interval=0)
                out.append(f"🔄 Ротация IP: {'✅ выполнена' if _rot_ok else '❌ не сработала'}")
            else:
                out.append("🔄 Ротация IP: НЕ настроена (задайте AVITO_PROXY_ROTATE_URL)")
            try:
                ip2 = _rq.get("https://api.ipify.org", proxies=_avito_proxies(), timeout=8).text.strip()
                out.append(f"🔀 Прокси IP: {ip2} ✅")
                out.append(f"   Прокси: {AVITO_PROXY_HOST}:{AVITO_PROXY_PORT} / {AVITO_PROXY_USER}:***")
            except Exception as e:
                out.append(f"🔀 Прокси: ❌ {str(e)[:80]}")
        else:
            out.append("🔀 Прокси: не настроен (PROXY_URL не задан)")

        # 2. ГЛАВНЫЙ метод: веб-JSON API Авито (/web/1/js/items) — структурированный
        #    каталог без Cloudflare/JS. Сначала через прокси, потом напрямую.
        _avito_ok = False
        _web_hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9",
            "x-requested-with": "XMLHttpRequest",
            "Referer": "https://www.avito.ru/ekaterinburg/avtomobili",
        }
        # Тестируем реальный городской locationId (Екатеринбург), а не Москву —
        # иначе debug маскирует ошибки в ID конкретных городов.
        _web_params = {"categoryId": 9, "locationId": AVITO_LOCATION_IDS.get("ekaterinburg", 654070), "page": 1, "owner": 1, "pmax": 300000}
        for _wtag, _wpx in ([("прокси", _avito_proxies())] if AVITO_PROXIES else []) + [("напрямую", None)]:
            if _avito_ok:
                break
            try:
                _wr = _rq.get("https://www.avito.ru/web/1/js/items",
                              params=_web_params, headers=_web_hdrs,
                              proxies=_wpx or {}, timeout=18)
                if _wr.status_code == 200:
                    try:
                        _wd = _wr.json()
                    except Exception:
                        _wd = {}
                    _witems = [x for x in (_wd.get("catalog", {}) or {}).get("items", [])
                               if isinstance(x, dict) and x.get("id")]
                    if _witems:
                        _avito_ok = True
                        out.append(f"🟢 webJSON {_wtag}: HTTP 200, объявлений={len(_witems)} ✅ (каталог {_wd.get('count','?')})")
                        out.append(f"   → первое: {_witems[0].get('title','?')[:50]} | {_witems[0].get('priceDetailed',{}).get('string','?')} ₽")
                    elif "too-many-requests" in _wd:
                        out.append(f"🟡 webJSON {_wtag}: firewall (IP лимит) — нужен свежий IP")
                    else:
                        out.append(f"🟡 webJSON {_wtag}: HTTP 200 но items=0, ключи={list(_wd.keys())[:6]}")
                elif _wr.status_code == 429:
                    out.append(f"🟡 webJSON {_wtag}: HTTP 429 (IP лимит) — смените IP или подождите")
                else:
                    out.append(f"🟡 webJSON {_wtag}: HTTP {_wr.status_code}")
            except Exception as e:
                out.append(f"🟡 webJSON {_wtag}: ❌ {str(e)[:80]}")

        # 3. curl_cffi через прокси (веб-интерфейс, нужен JS — для диагностики)
        if not _avito_ok and AVITO_PROXIES:
            try:
                from curl_cffi import requests as _cffi
                _r = _cffi.get(
                    "https://www.avito.ru/moskva/avtomobili",
                    params={"seller_type": "1", "pmax": "300000"},
                    proxies=_avito_proxies(),
                    impersonate="chrome124", timeout=15,
                    headers={"Accept-Language": "ru-RU,ru;q=0.9",
                             "Referer": "https://www.avito.ru/",
                             "Accept": "text/html,application/xhtml+xml,*/*;q=0.9"})
                _has = '"urlPath"' in _r.text or '"canonicalUrl"' in _r.text or '__NEXT_DATA__' in _r.text
                _perm_ban = _r.status_code == 200 and "проблема с IP" in _r.text
                _rate_limit = _r.status_code == 429
                _status_emoji = "✅" if _has else ("⛔" if _perm_ban else ("⏳" if _rate_limit else "❌"))
                out.append(f"🌐 Авито HTML через прокси: HTTP {_r.status_code}, {len(_r.text):,}б {_status_emoji}")
                if _has:
                    _avito_ok = True
                    _parsed = _parse_avito_html(_r.text, "moskva", _dt.date.today())
                    out.append(f"   → объявлений: {len(_parsed)} {'✅' if _parsed else '⚠️'}")
                elif _perm_ban:
                    out.append(f"   ⛔ IP постоянно заблокирован — смените IP")
                elif _rate_limit:
                    out.append(f"   ⏳ rate-limit 429")
                else:
                    _title_m = _re.search(r'<title>([^<]{0,80})', _r.text)
                    out.append(f"   → title: {repr(_title_m.group(1)) if _title_m else '?'} (нужен JS для данных)")
            except Exception as e:
                out.append(f"🌐 Авито через прокси: ❌ {str(e)[:120]}")

        # 3. Playwright через прокси (если curl_cffi не дал данных)
        if not _avito_ok:
            try:
                from playwright.sync_api import sync_playwright as _spw
                def _find_pw():
                    for _pat in ["/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                                  "/opt/pw-browsers/chromium", "/usr/bin/chromium-browser",
                                  "/usr/bin/chromium", "/usr/bin/google-chrome-stable"]:
                        _f = _gl.glob(_pat)
                        if _f: return _f[0]
                        if _os_d.path.exists(_pat): return _pat
                    return None
                _exe = _find_pw()
                out.append(f"🎭 Playwright: {_exe or 'авто-поиск'}")
                with _spw() as _pw:
                    _lopts = {"headless": True, "args": [
                        "--no-sandbox","--disable-setuid-sandbox",
                        "--disable-dev-shm-usage","--disable-gpu",
                        "--no-zygote","--single-process",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars","--window-size=1280,900",
                        "--disable-extensions","--disable-background-networking",
                        "--disable-default-apps","--mute-audio",
                    ]}
                    if _exe:
                        _lopts["executable_path"] = _exe
                    _br = _pw.chromium.launch(**_lopts)
                    _ctx_opts = {
                        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "locale": "ru-RU",
                        "viewport": {"width": 1280, "height": 900},
                        "extra_http_headers": {
                            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                            "sec-ch-ua-mobile": "?0",
                            "sec-ch-ua-platform": '"Windows"',
                        },
                    }
                    if AVITO_PROXIES and not _proxy_auth_failed and AVITO_PROXY_USER:
                        _ctx_opts["proxy"] = {
                            "server": f"http://{AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}",
                            "username": AVITO_PROXY_USER,
                            "password": AVITO_PROXY_PASS,
                        }
                        out.append(f"   → прокси: {AVITO_PROXY_HOST}:{AVITO_PROXY_PORT}")
                    else:
                        out.append(f"   → ⚠️ прокси не настроен, Railway IP заблокирован")
                    _ctx = _br.new_context(**_ctx_opts)
                    try:
                        from playwright_stealth import stealth_sync as _stealth
                        _pg = _ctx.new_page()
                        _stealth(_pg)
                        out.append(f"   → stealth ✅")
                    except ImportError:
                        _pg = _ctx.new_page()
                    try:
                        _ctx.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                            window.chrome = {runtime: {}};
                        """)
                    except Exception:
                        pass
                    # Прогрев через главную (НЕ через город — редирект ломает регион)
                    try:
                        _pg.goto("https://www.avito.ru/", timeout=12000, wait_until="domcontentloaded")
                        _pg.wait_for_timeout(1500)
                    except Exception:
                        pass
                    # Используем Москву т.к. прокси московский (без ред. по региону)
                    _pg.goto("https://www.avito.ru/moskva/avtomobili?seller_type=1&pmax=300000",
                             timeout=25000, wait_until="networkidle")
                    _cur_url = _pg.url
                    _html = _pg.content()
                    _br.close()
                _has_pw = "__NEXT_DATA__" in _html or '"canonicalUrl"' in _html
                _perm_pw = _r.status_code == 200 and "проблема с IP" in _html if '_r' in dir() else "проблема с IP" in _html
                out.append(f"   → URL после загрузки: {_cur_url[:80]}")
                out.append(f"   → {len(_html):,}б, данные: {'✅' if _has_pw else ('⛔[IP-бан]' if _perm_pw else '❌')}")
                if _has_pw:
                    _pp = _parse_avito_html(_html, "moskva", _dt.date.today())
                    out.append(f"   → парсер: {len(_pp)} объявлений {'✅' if _pp else '⚠️'}")
                    if _pp:
                        out.append(f"   → первое: {_pp[0].get('title','?')[:50]} | {_pp[0].get('price','?')}")
                else:
                    _title = _re.search(r'<title>([^<]{0,60})', _html)
                    out.append(f"   → title: {repr(_title.group(1)) if _title else '?'}")
            except ImportError:
                out.append("🎭 Playwright: ❌ библиотека не установлена")
            except Exception as e:
                out.append(f"🎭 Playwright: ❌ {str(e)[:150]}")

        return out

    try:
        lines = await loop.run_in_executor(None, _run_test)
    except Exception as e:
        lines = [f"Ошибка: {e}"]

    await msg.answer("\n".join(lines)[:4000])


# ════════════════════════════════════════════════════════════════
# АДМИН-РАЗДЕЛ «СТАТИСТИКА» (только для ADMIN_IDS)
# Построен на существующем файловом модуле analytics + settings.json.
# ════════════════════════════════════════════════════════════════
_MSK = datetime.timezone(datetime.timedelta(hours=3))
_RU_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _fmt_n(n) -> str:
    """Число с пробелами-разделителями: 1234 → '1 234'."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def _msk_date(ts):
    try:
        return datetime.datetime.fromtimestamp(ts, _MSK).date()
    except Exception:
        return None


def _admin_collect():
    """Собирает сырьё: профили, события, кол-во мониторингов, подписки.
    Источник пользователей — НАДЁЖНЫЙ реестр PG (переживает деплой); файловая
    analytics добавляется поверх для тех, кого ещё нет в PG."""
    users = dict(analytics.load_users())
    db_users = _db_users()  # PG-реестр (авторитетный)
    # Сливаем: PG-данные приоритетнее (полнее и сохраняются между деплоями)
    for uid, du in db_users.items():
        cur = users.get(uid, {})
        merged = dict(cur)
        merged["first_seen"] = min(cur.get("first_seen") or du["first_seen"], du["first_seen"]) if cur.get("first_seen") else du["first_seen"]
        merged["last_seen"] = max(cur.get("last_seen", 0), du["last_seen"])
        merged["searches"] = max(cur.get("searches", 0), du["searches"])
        merged["username"] = du.get("username") or cur.get("username")
        merged["_monitoring"] = du.get("monitoring", False)
        users[uid] = merged
    events = analytics.read_events()
    # Кол-во мониторингов: из PG-флага, иначе из settings.json
    mon_count = sum(1 for u in users.values() if u.get("_monitoring"))
    subs = []  # (uid, settings)
    if USERS_DIR.exists():
        _settings_mon = 0
        for p in USERS_DIR.iterdir():
            if not (p.is_dir() and p.name.isdigit()):
                continue
            try:
                s = json.loads((p / "settings.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            if s.get("monitor_enabled"):
                _settings_mon += 1
            st = s.get("subscription_type")
            if st and st != "free":
                subs.append((int(p.name), s))
        mon_count = max(mon_count, _settings_mon)
    return users, events, mon_count, subs


def _uname(users: dict, uid) -> str:
    u = users.get(str(uid)) or {}
    un = u.get("username")
    return f"@{un}" if un else f"id{uid}"


def _admin_today_text(target_date=None) -> str:
    users, events, mon_count, subs = _admin_collect()
    now = datetime.datetime.now(_MSK)
    day = target_date or now.date()  # счётчики обнуляются в 00:00 МСК
    active, searches, mon_on, subs_today = set(), 0, 0, 0
    for ev in events:
        if _msk_date(ev.get("ts", 0)) != day:
            continue
        if ev.get("uid"):
            active.add(ev["uid"])
        act = ev.get("action")
        if act == "search":
            searches += 1
        elif act == "monitor_on":
            mon_on += 1
        elif act == "subscription":
            subs_today += 1
    new_today = sum(1 for u in users.values() if _msk_date(u.get("first_seen", 0)) == day)
    _label = "СЕГОДНЯ" if (target_date is None) else "ИТОГИ ДНЯ"
    return (
        f"📊 <b>Perekup Drive — {_label} ({day.strftime('%d.%m.%Y')})</b>\n\n"
        f"👤 Всего пользователей: <code>{_fmt_n(len(users))}</code>\n"
        f"🟢 Активных: <code>{_fmt_n(len(active))}</code>\n"
        f"🆕 Новых: <code>{_fmt_n(new_today)}</code>\n"
        f"🔍 Поисков выполнено: <code>{_fmt_n(searches)}</code>\n"
        f"🔔 Включили мониторинг: <code>{_fmt_n(mon_on)}</code>\n"
        f"💎 Новых подписок: <code>{_fmt_n(subs_today)}</code>\n\n"
        f"Обновлено: {now.strftime('%H:%M')} МСК"
    )


def _admin_week_text() -> str:
    users, events, mon_count, subs = _admin_collect()
    now = datetime.datetime.now(_MSK)
    today = now.date()
    week_days = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
    wset = set(week_days)
    new_total = active_set = searches_total = mon_total = subs_total = 0
    active_set = set()
    per_day_new = {d: 0 for d in week_days}
    per_day_search = {d: 0 for d in week_days}
    for u in users.values():
        d = _msk_date(u.get("first_seen", 0))
        if d in wset:
            new_total += 1
            per_day_new[d] += 1
    for ev in events:
        d = _msk_date(ev.get("ts", 0))
        if d not in wset:
            continue
        if ev.get("uid"):
            active_set.add(ev["uid"])
        act = ev.get("action")
        if act == "search":
            searches_total += 1
            per_day_search[d] += 1
        elif act == "monitor_on":
            mon_total += 1
        elif act == "subscription":
            subs_total += 1
    lines = [
        f"📊 <b>Perekup Drive — НЕДЕЛЯ ({week_days[0].strftime('%d.%m')} – {week_days[-1].strftime('%d.%m')})</b>\n",
        f"🆕 Новых: <code>{_fmt_n(new_total)}</code>",
        f"🟢 Активных: <code>{_fmt_n(len(active_set))}</code>",
        f"🔍 Поисков: <code>{_fmt_n(searches_total)}</code>",
        f"🔔 Мониторинг: <code>{_fmt_n(mon_total)}</code>",
        f"💎 Подписок: <code>{_fmt_n(subs_total)}</code>",
        "\n📈 <b>По дням:</b>",
    ]
    for d in week_days:
        wd = _RU_WD[d.weekday()]
        n, s = per_day_new[d], per_day_search[d]
        if n == 0 and s == 0:
            lines.append(f"{wd}: Нет данных")
        else:
            lines.append(f"{wd}: +{_fmt_n(n)} новых, {_fmt_n(s)} поисков")
    return "\n".join(lines)


def _admin_overall_text() -> str:
    users, events, mon_count, subs = _admin_collect()
    now = time.time()
    total = len(users)
    a7 = a30 = 0
    for u in users.values():
        ls = u.get("last_seen", 0)
        if now - ls <= 7 * 86400:
            a7 += 1
        if now - ls <= 30 * 86400:
            a30 += 1
    inactive = total - a30
    # Берём бОльшее из событий и PG-счётчика (PG кумулятивный, переживает деплой)
    searches_total = max(
        sum(1 for ev in events if ev.get("action") == "search"),
        sum(u.get("searches", 0) for u in users.values()),
    )
    paid = len(subs)

    def _pct(x):
        return f"{(x / total * 100):.1f}%" if total else "0.0%"

    # среднее: поисков в день и дней активности
    days_active = {}
    for ev in events:
        if ev.get("action") == "search" and ev.get("uid"):
            d = _msk_date(ev.get("ts", 0))
            days_active.setdefault(ev["uid"], set()).add(d)
    avg_days = round(sum(len(v) for v in days_active.values()) / total, 1) if total else 0
    # поисков в день: всего поисков / число уникальных дней с поисками
    all_days = set()
    for v in days_active.values():
        all_days |= v
    avg_per_day = round(searches_total / len(all_days), 1) if all_days else 0
    return (
        f"📊 <b>Perekup Drive — ВСЁ ВРЕМЯ</b>\n\n"
        f"👤 Всего: <code>{_fmt_n(total)}</code>\n"
        f"🟢 Активных за 7 дн: <code>{_fmt_n(a7)}</code> ({_pct(a7)})\n"
        f"🟢 Активных за 30 дн: <code>{_fmt_n(a30)}</code> ({_pct(a30)})\n"
        f"💤 Неактивных 30+ дн: <code>{_fmt_n(inactive)}</code> ({_pct(inactive)})\n\n"
        f"🔍 Поисков всего: <code>{_fmt_n(searches_total)}</code>\n"
        f"🔔 С мониторингом: <code>{_fmt_n(mon_count)}</code> ({_pct(mon_count)})\n"
        f"💎 Платных: <code>{_fmt_n(paid)}</code> ({_pct(paid)})\n\n"
        f"📊 <b>Среднее на пользователя:</b>\n"
        f"• Поисков в день: <code>{avg_per_day}</code>\n"
        f"• Дней активности: <code>{avg_days}</code>"
    )


def _admin_users_text() -> str:
    users, events, mon_count, subs = _admin_collect()
    # топ по поискам
    by_searches = sorted(users.items(), key=lambda kv: kv[1].get("searches", 0), reverse=True)
    lines = ["👤 <b>Топ-10 активных:</b>"]
    any_top = False
    for i, (uid, u) in enumerate(by_searches[:10], 1):
        sc = u.get("searches", 0)
        if sc <= 0:
            continue
        any_top = True
        lines.append(f"{i}. {_uname(users, uid)} — {_fmt_n(sc)} поисков")
    if not any_top:
        lines.append("Нет данных")
    # последние подписки
    lines.append("\n💎 <b>Последние 5 подписок:</b>")
    if subs:
        _s = sorted(subs, key=lambda x: x[1].get("subscription_start", 0), reverse=True)[:5]
        for uid, s in _s:
            lines.append(f"{_uname(users, uid)} — {s.get('subscription_type','?')}")
    else:
        lines.append("Нет данных")
    # последние новые
    lines.append("\n🆕 <b>Последние 5 новых:</b>")
    by_new = sorted(users.items(), key=lambda kv: kv[1].get("first_seen", 0), reverse=True)[:5]
    if by_new:
        for uid, u in by_new:
            fs = _msk_date(u.get("first_seen", 0))
            when = datetime.datetime.fromtimestamp(u.get("first_seen", 0), _MSK).strftime("%d.%m %H:%M") if u.get("first_seen") else "?"
            lines.append(f"{_uname(users, uid)} — {when}")
    else:
        lines.append("Нет данных")
    return "\n".join(lines)


def _admin_funnel_text() -> str:
    users, events, mon_count, subs = _admin_collect()
    now = time.time()
    total = len(users)
    # сделали поиск за 30 дн
    searched = set()
    for ev in events:
        if ev.get("action") == "search" and ev.get("uid") and now - ev.get("ts", 0) <= 30 * 86400:
            searched.add(ev["uid"])
    did_search = len(searched)
    paid = len(subs)

    def _pct(x, base):
        return f"{(x / base * 100):.1f}%" if base else "0.0%"

    return (
        f"📊 <b>Воронка (30 дней)</b>\n\n"
        f"👤 Всего: <code>{_fmt_n(total)}</code> (100%)\n"
        f"🔍 Сделали поиск: <code>{_fmt_n(did_search)}</code> ({_pct(did_search, total)})\n"
        f"🔔 Вкл. мониторинг: <code>{_fmt_n(mon_count)}</code> ({_pct(mon_count, total)})\n"
        f"💎 Подписка: <code>{_fmt_n(paid)}</code> ({_pct(paid, total)})\n\n"
        f"⚠️ Конверсия в мониторинг: {_pct(mon_count, total)}\n"
        f"⚠️ Конверсия в подписку: {_pct(paid, total)}\n"
        f"⚠️ Из мониторинга в подписку: {_pct(paid, mon_count)}"
    )


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сегодня", callback_data="adm|today"),
         InlineKeyboardButton(text="📈 Неделя", callback_data="adm|week")],
        [InlineKeyboardButton(text="📋 Общая", callback_data="adm|overall"),
         InlineKeyboardButton(text="👤 Пользователи", callback_data="adm|users")],
        [InlineKeyboardButton(text="🔄 Воронка", callback_data="adm|funnel"),
         InlineKeyboardButton(text="📎 Экспорт", callback_data="adm|export")],
    ])


def _admin_refresh_kb(kind: str) -> InlineKeyboardMarkup:
    rows = []
    if kind == "users":
        rows.append([InlineKeyboardButton(text="📎 Экспорт всех", callback_data="adm|export")])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm|menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_export_csv() -> str:
    """Выгружает пользователей в CSV, возвращает путь к файлу."""
    import csv as _csv, tempfile as _tf
    users, events, mon_count, subs = _admin_collect()
    path = os.path.join(_tf.gettempdir(), "users_export.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["user_id", "username", "first_seen", "last_seen", "searches", "region"])
        for uid, u in users.items():
            fs = datetime.datetime.fromtimestamp(u.get("first_seen", 0), _MSK).strftime("%Y-%m-%d %H:%M") if u.get("first_seen") else ""
            ls = datetime.datetime.fromtimestamp(u.get("last_seen", 0), _MSK).strftime("%Y-%m-%d %H:%M") if u.get("last_seen") else ""
            w.writerow([uid, u.get("username", ""), fs, ls, u.get("searches", 0), u.get("region", "")])
    return path


_ADMIN_BUILDERS = {
    "today": _admin_today_text,
    "week": _admin_week_text,
    "overall": _admin_overall_text,
    "users": _admin_users_text,
    "funnel": _admin_funnel_text,
}


@dp.message(Command("admin"))
@dp.message(F.text == "📊 Статистика")
async def cmd_admin(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return  # не-админам ничего не показываем
    await msg.answer("📊 <b>Админ-статистика</b>\nВыбери раздел:",
                     parse_mode="HTML", reply_markup=_admin_menu_kb())


@dp.message(Command("deploy"))
async def cmd_deploy(msg: Message):
    """Показывает, какая версия кода реально запущена в Railway."""
    if msg.from_user.id not in ADMIN_IDS:
        return
    await msg.answer(
        "🚂 <b>Railway deploy</b>\n"
        f"• commit: <code>{DEPLOY_REVISION}</code>\n"
        f"• service: <code>{DEPLOY_SERVICE}</code>\n"
        f"• environment: <code>{DEPLOY_ENVIRONMENT}</code>\n\n"
        "Если commit здесь старый — Railway запустил старый GitHub commit. "
        "Нужно запушить/смёржить последнюю ветку и сделать Redeploy.",
        parse_mode="HTML",
    )


@dp.message(Command("dbcheck"))
async def cmd_dbcheck(msg: Message):
    """Диагностика хранилища статистики — для админа."""
    if msg.from_user.id not in ADMIN_IDS:
        return
    pg = "✅ подключён" if _get_db() else "❌ НЕ подключён (DATABASE_URL не задан)"
    in_mem = len(_USER_REGISTRY)
    pg_cnt = "—"
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_users")
                pg_cnt = cur.fetchone()[0]
    except Exception:
        pass
    # Сразу делаем бэкап и показываем РЕАЛЬНЫЙ результат
    global _registry_dirty
    _registry_dirty = True
    _save_status = await _tg_backup_save(force=True)
    backup = "✅ закреплён в этом чате" if _BACKUP_MSG_ID else "❌ не создан"
    await msg.answer(
        f"🗄 <b>Хранилище статистики</b>\n\n"
        f"PostgreSQL: {pg}\n"
        f"Пользователей в памяти: <b>{in_mem}</b>\n"
        f"В PG (bot_users): <b>{pg_cnt}</b>\n"
        f"Telegram-бэкап: {backup}\n"
        f"Результат сохранения: {_save_status}\n\n"
        f"⚠️ <b>Не удаляй закреплённый документ</b> «📦 авто-бэкап статистики» — "
        f"из него восстанавливается список пользователей после деплоя.\n\n"
        f"Для 100% надёжности подключи PostgreSQL на Railway (New → Database → "
        f"PostgreSQL) — тогда всё хранится в настоящей таблице.",
        parse_mode="HTML",
    )


@dp.message(Command("reflink"))
async def cmd_reflink(msg: Message):
    """Показывает РЕАЛЬНОЕ имя бота и рабочую реф-ссылку (для проверки)."""
    uid = msg.from_user.id
    try:
        me = await bot.get_me()
        real_un = me.username or BOT_USERNAME
    except Exception:
        real_un = BOT_USERNAME
    link = f"https://t.me/{real_un}?start=ref_{uid}"
    await msg.answer(
        f"🤖 Реальное имя бота: @{real_un}\n\n"
        f"🔗 Твоя рабочая ссылка:\n{link}\n\n"
        f"Открой её с ДРУГОГО аккаунта — придёт уведомление.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                url=f"https://t.me/share/url?url={link}&text=Бот ищет авто ниже рынка — попробуй!")],
        ]),
    )


@dp.callback_query(F.data.startswith("adm|"))
async def cb_admin(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для администраторов", show_alert=True)
        return
    kind = cb.data.split("|", 1)[1]
    try:
        if kind == "menu":
            await cb.message.edit_text("📊 <b>Админ-статистика</b>\nВыбери раздел:",
                                       parse_mode="HTML", reply_markup=_admin_menu_kb())
            await cb.answer()
            return
        if kind == "export":
            await cb.answer("Готовлю файл…")
            path = await asyncio.get_running_loop().run_in_executor(None, _admin_export_csv)
            from aiogram.types import FSInputFile
            await cb.message.answer_document(FSInputFile(path, filename="users_export.csv"),
                                             caption="📎 Экспорт пользователей")
            return
        builder = _ADMIN_BUILDERS.get(kind)
        if not builder:
            await cb.answer()
            return
        text = await asyncio.get_running_loop().run_in_executor(None, builder)
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_admin_refresh_kb(kind))
        await cb.answer("Обновлено")
    except Exception as e:
        # Если текст не изменился — Telegram кидает ошибку, гасим
        if "message is not modified" in str(e).lower():
            await cb.answer("Без изменений")
        else:
            await cb.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)


async def notify_admins_subscription(uid: int, username: str, plan: str, amount: int):
    """Мгновенное уведомление админам о новой подписке. Вызывать при оплате."""
    analytics.track("subscription", uid=uid, username=username, plan=plan, amount=amount)
    who = f"@{username}" if username else f"id{uid}"
    txt = f"💎 {who} оформил {plan} за {_fmt_n(amount)}₽"
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, txt)
        except Exception:
            pass


# ── Рассылка всем пользователям (только админ) ──────────────────
_pending_broadcast: dict[int, dict] = {}  # admin_uid -> {"text":..., "from_chat":..., "msg_id":...}
_pending_fomo_broadcast: dict[int, dict] = {}


def _all_user_ids() -> list[int]:
    """Все uid пользователей бота — объединение надёжного реестра (переживает
    деплой) и папок USERS_DIR. Используется для рассылки и статистики."""
    ids = set()
    # 1) Реестр в памяти/PG/бэкапе — главный источник, переживает деплой
    try:
        for k in (_db_users() or {}).keys():
            if str(k).isdigit():
                ids.add(int(k))
    except Exception:
        pass
    # 2) Папки users/<uid> — на случай, если кто-то ещё не попал в реестр
    if USERS_DIR.exists():
        for p in USERS_DIR.iterdir():
            if p.is_dir() and p.name.isdigit():
                ids.add(int(p.name))
    return sorted(ids)


@dp.message(Command("broadcast"))
async def cmd_broadcast(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    total = len(_all_user_ids())
    # Вариант 1: ответом на сообщение (текст/фото/что угодно) → скопируем его всем
    if msg.reply_to_message:
        _pending_broadcast[msg.from_user.id] = {
            "from_chat": msg.reply_to_message.chat.id,
            "msg_id": msg.reply_to_message.message_id,
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"📢 Отправить всем ({total})", callback_data="bcast|go"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="bcast|cancel"),
        ]])
        await msg.answer(f"👆 Это сообщение будет разослано {total} пользователям. Отправляем?", reply_markup=kb)
        return
    # Вариант 2: текст после команды
    text = (msg.text or "").split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await msg.answer(
            "📢 *Рассылка*\n\n"
            "Способ 1: `/broadcast текст сообщения`\n"
            "Способ 2: ответь командой `/broadcast` на любое сообщение "
            "(с фото/форматированием) — оно разошлётся как есть.",
            parse_mode="Markdown",
        )
        return
    _pending_broadcast[msg.from_user.id] = {"text": text[1].strip()}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"📢 Отправить всем ({total})", callback_data="bcast|go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bcast|cancel"),
    ]])
    await msg.answer("📢 *Предпросмотр рассылки:*", parse_mode="Markdown")
    await msg.answer(text[1].strip())
    await msg.answer(f"Разослать это {total} пользователям?", reply_markup=kb)


@dp.callback_query(F.data.startswith("bcast|"))
async def cb_broadcast(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для администраторов", show_alert=True)
        return
    action = cb.data.split("|", 1)[1]
    pend = _pending_broadcast.pop(cb.from_user.id, None)
    if action == "cancel" or not pend:
        await cb.message.edit_text("❌ Рассылка отменена.")
        await cb.answer()
        return
    await cb.message.edit_text("📤 Рассылаю…")
    await cb.answer()
    uids = _all_user_ids()
    sent = failed = blocked = 0
    for uid in uids:
        try:
            if "msg_id" in pend:
                await bot.copy_message(uid, pend["from_chat"], pend["msg_id"])
            else:
                await bot.send_message(uid, pend["text"])
            sent += 1
        except Exception as e:
            es = str(e).lower()
            if "blocked" in es or "deactivated" in es or "chat not found" in es:
                blocked += 1
            else:
                failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    await bot.send_message(
        cb.from_user.id,
        f"✅ Рассылка завершена.\n\n"
        f"📨 Доставлено: {_fmt_n(sent)}\n"
        f"🚫 Заблокировали бота: {_fmt_n(blocked)}\n"
        f"⚠️ Ошибок: {_fmt_n(failed)}\n"
        f"👥 Всего: {_fmt_n(len(uids))}"
    )


@dp.message(Command("access_status"))
async def cmd_access_status(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    rows = _access_rows(120)
    active = [r for r in rows if r.get("days_left") and r["days_left"] > 0]
    expiring = [r for r in active if r["days_left"] <= 3]
    expired = [r for r in rows if r.get("days_left") == 0]
    lines = [
        "⏳ <b>Сроки доступа</b>",
        "",
        f"Активных: <b>{_fmt_n(len(active))}</b>",
        f"Осталось 1-3 дня: <b>{_fmt_n(len(expiring))}</b>",
        f"Истекли: <b>{_fmt_n(len(expired))}</b>",
        "",
        "<b>Ближайшие окончания:</b>",
    ]
    for r in rows[:30]:
        uid = r["uid"]
        who = f"@{html.escape(r['username'])}" if r.get("username") else f"id{uid}"
        days = r.get("days_left")
        if days is None:
            left = "нет даты"
        elif days == 0:
            left = "истек"
        elif days == 1:
            left = "1 день"
        else:
            left = f"{days} дн."
        lines.append(f"{who} — <b>{left}</b> ({html.escape(str(r.get('plan') or 'trial'))})")
    lines.append("")
    lines.append("<code>/set_access UID DAYS [plan]</code> — поставить срок доступа")
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


@dp.message(Command("set_access"))
async def cmd_set_access(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    parts = (msg.text or "").split(maxsplit=3)
    if len(parts) < 3 or not parts[1].isdigit():
        await msg.answer("Формат: <code>/set_access UID DAYS [plan]</code>", parse_mode="HTML")
        return
    try:
        uid = int(parts[1])
        days = int(parts[2])
        plan = parts[3].strip() if len(parts) > 3 else "paid"
    except Exception:
        await msg.answer("DAYS должен быть числом.")
        return
    if _set_user_access(uid, days, plan):
        await msg.answer(f"✅ Доступ id{uid}: {days} дн., план {html.escape(plan)}", parse_mode="HTML")
        try:
            await bot.send_message(
                uid,
                f"✅ Доступ продлён.\n\nОсталось дней: {days}\nПлан: {plan}\n\nИщи свежие авто и включай уведомления, чтобы не пропустить хорошие варианты.",
            )
        except Exception:
            pass
    else:
        await msg.answer("❌ Не удалось обновить доступ.")


@dp.message(Command("fomo_broadcast"))
async def cmd_fomo_broadcast(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    stats = _marketing_stats()
    fomo = _marketing_fomo_message(stats)
    if fomo:
        text, button = fomo
    else:
        text, button = _marketing_morning_message(stats)
        text = "⚠️ FOMO-данных по исчезнувшим авто сейчас мало.\n\n" + text
    _pending_fomo_broadcast[msg.from_user.id] = {"text": text, "button": button}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🚨 Разослать FOMO всем ({len(_all_user_ids())})", callback_data="fomo|go"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="fomo|cancel"),
    ]])
    await msg.answer("🚨 <b>Предпросмотр FOMO-рассылки:</b>", parse_mode="HTML")
    await msg.answer(text, reply_markup=_marketing_keyboard(button))
    await msg.answer("Отправить всем пользователям?", reply_markup=kb)


@dp.callback_query(F.data.startswith("fomo|"))
async def cb_fomo_broadcast(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для администраторов", show_alert=True)
        return
    action = cb.data.split("|", 1)[1]
    pend = _pending_fomo_broadcast.pop(cb.from_user.id, None)
    if action == "cancel" or not pend:
        await cb.message.edit_text("❌ FOMO-рассылка отменена.")
        await cb.answer()
        return
    await cb.message.edit_text("🚨 Рассылаю FOMO…")
    await cb.answer()
    await _marketing_send_all(pend["text"], pend["button"])
    await bot.send_message(cb.from_user.id, "✅ FOMO-рассылка завершена.")


def _access_notice_text(days_left: int, plan: str) -> str:
    if days_left <= 1:
        return (
            "🚨 Сегодня последний день доступа к PerekupDrive.\n\n"
            "Дальше ты можешь пропустить свежие объявления ниже рынка: хорошие варианты часто уходят за часы.\n\n"
            "Продли пользование и купи подписку, чтобы поиск и уведомления не остановились."
        )
    return (
        f"⏳ Осталось {days_left} дня доступа к PerekupDrive.\n\n"
        "Бот продолжает искать свежие объявления и скидки ниже рынка. "
        "Продли доступ заранее: купи подписку, чтобы уведомления не остановились в самый неподходящий момент."
    )


def _subscription_offer_keyboard() -> InlineKeyboardMarkup:
    pay_url = os.getenv("SUBSCRIPTION_URL", "").strip()
    if not pay_url and ADMIN_IDS:
        pay_url = f"tg://user?id={next(iter(ADMIN_IDS))}"
    rows = []
    if pay_url:
        rows.append([InlineKeyboardButton(text="💳 Купить / продлить подписку", url=pay_url)])
    rows.append([InlineKeyboardButton(text="🚗 Смотреть свежие авто", callback_data="start_search")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("buy", "subscribe"))
@dp.message(F.text == "💎 Купить подписку")
async def cmd_buy_subscription(msg: Message):
    """Показывает пользователю рабочие ссылки оплаты подписки."""
    import urllib.parse

    plans = (
        ("Неделя", 349),
        ("Месяц", 999),
    )
    rows = []
    if YOOMONEY_WALLET:
        for plan_name, amount in plans:
            label = f"sub_{msg.from_user.id}_{amount}_{int(time.time())}"
            pay_url = "https://yoomoney.ru/quickpay/confirm.xml?" + urllib.parse.urlencode({
                "receiver": YOOMONEY_WALLET,
                "quickpay-form": "button",
                "paymentType": "AC",
                "sum": str(amount),
                "label": label,
                "targets": f"Подписка PerekupDrive: {plan_name}",
            })
            rows.append([InlineKeyboardButton(
                text=f"💳 {plan_name} — {amount} ₽",
                url=pay_url,
            )])
    support_url = _support_url()
    if support_url:
        rows.append([InlineKeyboardButton(
            text="🛟 Поддержка / отправить чек",
            url=support_url,
        )])

    text = (
        "💎 <b>Подписка PerekupDrive</b>\n\n"
        "📅 Неделя — 349 ₽\n"
        "🗓 Месяц — 999 ₽\n\n"
        "Выбери тариф и после оплаты отправь чек администратору."
    )
    await msg.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@dp.message(Command("support"))
@dp.message(F.text == "🛟 Поддержка")
async def cmd_support(msg: Message):
    """Открывает прямой контакт поддержки из команды или главного меню."""
    support_url = _support_url()
    rows = []
    if support_url:
        rows.append([InlineKeyboardButton(text="🛟 Написать в поддержку", url=support_url)])
    await msg.answer(
        "🛟 <b>Поддержка PerekupDrive</b>\n\n"
        "Напиши сюда, если поиск не отвечает, не прошла оплата или нужно активировать подписку.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


async def _access_expiry_notice_loop():
    await asyncio.sleep(90)
    print("  [access] планировщик уведомлений о сроке доступа запущен")
    while True:
        try:
            today = datetime.datetime.now(_MSK).date().isoformat()
            rows = []
            db = _get_db()
            if db:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT uid, subscription_plan, EXTRACT(EPOCH FROM subscription_expires_at), "
                        "last_expiry_notice_days, last_expiry_notice_date FROM bot_users "
                        "WHERE subscription_expires_at IS NOT NULL"
                    )
                    rows = cur.fetchall()
            sent = 0
            for uid, plan, exp, last_days, last_date in rows:
                days_left = _days_left_from_ts(int(exp or 0))
                if days_left not in (3, 1):
                    continue
                if int(last_days or -1) == days_left and str(last_date or "") == today:
                    continue
                try:
                    await bot.send_message(
                        int(uid),
                        _access_notice_text(days_left, plan or "trial"),
                        reply_markup=_subscription_offer_keyboard(),
                    )
                    sent += 1
                    if db:
                        with db.cursor() as cur:
                            cur.execute(
                                "UPDATE bot_users SET last_expiry_notice_days=%s, last_expiry_notice_date=%s WHERE uid=%s",
                                (days_left, today, int(uid)),
                            )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"  [access] notice failed uid={uid}: {str(e)[:80]}")
            if sent:
                print(f"  [access] expiry notices sent={sent}")
        except Exception as e:
            print(f"  [access] loop error: {str(e)[:100]}")
        await asyncio.sleep(3600)


async def _admin_report_scheduler():
    """В 00:00 МСК — итоги завершившегося дня; по понедельникам — «Неделя».
    Раздел «Сегодня» обнуляется в полночь МСК (счёт по МСК-дню).
    Реализовано на asyncio (как остальные фоновые циклы бота), без apscheduler."""
    await asyncio.sleep(30)
    _last_sent_date = None
    while True:
        try:
            now = datetime.datetime.now(_MSK)
            # Фиксируем итоги дня сразу после полуночи МСК (00:00–00:09)
            if now.hour == 0 and _last_sent_date != now.date():
                _last_sent_date = now.date()
                yesterday = now.date() - datetime.timedelta(days=1)
                loop = asyncio.get_running_loop()
                day_txt = await loop.run_in_executor(None, lambda: _admin_today_text(yesterday))
                for aid in ADMIN_IDS:
                    try:
                        await bot.send_message(aid, "🌙 Итоги дня (00:00 МСК)\n\n" + day_txt, parse_mode="HTML")
                    except Exception:
                        pass
                if now.weekday() == 0:  # понедельник — недельный отчёт
                    week_txt = await loop.run_in_executor(None, _admin_week_text)
                    for aid in ADMIN_IDS:
                        try:
                            await bot.send_message(aid, "📅 Недельный отчёт\n\n" + week_txt, parse_mode="HTML")
                        except Exception:
                            pass
        except Exception as e:
            print(f"  [admin-scheduler] {str(e)[:80]}")
        await asyncio.sleep(300)  # проверяем каждые 5 минут


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


@dp.message(Command("mailing_status"))
async def cmd_mailing_status(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        users = _all_user_ids()
        data = _discount_hunt_load()
        now = time.time()
        recent = [
            rec for rec in data.values()
            if now - float(rec.get("first_seen_ts", now)) <= 24 * 3600
        ]
        available = [rec for rec in recent if not rec.get("removed_ts")]
        removed = [
            rec for rec in data.values()
            if rec.get("removed_ts") and now - float(rec.get("removed_ts", now)) <= 24 * 3600
        ]
        dt = datetime.datetime.now(_MSK)
        today = dt.date().isoformat()
        lines = [
            "📣 <b>Статус рассылок</b>",
            "",
            f"👥 Пользователей для рассылки: <b>{_fmt_n(len(users))}</b>",
            f"🧲 Охота включена: <b>{'да' if DISCOUNT_HUNT_ENABLED else 'нет'}</b>",
            f"📨 Маркетинг включён: <b>{'да' if MARKETING_BROADCAST_ENABLED else 'нет'}</b>",
            f"📢 Канал включён: <b>{'да' if CHANNEL_POSTS_ENABLED else 'нет'}</b>",
            f"📍 Канал: <code>{html.escape(CHANNEL_ID or '-')}</code>",
            "",
            f"🚗 Кандидатов всего в базе: <b>{_fmt_n(len(data))}</b>",
            f"🕓 За 24ч найдено: <b>{_fmt_n(len(recent))}</b>",
            f"✅ Сейчас доступны: <b>{_fmt_n(len(available))}</b>",
            f"❌ Исчезли за 24ч: <b>{_fmt_n(len(removed))}</b>",
            "",
            f"📅 Сегодня МСК: <code>{today}</code>",
            f"🌅 Массовая сегодня: <b>{'да' if _marketing_mass_sent_today(today) else 'нет'}</b>",
            f"🏁 Охота сегодня: <b>{'да' if (_kv_get(_DISCOUNT_HUNT_LAST_SENT_KV) or '') == today else 'нет'}</b>",
            "",
            "Ручные команды:",
            "<code>/broadcast текст</code> — ручная рассылка всем",
            "<code>/channel_post stats_day</code> — тест поста в канал",
        ]
        await msg.answer("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await msg.answer(f"❌ Ошибка проверки рассылок: {html.escape(str(e)[:200])}", parse_mode="HTML")


@dp.message(Command("mailing_collect"))
async def cmd_mailing_collect(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    await msg.answer("🔄 Собираю кандидатов для рассылок сейчас…")
    try:
        before = len(_discount_hunt_load())
        await _discount_hunt_collect_once()
        await _discount_hunt_detect_removed(limit=80)
        data = _discount_hunt_load()
        now = time.time()
        recent = [
            rec for rec in data.values()
            if now - float(rec.get("first_seen_ts", now)) <= 24 * 3600
        ]
        available = [rec for rec in recent if not rec.get("removed_ts")]
        removed = [
            rec for rec in data.values()
            if rec.get("removed_ts") and now - float(rec.get("removed_ts", now)) <= 24 * 3600
        ]
        await msg.answer(
            "✅ Сбор завершён.\n\n"
            f"Было в базе: {_fmt_n(before)}\n"
            f"Стало в базе: {_fmt_n(len(data))}\n"
            f"За 24ч: {_fmt_n(len(recent))}\n"
            f"Доступны сейчас: {_fmt_n(len(available))}\n"
            f"Исчезли за 24ч: {_fmt_n(len(removed))}"
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка сбора: {html.escape(str(e)[:200])}", parse_mode="HTML")


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


@dp.message(Command("analyze"))
async def cmd_analyze_competitors(msg: Message):
    """Анализ конкурентов на Haraba.ru и свежих объявлений Авито."""
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("❌ Только для администраторов.")
        return
    
    await msg.answer("🔍 Анализирую конкурентов Haraba.ru и свежие объявления Авито...\n⏳ Это может занять 1-2 минуты")
    
    try:
        # Получаем данные
        region = "moscow"  # по умолчанию Москва
        
        haraba_items = scrape_haraba(region)
        fresh_avito = get_fresh_avito_items(region, max_age_minutes=30)
        all_avito = scrape_avito(region, pages=3)
        
        # Анализ
        analysis = analyze_competitors(haraba_items, fresh_avito)
        
        # Форматируем результат
        result = f"""
📊 **Анализ конкурентов Haraba.ru**

**Статистика:**
• Haraba объявлений: {analysis['haraba_count']} шт
• Наши на Авито: {analysis['our_count']} шт
• Свежие Авито (≤30 мин): {len(fresh_avito)} шт

**Цены:**
• Средняя цена Haraba: {analysis['haraba_avg_price']:,} ₽
• Средняя цена наших: {analysis['our_avg_price']:,} ₽
• Разница: {analysis['price_gap_pct']:+.1f}%

**Их модели (топ-5):**
"""
        for model, count in list(analysis['haraba_models'].items())[:5]:
            result += f"\n  • {model}: {count} шт"
        
        result += f"\n\n**Наши модели (топ-5):**"
        for model, count in list(analysis['our_models'].items())[:5]:
            result += f"\n  • {model}: {count} шт"
        
        if analysis['haraba_advantages']:
            result += "\n\n**Их преимущества:**"
            for adv in analysis['haraba_advantages']:
                result += f"\n  ❌ {adv}"
        
        if analysis['our_advantages']:
            result += "\n\n**Наши преимущества:**"
            for adv in analysis['our_advantages']:
                result += f"\n  ✅ {adv}"
        
        if analysis['recommendations']:
            result += "\n\n**📌 Рекомендации:**"
            for rec in analysis['recommendations']:
                result += f"\n  • {rec}"
        
        if fresh_avito:
            result += f"\n\n**🔥 Свежие Авито объявления (последние 30 мин):**"
            for it in fresh_avito[:5]:
                price = it.get("_price_int", 0)
                age = it.get("_age_minutes", 0)
                result += f"\n  • {it.get('title', 'N/A')[:50]} — {price:,} ₽ ({age:.0f} мин назад)"
        
        await msg.answer(result, parse_mode="Markdown")
        
    except Exception as e:
        print(f"[анализ] ошибка: {e}")
        import traceback
        traceback.print_exc()
        await msg.answer(f"❌ Ошибка при анализе: {e}")


# Все возможные площадки для мониторинга
_MONITOR_SOURCES = [
    ("avito",  "Авито"),
    ("drom",   "Дром"),
    ("autoru", "Auto.ru"),
    ("youla",  "Юла"),
    ("vk",     "ВКонтакте"),
    ("tg",     "Telegram"),
]

def _monitor_sources(s: dict) -> list[str]:
    """Возвращает список включённых площадок. По умолчанию — все."""
    src = s.get("monitor_sources")
    if not src or not isinstance(src, list):
        return [k for k, _ in _MONITOR_SOURCES]
    allowed = {k for k, _ in _MONITOR_SOURCES}
    return [x for x in src if x in allowed] or [k for k, _ in _MONITOR_SOURCES]


_SELLER_TYPE_LABELS = {"private": "Частник", "pro": "Профи/перекуп", "dealer": "Автодилер"}

def _get_seller_types(s: dict) -> list:
    """Какие типы продавцов показывать. По умолчанию — все три."""
    st = s.get("seller_types")
    if st is None:
        return ["private"] if s.get("private_only") else ["private", "pro", "dealer"]
    return st or ["private", "pro", "dealer"]

def _seller_types_label(s: dict) -> str:
    st = _get_seller_types(s)
    if len(st) >= 3:
        return "все"
    return ", ".join(_SELLER_TYPE_LABELS.get(t, t) for t in st) or "все"

def _seller_types_keyboard(s: dict) -> InlineKeyboardMarkup:
    st = set(_get_seller_types(s))
    rows = []
    for k, lbl in _SELLER_TYPE_LABELS.items():
        chk = "✅" if k in st else "⬜"
        rows.append([InlineKeyboardButton(text=f"{chk} {lbl}", callback_data=f"st_toggle|{k}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="seller_types_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        [InlineKeyboardButton(text=f"🧑‍💼 Тип продавца: {_seller_types_label(s)}", callback_data="seller_types")],
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


@dp.callback_query(F.data == "seller_types")
async def cb_seller_types(cb: CallbackQuery):
    await cb.answer()
    s = load_settings(cb.from_user.id)
    await cb.message.answer(
        "🧑‍💼 <b>Тип продавца</b> — у кого искать объявления:\n\n"
        "👤 <b>Частник</b> — продаёт своё авто, не перекуп\n"
        "💼 <b>Профи/перекуп</b> — перепродаёт (один телефон в 3+ объявлениях или услуги выкупа)\n"
        "🏢 <b>Автодилер</b> — салоны и дилерские центры\n\n"
        "Отметь нужные:",
        parse_mode="HTML", reply_markup=_seller_types_keyboard(s))


@dp.callback_query(F.data.startswith("st_toggle|"))
async def cb_seller_type_toggle(cb: CallbackQuery):
    uid = cb.from_user.id
    key = cb.data.split("|")[1]
    s = load_settings(uid)
    st = set(_get_seller_types(s))
    if key in st:
        st.discard(key)
    else:
        st.add(key)
    if not st:                      # хотя бы один тип должен остаться
        st = {key}
        await cb.answer("Оставь хотя бы один тип продавца")
    else:
        await cb.answer("Обновил")
    # порядок сохраняем стабильным
    s["seller_types"] = [k for k in ("private", "pro", "dealer") if k in st]
    s.pop("private_only", None)     # старый флаг больше не нужен
    save_settings(uid, s)
    try:
        await cb.message.edit_reply_markup(reply_markup=_seller_types_keyboard(s))
    except Exception:
        pass


@dp.callback_query(F.data == "seller_types_back")
async def cb_seller_types_back(cb: CallbackQuery):
    await cb.answer()
    s = load_settings(cb.from_user.id)
    try:
        await cb.message.edit_reply_markup(reply_markup=_notify_keyboard(s))
    except Exception:
        pass


@dp.callback_query(F.data == "notify_toggle")
async def cb_notify_toggle(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = not s.get("monitor_enabled", False)
    s["monitor_enabled"] = enabled
    save_settings(uid, s)
    _set_user_monitoring(uid, enabled)  # надёжный флаг в PG
    if enabled:
        analytics.track("monitor_on", uid=uid, username=cb.from_user.username)
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
    await cb.message.answer(f"⭐ Избранное ({len(favs)} шт.):")
    for it in favs[-10:]:
        url = it.get("url", "")
        sid = url_to_id(url)
        kb = InlineKeyboardMarkup(inline_keyboard=[r for r in [
            [InlineKeyboardButton(text="🔗 Открыть", url=url)] if url else [],
            [InlineKeyboardButton(text="🔍 Пробить машину (штрафы, аресты)", callback_data=f"check|{sid}|{uid}")],
        ] if r])
        await cb.message.answer(f"🚗 {it.get('title','')}\n💰 {it.get('price','?')}", reply_markup=kb)


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
        f"Выбери площадки для поиска:",
        reply_markup=sources_keyboard(_get_enabled_sources(settings), show_back=False)
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
        f"Выбери площадки для поиска:",
        reply_markup=sources_keyboard(_get_enabled_sources(s), show_back=False)
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


ALL_SOURCES = ["drom", "autoru", "avito", "youla", "vk", "tg"]
USER_SEARCH_SOURCES = list(ALL_SOURCES)
SOURCE_NAMES = {
    "drom":   "🔵 Дром",
    "autoru": "🟠 Auto.ru",
    "avito":  "🔴 Авито",
    "youla":  "🟡 Юла",
    "vk":     "📘 ВКонтакте",
    "tg":     "✈️ Telegram",
}



def sources_keyboard(enabled: list[str], show_back: bool = True) -> InlineKeyboardMarkup:
    rows = []
    enabled_set = set(enabled or [])
    for src in USER_SEARCH_SOURCES:
        check = "✅" if src in enabled_set else "☐"
        source_name = SOURCE_NAMES.get(src, src)
        rows.append([InlineKeyboardButton(
            text=f"{check} {source_name}",
            callback_data=f"toggle_src|{src}"
        )])
    rows.append([
        InlineKeyboardButton(text="🌐 Все площадки", callback_data="src_all"),
        InlineKeyboardButton(text="🔍 Искать", callback_data="start_search"),
    ])
    if show_back:
        rows.append([InlineKeyboardButton(text="◀️ Назад (цена)", callback_data="setup_back_to_price")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_enabled_sources(s: dict) -> list[str]:
    """Возвращает список включённых площадок, по умолчанию — все."""
    enabled = s.get("sources", [])
    if not enabled:
        return list(ALL_SOURCES)
    filtered = [src for src in enabled if src in ALL_SOURCES]
    return filtered or list(ALL_SOURCES)


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
        None, lambda: scrape_avito(
            region,
            pages=1,
            price_min=pmin,
            price_max=pmax,
            sort_by_date=True,
            fast=True,
        )
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
        and not is_not_running(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm_today
    ]
    suitable = _filter_by_category(suitable, category, brand)
    # Авито-эталон для "Сегодня": берём широкий срез без ценового фильтра
    _avito_ref_today = await loop.run_in_executor(
        None, lambda: scrape_avito(
            region,
            pages=1,
            price_min=0,
            price_max=99_000_000,
            fast=True,
        )
    )
    for _ar in _avito_ref_today:
        _ar["_market_ref_only"] = True
    suitable = rank_by_market_price(
        suitable,
        ref_items=_avito_ref_today or suitable,
        avito_only_median=bool(_avito_ref_today),
    )
    # Только ниже рынка
    suitable = _best_below_market_items(suitable)

    if not suitable:
        await msg.answer(
            f"😔 Свежих объявлений ниже рынка за последние 24 часа не нашлось.\n"
            f"Попробуй 🔍 Найти авто для более широкого поиска.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track("new_today", uid=uid, region=region, results=len(suitable))
    below = sum(1 for x in suitable if _is_strong_below_market(x))
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
    brand = s.get("brand", "")
    _br = brand if brand and brand != "any" else ""
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
        "drom":   lambda: scrape_drom(region, pages=10, price_min=pmin, price_max=pmax, brand=_br),
        "autoru": lambda: scrape_autoru(region, pages=2, price_min=pmin, price_max=pmax, brand=_br, deadline_sec=24),
        "avito":  lambda: scrape_avito(region, pages=1, price_min=pmin, price_max=pmax, sort_by_date=True, fast=True),
        "youla":  lambda: scrape_youla(region, pages=16, price_min=pmin, price_max=pmax, brand=_br),
        "vk":     lambda: scrape_vk_groups(region, pmin, pmax),
    }
    task_pairs = [
        (src, loop.run_in_executor(None, fn))
        for src, fn in scraper_map.items()
    ]
    task_pairs.append(("tg", loop.run_in_executor(None, lambda: scrape_tg_channels(region, pmin, pmax, fast=True))))
    done, pending = await asyncio.wait([task for _, task in task_pairs], timeout=SEARCH_SOURCE_TIMEOUT_SEC)
    if pending:
        for task in pending:
            task.cancel()
        print(f"  [global search] timeout: {len(pending)} source(s) still running, showing completed results")

    items: list[dict] = []
    stat_parts: list[str] = []
    for src, task in task_pairs:
        tag = "✈️ Telegram" if src == "tg" else SOURCE_TAGS.get(src, src)
        if task not in done:
            batch = []
            print(f"  [global scraper] {src}: timeout")
            if src in ("avito", "autoru"):
                stat_parts.append(f"{tag}: не успел")
            continue
        else:
            try:
                batch = task.result()
            except Exception as e:
                print(f"  [global scraper] {src} error: {e}")
                batch = []
                if src in ("avito", "autoru"):
                    stat_parts.append(f"{tag}: ошибка")
                continue
        if isinstance(batch, list):
            items.extend(batch)
            cnt = len(batch)
        else:
            cnt = 0
        if cnt > 0 or src not in ("avito", "autoru"):
            stat_parts.append(f"{tag}: {cnt}")
        else:
            stat_parts.append(f"{tag}: пусто")

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
        and not is_not_running(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped_norm_g
        and i["url"] not in seen_norm_g
    ]
    suitable = rank_by_market_price(suitable)
    # Показываем только те что ниже рынка — остальные не интересны перекупу
    suitable = _best_below_market_items(suitable)

    if not suitable:
        await msg.answer(
            f"😔 Не нашёл новых объявлений ниже рынка. Нажми ♻️ Сбросить историю и попробуй снова.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track(
        "global_search", uid=uid, region=region, price_min=pmin, price_max=pmax,
        results=len(suitable),
    )
    below = len(below_market)
    await msg.answer(f"✅ Найдено {len(suitable)} объявлений ниже рынка!")
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
    suitable = _filter_by_category(suitable, s.get("category", "all"), s.get("brand", ""))
    suitable = rank_by_market_price(suitable)
    suitable = _best_below_market_items(suitable)

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
    below = sum(1 for x in suitable if _is_strong_below_market(x))
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
    await cb.message.edit_text("Выбери площадки для поиска:", reply_markup=sources_keyboard(enabled))


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
    "автомобиль снят с продажи", "показать только актуальные объявления",
    "мы показываем такие объявления, чтобы вам было проще ориентироваться",
    "объявление не найдено", "объявление недоступно", "объявление удалено",
    "снят с публикации", "архивное объявление", "продано или снято",
    '"isSold":true', '"sold":true', '"status":"sold"', '"status":"inactive"',
    '"isArchived":true', 'bulletin-sold', 'data-bulletin-status="sold"',
    "listing not found", "offer not found",
]

_DROM_SOLD_RE = re.compile(
    r"(?:автомобиль|объявление)\s+снят[о]?\s+с\s+продажи|"
    r"показать\s+только\s+актуальные\s+объявления|"
    r"мы\s+показываем\s+такие\s+объявления.{0,120}ориентироваться",
    re.IGNORECASE | re.S,
)

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
        elif source == "drom":
            try:
                import cloudscraper as _cs
                _scraper = _cs.create_scraper()
                r = _scraper.get(url, timeout=12, headers=_headers)
            except Exception:
                r = None
            if r is None:
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
        page_text = re.sub(r"\s+", " ", text_lower)
        if source == "drom" and _DROM_SOLD_RE.search(page_text):
            return None

        soup = _BS(text, "lxml")
        visible_text = soup.get_text("\n", strip=True)
        visible_lower = re.sub(r"\s+", " ", visible_text.lower())
        if any(m.lower() in visible_lower for m in _SOLD_MARKERS):
            return None
        if source == "drom" and _DROM_SOLD_RE.search(visible_lower):
            return None

        avito_ai_market = 0
        avito_rating_text = ""
        avito_rating_score = None
        if source == "avito":
            avito_page_blob = text + "\n" + soup.get_text("\n", strip=True)
            avito_ai_market = _avito_ai_estimate_from_text(avito_page_blob)
            avito_rating_text, avito_rating_score = _avito_rating_from_text(avito_page_blob)
            if not avito_ai_market and "avito.ru" in url:
                try:
                    mobile_url = re.sub(r"https?://(?:www\.)?avito\.ru", "https://m.avito.ru", url)
                    mr = _req.get(
                        mobile_url,
                        headers={
                            **_headers,
                            "Referer": "https://m.avito.ru/",
                            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                        },
                        timeout=8,
                        allow_redirects=True,
                    )
                    if mr.status_code == 200:
                        avito_ai_market = _avito_ai_estimate_from_text(mr.text)
                        if not avito_rating_text:
                            avito_rating_text, avito_rating_score = _avito_rating_from_text(mr.text)
                except Exception:
                    pass
        drom_market = 0
        if source == "drom":
            drom_market = _drom_estimate_from_text(
                text + "\n" + soup.get_text("\n", strip=True)
            )
        generic_market = 0
        if source in ("autoru", "auto.ru"):
            generic_market = _extract_market_estimate_from_text(
                text + "\n" + soup.get_text("\n", strip=True),
                "autoru",
            )

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

        details = {"_photo_url": photo_url, "description": description}
        if avito_ai_market:
            details["_avito_market"] = avito_ai_market
        if avito_rating_text:
            details["_avito_rating"] = avito_rating_text
            details["_avito_rating_score"] = avito_rating_score
        if drom_market:
            details["_drom_market"] = drom_market
        if generic_market:
            details["_autoru_market"] = generic_market
        return details
    except Exception:
        # Для площадок, где мы явно проверяем актуальность перед отправкой,
        # ошибка проверки не должна пропускать снятое объявление пользователю.
        # Но это не то же самое, что "снято": статистика исчезнувших авто не
        # должна срабатывать от сетевого сбоя.
        if source in ("drom", "avito", "autoru"):
            return {"_check_failed": True}
        return {}


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
        if details.get("_check_failed"):
            if (item.get("source", "") or "").lower() == "drom":
                continue
            item["_sale_status_check_failed"] = True
            active.append(item)
            continue
        item["_enriched"] = True
        item["_sale_status_checked"] = True
        if details.get("_photo_url"):
            item["_photo_url"] = details["_photo_url"]
        if details.get("description"):
            item["description"] = details["description"]
        if details.get("_avito_market"):
            _apply_page_market(item, int(details["_avito_market"]), "avito")
        if details.get("_avito_rating"):
            item["_avito_rating"] = details["_avito_rating"]
            item["_avito_rating_score"] = details.get("_avito_rating_score")
            item["_below_market"] = _is_strong_below_market(item)
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
    "youla":      "🟡 Юла",
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
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS seen_urls (
                            uid BIGINT,
                            url TEXT,
                            added_at TIMESTAMP DEFAULT NOW(),
                            PRIMARY KEY (uid, url)
                        )
                    """)
                    # Хранилище ключ-значение — переживает рестарт (для аналитики)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS kv_store (
                            k TEXT PRIMARY KEY,
                            v TEXT,
                            updated_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
                    # Реестр пользователей — НАДЁЖНЫЙ счётчик статистики (переживает деплой)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS bot_users (
                            uid BIGINT PRIMARY KEY,
                            username TEXT,
                            first_seen TIMESTAMP DEFAULT NOW(),
                            last_seen TIMESTAMP DEFAULT NOW(),
                            search_count INTEGER DEFAULT 0,
                            monitoring BOOLEAN DEFAULT FALSE,
                            subscription_plan TEXT DEFAULT 'trial',
                            subscription_expires_at TIMESTAMP,
                            last_expiry_notice_days INTEGER,
                            last_expiry_notice_date TEXT
                        )
                    """)
                    cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS subscription_plan TEXT DEFAULT 'trial'")
                    cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMP")
                    cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_expiry_notice_days INTEGER")
                    cur.execute("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_expiry_notice_date TEXT")
                    cur.execute(
                        "UPDATE bot_users SET subscription_expires_at = first_seen + (%s * INTERVAL '1 day') "
                        "WHERE subscription_expires_at IS NULL",
                        (DEFAULT_TRIAL_DAYS,),
                    )
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS published_channel_posts (
                            id SERIAL PRIMARY KEY,
                            post_type TEXT,
                            car_id TEXT,
                            published_at TIMESTAMP DEFAULT NOW()
                        )
                    """)
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_published_channel_posts_car
                        ON published_channel_posts(car_id)
                    """)
        except Exception:
            return None
        return _db_conn


def _kv_set(key: str, val: str):
    """Сохранить значение в PG (переживает рестарт контейнера)."""
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO kv_store(k, v, updated_at) VALUES(%s,%s,NOW()) "
                    "ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v, updated_at=NOW()",
                    (key, val),
                )
            return True
    except Exception:
        pass
    return False


def _kv_get(key: str) -> "str | None":
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("SELECT v FROM kv_store WHERE k=%s", (key,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        pass
    return None


# Реестр пользователей в ПАМЯТИ — источник правды. Сохраняется и в PG (если есть),
# и в закреплённое сообщение Telegram (работает БЕЗ внешней БД). При старте
# восстанавливается из любого доступного источника → статистика не сбрасывается.
_USER_REGISTRY: "dict[str, dict]" = {}
_registry_dirty = False
_registry_new_user = False  # появился НОВЫЙ пользователь → бэкап в ближайшую минуту


def _register_user(uid: int, username: "str | None" = None, is_search: bool = False):
    """Обновляет реестр (в памяти + PG). Вызывается на каждое сообщение."""
    global _registry_dirty, _registry_new_user
    k = str(uid)
    now = int(time.time())
    _is_new = k not in _USER_REGISTRY
    u = _USER_REGISTRY.get(k) or {"first_seen": now, "searches": 0}
    u["last_seen"] = now
    if username:
        u["username"] = username
    if is_search:
        u["searches"] = u.get("searches", 0) + 1
    _USER_REGISTRY[k] = u
    _registry_dirty = True
    if _is_new:
        _registry_new_user = True  # критично: сохранить нового юзера быстро
    # Зеркалим в PG (если подключён)
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_users(uid, username, first_seen, last_seen, search_count, subscription_plan, subscription_expires_at) "
                    "VALUES(%s,%s,NOW(),NOW(),%s,'trial',NOW() + (%s * INTERVAL '1 day')) "
                    "ON CONFLICT(uid) DO UPDATE SET last_seen=NOW(), "
                    "  username=COALESCE(EXCLUDED.username, bot_users.username), "
                    "  search_count=bot_users.search_count + %s, "
                    "  subscription_plan=COALESCE(bot_users.subscription_plan, 'trial'), "
                    "  subscription_expires_at=COALESCE(bot_users.subscription_expires_at, bot_users.first_seen + (%s * INTERVAL '1 day'))",
                    (uid, username, 1 if is_search else 0, DEFAULT_TRIAL_DAYS, 1 if is_search else 0, DEFAULT_TRIAL_DAYS),
                )
    except Exception:
        pass


def _set_user_monitoring(uid: int, on: bool):
    global _registry_dirty
    k = str(uid)
    u = _USER_REGISTRY.get(k) or {"first_seen": int(time.time()), "searches": 0}
    u["monitoring"] = on
    _USER_REGISTRY[k] = u
    _registry_dirty = True
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("UPDATE bot_users SET monitoring=%s WHERE uid=%s", (on, uid))
    except Exception:
        pass


def _db_users() -> dict:
    """Реестр пользователей. Источник — память (всегда полон); при пустой памяти
    читаем из PG. Формат как у analytics ({uid:{first_seen,last_seen,username,searches}})."""
    if _USER_REGISTRY:
        return dict(_USER_REGISTRY)
    out = {}
    try:
        db = _get_db()
        if not db:
            return out
        with db.cursor() as cur:
            cur.execute("SELECT uid, username, EXTRACT(EPOCH FROM first_seen), "
                        "EXTRACT(EPOCH FROM last_seen), search_count, monitoring, subscription_plan, "
                        "EXTRACT(EPOCH FROM subscription_expires_at) FROM bot_users")
            for uid, un, fs, ls, sc, mon, plan, exp in cur.fetchall():
                exp_i = int(exp or 0)
                if not exp_i and fs:
                    exp_i = int(fs) + DEFAULT_TRIAL_DAYS * 86400
                out[str(uid)] = {
                    "username": un, "first_seen": int(fs or 0),
                    "last_seen": int(ls or 0), "searches": int(sc or 0),
                    "monitoring": bool(mon), "subscription_plan": plan or "trial",
                    "subscription_expires_at": exp_i,
                }
    except Exception:
        pass
    return out


def _days_left_from_ts(expires_ts: int | float | None) -> int | None:
    if not expires_ts:
        return None
    seconds = int(expires_ts - time.time())
    if seconds <= 0:
        return 0
    return max(1, (seconds + 86399) // 86400)


def _set_user_access(uid: int, days: int, plan: str = "paid") -> bool:
    days = max(0, int(days))
    plan = (plan or "paid").strip()[:40]
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_users(uid, first_seen, last_seen, subscription_plan, subscription_expires_at) "
                    "VALUES(%s,NOW(),NOW(),%s,NOW() + (%s * INTERVAL '1 day')) "
                    "ON CONFLICT(uid) DO UPDATE SET "
                    "subscription_plan=EXCLUDED.subscription_plan, "
                    "subscription_expires_at=EXCLUDED.subscription_expires_at, "
                    "last_expiry_notice_days=NULL, last_expiry_notice_date=NULL",
                    (uid, plan, days),
                )
        k = str(uid)
        u = _USER_REGISTRY.get(k) or {"first_seen": int(time.time()), "searches": 0}
        u["subscription_plan"] = plan
        u["subscription_expires_at"] = int(time.time()) + days * 86400
        _USER_REGISTRY[k] = u
        return True
    except Exception as e:
        print(f"  [access] set failed uid={uid}: {str(e)[:100]}")
        return False


def _access_rows(limit: int = 200) -> list[dict]:
    rows: list[dict] = []
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT uid, username, subscription_plan, EXTRACT(EPOCH FROM subscription_expires_at), "
                    "EXTRACT(EPOCH FROM last_seen) FROM bot_users "
                    "ORDER BY subscription_expires_at NULLS LAST, last_seen DESC LIMIT %s",
                    (limit,),
                )
                for uid, username, plan, exp, last_seen in cur.fetchall():
                    exp_i = int(exp or 0)
                    rows.append({
                        "uid": int(uid), "username": username, "plan": plan or "trial",
                        "expires_ts": exp_i, "days_left": _days_left_from_ts(exp_i),
                        "last_seen": int(last_seen or 0),
                    })
            return rows
    except Exception as e:
        print(f"  [access] rows failed: {str(e)[:100]}")
    for uid_s, u in (_db_users() or {}).items():
        if not str(uid_s).isdigit():
            continue
        exp_i = int(u.get("subscription_expires_at") or 0)
        rows.append({
            "uid": int(uid_s), "username": u.get("username"), "plan": u.get("subscription_plan") or "trial",
            "expires_ts": exp_i, "days_left": _days_left_from_ts(exp_i),
            "last_seen": int(u.get("last_seen") or 0),
        })
    rows.sort(key=lambda r: (999999 if r["days_left"] is None else r["days_left"], -r["last_seen"]))
    return rows[:limit]


# ── Бэкап реестра в Telegram (закреплённый документ) — работает без БД ──
_BACKUP_MSG_ID = None


async def _tg_backup_save(force: bool = False) -> str:
    """Сохраняет реестр в закреплённый документ в чате админа. Возвращает статус."""
    global _BACKUP_MSG_ID, _registry_dirty
    if not ADMIN_IDS:
        return "нет ADMIN_IDS"
    if not _USER_REGISTRY:
        return "реестр пуст"
    if not _registry_dirty and not force:
        return "без изменений"
    try:
        from aiogram.types import BufferedInputFile
        payload = json.dumps({"users": _USER_REGISTRY, "ts": int(time.time())}, ensure_ascii=False)
        admin = next(iter(ADMIN_IDS))
        msg = await bot.send_document(
            admin, BufferedInputFile(payload.encode("utf-8"), "stats_backup.json"),
            caption="📦 авто-бэкап статистики (НЕ удаляй этот закреп)", disable_notification=True,
        )
        _pinned = False
        try:
            await bot.pin_chat_message(admin, msg.message_id, disable_notification=True)
            _pinned = True
        except Exception as pe:
            print(f"  [tg-backup] закрепить не удалось: {str(pe)[:60]}")
        if _BACKUP_MSG_ID and _BACKUP_MSG_ID != msg.message_id:
            try:
                await bot.delete_message(admin, _BACKUP_MSG_ID)
            except Exception:
                pass
        _BACKUP_MSG_ID = msg.message_id
        _registry_dirty = False
        return f"✅ сохранено ({len(_USER_REGISTRY)} польз.)" + ("" if _pinned else " ⚠️ но не закреплено")
    except Exception as e:
        print(f"  [tg-backup] {str(e)[:80]}")
        return f"❌ ошибка: {str(e)[:80]}"


async def _tg_backup_restore():
    """Восстанавливает реестр из закреплённого документа в чате админа."""
    if not ADMIN_IDS:
        return
    try:
        chat = await bot.get_chat(next(iter(ADMIN_IDS)))
        pm = getattr(chat, "pinned_message", None)
        doc = getattr(pm, "document", None) if pm else None
        if doc and "stats_backup" in (doc.file_name or ""):
            global _BACKUP_MSG_ID
            _BACKUP_MSG_ID = pm.message_id
            f = await bot.get_file(doc.file_id)
            buf = await bot.download_file(f.file_path)
            data = json.loads(buf.read().decode("utf-8"))
            restored = data.get("users", {})
            for k, v in restored.items():
                cur = _USER_REGISTRY.get(k)
                if not cur:
                    _USER_REGISTRY[k] = v
                else:
                    cur["searches"] = max(cur.get("searches", 0), v.get("searches", 0))
                    cur["first_seen"] = min(cur.get("first_seen") or v.get("first_seen", 0), v.get("first_seen", 0)) or v.get("first_seen", 0)
                    cur["last_seen"] = max(cur.get("last_seen", 0), v.get("last_seen", 0))
            print(f"  [tg-backup] восстановлено {len(restored)} пользователей из Telegram")
    except Exception as e:
        print(f"  [tg-backup] restore: {str(e)[:80]}")


async def _tg_backup_loop():
    """Сохраняет реестр в Telegram. Новый пользователь → бэкап в течение ~40с
    (чтобы не потерять его при деплое); иначе — раз в 15 мин для счётчиков."""
    global _registry_new_user
    await asyncio.sleep(60)
    _last_full = 0.0
    while True:
        try:
            now = time.time()
            if _registry_new_user or (now - _last_full > 900 and _registry_dirty):
                await _tg_backup_save()
                _registry_new_user = False
                _last_full = now
        except Exception:
            pass
        await asyncio.sleep(40)


def _analytics_persist():
    """Сохраняет файлы аналитики в PostgreSQL. Старые события (>40 дн) обрезаем,
    чтобы блоб не разрастался. Безопасно: ошибки гасятся."""
    try:
        import analytics as _an
        # users.json — целиком (небольшой)
        if _an.USERS_FILE.exists():
            _kv_set("analytics_users", _an.USERS_FILE.read_text(encoding="utf-8"))
        # analytics.jsonl — только события за последние 40 дней
        if _an.EVENTS_FILE.exists():
            cutoff = time.time() - 40 * 86400
            keep = []
            for line in _an.EVENTS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("ts", 0) >= cutoff:
                        keep.append(line)
                except Exception:
                    pass
            _kv_set("analytics_events", "\n".join(keep))
    except Exception as e:
        print(f"  [analytics-persist] {str(e)[:80]}")


def _analytics_restore():
    """При старте восстанавливает аналитику из PostgreSQL, если локальные файлы
    отсутствуют/пусты (контейнер пересоздан при деплое)."""
    try:
        import analytics as _an
        _an._ensure_dir()
        u = _kv_get("analytics_users")
        if u and (not _an.USERS_FILE.exists() or _an.USERS_FILE.stat().st_size < 5):
            _an.USERS_FILE.write_text(u, encoding="utf-8")
            print(f"  [analytics-restore] users.json восстановлен ({len(u)}б)")
        e = _kv_get("analytics_events")
        if e and (not _an.EVENTS_FILE.exists() or _an.EVENTS_FILE.stat().st_size < 5):
            _an.EVENTS_FILE.write_text(e + ("\n" if e and not e.endswith("\n") else ""), encoding="utf-8")
            print(f"  [analytics-restore] analytics.jsonl восстановлен ({len(e)}б)")
    except Exception as ex:
        print(f"  [analytics-restore] {str(ex)[:80]}")


async def _analytics_persist_loop():
    """Периодически сохраняет аналитику в PG (раз в 3 минуты)."""
    while True:
        await asyncio.sleep(180)
        try:
            await asyncio.get_running_loop().run_in_executor(None, _analytics_persist)
        except Exception:
            pass

import atexit as _atexit
_atexit.register(lambda: _db_conn and _db_conn.close())


def _save_cache(uid: int, items: list[dict]):
    items = _safe_rank_search_items(items)
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
        for _it in items:
            _sanitize_social_price(_it)
        items = _safe_rank_search_items(items)
        _search_cache[uid] = items  # восстанавливаем в память после перезапуска
        _save_cache(uid, items)
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

    async def _send_item(item: dict) -> bool:
        url = item.get("url", "")
        source = item.get("source", "")
        _sanitize_social_price(item)
        needs_avito_ai_check = False
        needs_sale_status_check = (
            source == "drom"
        )
        # Если нет описания или это Авито — догружаем страницу перед показом карточки.
        if url and (needs_sale_status_check or not item.get("description") or needs_avito_ai_check):
            try:
                loop_s = asyncio.get_running_loop()
                check_timeout = 12 if source == "drom" else (8 if needs_avito_ai_check else 6)
                details = await asyncio.wait_for(
                    loop_s.run_in_executor(None, _fetch_and_check, url, source),
                    timeout=check_timeout
                )
                if details is None:
                    item["_sale_status_check_failed"] = True
                    details = {}
                if details.get("_check_failed"):
                    item["_sale_status_check_failed"] = True
                    needs_sale_status_check = False
                    needs_avito_ai_check = False
                    details = {}
                if needs_sale_status_check:
                    item["_sale_status_checked"] = True
                if needs_avito_ai_check:
                    item["_avito_page_checked"] = True
                if details.get("description"):
                    item["description"] = details["description"]
                if details.get("_photo_url") and not item.get("_photo_url"):
                    item["_photo_url"] = details["_photo_url"]
                if details.get("_avito_market") and item.get("_price_int"):
                    _apply_page_market(item, int(details["_avito_market"]), "avito")
                elif details.get("_drom_market") and item.get("_price_int"):
                    _apply_page_market(item, int(details["_drom_market"]), "drom")
                elif details.get("_autoru_market") and item.get("_price_int"):
                    _apply_page_market(item, int(details["_autoru_market"]), "autoru")
                if details.get("_avito_rating"):
                    item["_avito_rating"] = details["_avito_rating"]
                    item["_avito_rating_score"] = details.get("_avito_rating_score")
                    item["_below_market"] = _is_strong_below_market(item)
            except Exception:
                item["_sale_status_check_failed"] = True
        if is_not_running(item) or not item.get("url"):
            return False
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
        # Перепроверяем битость на ПОЛНОМ описании (оно подгружается лениво, уже
        # после ранжирования — иначе битые/не на ходу всплывают как «ВЫГОДНО»).
        if not item.get("_is_junk") and _text_is_junk(item.get("title", ""), item.get("description", "")):
            item["_is_junk"] = True
        _is_junk = bool(item.get("_is_junk"))
        # Рыночную цену показываем на КАЖДОЙ машине, где она известна.
        deal_line = ""
        analysis_line = ""
        reserve_line = ""
        _lvl_now = str(item.get("_market_lvl") or "")
        _trusted_market_levels = {"avito", "drom", "autoru", "near", "bracket", "medium", "wide", "model"}
        if _lvl_now and _lvl_now not in _trusted_market_levels:
            _clear_market_fields(item)
        market = item.get("_market_price", 0)
        pct = item.get("_savings_pct", 0)
        if source == "avito" and item.get("_market_lvl") != "avito" and (item.get("_avito_rating_score") is not None):
            if (item.get("_avito_rating_score") or 0) < 0:
                market = 0
                pct = 0
        if market and _pi:
            saving = market - _pi
            _confidence = _market_confidence_text(item)
            _pct_text = f"{abs(float(pct)):.1f}"
            market_note = " с учётом пробега" if item.get("_market_mileage_factor") else ""
            if item.get("_market_lvl") == "drom":
                market_note = " Дром" + market_note
            elif item.get("_market_lvl") == "avito":
                market_note = " Авито" + market_note
            elif item.get("_market_lvl") == "autoru":
                market_note = " Auto.ru" + market_note
            elif item.get("_market_lvl") == "model":
                market_note = " грубо" + market_note
            if pct >= 5:
                # Дешевле рынка
                price_line += f"  🔻 рынок{market_note} ~{market:,} ₽ (-{_pct_text}%)".replace(",", " ")
                if _is_junk:
                    # Не на ходу / на запчасти — это НЕ выгода, а причина низкой цены.
                    deal_line = "\n🔴 не на ходу / на запчасти — низкая цена не выгода"
                else:
                    tier = "🟢 ВЫГОДНО" if pct >= 25 else "🟡 ниже рынка"
                    deal_line = f"\n{tier}: дешевле рынка на ~{saving:,} ₽".replace(",", " ")
                    # Потенциальная прибыль перекупа: рынок − цена − примерные
                    # расходы (комиссия площадки ~4% + подготовка ~10 000 ₽).
                    _costs = int(_pi * 0.04) + 10_000
                    _profit = saving - _costs
                    if _profit >= 15_000:
                        reserve_line = f"💵 Потенциальная прибыль ~{_profit:,} ₽ (после расходов)".replace(",", " ")
                    else:
                        reserve_line = "💵 Запас маленький: торг/вложения могут съесть выгоду"
            elif pct > 0:
                price_line += f"  ≈ рынок{market_note} ~{market:,} ₽".replace(",", " ")
                deal_line = f"\n⚪ около рынка: скидка {pct}%, не считаю выгодой ниже рынка"
            elif pct < 0:
                # Дороже рынка
                price_line += f"  🔺 рынок{market_note} ~{market:,} ₽ (+{_pct_text}%)".replace(",", " ")
            else:
                # По рынку
                price_line += f"  ≈ рынок{market_note} ~{market:,} ₽".replace(",", " ")
            if pct > 0:
                _analysis = f"ниже рынка на ~{max(0, saving):,} ₽ ({_pct_text}%)".replace(",", " ")
            elif pct < 0:
                _analysis = f"выше рынка на ~{abs(saving):,} ₽ ({_pct_text}%)".replace(",", " ")
            else:
                _analysis = "на уровне рынка (0.0%)"
            analysis_line = f"📊 Анализ цены: {_analysis} · доверие: {_confidence}"
        elif _pi:
            analysis_line = "📊 рынок: мало похожих авто для точной оценки"

        mileage = item.get("mileage", 0)
        mileage_str = ""
        if mileage and mileage < 900_000:
            mileage_str = f"  ·  🛣 {mileage:,} км".replace(",", " ")

        dealer_tag = " 🏢" if item.get("_is_dealer") else ""
        _light = _traffic_light(item)  # 🚦 светофор выгодности/чистоты
        caption = (
            f"{_light} {source_tag} {item.get('title', '')}{hot_tag}{dealer_tag}\n"
            f"💰 {price_line}{deal_line}\n"
            f"📅 {days_str}{mileage_str}"
        )
        if analysis_line:
            caption += f"\n{analysis_line}"
        if reserve_line:
            caption += f"\n{reserve_line}"
        _liq = _liquidity_note(item)  # 📊 ликвидность модели
        if _liq:
            caption += f"\n📊 {_liq}"
        _av_rating = item.get("_avito_rating")
        if _av_rating:
            _rt_score = item.get("_avito_rating_score", 0) or 0
            _rt_icon = "🟢" if _rt_score > 0 else ("🔴" if _rt_score < 0 else "⚪")
            caption += f"\n{_rt_icon} оценка Авито: {_av_rating}"
        _drop = item.get("_price_drop", 0)
        if _drop:
            caption += f"\n📉 продавец снизил цену на ~{_drop:,} ₽ — готов торговаться".replace(",", " ")
        _owners = _extract_owners(f"{item.get('title','')} {item.get('description','')}")
        if _owners:
            _own_word = "владелец" if _owners == 1 else ("владельца" if _owners <= 4 else "владельцев")
            _own_tag = " 👍" if _owners <= 2 else ""
            caption += f"\n🧾 {_owners} {_own_word} по ПТС{_own_tag}"
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
            _seller_url = item.get("_seller_url", "")
            row1 = [InlineKeyboardButton(text="📘 Объявление ВК", url=url)]
            # Кнопка на ЛИЧНУЮ страницу продавца (только если она реально найдена;
            # для постов от имени группы _seller_url пустой → кнопки нет)
            if _seller_url and _seller_url != url:
                row1.append(InlineKeyboardButton(text="👤 Продавец", url=_seller_url))
            row1.append(InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"))
        elif source in ("tg", "tg_channel"):
            caption += f"\n📢 Канал: {seller}" if seller else ""
            seller_url = item.get("_seller_url", url)
            row1 = [
                InlineKeyboardButton(text="💬 Открыть в TG", url=url or seller_url),
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
        row3 = [
            InlineKeyboardButton(text="🔍 Пробить машину (штрафы, аресты)", callback_data=f"check|{sid}|{uid}"),
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])

        photo_url = item.get("_photo_url", "")
        if photo_url:
            try:
                import requests as _req
                from aiogram.types import BufferedInputFile
                loop = asyncio.get_running_loop()

                def _download_photo():
                    _item_source = item.get("source", "")
                    # Referer и прокси зависят от источника.
                    # TG/VK CDN доступны напрямую с Railway — прокси Авито им мешает.
                    if _item_source == "autoru":
                        _referer = "https://auto.ru/"
                        _px = _avito_proxies()
                    elif _item_source == "drom":
                        _referer = "https://auto.drom.ru/"
                        _px = _avito_proxies()
                    elif _item_source in ("tg", "tg_channel"):
                        _referer = "https://t.me/"
                        _px = None  # Telegram CDN — напрямую, без прокси Авито
                    elif _item_source == "vk":
                        _referer = "https://vk.com/"
                        _px = None  # VK CDN — напрямую
                    elif _item_source == "youla":
                        _referer = "https://youla.ru/"
                        _px = None  # Youla CDN — напрямую, без прокси
                    else:
                        _referer = "https://www.avito.ru/"
                        _px = _avito_proxies()
                    # 1. curl_cffi — обходит блокировку CDN с Railway IP
                    try:
                        from curl_cffi import requests as _cffi
                        r = _cffi.get(photo_url, impersonate="chrome124", timeout=5,
                                      headers={"Referer": _referer},
                                      proxies=_px)
                        if r.status_code == 200 and len(r.content) > 3_000:
                            return r.content
                    except Exception:
                        pass
                    # 2. Обычный requests с Referer
                    try:
                        r2 = _req.get(photo_url, timeout=5, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "Referer": _referer,
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        }, proxies=_px)
                        if r2.status_code == 200 and len(r2.content) > 3_000:
                            return r2.content
                    except Exception:
                        pass
                    return None

                content = await loop.run_in_executor(None, _download_photo)
                if content:
                    photo_bytes = BufferedInputFile(content, filename="photo.jpg")
                    await bot.send_photo(chat_id, photo=photo_bytes, caption=caption, reply_markup=kb)
                    return True
            except Exception:
                pass
            # Fallback: передаём URL напрямую Telegram
            try:
                await bot.send_photo(chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                return True
            except Exception:
                pass
        try:
            await bot.send_message(chat_id, caption, reply_markup=kb)
            return True
        except Exception as e:
            print(f"  [send] full card failed: {str(e)[:100]}")
            try:
                short_caption = (
                    f"{source_tag} {item.get('title', 'Объявление')}\n"
                    f"💰 {price_line}\n"
                    f"🔗 {url}"
                )
                await bot.send_message(chat_id, short_caption, reply_markup=kb)
                return True
            except Exception as e2:
                print(f"  [send] short card failed: {str(e2)[:100]}")
                return False

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

    sent_items: list[dict] = []
    scanned = offset
    deferred_junk: list[dict] = []
    while scanned < total and len(sent_items) < 10:
        item = items[scanned]
        scanned += 1
        try:
            await _prefetch(item)
        except Exception:
            pass
        if not item.get("_is_junk") and _text_is_junk(item.get("title", ""), item.get("description", "")):
            item["_is_junk"] = True
        if item.get("_is_junk"):
            deferred_junk.append(item)
            continue
        if await _send_item(item):
            sent_items.append(item)
            await asyncio.sleep(0.01)

    for item in deferred_junk:
        if len(sent_items) >= 10:
            break
        if await _send_item(item):
            sent_items.append(item)
            await asyncio.sleep(0.01)

    if not sent_items and total:
        for item in items[offset:min(total, offset + 20)]:
            if not item.get("url"):
                continue
            if await _send_item(item):
                sent_items.append(item)
                await asyncio.sleep(0.01)
                if len(sent_items) >= 10:
                    break

    if not sent_items:
        await bot.send_message(
            chat_id,
            f"⚠️ Нашёл {total} объявлений, но Telegram не принял карточки для отправки. "
            "Попробуй нажать «Искать» ещё раз или расширить бюджет в /settings.",
        )
        return

    next_offset = scanned
    shown_str = f"{len(sent_items)} авто, просмотрено {next_offset}/{total}"
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
    for item in sent_items:
        u = item.get("url", "")
        if u:
            seen.add(_norm_url(u))
    if len(seen) > 2000:
        seen = set(list(seen)[-1500:])
    save_seen(uid, seen)


async def do_search_for_user(uid: int, reply_to):
    # Обязательная подписка на канал отключена — поиск доступен всем.
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
    await reply_to.answer(f"🔍 Ищу в {region_name} и области ({pmin:,}–{pmax:,} ₽)\n{src_labels}")

    skipped = load_skipped(uid)
    seen = load_seen(uid)
    loop = asyncio.get_running_loop()

    scraper_map = {
        "drom":   lambda: scrape_drom(region, pages=10, price_min=pmin, price_max=pmax, brand=(brand if brand and brand != "any" else "")),
        "autoru": lambda: scrape_autoru(region, pages=2, price_min=pmin, price_max=pmax, brand=(brand if brand and brand != "any" else ""), deadline_sec=24),
        "avito":  lambda: scrape_avito(region, pages=1, price_min=pmin, price_max=pmax, sort_by_date=True, brand=(brand if brand and brand != "any" else ""), fast=True),
        "youla":  lambda: scrape_youla(region, pages=16, price_min=pmin, price_max=pmax, brand=(brand if brand and brand != "any" else "")),
        "vk":     lambda: scrape_vk_groups(region, pmin, pmax),
        "tg":     lambda: scrape_tg_channels(region, pmin, pmax, fast=True),
    }
    # Ищем ТОЛЬКО выбранные пользователем площадки.
    src_keys = [src for src in enabled_sources if src in scraper_map]
    if not src_keys:
        src_keys = list(scraper_map.keys())  # подстраховка: если выбор пуст — все
    futures = [loop.run_in_executor(None, scraper_map[src]) for src in src_keys]

    # Avito is both a search source and the market reference. When Avito is
    # already selected, reuse that result/cache instead of launching a duplicate
    # Avito scrape that often makes the source miss the user-facing timeout.
    _avito_ref_fut = None
    if "avito" not in src_keys:
        _avito_ref_fut = loop.run_in_executor(
            None, lambda: scrape_avito(
                region,
                pages=1,
                price_min=0,
                price_max=99_000_000,
                fast=True,
            )
        )
    done, pending = await asyncio.wait(futures, timeout=SEARCH_SOURCE_TIMEOUT_SEC)
    # Авито и Auto.ru проходят более тяжёлую антибот-защиту и часто завершаются
    # чуть позже быстрых площадок. Даём им отдельный резерв времени, иначе готовый
    # результат выбрасывался ровно на общем таймауте и в статистике появлялось
    # «не успел», хотя поток продолжал работу и наполнял кэш уже после ответа.
    critical_pending = {
        f for src, f in zip(src_keys, futures)
        if src in ("avito", "autoru") and f in pending
    }
    if critical_pending:
        critical_done, _ = await asyncio.wait(
            critical_pending,
            timeout=min(3, SEARCH_CRITICAL_SOURCE_GRACE_SEC),
        )
        done = set(done) | set(critical_done)
        pending = set(pending) - set(critical_done)
        if critical_done:
            print(
                f"  [search] critical grace: завершено "
                f"{len(critical_done)}/{len(critical_pending)} медленных источников"
            )
    if pending:
        for f in pending:
            f.cancel()
        print(f"  [search] timeout: {len(pending)} source(s) still running, showing completed results")
    _avito_ref_extra: list[dict] = []
    if _avito_ref_fut is not None:
        try:
            _avito_ref_extra = await asyncio.wait_for(_avito_ref_fut, timeout=5)
        except Exception as _e:
            _avito_ref_extra = []
            print(f"  [рынок] Авито-эталон не успел/ошибка: {str(_e)[:80]}")
    results = []
    source_status: dict[str, str] = {}
    for src, f in zip(src_keys, futures):
        if f in done:
            try:
                results.append(f.result())
                source_status[src] = "ok"
            except Exception as e:
                print(f"  [скрапер] ошибка: {e}")
                results.append([])
                source_status[src] = "error"
        else:
            results.append([])
            source_status[src] = "timeout"

    if "avito" in src_keys:
        try:
            _avito_ref_extra = list(results[src_keys.index("avito")] or [])
        except Exception:
            _avito_ref_extra = []

    avito_enabled = "avito" in enabled_sources and "avito" in scraper_map
    if avito_enabled and "avito" in src_keys:
        avito_idx = src_keys.index("avito")
        if len(results[avito_idx] or []) == 0:
            def _avito_budget_candidates(pool: list[dict]) -> list[dict]:
                picked: list[dict] = []
                seen_urls: set[str] = set()
                for it in pool or []:
                    if (it.get("source", "") or "").lower() != "avito":
                        continue
                    price = int(it.get("_price_int") or parse_price(it.get("price", "")) or 0)
                    if not price or price < pmin or price > pmax:
                        continue
                    if is_dealer(it) or not it.get("url"):
                        continue
                    url = _norm_url(it.get("url", ""))
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    picked.append(dict(it))
                return picked

            avito_fallback_pool = list(_avito_ref_extra or [])
            for cache_key, (_ts, cached_items) in list(_AVITO_REGION_CACHE.items()):
                if cache_key == region or cache_key.startswith(region + "_"):
                    avito_fallback_pool.extend(cached_items or [])
            avito_fallback = _avito_budget_candidates(avito_fallback_pool)[:60]
            if avito_fallback:
                results[avito_idx] = avito_fallback
                source_status["avito"] = "ok"
                print(
                    f"  [fallback] Авито основной поиск 0 → восстановлено "
                    f"{len(avito_fallback)} объявлений из эталона/кэша"
                )

    if "autoru" in src_keys:
        autoru_idx = src_keys.index("autoru")
        if len(results[autoru_idx] or []) == 0:
            autoru_cached = _cached_source_results("autoru", region, pmin, pmax, limit=30)
            for it in (_search_cache.get(uid) or _load_cache(uid) or []):
                if (it.get("source", "") or "").lower() != "autoru":
                    continue
                price = int(it.get("_price_int") or parse_price(it.get("price", "")) or 0)
                if price and not (pmin <= price <= pmax):
                    continue
                if is_dealer(it) or not it.get("url"):
                    continue
                autoru_cached.append(dict(it))
            if autoru_cached:
                results[autoru_idx] = _dedupe_search_items(autoru_cached)[:30]
                source_status["autoru"] = "ok"
                print(f"  [fallback] Auto.ru восстановлено {len(results[autoru_idx])} объявлений из общего кэша")

    if "tg" in src_keys:
        tg_idx = src_keys.index("tg")
        if len(results[tg_idx] or []) == 0:
            tg_cached = _cached_source_results("tg", region, pmin, pmax, limit=30)
            if tg_cached:
                results[tg_idx] = tg_cached
                source_status["tg"] = "ok"
                print(f"  [fallback] Telegram восстановлено {len(tg_cached)} объявлений из кэша")

    items = []
    stat_parts = []
    for src, batch in zip(src_keys, results):
        items.extend(batch)
        tag = SOURCE_TAGS.get(src, src)
        status = source_status.get(src, "ok")
        if status == "timeout":
            print(f"  [scraper] {src}: timeout")
            stat_parts.append(f"{tag}: не успел")
        elif status == "error":
            print(f"  [scraper] {src}: error")
            stat_parts.append(f"{tag}: ошибка")
        elif len(batch) == 0:
            print(f"  [scraper] {src}: no data")
            stat_parts.append(f"{tag}: пусто")
        else:
            stat_parts.append(f"{tag}: {len(batch)}")

    if stat_parts:
        await reply_to.answer("📊 " + " | ".join(stat_parts))

    # Если Авито — единственный включённый источник и вернул 0 результатов,
    # автоматически добавляем Дром как запасной источник.
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
                    None, lambda: scrape_drom(region, pages=4, price_min=pmin, price_max=pmax)
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
                    await asyncio.wait_for(
                        loop2.run_in_executor(None, _fetch_price_sync, it),
                        timeout=SEARCH_PRICE_FILL_TIMEOUT_SEC,
                    )
                except Exception:
                    pass
        await asyncio.gather(*[_fetch_price(it) for it in no_price[:SEARCH_PRICE_FILL_LIMIT]])

    # Эталон рынка — ВСЕГДА цены Авито (требование: сравнивать с рынком Авито).
    # Авито-объявления берём из основного поиска (если Авито выбран) либо из
    # отдельного эталонного скрейпа (если Авито не выбран). rank_by_market_price
    # группирует по марке+модели+году → медиана корректна даже в пределах бюджета.
    import copy as _copy
    _avito_ref_items = [i for i in items if i.get("source") == "avito"]
    if _avito_ref_extra:
        try:
            _extra_ref = _avito_ref_extra or []
            if _extra_ref:
                _seen_ref_u = {_norm_url(i.get("url", "")) for i in _avito_ref_items}
                _avito_ref_items = list(_avito_ref_items) + [
                    i for i in _extra_ref
                    if _norm_url(i.get("url", "")) not in _seen_ref_u
                ]
        except Exception as _e:
            print(f"  [рынок] Авито-эталон ошибка: {_e}")
    # ВАЖНО для поиска ниже рынка: результаты Авито отфильтрованы БЮДЖЕТОМ, поэтому
    # «рынок» из них занижен (только дешёвые машины) и настоящая скидка теряется.
    # Добавляем полный кэш региона (без фильтра по цене) — он собирается фоновым
    # прогревом и содержит РЕАЛЬНЫЙ рынок модели во всём ценовом диапазоне.
    try:
        _reg_cache = _AVITO_REGION_CACHE.get(region)
        if not _reg_cache:
            for _ck, _cv in _AVITO_REGION_CACHE.items():
                if _ck == region or _ck.startswith(region + "_"):
                    if not _reg_cache or len(_cv[1]) > len(_reg_cache[1]):
                        _reg_cache = _cv
        if _reg_cache and _reg_cache[1]:
            _seen_ref_u = {_norm_url(i.get("url", "")) for i in _avito_ref_items}
            _extra = [i for i in _reg_cache[1]
                      if i.get("_price_int", 0) and _norm_url(i.get("url", "")) not in _seen_ref_u]
            if _extra:
                _avito_ref_items = list(_avito_ref_items) + _extra
                print(f"  [рынок] +{len(_extra)} записей из полного кэша региона (истинный рынок)")
    except Exception as _e:
        print(f"  [рынок] кэш-эталон ошибка: {_e}")
    if _avito_ref_items:
        _ref_copies = [_copy.copy(i) for i in _avito_ref_items]
        for _rc in _ref_copies:
            _rc["_market_ref_only"] = True
        items = items + _ref_copies
        print(f"  [рынок] Авито-эталон: {len(_ref_copies)} записей для медианы цен")
    else:
        print("  [рынок] нет Авито-эталона — используем Дром/Auto.ru/Юлу после фильтрации")

    # seen хранит нормализованные URL — сравниваем тоже по нормализованным
    seen_norm = {_norm_url(u) for u in seen}
    skipped_norm = {_norm_url(u) for u in skipped}
    def _display_fallback_candidates() -> list[dict]:
        preferred: list[dict] = []
        broader: list[dict] = []
        for it in items:
            if it.get("_market_ref_only") or not it.get("url"):
                continue
            if _norm_url(it.get("url", "")) in skipped_norm:
                continue
            price = int(it.get("_price_int") or parse_price(it.get("price", "")) or 0)
            if price:
                it["_price_int"] = price
            if price and pmin <= price <= pmax:
                preferred.append(it)
            else:
                broader.append(it)
        return _safe_rank_search_items(preferred or broader)[:120]

    _display_fallback = _display_fallback_candidates()
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
        and (i.get("_price_int") or parse_price(i.get("price", "")))
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

    # Фильтр по типу продавца (частник / перекуп / автодилер).
    _allowed_types = set(_get_seller_types(load_settings(uid)))
    if _allowed_types and _allowed_types != {"private", "pro", "dealer"}:
        # Считаем телефоны, чтобы отличить перекупа (один номер в 3+ объявлениях)
        _phone_counts: dict = {}
        for it in suitable:
            ph = _seller_phone(it)
            if ph:
                _phone_counts[ph] = _phone_counts.get(ph, 0) + 1
        _before_priv = len(suitable)
        suitable = [it for it in suitable if _seller_type(it, _phone_counts) in _allowed_types]
        print(f"  [фильтр] тип продавца {sorted(_allowed_types)}: {len(suitable)}/{_before_priv}")

    # Финальная дедупликация suitable (могут быть дубли если разные источники нашли одно).
    # Дедуп по URL И по сигнатуре содержимого (телефон / нормализованный текст) —
    # один и тот же пост перепубликовывают в разных группах с разными URL.
    def _content_sig(it: dict) -> str:
        """Сигнатура объявления: телефон (если есть) или нормализованный текст."""
        txt = (it.get("description", "") or "") + " " + (it.get("title", "") or "")
        # Телефон — самый надёжный признак одного продавца/объявления
        digits = re.sub(r"\D", "", txt)
        _phones = re.findall(r"[78]\d{10}", digits)
        if _phones:
            return "tel:" + _phones[0][-10:]
        # Иначе — нормализованный текст (буквы+цифры, первые 80 симв)
        norm = re.sub(r"[^a-zа-я0-9]", "", txt.lower())
        return "txt:" + norm[:80] if len(norm) >= 20 else ""

    _seen_final: set[str] = set()
    _seen_sig: set[str] = set()
    _deduped_suitable: list[dict] = []
    for _it in suitable:
        _u = _norm_url(_it.get("url", ""))
        if not _u or _u in _seen_final:
            continue
        _sig = _content_sig(_it)
        if _sig and _sig in _seen_sig:
            continue  # дубль по содержимому (репост в другой группе)
        _seen_final.add(_u)
        if _sig:
            _seen_sig.add(_sig)
        _deduped_suitable.append(_it)
    suitable = _deduped_suitable
    print(f"  [фильтр] после финальной дедупликации: {len(suitable)}")

    # Помечаем уже просмотренные — они получат штраф и уйдут в конец
    for it in suitable:
        if it.get("url") and _norm_url(it["url"]) in seen_norm:
            it["_already_seen"] = True

    _ref_items = [
        i for i in items
        if i.get("_market_ref_only") and (i.get("source", "") or "").lower() == "avito"
    ]
    _avito_available = bool(_ref_items)
    _market_ref_items = list(_ref_items)
    if _avito_available:
        print(f"  [рынок] Авито-референс: {len(_ref_items)} объявлений → считаем рыночную цену")
        suitable = rank_by_market_price(suitable, ref_items=_ref_items, avito_only_median=True)
    else:
        _market_ref_items = [
            i for i in suitable
            if (i.get("source", "") or "").lower() in {"drom", "autoru", "youla", "yula"}
            and int(i.get("_price_int") or 0) > 0
        ]
        print(
            f"  [рынок] резервный эталон: {len(_market_ref_items)} объявлений "
            f"Дром/Auto.ru/Юла"
        )
        suitable = rank_by_market_price(
            suitable,
            ref_items=_market_ref_items,
            avito_only_median=False,
        )
    # 📊 Ликвидность считаем по тому же эталону, что и рыночную цену. Раньше
    # строка «в продаже / срок продажи» появлялась только при живом Авито и
    # исчезала при резервном эталоне Дром/Auto.ru/Юла.
    try:
        from statistics import median as _median
        _liq_cnt: dict[str, int] = {}
        _liq_days: dict[str, list] = {}
        for _r in _market_ref_items:
            _k = _car_group_key(_r.get("title", ""))
            if not _k:
                continue
            _liq_cnt[_k] = _liq_cnt.get(_k, 0) + 1
            _d = int(_r.get("_days_on_site") or 0)
            if _d > 0:
                _liq_days.setdefault(_k, []).append(_d)
        for it in suitable:
            _k = _car_group_key(it.get("title", ""))
            if _k and _k in _liq_cnt:
                it["_liq_count"] = _liq_cnt[_k]
                if _liq_days.get(_k):
                    it["_liq_days"] = int(_median(_liq_days[_k]))
    except Exception as _le:
        print(f"  [ликвидность] ошибка: {_le}")
    # Дилерские объявления — добавляем штраф к deal_score
    for it in suitable:
        if is_dealer(it):
            it["_is_dealer"] = True
            it["_deal_score"] = it.get("_deal_score", 0) - 30
    suitable = _sort_by_deal(suitable)
    if not suitable and _display_fallback:
        suitable = _display_fallback
        await reply_to.answer(
            f"⚠️ Строгие фильтры убрали все карточки. Показываю {len(suitable)} найденных объявлений, чтобы выдача не была пустой."
        )

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

    # Быстро проверяем первые объявления: убираем проданные, загружаем фото+описание.
    check_batch = suitable[:SEARCH_DETAIL_CHECK_LIMIT]
    rest_batch = suitable[SEARCH_DETAIL_CHECK_LIMIT:]
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
                check_timeout = 12 if source == "drom" else SEARCH_DETAIL_CHECK_TIMEOUT_SEC
                details = await asyncio.wait_for(
                    loop_pre.run_in_executor(None, _fetch_and_check, it["url"], source),
                    timeout=check_timeout
                )
                if details is None:
                    return None
                if details.get("_check_failed"):
                    it["_sale_status_check_failed"] = True
                    return it
                it["_enriched"] = True
                it["_sale_status_checked"] = True
                # Фото обновляем только если у объявления его нет (не перезаписываем хорошее)
                if details.get("_photo_url") and not it.get("_photo_url"):
                    it["_photo_url"] = details["_photo_url"]
                if details.get("description") and not it.get("description"):
                    it["description"] = details["description"]
                if details.get("_avito_market") and it.get("_price_int"):
                    _apply_page_market(it, int(details["_avito_market"]), "avito")
                elif details.get("_drom_market") and it.get("_price_int"):
                    _apply_page_market(it, int(details["_drom_market"]), "drom")
                elif details.get("_autoru_market") and it.get("_price_int"):
                    _apply_page_market(it, int(details["_autoru_market"]), "autoru")
                if details.get("_avito_rating"):
                    it["_avito_rating"] = details["_avito_rating"]
                    it["_avito_rating_score"] = details.get("_avito_rating_score")
                    it["_below_market"] = _is_strong_below_market(it)
                return it
            except Exception:
                it["_sale_status_check_failed"] = True
                return it  # VK/TG не проверяем по странице

    try:
        checked = await asyncio.wait_for(
            asyncio.gather(*[_check_item(it) for it in check_batch]),
            timeout=SEARCH_DETAIL_TOTAL_TIMEOUT_SEC
        ) if check_batch else []
    except asyncio.TimeoutError:
        checked = check_batch

    active = [it for it in checked if it is not None]
    sold_count = len(check_batch) - len(active)
    if sold_count:
        print(f"  [фильтр] убрано {sold_count} проданных объявлений из первых {len(check_batch)}")
    suitable = active + rest_batch
    for it in suitable:
        if _text_is_junk(it.get("title", ""), it.get("description", "")):
            it["_is_junk"] = True
    suitable = rank_by_market_price(
        suitable,
        ref_items=_market_ref_items,
        avito_only_median=_avito_available,
    )
    suitable = _sort_by_deal(suitable)

    # После загрузки цен — выкидываем только те, у кого цена ИЗВЕСТНА и вышла за бюджет.
    # Объявления без цены (_price_int=0) — оставляем: пользователь откроет ссылку и проверит.
    # Это критично для объявлений из поисковых сниппетов — там цена в HTML не всегда есть.
    # Второй price-фильтр убран — первый in_price_range уже отфильтровал.
    # Дополнительно фильтровать не нужно, это только теряет объявления с неизвестной ценой.
    print(f"  [фильтр] suitable после всех фильтров: {len(suitable)}")

    if not suitable:
        await reply_to.answer("😔 Не нашёл объявлений в твоём бюджете. Попробуй расширить диапазон цен: /settings")
        return

    # Пользовательский поиск должен быть широким: если точных сделок мало, не
    # срезаем выдачу до 1-2 карточек. Реальные скидки идут первыми, остальные
    # релевантные объявления в бюджете остаются ниже в списке.
    _market_items_count = sum(1 for i in suitable if i.get("_market_price") and i.get("_price_int"))
    _below_count = sum(1 for i in suitable if _is_strong_below_market(i))
    _market_available = _market_items_count > 0
    if _market_available:
        below_items = _best_below_market_items(suitable)
        below_urls = {_norm_url(i.get("url", "")) for i in below_items}
        market_items = _sort_by_deal([
            i for i in suitable
            if _norm_url(i.get("url", "")) not in below_urls
            and i.get("_market_price") and i.get("_price_int")
            and _is_market_candidate(i)
        ])
        market_urls = {_norm_url(i.get("url", "")) for i in market_items}
        rest_items = _sort_by_deal([
            i for i in suitable
            if _norm_url(i.get("url", "")) not in below_urls
            and _norm_url(i.get("url", "")) not in market_urls
            and not (i.get("_market_price") and i.get("_price_int"))
            and _is_market_candidate(i)
        ])
        suitable = _safe_rank_search_items(suitable)[:120]
        _below_count = sum(1 for i in suitable if _is_strong_below_market(i))
        _market_tail_count = sum(
            1 for i in suitable
            if i.get("_market_price") and i.get("_price_int") and not _is_strong_below_market(i)
        )
        print(
            f"  [фильтр] ниже рынка={_below_count}, с рынком={_market_items_count}, "
            f"рынок внизу={_market_tail_count}, показываем={len(suitable)}"
        )
    else:
        suitable = _safe_rank_search_items(suitable)[:80]
        print(f"  [фильтр] точных оценок нет → показываем {len(suitable)} объявлений в бюджете")

    if not suitable and _display_fallback:
        suitable = _display_fallback[:80]
        print(f"  [fallback] final ranking empty -> showing {len(suitable)} basic listings")

    if not suitable:
        await reply_to.answer(
            f"😔 Не нашёл подтверждённых авто ниже рынка (≥{MARKET_DEAL_MIN_PCT:.0f}%).\n"
            f"Отфильтровал объявления по рынку, выше рынка и без точной оценки, чтобы не присылать мусор.\n"
            f"Попробуй расширить бюджет, регион или включить больше площадок: /settings"
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    analytics.track(
        "search", uid=uid, region=region, price_min=pmin, price_max=pmax,
        source=",".join(enabled_sources), results=len(suitable),
    )
    # Надёжный счётчик поиска в PG (переживает деплой)
    try:
        await asyncio.get_running_loop().run_in_executor(None, lambda: _register_user(uid, None, True))
    except Exception:
        pass
    _seen_cnt = sum(1 for i in suitable if i.get("_already_seen"))
    src_found = list(dict.fromkeys(i.get("source","") for i in suitable if i.get("source")))
    src_icons = {"avito":"🟠","drom":"🔵","autoru":"🔴","youla":"🟡","vk":"💙","tg":"✈️"}
    src_str = " ".join(src_icons.get(s,"") for s in src_found if s)
    if _market_available:
        _extra = max(0, len(suitable) - _below_count)
        if _below_count:
            _msg = (
                f"✅ {src_str} Найдено {_below_count} объявлений ниже рынка "
                f"(≥{MARKET_DEAL_MIN_PCT:.0f}%)."
            )
            if _extra:
                _msg += f"\n➕ Ещё {_extra} авто в бюджете — ниже в списке."
        else:
            _msg = (
                f"✅ {src_str} Сильных скидок ≥{MARKET_DEAL_MIN_PCT:.0f}% сейчас нет.\n"
                f"Показываю {len(suitable)} авто в бюджете, лучшие с анализом рынка сверху."
            )
    else:
        _msg = (
            f"✅ {src_str} Найдено {len(suitable)} авто в бюджете.\n"
            f"⚠️ Точной рыночной оценки сейчас нет — показываю релевантные объявления, а не пустую выдачу."
        )
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


@dp.callback_query(F.data.startswith("check|"))
async def cb_check_car(cb: CallbackQuery):
    """Пробить машину: штрафы, аресты, залоги, ДТП, ограничения ГИБДД."""
    sid = cb.data.split("|")[1]
    uid = cb.from_user.id  # защита от IDOR
    url = id_to_url(sid)
    items = _search_cache.get(uid) or _load_cache(uid)
    item = next((it for it in items if it.get("url") == url), None)
    # Ищем и в избранном/гараже — кнопка «Пробить» есть и там
    if item is None:
        try:
            _ff = user_dir(uid) / "favorites.json"
            if _ff.exists():
                _favs = json.loads(_ff.read_text(encoding="utf-8"))
                item = next((it for it in _favs if it.get("url") == url), None)
        except Exception:
            pass
    analytics.track("check_car", uid=uid, username=cb.from_user.username)

    # Пытаемся вытащить VIN (17 символов, без I,O,Q) и госномер из текста объявления
    _txt = f"{(item or {}).get('title','')} {(item or {}).get('description','')}"
    _vin_m = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', _txt.upper())
    vin = _vin_m.group(1) if _vin_m else ""
    _plate_m = re.search(r'\b([АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3})\b', _txt.upper())
    plate = _plate_m.group(1) if _plate_m else ""

    title = (item or {}).get("title", "автомобиль")
    lines = [f"🔍 <b>Проверка авто:</b> {title}", ""]
    if vin:
        lines.append(f"🔑 <b>VIN найден в объявлении:</b> <code>{vin}</code>")
    if plate:
        lines.append(f"🚘 <b>Госномер:</b> <code>{plate}</code>")
    if not vin and not plate:
        lines.append("ℹ️ VIN/госномер не указан в объявлении — узнай его у продавца "
                     "и введи на сайтах ниже. По ним проверишь штрафы, аресты, залоги, ДТП и ограничения.")
    lines.append("\nОткрой нужный сервис (все бесплатные, кроме полного отчёта):")

    rows = []
    # ГИБДД — ДТП, розыск, ограничения (аресты), история регистрации (по VIN)
    rows.append([InlineKeyboardButton(text="🚔 ГИБДД: ДТП, аресты, розыск", url="https://xn--90adear.xn--p1ai/check/auto")])
    # ФССП — долги и исполнительные производства
    rows.append([InlineKeyboardButton(text="⚖️ ФССП: долги, аресты приставов", url="https://fssp.gov.ru/iss/ip")])
    # Реестр залогов ФНП
    rows.append([InlineKeyboardButton(text="💰 Реестр залогов (в залоге?)", url="https://www.reestr-zalogov.ru/search/index")])
    # РСА — полисы ОСАГО
    rows.append([InlineKeyboardButton(text="🛡 РСА: полисы ОСАГО", url="https://dkbm-web.autoins.ru/dkbm-web-1.0/bsostate.htm")])
    # Полный отчёт по VIN — Дром (прямая ссылка если VIN есть)
    if vin:
        rows.append([InlineKeyboardButton(text="📄 Полный отчёт по VIN (Дром)", url=f"https://vin.drom.ru/{vin}/")])
    else:
        rows.append([InlineKeyboardButton(text="📄 Полный отчёт (Автотека)", url="https://avtoteka.ru/")])
    rows.append([InlineKeyboardButton(text="🔗 Открыть объявление", url=url)])

    await cb.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        disable_web_page_preview=True,
    )
    await cb.answer()


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
    await msg.answer(f"🚗 Мой гараж ({len(favs)} авто):")
    for it in favs[-10:]:
        url = it.get("url", "")
        sid = url_to_id(url)
        caption = f"🚗 {it.get('title','')}\n💰 {it.get('price','?')}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть", url=url)] if url else [],
            [InlineKeyboardButton(text="🔍 Пробить машину (штрафы, аресты)", callback_data=f"check|{sid}|{uid}")],
        ])
        # убираем пустой ряд (если url нет)
        kb.inline_keyboard = [r for r in kb.inline_keyboard if r]
        await msg.answer(caption, reply_markup=kb)


# ── 🚗 Мои сделки (аналитика перекупа) ───────────────────────────
@dp.message(F.text == "💼 Мои сделки")
async def cmd_my_deals(msg: Message):
    uid = msg.from_user.id
    deals = _load_deals(uid)
    if not deals:
        await msg.answer(
            "💼 *Мои сделки* — учёт купленных авто: вложения, прибыль, срок продажи.\n\n"
            "Добавь первую машину, которую купил на перепродажу 👇",
            parse_mode="Markdown",
            reply_markup=_deals_keyboard(deals),
        )
        return
    # Список авто в работе с подробностями
    detail = []
    for d in deals:
        if d.get("status") == "active":
            cost = d.get("buy", 0) + d.get("expenses", 0)
            detail.append(f"🔧 {d.get('title','')} — вложено {cost:,} ₽".replace(",", " "))
    for d in deals[-15:]:
        if d.get("status") == "sold":
            detail.append(
                f"✅ {d.get('title','')} — прибыль {d.get('profit',0):+,} ₽".replace(",", " ")
            )
    text = _deals_summary(deals) + ("\n\n" + "\n".join(detail) if detail else "")
    await msg.answer(text, parse_mode="Markdown", reply_markup=_deals_keyboard(deals))


@dp.callback_query(F.data == "deal_add")
async def cb_deal_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(MyDeals.add_title)
    await cb.message.answer("🚗 Введи марку и модель авто (например: Kia Rio 2014):")
    await cb.answer()


@dp.message(MyDeals.add_title)
async def deal_add_title(msg: Message, state: FSMContext):
    title = (msg.text or "").strip()[:80]
    if not title:
        await msg.answer("Введи название авто текстом.")
        return
    await state.update_data(deal_title=title)
    await state.set_state(MyDeals.add_buy)
    await msg.answer(f"💰 За сколько купил «{title}»? (только число, ₽):")


@dp.message(MyDeals.add_buy)
async def deal_add_buy(msg: Message, state: FSMContext):
    buy = re.sub(r"[^\d]", "", msg.text or "")
    if not buy:
        await msg.answer("Введи цену покупки числом, например 450000.")
        return
    await state.update_data(deal_buy=int(buy))
    await state.set_state(MyDeals.add_expenses)
    await msg.answer("🔧 Доп. расходы (ремонт, мойка, перегон)? Число в ₽, или 0:")


@dp.message(MyDeals.add_expenses)
async def deal_add_expenses(msg: Message, state: FSMContext):
    exp = re.sub(r"[^\d]", "", msg.text or "") or "0"
    data = await state.get_data()
    uid = msg.from_user.id
    deals = _load_deals(uid)
    deals.append({
        "id": str(int(time.time())),
        "title": data.get("deal_title", "Авто"),
        "buy": data.get("deal_buy", 0),
        "expenses": int(exp),
        "status": "active",
        "buy_ts": time.time(),
    })
    _save_deals(uid, deals)
    await state.clear()
    await msg.answer(
        "✅ Добавлено в сделки!",
        reply_markup=_deals_keyboard(deals),
    )
    await msg.answer(_deals_summary(deals), parse_mode="Markdown", reply_markup=_deals_keyboard(deals))


@dp.callback_query(F.data.startswith("deal_sell|"))
async def cb_deal_sell(cb: CallbackQuery, state: FSMContext):
    did = cb.data.split("|", 1)[1]
    await state.set_state(MyDeals.sell_price)
    await state.update_data(sell_id=did)
    await cb.message.answer("💵 За сколько продал? (число, ₽):")
    await cb.answer()


@dp.message(MyDeals.sell_price)
async def deal_sell_price(msg: Message, state: FSMContext):
    sell = re.sub(r"[^\d]", "", msg.text or "")
    if not sell:
        await msg.answer("Введи цену продажи числом.")
        return
    data = await state.get_data()
    did = data.get("sell_id")
    uid = msg.from_user.id
    deals = _load_deals(uid)
    for d in deals:
        if d.get("id") == did and d.get("status") == "active":
            d["sell"] = int(sell)
            d["sell_ts"] = time.time()
            d["status"] = "sold"
            d["profit"] = int(sell) - d.get("buy", 0) - d.get("expenses", 0)
            break
    _save_deals(uid, deals)
    await state.clear()
    _sold = next((x for x in deals if x.get("id") == did), None)
    if _sold:
        await msg.answer(
            f"✅ «{_sold['title']}» продано!\n"
            f"💰 Прибыль: *{_sold.get('profit',0):+,} ₽*".replace(",", " "),
            parse_mode="Markdown",
        )
    await msg.answer(_deals_summary(deals), parse_mode="Markdown", reply_markup=_deals_keyboard(deals))


@dp.callback_query(F.data.startswith("deal_del|"))
async def cb_deal_del(cb: CallbackQuery):
    did = cb.data.split("|", 1)[1]
    uid = cb.from_user.id
    deals = [d for d in _load_deals(uid) if d.get("id") != did]
    _save_deals(uid, deals)
    await cb.answer("Удалено")
    try:
        await cb.message.edit_reply_markup(reply_markup=_deals_keyboard(deals))
    except Exception:
        pass


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
        "Бот ищет объявления от частных лиц на *Авито, Дром, Авто.ру, ВКонтакте и Telegram*, "
        "сравнивает цены с рынком Авито и показывает выгодные первыми.\n\n"
        "📌 *Кнопки меню:*\n"
        "🔍 *Найти авто* — поиск по всем выбранным площадкам; ниже рынка идут первыми\n"
        "🌐 *Глобальный поиск* — поиск по барахолкам ВК и Telegram\n"
        "🆕 *Новые сегодня* — только свежие объявления за 24 часа, ниже рынка\n"
        "🎯 *Следить за маркой* — мониторинг конкретной марки/модели\n"
        "🔔 *Уведомления* — авто-мониторинг: бот сам пришлёт новое выгодное авто "
        "(проверяет каждые 2 минуты, свежие объявления долетают за ~10 минут)\n"
        "🚗 *Мой гараж* — сохранённые объявления (кнопка ⭐ Сохранить)\n"
        "💼 *Мои сделки* — учёт купленных авто: вложено / продано / *прибыль / ROI / срок продажи* (для перекупа)\n"
        "⚙️ *Настройки* — город, бюджет, категория, площадки\n"
        "💎 *Купить подписку* — выбрать тариф и перейти к оплате\n"
        "🛟 *Поддержка* — написать администратору или отправить чек\n"
        "♻️ *Сбросить историю* — показать все объявления заново\n\n"
        "📌 *Значки на карточке:*\n"
        "🚦 Светофор выгодности: 🟢 выгодно и чисто · 🟡 нейтрально · 🔴 дорого/риск\n"
        "🔻 рынок ~X₽ (−Y%) — цена ниже рыночной на Y%\n"
        "📊 ликвидность — сколько таких в продаже и средний срок продажи\n"
        "🔥 — срочная продажа (торг, срочно) · ⭐ — давно висит, продавец готов уступить\n\n"
        "📌 *Как считается «ниже рынка»:*\n"
        "Бот берёт медиану цен Авито по этой марке+модели+году и сравнивает. "
        "Битые / не на ходу — уходят в конец.\n\n"
        "📌 *Если ничего не нашлось:*\n"
        "— Нажми ♻️ *Сбросить историю* и ищи снова\n"
        "— Или расширь бюджет в ⚙️ *Настройках*",
        parse_mode="Markdown",
        reply_markup=kb_for(msg.from_user.id),
    )


async def _monitor_loop(uid: int):
    """Фоновая задача одного пользователя — делегирует в глобальный монитор."""
    # Просто держим флаг, глобальный монитор сам опрашивает всех активных
    while True:
        await asyncio.sleep(3600)


# ── "Охота за скидками": ежедневный FOMO-топ исчезнувших авто ───────────────
DISCOUNT_HUNT_ENABLED = os.getenv("DISCOUNT_HUNT_ENABLED", "1").lower() not in ("0", "false", "no")
DISCOUNT_HUNT_REGIONS = [
    r.strip() for r in os.getenv(
        "DISCOUNT_HUNT_REGIONS",
        "moscow,spb,ekaterinburg,krasnodar,krasnoyarsk,novosibirsk",
    ).split(",") if r.strip()
]
DISCOUNT_HUNT_SOURCES = [
    s.strip() for s in os.getenv("DISCOUNT_HUNT_SOURCES", "avito,drom,autoru").split(",") if s.strip()
]
DISCOUNT_HUNT_SCAN_SEC = _env_int("DISCOUNT_HUNT_SCAN_SEC", 3600, 600)
DISCOUNT_HUNT_SEND_HOUR_MSK = _env_int("DISCOUNT_HUNT_SEND_HOUR_MSK", 11, 0)
_DISCOUNT_HUNT_KV = "discount_hunt_items_v1"
_DISCOUNT_HUNT_LAST_SENT_KV = "discount_hunt_last_sent_date"


def _discount_hunt_load() -> dict:
    raw = _kv_get(_DISCOUNT_HUNT_KV)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _discount_hunt_save(data: dict) -> None:
    try:
        now = time.time()
        pruned = {
            k: v for k, v in data.items()
            if now - float(v.get("first_seen_ts", now)) <= 8 * 24 * 3600
            or (v.get("removed_ts") and not v.get("sent"))
        }
        if len(pruned) > 1200:
            ordered = sorted(pruned.items(), key=lambda kv: kv[1].get("first_seen_ts", 0), reverse=True)
            pruned = dict(ordered[:1200])
        _kv_set(_DISCOUNT_HUNT_KV, json.dumps(pruned, ensure_ascii=False))
    except Exception as e:
        print(f"  [охота] save error: {str(e)[:80]}")


def _discount_hunt_track(items: list[dict], region: str = "") -> None:
    if not items:
        return
    data = _discount_hunt_load()
    now = time.time()
    added = 0
    for it in items:
        url = _norm_url(it.get("url", ""))
        price = int(it.get("_price_int") or 0)
        market = int(it.get("_market_price") or 0)
        pct = float(it.get("_savings_pct") or 0)
        if not url or price <= 0 or market <= 0 or pct < 10:
            continue
        if is_dealer(it) or is_not_running(it) or it.get("_is_junk"):
            continue
        rec = data.get(url)
        if rec and rec.get("sent"):
            continue
        payload = {
            "url": url,
            "title": it.get("title", ""),
            "price": price,
            "market": market,
            "savings_pct": pct,
            "source": it.get("source", ""),
            "region": region or it.get("_monitor_region", ""),
            "photo": it.get("_photo_url") or it.get("photo_url") or "",
            "last_seen_ts": now,
        }
        if not rec:
            payload["first_seen_ts"] = now
            payload["removed_ts"] = 0
            payload["sent"] = False
            added += 1
        else:
            payload["first_seen_ts"] = rec.get("first_seen_ts", now)
            payload["removed_ts"] = rec.get("removed_ts", 0)
            payload["sent"] = rec.get("sent", False)
        data[url] = payload
    if added or items:
        _discount_hunt_save(data)
    if added:
        print(f"  [охота] добавлено кандидатов: {added}")


def _discount_hunt_scrape_region(region: str) -> list[dict]:
    raw: list[dict] = []
    avito_ref: list[dict] = []
    try:
        if "avito" in DISCOUNT_HUNT_SOURCES:
            avito_recent = _scrape_avito_background(region, pages=1, sort_by_date=True) or []
            raw.extend(avito_recent)
            avito_ref = avito_recent
    except Exception as e:
        print(f"  [охота] avito {region}: {str(e)[:60]}")
    try:
        if "drom" in DISCOUNT_HUNT_SOURCES:
            raw.extend(scrape_drom(region, pages=2, price_min=0, price_max=99_000_000) or [])
    except Exception as e:
        print(f"  [охота] drom {region}: {str(e)[:60]}")
    try:
        if "autoru" in DISCOUNT_HUNT_SOURCES:
            raw.extend(_scrape_autoru_background(region, pages=2) or [])
    except Exception as e:
        print(f"  [охота] autoru {region}: {str(e)[:60]}")
    raw = [it for it in raw if it.get("url") and not is_dealer(it) and not is_not_running(it)]
    if not raw:
        return []
    ranked = rank_by_market_price(
        raw,
        ref_items=avito_ref or raw,
        avito_only_median=bool(avito_ref),
    )
    return [
        it for it in ranked
        if (it.get("_savings_pct") or 0) >= 5
        and int(it.get("_market_price") or 0) > 0
        and int(it.get("_market_price") or 0) > int(it.get("_price_int") or 0)
    ][:40]


async def _discount_hunt_collect_once() -> None:
    if not DISCOUNT_HUNT_ENABLED:
        return
    loop = asyncio.get_running_loop()
    tasks = {
        region: loop.run_in_executor(None, _discount_hunt_scrape_region, region)
        for region in DISCOUNT_HUNT_REGIONS
    }
    if not tasks:
        return
    done, _ = await asyncio.wait(list(tasks.values()), timeout=120)
    total = 0
    for region, fut in tasks.items():
        if fut not in done:
            continue
        try:
            items = fut.result()
        except Exception:
            items = []
        total += len(items)
        _discount_hunt_track(items, region)
    print(f"  [охота] сбор завершён: {total} кандидатов")


async def _discount_hunt_detect_removed(limit: int = 80) -> list[dict]:
    data = _discount_hunt_load()
    if not data:
        return []
    now = time.time()
    candidates = [
        (url, rec) for url, rec in data.items()
        if not rec.get("sent")
        and not rec.get("removed_ts")
        and 20 * 60 <= now - float(rec.get("first_seen_ts", now)) <= 30 * 3600
    ]
    candidates.sort(key=lambda kv: -float(kv[1].get("savings_pct") or 0))
    loop = asyncio.get_running_loop()
    changed = False
    for url, rec in candidates[:limit]:
        try:
            source = rec.get("source", "")
            details = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_and_check, rec.get("url", url), source),
                timeout=12 if source == "drom" else 8,
            )
            if details is None:
                rec["removed_ts"] = now
                changed = True
        except Exception:
            pass
        await asyncio.sleep(0.02)
    if changed:
        _discount_hunt_save(data)
    removed = [
        rec for rec in data.values()
        if rec.get("removed_ts")
        and not rec.get("sent")
        and 0 < float(rec["removed_ts"]) - float(rec.get("first_seen_ts", rec["removed_ts"])) <= 24 * 3600
        and now - float(rec["removed_ts"]) <= 36 * 3600
    ]
    removed.sort(key=lambda r: (-(float(r.get("savings_pct") or 0)), float(r.get("removed_ts") or 0)))
    return removed[:10]


def _discount_hunt_format(top: list[dict]) -> str:
    lines = ["<b>Охота за скидками</b>", "", "ТОП-10 автомобилей, исчезнувших менее чем за 24 часа.", ""]
    for i, rec in enumerate(top, 1):
        title = html.escape(rec.get("title") or "Автомобиль")
        price = _fmt_n(rec.get("price", 0))
        market = _fmt_n(rec.get("market", 0))
        hours = max(1, round((float(rec.get("removed_ts", time.time())) - float(rec.get("first_seen_ts", time.time()))) / 3600))
        hour_word = "час" if hours % 10 == 1 and hours % 100 != 11 else ("часа" if 2 <= hours % 10 <= 4 and not 12 <= hours % 100 <= 14 else "часов")
        lines.extend([
            f"{i}. <b>{title}</b>",
            f"Цена: {price} ₽",
            f"Рынок: {market} ₽",
            f"Ушла за {hours} {hour_word}.",
            "",
        ])
    lines.append("Такие объявления бот присылает моментально.")
    return "\n".join(lines)


async def _discount_hunt_broadcast(top: list[dict]) -> None:
    if not top:
        return
    text = _discount_hunt_format(top)
    uids = _all_user_ids()
    sent = failed = 0
    for uid in uids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    data = _discount_hunt_load()
    for rec in top:
        url = rec.get("url")
        if url in data:
            data[url]["sent"] = True
    _discount_hunt_save(data)
    print(f"  [охота] рассылка: sent={sent}, failed={failed}, top={len(top)}")


async def _discount_hunt_loop():
    if not DISCOUNT_HUNT_ENABLED:
        print("  [охота] выключена DISCOUNT_HUNT_ENABLED=0")
        return
    print(f"  [охота] цикл запущен: регионы={','.join(DISCOUNT_HUNT_REGIONS)}")
    await asyncio.sleep(20)
    last_collect = 0.0
    while True:
        try:
            now = time.time()
            if now - last_collect >= DISCOUNT_HUNT_SCAN_SEC:
                last_collect = now
                await _discount_hunt_collect_once()
                await _discount_hunt_detect_removed(limit=50)
            dt = datetime.datetime.now(_MSK)
            today = dt.date().isoformat()
            last_sent = _kv_get(_DISCOUNT_HUNT_LAST_SENT_KV) or ""
            if dt.hour >= DISCOUNT_HUNT_SEND_HOUR_MSK and last_sent != today:
                top = await _discount_hunt_detect_removed(limit=100)
                if top:
                    if not _marketing_mass_sent_today(today):
                        await _discount_hunt_broadcast(top)
                        _marketing_mark_mass_sent(today)
                    _kv_set(_DISCOUNT_HUNT_LAST_SENT_KV, today)
                elif dt.hour >= DISCOUNT_HUNT_SEND_HOUR_MSK + 2:
                    _kv_set(_DISCOUNT_HUNT_LAST_SENT_KV, today)
        except Exception as e:
            print(f"  [охота] loop error: {str(e)[:100]}")
        await asyncio.sleep(600)


# ── Маркетинговые рассылки PerekupDrive: максимум 1 массовая в день ─────────
MARKETING_BROADCAST_ENABLED = os.getenv("MARKETING_BROADCAST_ENABLED", "1").lower() not in ("0", "false", "no")
MARKETING_MORNING_START_HOUR = _env_int("MARKETING_MORNING_START_HOUR", 8, 0)
MARKETING_MORNING_END_HOUR = _env_int("MARKETING_MORNING_END_HOUR", 9, 0)
_MARKETING_LAST_MASS_KV = "marketing_last_mass_date"


def _marketing_stats() -> dict:
    data = _discount_hunt_load()
    now = time.time()
    recent = [
        rec for rec in data.values()
        if now - float(rec.get("first_seen_ts", now)) <= 24 * 3600
        and int(rec.get("market") or 0) > int(rec.get("price") or 0) > 0
    ]
    available = [rec for rec in recent if not rec.get("removed_ts")]
    sold = [
        rec for rec in data.values()
        if rec.get("removed_ts")
        and now - float(rec.get("removed_ts", now)) <= 24 * 3600
    ]
    savings = [int(rec.get("market") or 0) - int(rec.get("price") or 0) for rec in recent]
    discounts = [float(rec.get("savings_pct") or 0) for rec in recent]
    lifetimes = [
        float(rec.get("removed_ts", 0)) - float(rec.get("first_seen_ts", 0))
        for rec in sold
        if rec.get("removed_ts") and rec.get("first_seen_ts")
    ]
    best = None
    if available:
        best = max(available, key=lambda rec: (float(rec.get("savings_pct") or 0), int(rec.get("market") or 0) - int(rec.get("price") or 0)))
    return {
        "cars_count": len(recent),
        "available_count": len(available),
        "sold_count": len(sold),
        "total_saving": sum(max(0, x) for x in savings),
        "max_saving": max([0] + savings),
        "max_discount": round(max([0.0] + discounts), 1),
        "avg_discount": round(sum(discounts) / len(discounts), 1) if discounts else 0,
        "avg_lifetime": (sum(lifetimes) / len(lifetimes)) if lifetimes else 0,
        "best": best,
    }


def _hours_label(seconds: float) -> str:
    hours = max(1, round(seconds / 3600))
    word = "час" if hours % 10 == 1 and hours % 100 != 11 else ("часа" if 2 <= hours % 10 <= 4 and not 12 <= hours % 100 <= 14 else "часов")
    return f"{hours} {word}"


def _marketing_keyboard(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data="start_search")
    ]])


def _marketing_mass_sent_today(today: str) -> bool:
    return (_kv_get(_MARKETING_LAST_MASS_KV) or "") == today


def _marketing_mark_mass_sent(today: str) -> None:
    _kv_set(_MARKETING_LAST_MASS_KV, today)


def _marketing_morning_message(stats: dict) -> tuple[str, str]:
    cars = stats["cars_count"]
    available = stats["available_count"]
    max_saving = _fmt_n(stats["max_saving"])
    max_discount = stats["max_discount"]
    total_saving = _fmt_n(stats["total_saving"])
    if cars >= 30:
        return (
            "🔥 Сегодня жирный день.\n\n"
            f"За 24 часа найдено {cars} авто ниже рынка.\n\n"
            f"💰 Общая потенциальная экономия: {total_saving} ₽\n"
            f"🚗 Сейчас ещё доступны: {available}\n\n"
            "Я бы посмотрел прямо сейчас 👇",
            "⚡ Смотреть сейчас",
        )
    if cars < 5:
        return (
            f"Сегодня рынок тихий, но мы всё равно нашли {cars} интересных авто.\n\n"
            f"💰 Лучшая экономия: {max_saving} ₽\n"
            f"🚗 Доступно сейчас: {available}\n\n"
            "Проверьте, вдруг среди них ваша сделка 👇",
            "🔍 Смотреть находки",
        )
    return (
        f"🔥 Пока вы спали, PerekupDrive нашёл {cars} авто ниже рынка.\n\n"
        f"💰 Максимальная экономия: {max_saving} ₽\n"
        f"📉 Самая большая скидка: {max_discount}%\n"
        f"🚗 Сейчас доступны: {available} объявлений\n\n"
        "Лучшие варианты долго не висят 👇",
        "🚗 Открыть подборку",
    )


def _marketing_fomo_message(stats: dict) -> tuple[str, str] | None:
    if stats["sold_count"] <= 0:
        return None
    avg = _hours_label(stats["avg_lifetime"] or 3 * 3600)
    return (
        f"⚠️ {stats['sold_count']} выгодных авто уже исчезли за последние сутки.\n\n"
        f"Среднее время жизни хорошего объявления: {avg}\n\n"
        f"Сейчас доступны ещё {stats['available_count']} вариантов ниже рынка.\n\n"
        "Кто быстрее открыл — тот забрал 👇",
        "🚨 Не упустить",
    )


def _marketing_weekly_message(stats: dict) -> tuple[str, str]:
    return (
        "📊 Итоги недели в PerekupDrive\n\n"
        f"🚗 Найдено авто ниже рынка: {stats['cars_count']}\n"
        f"💰 Общая потенциальная экономия: {_fmt_n(stats['total_saving'])} ₽\n"
        f"📉 Средняя скидка: {stats['avg_discount']}%\n"
        f"🔥 Самая большая экономия: {_fmt_n(stats['max_saving'])} ₽\n\n"
        "На следующей неделе хорошие варианты тоже уйдут быстро.",
        "🚗 Смотреть свежие авто",
    )


async def _marketing_send_all(text: str, button_text: str) -> None:
    sent = failed = 0
    kb = _marketing_keyboard(button_text)
    for uid in _all_user_ids():
        try:
            await bot.send_message(uid, text, reply_markup=kb)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    print(f"  [marketing] broadcast sent={sent}, failed={failed}")


async def _marketing_broadcast_loop():
    if not MARKETING_BROADCAST_ENABLED:
        print("  [marketing] выключен MARKETING_BROADCAST_ENABLED=0")
        return
    print("  [marketing] планировщик рассылок запущен")
    await asyncio.sleep(240)
    while True:
        try:
            dt = datetime.datetime.now(_MSK)
            today = dt.date().isoformat()
            in_morning = (
                (dt.hour == MARKETING_MORNING_START_HOUR and dt.minute >= 30)
                or (MARKETING_MORNING_START_HOUR < dt.hour <= MARKETING_MORNING_END_HOUR)
            )
            if in_morning and not _marketing_mass_sent_today(today):
                stats = _marketing_stats()
                if stats["cars_count"] <= 0:
                    await asyncio.sleep(300)
                    continue
                if dt.weekday() == 6 and stats["cars_count"] > 0:
                    text, button = _marketing_weekly_message(stats)
                elif dt.weekday() in (1, 3, 5) and stats["sold_count"] > 0:
                    fomo = _marketing_fomo_message(stats)
                    text, button = fomo if fomo else _marketing_morning_message(stats)
                else:
                    text, button = _marketing_morning_message(stats)
                await _marketing_send_all(text, button)
                _marketing_mark_mass_sent(today)
        except Exception as e:
            print(f"  [marketing] loop error: {str(e)[:100]}")
        await asyncio.sleep(300)


# ── Посты для Telegram-канала PerekupDrive ────────────────────────────────
CHANNEL_POSTS_ENABLED = os.getenv("CHANNEL_POSTS_ENABLED", "1").lower() not in ("0", "false", "no")
CHANNEL_ID = os.getenv("CHANNEL_ID", os.getenv("TELEGRAM_CHANNEL_ID", "@PerekupDrive")).strip()
_CHANNEL_LAST_POST_KV_PREFIX = "channel_post_last_"


def _bot_deep_link(utm: str) -> str:
    username = BOT_USERNAME or "Perekupil_bot"
    return f"https://t.me/{username}?start={utm}"


def _channel_kb(text: str, utm: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, url=_bot_deep_link(utm))
    ]])


def _car_id(rec: dict) -> str:
    return _norm_url(rec.get("url", "")) or hashlib.sha1(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _published_car_ids() -> set[str]:
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                cur.execute("SELECT car_id FROM published_channel_posts")
                return {r[0] for r in cur.fetchall() if r and r[0]}
    except Exception:
        pass
    return set()


def _mark_channel_published(post_type: str, car_ids: list[str]) -> None:
    try:
        db = _get_db()
        if db:
            with db.cursor() as cur:
                for cid in car_ids:
                    cur.execute(
                        "INSERT INTO published_channel_posts(post_type, car_id) VALUES(%s,%s)",
                        (post_type, cid),
                    )
    except Exception as e:
        print(f"  [channel] publish mark error: {str(e)[:80]}")


def _channel_records(days: int = 1, include_removed: bool = True, unpublished_only: bool = True) -> list[dict]:
    data = _discount_hunt_load()
    now = time.time()
    published = _published_car_ids() if unpublished_only else set()
    out = []
    for rec in data.values():
        if unpublished_only and _car_id(rec) in published:
            continue
        first_seen = float(rec.get("first_seen_ts", now))
        removed_ts = float(rec.get("removed_ts") or 0)
        if now - first_seen > days * 24 * 3600 and (not removed_ts or now - removed_ts > days * 24 * 3600):
            continue
        if not include_removed and removed_ts:
            continue
        price = int(rec.get("price") or 0)
        market = int(rec.get("market") or 0)
        if not rec.get("title") or not (market > price > 0):
            continue
        out.append(rec)
    return out


def _car_title_parts(title: str) -> tuple[str, str, str]:
    t = re.sub(r"\s+", " ", title or "").strip(" ,")
    year_m = re.search(r"\b(19[5-9]\d|20[012]\d)\b", t)
    year = year_m.group(1) if year_m else ""
    clean = re.sub(r"\b(19[5-9]\d|20[012]\d)\b", "", t).strip(" ,-")
    words = clean.split()
    brand = words[0] if words else "Авто"
    model = " ".join(words[1:3]) if len(words) > 1 else ""
    return brand, model, year


def _channel_car_line(rec: dict) -> str:
    brand, model, year = _car_title_parts(rec.get("title", ""))
    label = " ".join(x for x in (brand, model) if x).strip()
    return f"{html.escape(label)}{', ' + year if year else ''}"


def _saving(rec: dict) -> int:
    return max(0, int(rec.get("market") or 0) - int(rec.get("price") or 0))


def _discount(rec: dict) -> int:
    return int(round(float(rec.get("savings_pct") or 0)))


def _ts_label(ts: float) -> str:
    if not ts:
        return "неизвестно"
    return datetime.datetime.fromtimestamp(ts, _MSK).strftime("%H:%M")


def _lifetime_label(rec: dict) -> str:
    start = float(rec.get("first_seen_ts") or time.time())
    end = float(rec.get("removed_ts") or time.time())
    return _hours_label(max(60, end - start))


def generate_channel_post(post_type: str) -> dict | None:
    post_type = (post_type or "").strip().lower()
    day = _channel_records(days=1, include_removed=True, unpublished_only=True)
    live = [r for r in day if not r.get("removed_ts")]
    removed = [r for r in day if r.get("removed_ts")]

    def car_payload(rec: dict, text: str, button: str, poll: bool = False) -> dict:
        return {
            "text": text + "\n\n<a href=\"https://t.me/Perekupil_bot\">@Perekupil_bot</a>",
            "keyboard": _channel_kb(button, f"channel_{post_type}"),
            "car_ids": [_car_id(rec)],
            "poll": poll,
        }

    if post_type in ("deal_day", "deal", "сделка"):
        if not live:
            return None
        rec = max(live, key=lambda r: (_saving(r), _discount(r)))
        text = (
            "🔥 <b>Сделка дня</b>\n\n"
            f"🚗 {_channel_car_line(rec)}\n\n"
            f"💵 Цена: {_fmt_n(rec['price'])} ₽\n"
            f"📊 Рынок: {_fmt_n(rec['market'])} ₽\n"
            f"💰 Экономия: {_fmt_n(_saving(rec))} ₽\n"
            f"📉 Ниже рынка: {_discount(rec)}%\n"
            f"⏱ Висит: {_hours_label(time.time() - float(rec.get('first_seen_ts', time.time())))}\n\n"
            "Такие объявления PerekupDrive находит каждый день."
        )
        return car_payload(rec, text, "🚗 Открыть в боте")

    if post_type in ("already_bought", "bought", "купили"):
        if not removed:
            return None
        rec = max(removed, key=_saving)
        text = (
            "❌ <b>Уже купили</b>\n\n"
            f"🚗 {_channel_car_line(rec)}\n\n"
            f"💵 Цена была: {_fmt_n(rec['price'])} ₽\n"
            f"📊 Рынок: {_fmt_n(rec['market'])} ₽\n"
            f"💰 Экономия: {_fmt_n(_saving(rec))} ₽\n"
            f"⏱ Объявление прожило: {_lifetime_label(rec)}\n\n"
            "Кто получил уведомление — тот успел."
        )
        return car_payload(rec, text, "🔥 Смотреть свежие варианты")

    if post_type in ("coffee", "fast_gone", "ушла"):
        if not removed:
            return None
        rec = min(removed, key=lambda r: float(r.get("removed_ts") or time.time()) - float(r.get("first_seen_ts") or time.time()))
        text = (
            "⚡ <b>Ушла, пока ты пил кофе</b>\n\n"
            f"🚗 {_channel_car_line(rec)}\n\n"
            f"Опубликована: {_ts_label(float(rec.get('first_seen_ts') or 0))}\n"
            f"Исчезла: {_ts_label(float(rec.get('removed_ts') or 0))}\n\n"
            f"⏱ Всего: {_lifetime_label(rec)}\n"
            f"💰 Экономия была: {_fmt_n(_saving(rec))} ₽\n\n"
            "Хорошие варианты долго не ждут."
        )
        return car_payload(rec, text, "🚨 Включить уведомления")

    if post_type in ("stats_day", "stats", "цифры"):
        stats = _marketing_stats()
        if stats["cars_count"] <= 0:
            return None
        regions: dict[str, int] = {}
        for rec in day:
            reg = rec.get("region") or "неизвестно"
            regions[reg] = regions.get(reg, 0) + 1
        top_region_key = max(regions, key=regions.get) if regions else ""
        top_region = REGIONS.get(top_region_key, top_region_key) or "нет данных"
        text = (
            "📊 <b>Цифры дня в PerekupDrive</b>\n\n"
            f"🚗 Найдено авто ниже рынка: {stats['cars_count']}\n"
            f"💰 Общая потенциальная экономия: {_fmt_n(stats['total_saving'])} ₽\n"
            f"📉 Средняя скидка: {int(round(stats['avg_discount']))}%\n"
            f"🔥 Максимальная экономия: {_fmt_n(stats['max_saving'])} ₽\n"
            f"📍 Самый активный регион: {html.escape(top_region)}"
        )
        return {"text": text + "\n\n<a href=\"https://t.me/Perekupil_bot\">@Perekupil_bot</a>", "keyboard": _channel_kb("🚗 Смотреть подборку", "channel_stats_day"), "car_ids": []}

    if post_type in ("top5", "top_5", "топ5"):
        top = sorted(live, key=lambda r: (_saving(r), _discount(r)), reverse=True)[:5]
        if len(top) < 1:
            return None
        lines = ["🏆 <b>ТОП-5 авто ниже рынка сегодня</b>", ""]
        for i, rec in enumerate(top, 1):
            lines.extend([
                f"{i}. {_channel_car_line(rec)}",
                f"💰 Экономия: {_fmt_n(_saving(rec))} ₽",
                f"📉 Ниже рынка: {_discount(rec)}%",
                "",
            ])
        lines.append('<a href="https://t.me/Perekupil_bot">@Perekupil_bot</a>')
        return {"text": "\n".join(lines), "keyboard": _channel_kb("🔍 Открыть все авто", "channel_top5"), "car_ids": [_car_id(r) for r in top]}

    if post_type in ("worth", "poll", "стоило"):
        if not live:
            return None
        rec = max(live, key=lambda r: (_discount(r), _saving(r)))
        text = (
            "🤔 <b>Стоило брать?</b>\n\n"
            f"🚗 {_channel_car_line(rec)}\n\n"
            f"💵 Цена: {_fmt_n(rec['price'])} ₽\n"
            f"📊 Рынок: {_fmt_n(rec['market'])} ₽\n"
            f"💰 Экономия: {_fmt_n(_saving(rec))} ₽\n"
            f"📉 Ниже рынка: {_discount(rec)}%\n\n"
            "Как думаете?"
        )
        return car_payload(rec, text, "🚗 Смотреть похожие", poll=True)

    if post_type in ("week_biggest", "weekly_biggest", "неделя"):
        week = _channel_records(days=7, include_removed=True, unpublished_only=True)
        if not week:
            return None
        rec = max(week, key=_saving)
        text = (
            "💸 <b>Самая дорогая скидка недели</b>\n\n"
            f"🚗 {_channel_car_line(rec)}\n\n"
            f"💵 Цена: {_fmt_n(rec['price'])} ₽\n"
            f"📊 Рынок: {_fmt_n(rec['market'])} ₽\n"
            f"💰 Экономия: {_fmt_n(_saving(rec))} ₽\n"
            f"📉 Ниже рынка: {_discount(rec)}%\n\n"
            "Такую разницу вручную найти почти нереально."
        )
        return car_payload(rec, text, "🔥 Открыть PerekupDrive")

    if post_type in ("region_day", "region", "регион"):
        if not day:
            return None
        buckets: dict[str, list[dict]] = {}
        for rec in day:
            buckets.setdefault(rec.get("region") or "unknown", []).append(rec)
        reg, rows = max(buckets.items(), key=lambda kv: len(kv[1]))
        best = max(rows, key=_saving)
        region_name = REGIONS.get(reg, reg)
        total = sum(_saving(r) for r in rows)
        text = (
            f"📍 <b>Регион дня: {html.escape(region_name)}</b>\n\n"
            "За 24 часа найдено:\n"
            f"🚗 {len(rows)} авто ниже рынка\n\n"
            f"💰 Общая экономия: {_fmt_n(total)} ₽\n"
            f"🔥 Лучшая находка: {_channel_car_line(best)} — {_fmt_n(_saving(best))} ₽ экономии"
        )
        return {"text": text + "\n\n<a href=\"https://t.me/Perekupil_bot\">@Perekupil_bot</a>", "keyboard": _channel_kb("Смотреть авто в регионе", f"channel_region_{reg}"), "car_ids": [_car_id(best)]}

    return None


async def _send_channel_post(post_type: str) -> bool:
    if not CHANNEL_ID:
        return False
    post = generate_channel_post(post_type)
    if not post:
        return False
    await bot.send_message(
        CHANNEL_ID,
        post["text"],
        parse_mode="HTML",
        reply_markup=post.get("keyboard"),
        disable_web_page_preview=True,
    )
    if post.get("poll"):
        await bot.send_poll(
            CHANNEL_ID,
            "Стоило брать?",
            ["✅ Да, забрал бы", "🤔 Сначала проверил бы", "❌ Нет, подозрительно"],
            is_anonymous=False,
        )
    _mark_channel_published(post_type, post.get("car_ids", []))
    return True


@dp.message(Command("channel_post"))
async def cmd_channel_post(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Типы: deal_day, already_bought, coffee, stats_day, top5, worth, week_biggest, region_day")
        return
    ok = await _send_channel_post(parts[1].strip())
    await msg.answer("✅ Пост отправлен" if ok else "⚠️ Нет данных для такого поста")


async def _channel_posts_loop():
    if not CHANNEL_POSTS_ENABLED:
        print("  [channel] выключен CHANNEL_POSTS_ENABLED=0")
        return
    schedule = {
        (9, 0): "deal_day",
        (12, 0): "already_bought",
        (16, 0): "worth",
        (20, 0): "top5",
        (21, 0): "stats_day",
    }
    print(f"  [channel] планировщик постов запущен: {CHANNEL_ID}")
    await asyncio.sleep(300)
    while True:
        try:
            dt = datetime.datetime.now(_MSK)
            post_type = schedule.get((dt.hour, 0)) if dt.minute < 10 else None
            if dt.weekday() == 6 and dt.hour == 19 and dt.minute < 10:
                post_type = "week_biggest"
            if dt.hour == 12 and dt.minute < 10:
                # Чередуем два FOMO-формата в обед.
                post_type = "coffee" if dt.day % 2 else "already_bought"
            if post_type:
                key = f"{_CHANNEL_LAST_POST_KV_PREFIX}{post_type}_{dt.date().isoformat()}"
                if not _kv_get(key):
                    ok = await _send_channel_post(post_type)
                    if ok:
                        _kv_set(key, "1")
        except Exception as e:
            print(f"  [channel] loop error: {str(e)[:100]}")
        await asyncio.sleep(60)


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
        f"⚡ 🆕 ТОЛЬКО ЧТО — ты видишь одним из первых\n"
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
        [
            InlineKeyboardButton(text="🔍 Пробить машину (штрафы, аресты)", callback_data=f"check|{sid}|{uid}"),
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
                    # Всегда скрейпим Авито для рыночной цены — даже если пользователь его не выбрал
                    needed[reg].add("avito")

            # Скрейпим только нужные (регион, источник) параллельно
            _src_scrapers = {
                "avito":  lambda r: _scrape_avito_background(r, pages=1, sort_by_date=True),
                "drom":   lambda r: scrape_drom(r, pages=3, price_min=0, price_max=99_000_000),
                "autoru": lambda r: _scrape_autoru_background(r, pages=2),
                "youla":  lambda r: scrape_youla(r, pages=3, price_min=0, price_max=99_000_000),
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
                    avito_ref = []  # Авито данные только для рыночной цены (если пользователь Авито не выбрал)
                    for reg in all_regions:
                        for src in user_srcs | {"avito"}:  # всегда включаем авито для рыночной цены
                            if src in ("vk", "tg") and not do_vk_tg:
                                continue
                            key_rs = f"{reg}:{src}"
                            items_rs = _region_src_cache.get(key_rs, [])
                            # Помечаем регион для уведомлений
                            for it in items_rs:
                                it["_monitor_region"] = reg
                            if src == "avito" and src not in user_srcs:
                                # Авито не выбрано пользователем — только для рыночной цены
                                avito_ref.extend(items_rs)
                            else:
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
                        and not is_not_running(it)
                        and in_price_range(it, pmin, pmax)
                    ]
                    if not new_items:
                        seen.update(it["url"] for it in raw if it.get("url"))
                        save_seen(uid, seen)
                        continue

                    cached = _search_cache.get(uid) or _load_cache(uid)
                    avito_market_ref = [
                        it for it in (avito_ref + raw + cached)
                        if (it.get("source", "") or "").lower() == "avito"
                    ]
                    pool = rank_by_market_price(
                        new_items,
                        ref_items=avito_market_ref,
                        avito_only_median=True,
                    )
                    new_urls = {x["url"] for x in new_items}

                    new_below = sorted(
                        [it for it in pool
                         if it.get("url") in new_urls
                         and _is_strong_below_market(it)
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
                                kb_tb = InlineKeyboardMarkup(inline_keyboard=[
                                    [
                                        InlineKeyboardButton(text="🔗 Открыть", url=url_tb),
                                        InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid_tb}|{uid}"),
                                    ],
                                    [
                                        InlineKeyboardButton(text="🔍 Пробить машину (штрафы, аресты)", callback_data=f"check|{sid_tb}|{uid}"),
                                    ],
                                ])
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
        f"🔔 *Уведомления · ⚡ Ранний доступ*\n\n"
        f"Бот каждые ~2 мин проверяет Авито (сортировка «сначала свежие») и "
        f"присылает новые авто ниже рынка первым — по сути, ты видишь объявления "
        f"раньше тех, кто листает вручную.\n\n"
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
    # Берём имя бота из Telegram (надёжно), не из возможно-устаревшей переменной
    try:
        me = await bot.get_me()
        _un = me.username or BOT_USERNAME
    except Exception:
        _un = BOT_USERNAME
    ref_link = f"https://t.me/{_un}?start=ref_{uid}"
    share_text = "Нашёл бота который ищет авто ниже рынка на Авито, Дроме, Авто.ру, ВК и Telegram — попробуй!"
    _bonus_line = f"🎁 Бонусных дней: <b>{bonus_days}</b>\n" if bonus_days else ""
    _milestone_line = f"🏆 До +30 дней осталось пригласить: <b>{10 - invited_count}</b>\n" if 0 < invited_count < 10 else ""
    # HTML: подчёркивания в ссылке остаются буквальными (Markdown их «съедал» → курсив)
    await msg.answer(
        f"📲 <b>Пригласи друга в PerekupDrive</b>\n\n"
        f"Сейчас идёт тестовый период — бот полностью бесплатен для всех.\n"
        f"За каждого друга — <b>+3 дня доступа</b>, за 10 друзей — <b>+30 дней</b>.\n\n"
        f"👥 Приглашено: <b>{invited_count}</b> друзей\n"
        f"{_bonus_line}{_milestone_line}\n"
        f"🔗 <b>Твоя ссылка</b> (нажми, чтобы скопировать):\n"
        f"<code>{ref_link}</code>\n\n"
        f"Когда друг перейдёт по ссылке — пришлю тебе уведомление 🔔",
        parse_mode="HTML",
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
                lambda r=oldest_region: _scrape_avito_background(
                    r,
                    pages=1,
                    sort_by_date=False,
                )
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
    Если настроен платный мобильный прокси — бесплатные не нужны (Авито ходит
    через мобильный, а у Auto.ru есть свой РФ-пул AUTORU_PROXIES), поэтому
    тяжёлый прогрев (80 запросов к Авито) пропускаем, чтобы не мешать поиску."""
    if AVITO_PROXIES:
        print("  [прокси-прогрев] платный прокси активен — прогрев бесплатных отключён")
        return
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, _pre_warm_free_proxies_sync)
        except Exception as e:
            print(f"  [прокси-прогрев] ошибка: {e}")
        await asyncio.sleep(900)


async def main():
    global BOT_USERNAME, _registry_dirty
    logging.basicConfig(level=logging.WARNING)
    _load_avito_cache()
    _load_price_history()
    _analytics_restore()  # восстановить статистику из PG (контейнер эфемерный)
    _restore_referrals()  # восстановить рефералов из PG
    # Реестр пользователей собираем из ВСЕХ доступных источников (чтобы не потерять
    # уже существующих): PG → папки users/ → analytics → Telegram-бэкап.
    try:
        for _k, _v in (_db_users() or {}).items():  # память пуста → читает PG
            _USER_REGISTRY.setdefault(_k, _v)
    except Exception:
        pass
    # Папки users/<uid> — каждый, кто хоть раз пользовался ботом
    try:
        if USERS_DIR.exists():
            for _p in USERS_DIR.iterdir():
                if not (_p.is_dir() and _p.name.isdigit()):
                    continue
                if _p.name in _USER_REGISTRY:
                    continue
                try:
                    _st = (_p / "settings.json").stat()
                    _fs = int(_st.st_mtime)
                except Exception:
                    _fs = int(time.time())
                _mon = False
                try:
                    _s = json.loads((_p / "settings.json").read_text(encoding="utf-8"))
                    _mon = bool(_s.get("monitor_enabled"))
                except Exception:
                    pass
                _USER_REGISTRY[_p.name] = {"first_seen": _fs, "last_seen": _fs,
                                           "searches": 0, "monitoring": _mon}
    except Exception:
        pass
    # Файловая analytics (если есть)
    try:
        for _k, _v in (analytics.load_users() or {}).items():
            cur = _USER_REGISTRY.get(_k)
            if not cur:
                _USER_REGISTRY[_k] = _v
            else:
                cur["searches"] = max(cur.get("searches", 0), _v.get("searches", 0))
                cur["username"] = cur.get("username") or _v.get("username")
    except Exception:
        pass
    try:
        await _tg_backup_restore()
    except Exception:
        pass
    _registry_dirty = True  # сохранить собранный реестр при первом бэкапе
    print(f"  [реестр] загружено пользователей: {len(_USER_REGISTRY)}")
    # Username бота берём ВСЕГДА из Telegram (get_me) — это единственный
    # достоверный источник. Переменная окружения может содержать опечатку
    # (например 'Perekupilbot' вместо 'Perekupil_bot') и ломать реф-ссылки.
    try:
        me = await bot.get_me()
        if me.username:
            BOT_USERNAME = me.username
        print(f"  [бот] username: @{BOT_USERNAME}")
    except Exception:
        if not BOT_USERNAME:
            BOT_USERNAME = "PerekupDriveBot"
    # Глобальный middleware проверки подписки — блокирует все апдейты
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    print("✅ Авто-брокер бот запущен!")
    print(f"  [DEPLOY] commit={DEPLOY_REVISION} service={DEPLOY_SERVICE} env={DEPLOY_ENVIRONMENT}")
    print("  [ВЕРСИЯ] 2026-07-02-v19 :: source selection + deploy revision")

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
            # (см. ниже; сначала — самопроверка РФ-прокси для Auto.ru)
            pass
        except Exception as ep:
            print(f"  [прокси] ❌ {ep}")

    # Самопроверка РФ-прокси для Auto.ru — сразу видно в логах, пробивают ли
    # они капчу Яндекса (HTTP 200 + большой размер = ок; ~13КБ = капча).
    if AUTORU_PROXIES:
        try:
            from curl_cffi import requests as _cffi_t
            for _p in AUTORU_PROXIES:
                _pu = ("socks5h://" + _p[len("socks5://"):]) if _p.startswith("socks5://") else _p
                _phost = _pu.split("@")[-1]
                try:
                    _rt = _cffi_t.get(
                        "https://auto.ru/sankt-peterburg/cars/used/?seller_group=PRIVATE",
                        impersonate="chrome124", timeout=9,
                        headers={"Accept-Language": "ru-RU,ru;q=0.9",
                                 "Referer": "https://auto.ru/"},
                        proxies={"http": _pu, "https": _pu},
                    )
                    _cap = _autoru_is_captcha(_rt.text)
                    _ok = _rt.status_code == 200 and not _cap and len(_rt.text) > 50_000
                    _verdict = "✅ РАБОТАЕТ" if _ok else ("🧱 капча" if _cap else "⚠️ мало данных")
                    print(f"  [Auto.ru тест] {_phost}: HTTP {_rt.status_code}, {len(_rt.text):,}б → {_verdict}")
                except Exception as _et:
                    print(f"  [Auto.ru тест] {_phost}: ❌ {str(_et)[:70]}")
        except Exception as _e:
            print(f"  [Auto.ru тест] curl_cffi недоступен: {str(_e)[:60]}")

    if AVITO_PROXY_HOST:
        try:
            import requests as _rq
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
    loop.create_task(_discount_hunt_loop())
    print("  [охота] сборщик скидок запущен")
    loop.create_task(_marketing_broadcast_loop())
    print("  [marketing] единый планировщик массовых рассылок запущен")
    loop.create_task(_access_expiry_notice_loop())
    print("  [access] expiry notice scheduler started")
    loop.create_task(_channel_posts_loop())
    print("  [channel] генератор постов канала запущен")
    loop.create_task(_admin_report_scheduler())
    print("  [admin] планировщик отчётов запущен (09:00 МСК)")
    loop.create_task(_analytics_persist_loop())
    print("  [analytics] автосохранение статистики в PG запущено (раз в 3 мин)")
    loop.create_task(_tg_backup_loop())
    print("  [реестр] Telegram-бэкап статистики запущен (раз в 15 мин)")
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
        BotCommand(command="buy",       description="💎 Купить подписку"),
        BotCommand(command="support",   description="🛟 Поддержка"),
        BotCommand(command="invite",    description="🤝 Пригласить друга"),
        BotCommand(command="settings",  description="⚙️ Настройки"),
        BotCommand(command="help",      description="❓ Помощь"),
    ]
    admin_commands = public_commands + [
        BotCommand(command="stats",     description="📊 Статистика"),
        BotCommand(command="dashboard", description="📈 Дашборд аналитики"),
        BotCommand(command="access_status", description="⏳ Сроки доступа"),
        BotCommand(command="set_access", description="✅ Выдать дни доступа"),
        BotCommand(command="fomo_broadcast", description="🚨 FOMO-рассылка"),
        BotCommand(command="deploy",    description="🚂 Версия Railway"),
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
