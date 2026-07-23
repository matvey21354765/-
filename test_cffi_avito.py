import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from curl_cffi import requests as _cffi

proxies = {"http": "http://huba:EDNyWFYHy228@mproxy.site:16358", "https": "http://huba:EDNyWFYHy228@mproxy.site:16358"}
url = "https://www.avito.ru/moskva/avtomobili?seller_type=1"

for imp in ("chrome124", "chrome120", "chrome116"):
    try:
        sess = _cffi.Session(impersonate=imp)
        sess.proxies = proxies
        w = sess.get("https://www.avito.ru/moskva", timeout=10)
        print(f"warm {imp}: {w.status_code}, {len(w.text)}")
        r = sess.get(url, timeout=14)
        print(f"get {imp}: {r.status_code}, {len(r.text)}, urlPath={chr(34)+'urlPath'+chr(34) in r.text}")
        if chr(34)+'urlPath'+chr(34) in r.text:
            print("SUCCESS")
            break
    except Exception as e:
        print(f"err {imp}: {e}")
