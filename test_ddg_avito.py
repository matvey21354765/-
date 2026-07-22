import re, urllib.parse, time, random
import requests

slug = "moskva"
price_min = 0
price_max = 99_000_000

_avito_url_re = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?avito\.ru/[a-z0-9_.-]+/avtomobili/[^"\'<>\s]{10,}',
    re.I,
)
_price_re = re.compile(r"(\d[\d\s]{2,8})\s*(?:₽|тыс\.?\s*р(?:уб)?\.?|руб\.?)", re.I)
_year_re = re.compile(r"\b(19[5-9]\d|20[012]\d)\b")

def extract_urls(html):
    found = []
    seen = set()
    decoded = html
    for _ in range(2):
        for m in _avito_url_re.finditer(decoded):
            raw = m.group(0)
            if not raw.startswith("http"):
                raw = "https://" + raw
            clean = raw.split("?")[0].split("#")[0].rstrip("/")
            if f"/{slug}/" not in clean:
                continue
            if clean not in seen:
                seen.add(clean)
                found.append(clean)
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    return found

def parse_price(text):
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

q = f"site:avito.ru/{slug}/avtomobili lada"
url = "https://html.duckduckgo.com/html/"
print("Fetching DDG...")
r = requests.get(url, params={"q": q, "kl": "ru-ru"}, headers=headers, timeout=15)
print("status", r.status_code, "size", len(r.text))
urls = extract_urls(r.text)
print("found urls", len(urls))
for u in urls[:10]:
    pos = r.text.find(u)
    ctx = r.text[max(0, pos-300):pos+500]
    ctx_clean = re.sub(r"<[^>]+>", " ", ctx)
    ctx_clean = re.sub(r"&[a-z]+;", " ", ctx_clean)
    ctx_clean = re.sub(r"\s+", " ", ctx_clean).strip()
    price = parse_price(ctx_clean)
    ym = _year_re.search(ctx_clean)
    year = int(ym.group(1)) if ym else 0
    print(" -", u, "| price", price, "| year", year)
