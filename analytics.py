"""
Аналитика пользователей PerekupDrive.

Лёгкий, файловый, неблокирующий трекинг событий + агрегация метрик +
веб-дашборд на aiohttp. Полностью изолирован от основной логики бота:
любая ошибка трекинга гасится в try/except и НИКОГДА не роняет бота.

Хранилище:
  data/analytics.jsonl  — поток событий (одна JSON-строка на событие)
  data/users.json       — профили пользователей {uid: {...}}
"""

import json
import os
import time
import datetime
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path("data")
EVENTS_FILE = DATA_DIR / "analytics.jsonl"
USERS_FILE = DATA_DIR / "users.json"


def _ensure_dir():
    try:
        DATA_DIR.mkdir(exist_ok=True)
    except Exception:
        pass


# ── Трекинг событий ─────────────────────────────────────────────

def track(action: str, uid: int | None = None, username: str | None = None, **fields):
    """
    Записать событие. Полностью безопасна — никогда не бросает исключений.
    """
    try:
        _ensure_dir()
        ev = {"ts": int(time.time()), "uid": uid, "action": action}
        for k, v in fields.items():
            if v is not None:
                ev[k] = v
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if uid is not None:
            _update_user(uid, action, username, fields)
    except Exception:
        pass


def _load_users() -> dict:
    try:
        if USERS_FILE.exists():
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_users(users: dict):
    try:
        _ensure_dir()
        USERS_FILE.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _update_user(uid: int, action: str, username: str | None, fields: dict):
    try:
        users = _load_users()
        key = str(uid)
        now = int(time.time())
        u = users.get(key) or {"first_seen": now, "searches": 0}
        u["last_seen"] = now
        if username:
            u["username"] = username
        if action == "search":
            u["searches"] = u.get("searches", 0) + 1
            if fields.get("region"):
                u["region"] = fields["region"]
        users[key] = u
        _save_users(users)
    except Exception:
        pass


def track_avito_deal(uid: int | None, score: int, profit: int, below_pct: float, price: int, source: str = "avito"):
    """Трекает найденную выгодную сделку для дашборда."""
    track("avito_deal", uid=uid, score=int(score), profit=int(profit),
          below_pct=float(below_pct), price=int(price), source=source)


# ── Публичные аксессоры (для админ-раздела бота) ────────────────

def read_events() -> list[dict]:
    """Все события трекинга (для админ-статистики)."""
    return _read_events()


def load_users() -> dict:
    """Профили пользователей {uid: {...}} (для админ-статистики)."""
    return _load_users()


# ── Агрегация ───────────────────────────────────────────────────

def _read_events() -> list[dict]:
    out = []
    try:
        if EVENTS_FILE.exists():
            for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _day_str(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def aggregate(region_names: dict | None = None) -> dict:
    """
    Посчитать все метрики из логов и профилей пользователей.
    region_names — опциональный словарь {code: human_name} для красивых названий.
    """
    region_names = region_names or {}
    events = _read_events()
    users = _load_users()
    now = time.time()
    today = _day_str(now)
    day_sec = 86400

    # ── По пользователям ──
    total_users = len(users)
    new_today = 0
    new_7d = 0
    for u in users.values():
        fs = u.get("first_seen", 0)
        if _day_str(fs) == today:
            new_today += 1
        if now - fs <= 7 * day_sec:
            new_7d += 1

    # ── Активность (DAU/WAU) по событиям ──
    dau, wau = set(), set()
    searches_total = 0
    searches_today = 0
    region_counter = Counter()
    price_counter = Counter()
    by_day_searches = defaultdict(int)
    by_day_newusers = defaultdict(int)

    for ev in events:
        ts = ev.get("ts", 0)
        uid = ev.get("uid")
        action = ev.get("action")
        if uid is not None:
            if now - ts <= day_sec:
                dau.add(uid)
            if now - ts <= 7 * day_sec:
                wau.add(uid)
        if action == "search":
            searches_total += 1
            d = _day_str(ts)
            by_day_searches[d] += 1
            if d == today:
                searches_today += 1
            reg = ev.get("region")
            if reg:
                region_counter[reg] += 1
            pmin = ev.get("price_min")
            pmax = ev.get("price_max")
            if pmin is not None or pmax is not None:
                price_counter[_price_bucket(pmin, pmax)] += 1

    # По площадкам
    platform_counter = Counter()
    error_counter = Counter()
    avito_deals_today = 0
    avito_deals_total = 0
    avito_profits: list[int] = []
    avito_best: list[dict] = []
    for ev in events:
        action = ev.get("action", "")
        if action == "search":
            results = ev.get("results", 0)
            if results == 0:
                error_counter["zero_results"] += 1
        if action in ("search", "vk_tg_search", "new_today", "global_search"):
            src = ev.get("source", "")
            if src:
                platform_counter[src] += 1
        if action == "error":
            err = ev.get("error_type", "unknown")
            error_counter[err] += 1
        if action == "avito_deal":
            avito_deals_total += 1
            score = ev.get("score", 0)
            profit = ev.get("profit", 0) or 0
            if _day_str(ev.get("ts", 0)) == today:
                avito_deals_today += 1
            avito_profits.append(profit)
            avito_best.append({
                "score": score,
                "profit": profit,
                "price": ev.get("price", 0),
                "below_pct": ev.get("below_pct", 0),
                "ts": ev.get("ts", 0),
            })

    # новые юзеры по дням из профилей
    for u in users.values():
        by_day_newusers[_day_str(u.get("first_seen", 0))] += 1

    # ── График за последние 14 дней ──
    timeline = []
    for i in range(13, -1, -1):
        d = _day_str(now - i * day_sec)
        timeline.append({
            "date": d,
            "searches": by_day_searches.get(d, 0),
            "new_users": by_day_newusers.get(d, 0),
            "avito_deals": sum(
                1 for ev in events
                if ev.get("action") == "avito_deal" and _day_str(ev.get("ts", 0)) == d
            ),
        })

    top_regions = [
        {"region": code, "name": region_names.get(code, code), "count": c}
        for code, c in region_counter.most_common(10)
    ]
    top_prices = [
        {"range": rng, "count": c} for rng, c in price_counter.most_common(10)
    ]

    avg_searches = round(searches_total / total_users, 2) if total_users else 0.0
    avg_profit = round(sum(avito_profits) / len(avito_profits), 0) if avito_profits else 0

    avito_best_sorted = sorted(
        avito_best, key=lambda x: (-x["score"], -x["profit"])
    )[:5]

    return {
        "generated_at": int(now),
        "total_users": total_users,
        "new_today": new_today,
        "new_7d": new_7d,
        "dau": len(dau),
        "wau": len(wau),
        "searches_total": searches_total,
        "searches_today": searches_today,
        "avg_searches_per_user": avg_searches,
        "top_regions": top_regions,
        "top_prices": top_prices,
        "timeline": timeline,
        "top_platforms": [{"platform": p, "count": c} for p, c in platform_counter.most_common(10)],
        "errors": [{"error": e, "count": c} for e, c in error_counter.most_common(10)],
        "avito_deals_today": avito_deals_today,
        "avito_deals_total": avito_deals_total,
        "avito_avg_profit": int(avg_profit),
        "avito_best": avito_best_sorted,
    }


def _fmt_k(v) -> str:
    try:
        v = int(v)
    except Exception:
        return "?"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}М".replace(".0", "")
    if v >= 1000:
        return f"{v // 1000}к"
    return str(v)


def _price_bucket(pmin, pmax) -> str:
    lo = _fmt_k(pmin) if pmin not in (None, 0) else "0"
    hi = _fmt_k(pmax) if pmax not in (None, 0, 99_000_000) else "∞"
    return f"{lo}–{hi} ₽"


# ── Текстовая сводка для /stats ─────────────────────────────────

def format_stats_text(region_names: dict | None = None) -> str:
    m = aggregate(region_names)
    lines = [
        "📊 *Статистика PerekupDrive*",
        "",
        f"👥 Всего пользователей: *{m['total_users']}*",
        f"🆕 Новых сегодня: *{m['new_today']}*  |  за 7 дней: *{m['new_7d']}*",
        f"🟢 Активны 24ч (DAU): *{m['dau']}*  |  7 дней (WAU): *{m['wau']}*",
        "",
        f"🔍 Всего поисков: *{m['searches_total']}*  |  сегодня: *{m['searches_today']}*",
        f"📈 В среднем поисков на юзера: *{m['avg_searches_per_user']}*",
    ]
    if m["top_regions"]:
        lines.append("")
        lines.append("🏙 *Топ регионов:*")
        for r in m["top_regions"][:5]:
            lines.append(f"  • {r['name']} — {r['count']}")
    if m["top_prices"]:
        lines.append("")
        lines.append("💰 *Топ бюджетов:*")
        for p in m["top_prices"][:5]:
            lines.append(f"  • {p['range']} — {p['count']}")
    return "\n".join(lines)


# ── Веб-дашборд (aiohttp) ───────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PerekupDrive — Аналитика</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:20px}
  .header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
  h1{font-size:20px;color:#fff}
  .sub{font-size:13px;color:#64748b;margin-top:4px}
  .refresh-btn{background:#2d3148;border:1px solid #3d4268;color:#94a3b8;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
  .refresh-btn:hover{background:#3b82f6;color:#fff;border-color:#3b82f6}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
  .card{background:#1a1d2e;border:1px solid #2d3148;border-radius:12px;padding:18px}
  .card .label{font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.5px}
  .card .value{font-size:30px;font-weight:700;color:#fff;margin-top:6px}
  .card .hint{font-size:12px;color:#94a3b8;margin-top:4px}
  .panel{background:#1a1d2e;border:1px solid #2d3148;border-radius:12px;padding:18px;margin-bottom:20px}
  .panel h2{font-size:15px;color:#fff;margin-bottom:14px}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:20px}
  @media(max-width:700px){.cols{grid-template-columns:1fr}}
  .row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #2d3148;font-size:14px}
  .row:last-child{border-bottom:none}
  .row .n{color:#94a3b8}
  .bar{background:#3b82f6;color:#fff;border-radius:10px;padding:1px 9px;font-size:12px;font-weight:600}
  .err{color:#f87171;padding:20px}
</style></head>
<body>
  <div class="header">
    <div><h1>🚗 PerekupDrive — Аналитика</h1><div class="sub" id="gen">загрузка…</div></div>
    <button class="refresh-btn" onclick="load()">↻ Обновить</button>
  </div>
  <div id="content"><div class="sub">Загрузка данных…</div></div>
<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
let chart = null;
function card(label, value, hint){return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div>${hint?`<div class="hint">${hint}</div>`:''}</div>`;}
function rows(arr, nameKey, valKey){
  if(!arr.length) return '<div class="sub">нет данных</div>';
  return arr.map(x=>`<div class="row"><span class="n">${x[nameKey]}</span><span class="bar">${x[valKey]}</span></div>`).join('');
}
async function load(){
  const c = document.getElementById('content');
  try{
    const r = await fetch('/api/stats?key='+encodeURIComponent(KEY));
    if(!r.ok){c.innerHTML='<div class="err">Ошибка '+r.status+' — проверь параметр ?key=</div>';return;}
    const m = await r.json();
    document.getElementById('gen').textContent = 'обновлено '+new Date(m.generated_at*1000).toLocaleString('ru-RU');
    c.innerHTML = `
      <div class="grid">
        ${card('Всего юзеров', m.total_users)}
        ${card('Новых сегодня', m.new_today, 'за 7 дней: '+m.new_7d)}
        ${card('DAU (24ч)', m.dau, 'WAU (7д): '+m.wau)}
        ${card('Поисков всего', m.searches_total, 'сегодня: '+m.searches_today)}
        ${card('Поисков / юзер', m.avg_searches_per_user)}
        ${card('Нулевых поисков', (m.errors||[]).find(e=>e.error==='zero_results')?.count || 0, 'поиски без результатов')}
        ${card('Avito сделок сегодня', m.avito_deals_today || 0, 'всего: '+(m.avito_deals_total||0))}
        ${card('Средняя прибыль', (m.avito_avg_profit||0).toLocaleString('ru-RU')+' ₽', 'по выгодным авто')}
      </div>
      <div class="panel"><h2>📈 За 14 дней</h2><canvas id="chart" height="90"></canvas></div>
      <div class="cols">
        <div class="panel"><h2>🏙 Топ регионов</h2>${rows(m.top_regions,'name','count')}</div>
        <div class="panel"><h2>💰 Топ бюджетов</h2>${rows(m.top_prices,'range','count')}</div>
      </div>
      <div class="cols">
        <div class="panel"><h2>🔥 Лучшие авто Avito</h2>${(m.avito_best||[]).map(x=>`<div class="row"><span class="n">Score ${x.score}/100, +${x.profit.toLocaleString('ru-RU')} ₽</span><span class="bar">${x.below_pct}%</span></div>`).join('') || '<div class="sub">нет данных</div>'}</div>
        <div class="panel"><h2>🔌 По площадкам</h2>${rows((m.top_platforms||[]),'platform','count')}</div>
      </div>
      <div class="cols">
        <div class="panel"><h2>⚠️ Проблемы</h2>${rows((m.errors||[]),'error','count')}</div>
      </div>`;
    const ctx = document.getElementById('chart');
    if(chart) chart.destroy();
    chart = new Chart(ctx, {type:'line', data:{
      labels:m.timeline.map(t=>t.date.slice(5)),
      datasets:[
        {label:'Поиски',data:m.timeline.map(t=>t.searches),borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.15)',tension:.3,fill:true},
        {label:'Новые юзеры',data:m.timeline.map(t=>t.new_users),borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.15)',tension:.3,fill:true}
      ]},options:{plugins:{legend:{labels:{color:'#94a3b8'}}},scales:{x:{ticks:{color:'#64748b'},grid:{color:'#2d3148'}},y:{ticks:{color:'#64748b'},grid:{color:'#2d3148'},beginAtZero:true}}}});
  }catch(e){c.innerHTML='<div class="err">Не удалось загрузить: '+e+'</div>';}
}
load();
</script>
</body></html>"""


def create_app(region_names: dict | None = None, dashboard_key: str | None = None,
               extra_routes: list | None = None):
    """
    Создать aiohttp-приложение дашборда. Импорт aiohttp внутри, чтобы
    модуль можно было импортировать без aiohttp (для /stats и трекинга).
    """
    from aiohttp import web

    if dashboard_key is None:
        dashboard_key = os.getenv("DASHBOARD_KEY", "")

    def _check(request) -> bool:
        if not dashboard_key:
            return True  # ключ не задан — доступ открыт (см. предупреждение в логе)
        provided = request.query.get("key") or request.headers.get("X-Dashboard-Key", "")
        return provided == dashboard_key

    async def index(request):
        if not _check(request):
            return web.Response(status=401, text="Unauthorized: add ?key=YOUR_DASHBOARD_KEY")
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def api_stats(request):
        if not _check(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(aggregate(region_names))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/stats", api_stats)
    for method, path, handler in (extra_routes or []):
        app.router.add_route(method, path, handler)
    return app


async def start_dashboard(region_names: dict | None = None, extra_routes: list | None = None):
    """
    Запустить веб-сервер параллельно с ботом (не блокирует polling).
    Порт из env PORT (Railway), fallback 8080.
    """
    try:
        from aiohttp import web
        port = int(os.getenv("PORT", "8080"))
        key = os.getenv("DASHBOARD_KEY", "")
        if not key:
            print("  [dashboard] ⚠️ DASHBOARD_KEY не задан — дашборд открыт без пароля!")
        app = create_app(region_names, key, extra_routes=extra_routes)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"  [dashboard] веб-дашборд запущен на :{port}")
    except Exception as e:
        print(f"  [dashboard] не удалось запустить: {e}")
