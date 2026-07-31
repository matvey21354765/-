"""AI-анализ описания объявления Avito: плюсы, минусы, риски, рекомендация."""
from __future__ import annotations

import re
from typing import Any

from avito_normalizer import normalize_avito_item


POSITIVE_KEYWORDS = (
    "срочно",
    "торг",
    "реальный торг",
    "торг уместен",
    "нужны деньги",
    "срочно продам",
    "в отличном состоянии",
    "идеальное состояние",
    "без ДТП",
    "не битая",
    "не крашена",
    "один хозяин",
    "собственник",
    "все ТО",
    "оригинал ПТС",
    "полный комплект",
    "зимняя резина",
    "летняя резина",
    "кожаный салон",
    "панорама",
    "круиз-контроль",
    "климат-контроль",
    "камера заднего вида",
    "парктроник",
    "автомат",
    "возможен обмен",
)

NEGATIVE_KEYWORDS = (
    "после дтп",
    "требует ремонта",
    "не на ходу",
    "двигатель не заводится",
    "под замену",
    "гниёт",
    "гниль",
    "ржавчина",
    "сварка",
    "сильно битая",
    "списанная",
    "утопленник",
    "страховая",
    "запрет регистрации",
    "залог",
    "арест",
    "левый руль",
    "на иностранных номерах",
    "без документов",
    "кузовные работы",
    "покраска",
    "под покраску",
    "обмен",
)

RISK_KEYWORDS = (
    "после дтп",
    "требует ремонта",
    "не на ходу",
    "утопленник",
    "страховая",
    "запрет регистрации",
    "залог",
    "арест",
    "без документов",
    "перекуп",
    "срочно даром",
    "цена по запросу",
    "торг у капота",
    "возможен торг",
    "звоните обсудим",
    "только звонки",
)

PRICE_SUSPICION_WORDS = (
    "срочно даром",
    "торг сильный",
    "любая проверка",
    "проверяйте любым сервисом",
    "не битая не крашена",
)


def analyze_car_text(item: dict[str, Any]) -> dict[str, Any]:
    """Анализирует текст объявления и возвращает плюсы, минусы, риски, рекомендацию."""
    normalized = normalize_avito_item(item)
    text = f"{normalized['title']}\n{normalized['description']}".lower()

    positives: list[str] = []
    negatives: list[str] = []
    risks: list[str] = []
    recommendation = ""

    for keyword in POSITIVE_KEYWORDS:
        if keyword in text and keyword not in negatives:
            positives.append(keyword.capitalize())

    for keyword in NEGATIVE_KEYWORDS:
        if keyword in text:
            negatives.append(keyword.capitalize())

    for keyword in RISK_KEYWORDS:
        if keyword in text:
            risks.append(keyword.capitalize())

    # Проверка на дублирование контактов / скрытие информации
    if len(normalized["description"]) < 30 and not normalized["images"]:
        risks.append("Короткое описание и нет фото — возможен подозрительный лот")

    if "только звонки" in text or "пишите в телеграм" in text or "напишите в лс" in text:
        risks.append("Продавец просит уйти с площадки — возможен развод")

    price = normalized["price"]
    if price < 50_000 and price > 0:
        risks.append("Слишком низкая цена — проверьте на подвох")

    if any(w in text for w in PRICE_SUSPICION_WORDS):
        risks.append("Фразы, часто используемые мошенниками")

    # Формируем рекомендацию
    if risks:
        recommendation = "⚠️ Проверьте машину перед осмотром: есть риски."
    elif negatives and positives:
        recommendation = "🟡 Средний вариант: есть минусы, но и плюсы. Осмотр обязателен."
    elif positives and not negatives:
        recommendation = "🟢 Хороший вариант: много плюсов. Рекомендуется быстрый осмотр."
    elif negatives:
        recommendation = "🔴 Много минусов: возможно, объявление не стоит рассматривать."
    else:
        recommendation = "⚪ Недостаточно данных. Запросите подробности и фото."

    return {
        "positives": list(dict.fromkeys(positives))[:8],
        "negatives": list(dict.fromkeys(negatives))[:8],
        "risks": list(dict.fromkeys(risks))[:8],
        "recommendation": recommendation,
        "text_length": len(normalized["description"]),
        "has_photos": bool(normalized["images"]),
    }


def format_ai_analysis(analysis: dict[str, Any]) -> str:
    """Форматирует AI-анализ для Telegram."""
    lines: list[str] = []
    if analysis.get("positives"):
        lines.append("✅ Плюсы:")
        for p in analysis["positives"][:5]:
            lines.append(f"• {p}")
    if analysis.get("negatives"):
        lines.append("❌ Минусы:")
        for n in analysis["negatives"][:5]:
            lines.append(f"• {n}")
    if analysis.get("risks"):
        lines.append("⚠️ Риски:")
        for r in analysis["risks"][:5]:
            lines.append(f"• {r}")
    if analysis.get("recommendation"):
        lines.append(f"\n💡 Рекомендация: {analysis['recommendation']}")
    return "\n".join(lines)


def extract_brand_model(title: str) -> dict[str, str]:
    """Простое извлечение марки и модели из заголовка."""
    title = title.strip().upper()
    # Основные марки
    brands = (
        "LADA", "ВАЗ", "TOYOTA", "NISSAN", "HONDA", "MITSUBISHI", "MAZDA",
        "FORD", "CHEVROLET", "RENAULT", "HYUNDAI", "KIA", "VOLKSWAGEN", "VW",
        "SKODA", "AUDI", "BMW", "MERCEDES", "OPEL", "PEUGEOT", "CITROEN",
        "SUBARU", "SUZUKI", "LEXUS", "INFINITI", "VOLVO", "CHERY", "GEELY",
        "HAVAL", "GREAT WALL", "УАЗ", "ГАЗ", "DAEWOO", "PORSCHE", "JAGUAR",
        "CADILLAC", "CHRYSLER", "DODGE", "JEEP", "LAND ROVER", "MINI",
    )
    found_brand = ""
    for brand in brands:
        if brand in title:
            found_brand = brand
            break

    # Модель — следующее слово после марки или цифры
    model = ""
    if found_brand:
        rest = title.split(found_brand, 1)[-1].strip()
        tokens = re.split(r"[\s,]+", rest, maxsplit=2)
        if tokens:
            model = tokens[0].title()
    return {"brand": found_brand, "model": model}
