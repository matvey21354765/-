import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from control_bot import scrape_avito

res = scrape_avito("moscow", pages=1, price_min=0, price_max=99_000_000)
print('RESULT:', len(res))
for it in res[:10]:
    print(' -', it['title'], it['price'], it['_price_int'], it.get('_year'), it['url'][:70])
