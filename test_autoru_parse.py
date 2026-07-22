import json, re
from datetime import date

def hot_score(item):
    return 0

def _autoru_parse_html(text: str, today) -> list[dict]:
    """Извлекает объявления из HTML Auto.ru (inline JSON, __INITIAL_STATE__ или regex)."""
    results = []

    # Метод 2: современный SSR Auto.ru — inline JSON-объекты с полем saleId.
    try:
        seen_ids: set[str] = set()
        for m in re.finditer(r'"saleId"\s*:\s*"', text):
            pos = m.start()
            depth = 0
            start = None
            for i in range(pos - 1, -1, -1):
                ch = text[i]
                if ch == "}":
                    depth += 1
                elif ch == "{":
                    if depth == 0:
                        start = i
                        break
                    depth -= 1
            if start is None:
                continue
            depth = 0
            end = None
            for i in range(start, len(text)):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end is None:
                continue
            try:
                obj = json.loads(text[start:end + 1])
            except Exception:
                continue
            sale_id = obj.get("saleId") or obj.get("id")
            if not sale_id or sale_id in seen_ids:
                continue
            if obj.get("seller_type") != "PRIVATE":
                continue
            url = obj.get("url", "")
            if not url.startswith("https://auto.ru/cars/"):
                continue
            price_info = obj.get("price_info") or {}
            price_val = price_info.get("price", 0)
            if not isinstance(price_val, int) or price_val < 10_000:
                continue
            vi = obj.get("vehicle_info") or {}
            mark_info = vi.get("mark_info") or {}
            model_info = vi.get("model_info") or {}
            super_gen = vi.get("super_gen") or {}
            mark = mark_info.get("name", "")
            model = model_info.get("name", "")
            year = super_gen.get("year_from", 0)
            title = obj.get("title") or f"{mark} {model} {year}".strip()
            price_str = f"{price_val:,} ₽".replace(",", " ")
            photo_url = ""
            images = (vi.get("state") or {}).get("image_urls") or []
            if images:
                photo_url = images[0].get("sizes", {}).get("1200x900", "")
                if not photo_url:
                    photo_url = images[0].get("sizes", {}).get("832x624", "")
                if not photo_url:
                    photo_url = images[0].get("sizes", {}).get("456x342", "")
            if photo_url.startswith("//"):
                photo_url = "https:" + photo_url
            mileage = (vi.get("state") or {}).get("mileage", 0)
            desc = f"{year} г., {mileage:,} км".replace(",", " ") if year or mileage else ""
            item = {
                "source": "autoru", "title": title, "price": price_str,
                "url": url, "date": str(today),
                "_photos": 1 if photo_url else 0, "_days_on_site": 0,
                "description": desc, "seller": "", "_photo_url": photo_url,
                "_price_int": price_val, "_year": year, "mileage": mileage,
            }
            item["_hot_score"] = hot_score(item)
            seen_ids.add(sale_id)
            results.append(item)
        if results:
            print(f"  [Auto.ru] inline JSON: {len(results)} объявлений")
            return results
    except Exception as e:
        print(f"  [Auto.ru] inline JSON parse error: {e}")

    return results


text = open('autoru.html', encoding='utf-8').read()
res = _autoru_parse_html(text, date.today())
print('RESULT:', len(res))
for it in res[:5]:
    print(' -', it['title'], it['price'], it['_price_int'], it['_year'], it['url'][:70])
