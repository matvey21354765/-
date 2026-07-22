import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from datetime import date
from control_bot import _autoru_parse_html

text = open('autoru_test.html', encoding='utf-8').read()
print('HTML size:', len(text))
res = _autoru_parse_html(text, date.today())
print('RESULT:', len(res))
for it in res[:10]:
    print(' -', it['title'], it['price'], it['_price_int'], it['_year'], it['url'][:70])
