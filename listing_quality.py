"""Качество выдачи: фильтрация, цена, фото, анализ и шаблоны карточек."""
import re
from typing import Any, Optional

# ──────────────────────────────────────────────────────────────────────
# Фильтрация: классификация объявлений
# ──────────────────────────────────────────────────────────────────────
_CAR_BRANDS = {
    "lada", "ваз", "toyota", "nissan", "honda", "mitsubishi", "mazda",
    "ford", "chevrolet", "renault", "hyundai", "kia", "volkswagen", "vw",
    "skoda", "audi", "bmw", "mercedes", "opel", "peugeot", "citroen",
    "subaru", "suzuki", "lexus", "infiniti", "volvo", "chery", "geely",
    "haval", "great wall", "уаз", "газ", "daewoo", "porsche", "jaguar",
    "cadillac", "chrysler", "dodge", "jeep",
}

_COMPOSITE_BRANDS = ("land rover", "alfa romeo", "great wall")

_CAR_BODY_TYPES = {
    "седан", "хэтчбек", "хэтч", "универсал", "кроссовер", "внедорожник",
    "минивэн", "купе", "кабриолет", "пикап", "лифтбек", "родстер", "микроавтобус",
}

_CAR_FEATURES = {
    "продам авто", "продам машину", "автомобиль", "легковой", "легковушка",
    "подержанный", "б\у", "б/у", "в отличном состоянии", "в хорошем состоянии",
    "полностью на ходу", "на ходу", "состояние хорошее", "состояние отличное",
    "владелец", "владельцев", "хозяин", "хозяев", "один хозяин", "два хозяина",
}

_TECH_FEATURES = {
    "птс", "стс", "vin", "вин", "кузов", "двигатель", "коробка передач", "кпп",
    "акпп", "мкпп", "вариатор", "робот", "турбо", "инжектор", "карбюратор",
    "передний привод", "задний привод", "полный привод", "4wd", "awd", "fwd",
    "левый руль", "правый руль", "гур", "абс", "кондиционер", "климат",
    "подогрев", "кожа", "салон", "шкода", "приора", "гранта", "калина", "веста",
}

_SALE_PHRASES = {
    "продам", "продаю", "продажа", "срочно продам", "продам срочно",
    "готов к продаже", "в продаже", "продается", "продаётся", "пpoдaм",
}

# Контекстные запчастные шаблоны, когда запчасть — основной товар.
_PARTS_FOR_CAR_PATTERNS = (
    "шины на", "зимние шины на", "резина на", "диски на", "колеса на",
    "запчасти на", "запчасти для", "двигатель на", "коробка на", "мотор на",
    "на запчасти", "разбор", "разборка", "запчастями",
)

# Отрицательные категории: штраф к score. Абсолютная блокировка отдельно.
_NEGATIVE_CATEGORIES = {
    "самокат": -6,
    "электросамокат": -6,
    "велосипед": -6,
    "мопед": -6,
    "скутер": -6,
    "мотоцикл": -6,
    "мото": -6,
    "квадроцикл": -6,
    "квадрик": -6,
    "лодка": -6,
    "катер": -6,
    "трактор": -6,
    "спецтехника": -6,
    "прицеп": -6,
    "банк": -6,
    "банкомат": -6,
    "обмен валюты": -6,
    "валюта": -3,
    "новость": -6,
    "вакансия": -4,
    "требуется": -4,
    "ищу работу": -4,
    "услуга": -4,
    "услуги": -4,
    "магазин": -4,
    "недвижимость": -6,
    "квартира": -6,
    "комната": -6,
    "электроника": -6,
    "iphone": -6,
    "samsung": -6,
    "мебель": -6,
    "диван": -6,
    "стол": -3,
    "холодильник": -6,
    "дом": -3,
}

# Абсолютно блокируемые фразы (source=vk/tg). Для коротких слов используем границы слов.
_NEGATIVE_PHRASES = {
    "банкомат", "банк", "обмен валют", "обмен валюты", "курс валют", "курс доллара",
    "курс евро", "вакансия", "требуется", "ищем сотрудника", "ищу работу",
    "на запчасти", "разбор", "разборка",
    "самокат", "электросамокат", "велосипед", "мотоцикл", "мопед", "скутер",
    "квадроцикл", "лодка", "катер", "трактор", "спецтехника", "прицеп",
    "недвижимость", "квартира", "комната",
    "iphone", "samsung", "холодильник", "мебель",
    "диван", "стол", "шкаф", "кровать", "телевизор", "ноутбук", "планшет",
    "кондиционер бытовой", "стиральная машина", "микроволновка", "посудомоечная",
}

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
_MILEAGE_RE = re.compile(r"\b(\d{1,3}(?:\s?\d{3})*)\s*(?:км|тыс\.?\s*км|тыс)\b", re.IGNORECASE)
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)


def _normalize_text(item: dict) -> str:
    parts = [
        str(item.get("title", "")),
        str(item.get("description", "")),
        str(item.get("category", "")),
    ]
    return " ".join(parts).lower()


def _word_boundaries(text: str, word: str) -> bool:
    """Проверяет, что word встречается в text как отдельное слово.

    Использует стандартный re, без неподдерживаемого \p{P}.
    """
    escaped = re.escape(word)
    pattern = rf"(?<![\wа-яё]){escaped}(?![\wа-яё])"
    return bool(re.search(pattern, text, re.IGNORECASE))


def _has_negative_phrase(text: str, phrase: str) -> bool:
    """Многословные фразы ищем как подстроки; одиночные слова — с границами."""
    if " " in phrase:
        return phrase in text
    return _word_boundaries(text, phrase)


def _has_parts_for_car_pattern(text: str) -> bool:
    return any(p in text for p in _PARTS_FOR_CAR_PATTERNS)


def classify_car_listing(item: dict) -> dict:
    """Классифицирует публикацию как автомобильное объявление.

    Returns: {"accepted": bool, "score": int, "reasons": list[str]}
    """
    text = _normalize_text(item)
    price = item.get("price") or item.get("_price_int") or 0
    score = 0
    reasons: list[str] = []

    # Положительные признаки: марка (составные сначала).
    found_brand = None
    for brand in _COMPOSITE_BRANDS:
        if _word_boundaries(text, brand):
            found_brand = brand
            break
    if not found_brand:
        for brand in _CAR_BRANDS:
            if _word_boundaries(text, brand):
                found_brand = brand
                break
    if found_brand:
        score += 3
        reasons.append("brand")

    sale_phrase = next((p for p in _SALE_PHRASES if p in text), None)
    if sale_phrase:
        score += 3
        reasons.append("sale_phrase")
    elif "продам" in text or "продаю" in text or "продажа" in text:
        score += 2
        reasons.append("sale_weak")

    if _YEAR_RE.search(text):
        score += 2
        reasons.append("year")

    if _MILEAGE_RE.search(text) or "пробег" in text:
        score += 2
        reasons.append("mileage")

    if any(k in text for k in ("птс", "стс", "vin", "вин")) or _VIN_RE.search(text):
        score += 2
        reasons.append("docs")

    if any(f in text for f in _CAR_FEATURES):
        score += 1
        reasons.append("car_features")

    if any(f in text for f in _TECH_FEATURES):
        score += 1
        reasons.append("tech_features")

    if any(b in text for b in _CAR_BODY_TYPES):
        score += 1
        reasons.append("body_type")

    try:
        p = int(price) if isinstance(price, (int, float)) else parse_price(str(price))
    except Exception:
        p = None
    if p and 10_000 <= p <= 99_000_000:
        score += 1
        reasons.append("price")

    # Отрицательные категории (штраф к score).
    for phrase, penalty in _NEGATIVE_CATEGORIES.items():
        if _has_negative_phrase(text, phrase):
            score += penalty
            reasons.append(phrase.replace(" ", "_"))

    # Контекст: запчасть как основной товар — отклоняем.
    if _has_parts_for_car_pattern(text):
        score -= 4
        reasons.append("parts_for_car")

    # Основной товар — не авто, если нет сильных признаков.
    if not found_brand and not _YEAR_RE.search(text) and not any(p in text for p in _SALE_PHRASES):
        score -= 3
        reasons.append("no_strong_car_signs")

    accepted = score >= 4 and ("brand" in reasons or "year" in reasons or "sale_phrase" in reasons)

    # Абсолютно блокируемые категории всегда отклоняем, даже если score высокий.
    for phrase in _NEGATIVE_PHRASES:
        if _has_negative_phrase(text, phrase):
            accepted = False
            reasons.append(f"blocked:{phrase.replace(' ', '_')}")
            break

    return {"accepted": bool(accepted), "score": score, "reasons": reasons}


# ──────────────────────────────────────────────────────────────────────
# Парсинг цены
# ──────────────────────────────────────────────────────────────────────
_PRICE_RE = re.compile(
    r"""
    (?<![\d\-/])
    (\d{1,3}(?:\s\d{3})+|\d{4,9})
    \s*
    (?:
        [₽рpр\u0440]
        |руб(?:\.?|лей|ля)?
        |тыс\.?\s*(?:[₽р]|руб)?
        |млн\.?\s*(?:[₽р]|руб)?
    )
    (?![\d])
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TYS_RE = re.compile(r"(\d{1,3}(?:[\.,]\d{1,2})?)\s*тыс\.?", re.IGNORECASE)
_MLN_RE = re.compile(r"(\d{1,3}(?:[\.,]\d{1,2})?)\s*млн\.?", re.IGNORECASE)


def parse_price(s: str) -> int | None:
    """Извлекает цену из строки, игнорируя случайные числа без контекста."""
    if not s:
        return None
    text = str(s).replace("\xa0", " ").strip().lower()
    if text in ("договорная", "договор", "обмен", "бесплатно", "цена по запросу", "по запросу"):
        return None

    # Прямые сопоставления: 100 тыс, 1.2 млн
    m = _TYS_RE.search(text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1_000)
    m = _MLN_RE.search(text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 1_000_000)

    # Число рядом с валютой/ценой/продажей
    matches = _PRICE_RE.findall(text)
    values = []
    for raw in matches:
        digits = int(re.sub(r"\D", "", raw))
        if 10_000 <= digits <= 99_000_000:
            values.append(digits)

    # Если прямых нет — ищем число с контекстом "цена" или "продам"
    if not values:
        for m in re.finditer(r"(?:цена|продам|продажа)\s*[\:\-]?\s*(\d{1,3}(?:\s?\d{3})+|\d{4,9})", text, re.IGNORECASE):
            digits = int(re.sub(r"\D", "", m.group(1)))
            if 10_000 <= digits <= 99_000_000:
                values.append(digits)

    return max(values) if values else None


# ──────────────────────────────────────────────────────────────────────
# Извлечение фото Auto.ru
# ──────────────────────────────────────────────────────────────────────
_IMAGE_KEYS = ("url", "src", "original", "preview", "image", "imageUrl", "image_url",
               "full", "large", "medium", "small", "thumb", "thumbnail")
_NESTED_KEYS = ("images", "gallery", "photo", "photos", "offer", "state", "vehicle_info",
                "external_panorama", "panorama", "image_group")


def _extract_url(value: Any) -> Optional[str]:
    if isinstance(value, str):
        url = value.strip()
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("//"):
            return "https:" + url
        return None
    if isinstance(value, dict):
        for key in _IMAGE_KEYS:
            if key in value:
                return _extract_url(value[key])
        # Auto.ru: часто внутри dict с "sizes" -> {size: url}
        sizes = value.get("sizes")
        if isinstance(sizes, dict):
            for size in ("1200x900", "1200x1200", "832x624", "456x342", "320x240"):
                if size in sizes:
                    return _extract_url(sizes[size])
            for size in sizes.values():
                url = _extract_url(size)
                if url:
                    return url
    return None


def extract_autoru_images(raw_item: Any) -> tuple[Optional[str], list[str]]:
    """Извлекает первое фото и список фото из любой структуры Auto.ru."""
    all_urls: list[str] = []
    if raw_item is None:
        return None, []

    if isinstance(raw_item, str):
        url = _extract_url(raw_item)
        return (url, [url]) if url else (None, [])

    if isinstance(raw_item, list):
        for el in raw_item:
            url = _extract_url(el)
            if url and url not in all_urls:
                all_urls.append(url)
        return (all_urls[0] if all_urls else None, all_urls)

    if isinstance(raw_item, dict):
        # Сначала ищем вложенные коллекции
        for key in _NESTED_KEYS:
            if key in raw_item and isinstance(raw_item[key], (list, dict)):
                nested_first, nested_all = extract_autoru_images(raw_item[key])
                if nested_all:
                    for url in nested_all:
                        if url not in all_urls:
                            all_urls.append(url)
                if nested_first and not all_urls:
                    all_urls.append(nested_first)
        # Прямые ключи
        url = _extract_url(raw_item)
        if url and url not in all_urls:
            all_urls.append(url)
        return (all_urls[0] if all_urls else None, all_urls)

    return None, []


# ──────────────────────────────────────────────────────────────────────
# Форматирование пробега/года
# ──────────────────────────────────────────────────────────────────────
def format_mileage(mileage: Any) -> Optional[str]:
    """Возвращает строку пробега или None, если пробег не указан."""
    if mileage is None or mileage == "":
        return None
    try:
        m = int(mileage)
    except Exception:
        return None
    if m < 0:
        return None
    return f"{m:,} км".replace(",", " ")


def format_year(year: Any) -> Optional[str]:
    """Возвращает строку года или None."""
    if year is None or year == "":
        return None
    try:
        y = int(year)
    except Exception:
        return None
    if 1950 <= y <= 2035:
        return f"{y} г."
    return None


# ──────────────────────────────────────────────────────────────────────
# Анализ Auto.ru
# ──────────────────────────────────────────────────────────────────────
def build_autoru_analysis(item: dict, market_price: int = 0, market_lvl: str = "") -> dict:
    """Всегда возвращает структуру analysis для Auto.ru."""
    title = str(item.get("title", "")).strip()
    price = item.get("_price_int") or parse_price(str(item.get("price", ""))) or 0
    year = item.get("_year") or item.get("year")
    mileage = item.get("mileage")
    description = str(item.get("description", "")).strip()

    has_data = bool(title) and price > 0 and (year is not None) and (mileage is not None) and description

    if not has_data:
        return {
            "summary": "Недостаточно данных для подробного анализа.",
            "flags": [],
            "score": None,
            "price_assessment": None,
        }

    flags: list[str] = []
    if price and market_price and market_price > 0:
        diff = price - market_price
        pct = round(diff / market_price * 100, 1)
        if diff < 0:
            price_assessment = f"ниже рынка на ~{abs(diff):,} ₽ (-{abs(pct)}%)".replace(",", " ")
        else:
            price_assessment = f"дороже рынка на ~{diff:,} ₽ (+{pct}%)".replace(",", " ")
    else:
        price_assessment = None

    if market_lvl:
        flags.append(f"оценка по рынку: {market_lvl}")

    return {
        "summary": "Данных достаточно для базового анализа.",
        "flags": flags,
        "score": None,
        "price_assessment": price_assessment,
    }


# ──────────────────────────────────────────────────────────────────────
# Шаблон карточки Auto.ru
# ──────────────────────────────────────────────────────────────────────
def format_autoru_card(item: dict) -> str:
    """Единый текст карточки Auto.ru."""
    source_tag = "🟡 Auto.ru"
    title = str(item.get("title", "") or "Автомобиль").strip()
    price_str = item.get("price", "") or "цена не указана"
    date_str = str(item.get("date", "")) or "сегодня"

    year_line = format_year(item.get("_year") or item.get("year"))
    mileage_line = format_mileage(item.get("mileage"))

    specs_parts = []
    if year_line:
        specs_parts.append(year_line)
    if mileage_line:
        specs_parts.append(mileage_line)

    specs = " · ".join(specs_parts)
    location = item.get("location", "") or ""
    analysis = item.get("analysis") or build_autoru_analysis(item)
    analysis_text = analysis.get("summary", "")
    price_assessment = analysis.get("price_assessment")

    lines = [f"{source_tag} {title}", f"💰 {price_str}", f"📅 {date_str}"]
    if specs:
        lines.append(f"📝 {specs}")
    if location:
        lines.append(f"📍 {location}")
    if price_assessment:
        lines.append(f"🔎 {price_assessment}")
    elif analysis_text:
        lines.append(f"🔎 {analysis_text}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Клавиатура карточки Auto.ru (список рядов для aiogram)
# ──────────────────────────────────────────────────────────────────────
def autoru_card_buttons(item: dict, uid: int) -> list[list[tuple[str, str]]]:
    """Возвращает ряды кнопок как список кортежей (text, callback/url).

    URL-адреса НЕ помещаются в callback_data — они передаются отдельно.
    """
    url = item.get("url", "")
    sid = item.get("_sid") or ""
    rows = []
    row1 = []
    if url:
        row1.append(("🔗 Открыть", ("url", url)))
    row1.append(("⭐ Сохранить", ("callback", f"fav|{sid}|{uid}")))
    rows.append(row1)
    rows.append([
        ("❌ Скрыть", ("callback", f"hide|{sid}|{uid}")),
        ("📋 Похожие", ("callback", f"sim|{sid}|{uid}")),
    ])
    rows.append([("🔍 Проверить машину", ("callback", f"check|{sid}|{uid}"))])
    return rows


def build_autoru_inline_keyboard(item: dict, uid: int):
    """Адаптер в aiogram InlineKeyboardMarkup.

    Длинные URL не попадают в callback_data.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for row in autoru_card_buttons(item, uid):
        buttons = []
        for text, payload in row:
            kind, value = payload
            if kind == "url":
                buttons.append(InlineKeyboardButton(text=text, url=value))
            else:
                buttons.append(InlineKeyboardButton(text=text, callback_data=value))
        rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────────────────────────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────────────────────────────
def log_relevance(source: str, item_or_text: Any, result: dict) -> None:
    """Безопасный лог релевантности."""
    if isinstance(item_or_text, dict):
        text = str(item_or_text.get("title", ""))[:60]
    else:
        text = str(item_or_text)[:60]
    reasons = ",".join(result.get("reasons", []))
    print(f"[RELEVANCE] source={source} accepted={result.get('accepted')} score={result.get('score')} reasons={reasons}")


def log_autoru(**kwargs) -> None:
    """Безопасный лог Auto.ru."""
    parts = [f"{k}={v}" for k, v in kwargs.items()]
    print(f"[Auto.ru] {' '.join(parts)}")
