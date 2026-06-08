"""Sample video with 5 different crypto news."""
import os, io, wave, tempfile, random, textwrap
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont
from piper.voice import PiperVoice
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
import numpy as np

random.seed(7)
VIDEO_W, VIDEO_H = 1080, 1920
DARK=(10,12,22); GREEN=(0,220,130); RED=(220,60,60); AMBER=(255,180,0)
WHITE=(240,240,250); GREY=(120,125,145); PANEL=(20,24,40)
BLUE=(30,100,220); PURPLE=(100,50,200); ORANGE=(255,120,0); CYAN=(0,180,220)

def font(sz, bold=True):
    ps=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if bold else \
       ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    for p in ps:
        if os.path.exists(p): return ImageFont.truetype(p, sz)
    return ImageFont.load_default()

def wrap(t, w=28): return textwrap.wrap(str(t), width=w) or [""]

def grad(d, top, bot=DARK):
    for y in range(VIDEO_H):
        t=y/VIDEO_H; c=tuple(int(top[i]*(1-t)+bot[i]*t) for i in range(3))
        d.line([(0,y),(VIDEO_W,y)], fill=c)

def rrect(d, xy, r=24, fill=PANEL):
    x0,y0,x1,y1=xy
    d.rectangle([x0+r,y0,x1-r,y1],fill=fill)
    d.rectangle([x0,y0+r,x1,y1-r],fill=fill)
    for cx,cy in[(x0+r,y0+r),(x1-r,y0+r),(x0+r,y1-r),(x1-r,y1-r)]:
        d.ellipse([cx-r,cy-r,cx+r,cy+r],fill=fill)

def spark(d, prices, rect, color=GREEN, t=5):
    if len(prices)<2: return
    x0,y0,x1,y1=rect; lo,hi=min(prices),max(prices)
    if hi==lo: hi+=1
    pts=[(x0+int(i/(len(prices)-1)*(x1-x0)), y1-int((p-lo)/(hi-lo)*(y1-y0)))
         for i,p in enumerate(prices)]
    for i in range(len(pts)-1): d.line([pts[i],pts[i+1]],fill=color,width=t)

def fake_h(start, n=32, bias=0.003):
    p=start; out=[]
    for _ in range(n):
        p*=1+random.uniform(-0.007,0.007+bias); out.append(p)
    return out

NOW = datetime.now(timezone.utc).strftime("%d.%m.%Y")

# ══════════════════════════════════════════════════════════════════════════════
# NEWS SLIDE FACTORY
# ══════════════════════════════════════════════════════════════════════════════
def news_slide(
    badge_text, badge_color,
    source, tag,
    headline, body,
    stats,           # list of (label, value, color)
    chart_prices=None, chart_color=GREEN,
    tag_color=None,
):
    if tag_color is None: tag_color = badge_color
    img=Image.new("RGB",(VIDEO_W,VIDEO_H),DARK); d=ImageDraw.Draw(img)
    grad(d, tuple(int(c*0.18) for c in badge_color))

    # Top badge
    bw = len(badge_text)*28+60
    rrect(d,(60,65,60+bw,155),r=22,fill=badge_color)
    d.text((84,80),badge_text,font=font(44),fill=DARK)
    d.text((VIDEO_W-80,100),NOW,font=font(38),fill=GREY,anchor="ra")
    d.rectangle([(60,172),(VIDEO_W-60,178)],fill=badge_color)

    # Source + tag
    rrect(d,(60,200,60+len(source)*22+44,264),r=14,fill=PANEL)
    d.text((80,212),source,font=font(40,False),fill=GREY)
    rrect(d,(VIDEO_W-60-len(tag)*22-44,200,VIDEO_W-60,264),r=14,fill=tag_color)
    d.text((VIDEO_W-80-len(tag)*22,212),tag,font=font(38),fill=DARK)

    # Headline
    y=295
    for line in wrap(headline,26):
        d.text((60,y),line,font=font(60),fill=WHITE); y+=78
    d.rectangle([(60,y+10),(VIDEO_W-60,y+16)],fill=PANEL); y+=36

    # Body
    for line in wrap(body,33):
        d.text((60,y),line,font=font(43,False),fill=(185,192,212)); y+=58
    y+=14

    # Chart
    if chart_prices:
        chart_top = y+10
        chart_bot = chart_top+230
        if chart_bot < VIDEO_H-420:
            d.rectangle([(60,chart_top),(VIDEO_W-60,chart_bot+2)],fill=PANEL)
            spark(d,chart_prices,(70,chart_top+10,VIDEO_W-70,chart_bot-10),color=chart_color,t=5)
            d.text((60,chart_bot+10),"48ч",font=font(36,False),fill=GREY)
            y=chart_bot+60

    # Stats panel
    panel_h = 60 + len(stats)*100 + 20
    py = max(y, VIDEO_H - panel_h - 160)
    rrect(d,(60,py,VIDEO_W-60,py+panel_h),r=28,fill=PANEL)
    for i,(lbl,val,col) in enumerate(stats):
        sy=py+40+i*100
        d.text((100,sy),lbl,font=font(44,False),fill=GREY)
        d.text((VIDEO_W-100,sy),str(val),font=font(50),fill=col,anchor="ra")

    d.text((60,VIDEO_H-95),"@cryptosignals  •  не финансовый совет",
           font=font(36,False),fill=GREY)
    return img


# ══════════════════════════════════════════════════════════════════════════════
# 5 РАЗНЫХ НОВОСТЕЙ
# ══════════════════════════════════════════════════════════════════════════════

slides = [

  # 1 ── ETH ОБНОВЛЕНИЕ ──────────────────────────────────────────────────────
  dict(
    badge_text="⚡ ОБНОВЛЕНИЕ СЕТИ",
    badge_color=PURPLE,
    source="The Block", tag="Ethereum",
    headline="Ethereum запустил обновление Pectra — комиссии упали в 3 раза",
    body=("Хардфорк Pectra снизил стоимость транзакций с $4 до $1.3 в среднем. "
          "Разработчики также увеличили лимит блоба для L2-сетей. "
          "Цена ETH отреагировала ростом +5.2% за 24 часа."),
    stats=[("Цена ETH","$3 720",GREEN),
           ("Рост за 24ч","+5.2%",GREEN),
           ("Комиссия","~$1.30",AMBER)],
    chart_prices=fake_h(3200,bias=0.005), chart_color=PURPLE,
  ),

  # 2 ── ТРАМП + КРИПТА ─────────────────────────────────────────────────────
  dict(
    badge_text="🏛️ ПОЛИТИКА",
    badge_color=AMBER,
    source="Reuters", tag="Регуляция",
    headline="США создают стратегический резерв Bitcoin на $100 млрд",
    body=("Конгресс США одобрил закон о создании национального резерва BTC. "
          "Правительство начнёт скупать биткоин на открытом рынке в течение 6 месяцев. "
          "Аналитики называют это 'самым бычьим событием в истории крипты'."),
    stats=[("Бюджет","$100 млрд",GREEN),
           ("BTC цена","$67 840",GREEN),
           ("Реакция рынка","+8.4%",GREEN)],
    chart_prices=fake_h(60000,bias=0.008), chart_color=AMBER,
  ),

  # 3 ── SOLANA ОБГОНЯЕТ ────────────────────────────────────────────────────
  dict(
    badge_text="🚀 РЕКОРД",
    badge_color=CYAN,
    source="DeFiLlama", tag="Solana",
    headline="Solana обошла Ethereum по объёму DEX-торгов за сутки",
    body=("Объём торгов на DEX-биржах Solana достиг $12.4 млрд за 24 часа, "
          "впервые превысив показатель Ethereum в $11.8 млрд. "
          "Основной рост обеспечил Jupiter — крупнейший агрегатор на сети."),
    stats=[("SOL DEX","$12.4 млрд",GREEN),
           ("ETH DEX","$11.8 млрд",GREY),
           ("Цена SOL","$168  +3.1%",GREEN)],
    chart_prices=fake_h(140,bias=0.006), chart_color=CYAN,
  ),

  # 4 ── ЛИКВИДАЦИИ ────────────────────────────────────────────────────────
  dict(
    badge_text="💥 ЛИКВИДАЦИИ",
    badge_color=RED,
    source="Coinglass", tag="Фьючерсы",
    headline="За 24 часа ликвидировано шортов на $680 млн — шорт-сквиз продолжается",
    body=("Резкий рост цены BTC выше $67 000 запустил цепную реакцию ликвидаций. "
          "Медведи потеряли $680 млн за сутки — один из крупнейших шорт-сквизов 2024 года. "
          "Open Interest вырос до $38 млрд — рынок перегрет."),
    stats=[("Ликвидаций шорт","$680 млн",RED),
           ("Open Interest","$38 млрд",AMBER),
           ("Funding Rate","+0.045%",AMBER)],
    chart_prices=fake_h(63000,bias=0.009), chart_color=RED,
  ),

  # 5 ── BLACKROCK ETF ──────────────────────────────────────────────────────
  dict(
    badge_text="🏦 ИНСТИТУЦИОНАЛЫ",
    badge_color=GREEN,
    source="Bloomberg", tag="ETF",
    headline="BlackRock Bitcoin ETF побил рекорд — $1.2 млрд притока за один день",
    body=("IBIT от BlackRock зафиксировал рекордный суточный приток $1.2 млрд. "
          "Общие активы всех Bitcoin ETF достигли $58 млрд. "
          "Аналитики Goldman Sachs повысили таргет по BTC до $100 000."),
    stats=[("Приток за день","$1.2 млрд",GREEN),
           ("Всего в ETF","$58 млрд",GREEN),
           ("Таргет GS","$100 000",AMBER)],
    chart_prices=fake_h(55000,bias=0.007), chart_color=GREEN,
  ),

]

# Build images
images = [news_slide(**s) for s in slides]

# ── Тексты для озвучки ────────────────────────────────────────────────────────
speeches = [
    ("Обновление сети Ethereum. "
     "Хардфорк Pectra снизил комиссии транзакций в три раза — с четырёх долларов до одного тридцати. "
     "Цена Эфира выросла на пять целых две процента за сутки."),

    ("Политические новости для криптовалют. "
     "Конгресс США одобрил создание национального резерва Биткоина на сто миллиардов долларов. "
     "Правительство начнёт скупать BTC в течение шести месяцев. "
     "Аналитики называют это самым бычьим событием в истории крипты."),

    ("Рекорд Солана. "
     "Первый раз в истории Solana обогнала Ethereum по объёму торгов на децентрализованных биржах. "
     "За сутки прошло двенадцать целых четыре миллиарда долларов против одиннадцати восьми у Ethereum."),

    ("Массовые ликвидации шортов. "
     "За двадцать четыре часа шортисты потеряли шестьсот восемьдесят миллионов долларов. "
     "Биткоин пробил уровень шестьдесят семь тысяч и запустил цепную реакцию принудительных закрытий. "
     "Открытый интерес достиг тридцати восьми миллиардов — рынок перегрет."),

    ("Рекорд ETF фонда BlackRock. "
     "За один торговый день в Bitcoin ETF фонд от BlackRock зашло один целых два миллиарда долларов. "
     "Общие активы всех BTC ETF достигли пятидесяти восьми миллиардов. "
     "Goldman Sachs повысил таргет по Биткоину до ста тысяч долларов."),
]

# ── Piper TTS ─────────────────────────────────────────────────────────────────
voice = PiperVoice.load('/tmp/piper_voices/ru.onnx', config_path='/tmp/piper_voices/ru.onnx.json')

def make_tts(text):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        voice.synthesize_wav(text, wf)
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd); open(path,'wb').write(buf.getvalue())
    return path

# ── Build video ───────────────────────────────────────────────────────────────
clips, tmps = [], []
for img, speech in zip(images, speeches):
    p = make_tts(speech)
    tmps.append(p)
    a = AudioFileClip(p)
    audio_dur = a.duration
    dur = max(6.0, audio_dur - 0.1)   # keep slide slightly shorter than audio
    a2 = a.subclipped(0, dur)
    c = ImageClip(np.array(img), duration=dur).with_audio(a2)
    clips.append(c)

final = concatenate_videoclips(clips, method="compose")
out = '/home/user/-/sample_tiktok.mp4'
final.write_videofile(out, fps=30, codec='libx264', audio_codec='aac',
                      temp_audiofile=out+'.tmp.m4a', remove_temp=True, logger=None)
final.close()
for p in tmps:
    try: os.remove(p)
    except: pass
print(f'Done: {out}  ({os.path.getsize(out)//1024} KB)')
