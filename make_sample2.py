"""Rebuild sample video with Piper neural TTS."""
import os, io, wave, tempfile, random, math, textwrap
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont
from piper.voice import PiperVoice
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
import numpy as np

random.seed(42)

VIDEO_W, VIDEO_H = 1080, 1920
DARK=(10,12,22); GREEN=(0,220,130); RED=(220,60,60); AMBER=(255,180,0)
WHITE=(240,240,250); GREY=(120,125,145); PANEL=(20,24,40); BLUE=(30,100,220)

def font(sz, bold=True):
    ps = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if bold else \
         ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    for p in ps:
        if os.path.exists(p): return ImageFont.truetype(p, sz)
    return ImageFont.load_default()

def wrap(t, w=28): return textwrap.wrap(t, width=w) or [""]

def grad(d, top, bot=DARK):
    for y in range(VIDEO_H):
        t=y/VIDEO_H; c=tuple(int(top[i]*(1-t)+bot[i]*t) for i in range(3))
        d.line([(0,y),(VIDEO_W,y)], fill=c)

def rrect(d, xy, r=24, fill=PANEL):
    x0,y0,x1,y1=xy
    d.rectangle([x0+r,y0,x1-r,y1],fill=fill); d.rectangle([x0,y0+r,x1,y1-r],fill=fill)
    for cx,cy in[(x0+r,y0+r),(x1-r,y0+r),(x0+r,y1-r),(x1-r,y1-r)]:
        d.ellipse([cx-r,cy-r,cx+r,cy+r],fill=fill)

def spark(d, prices, rect, color=GREEN, t=5):
    if len(prices)<2: return
    x0,y0,x1,y1=rect; lo,hi=min(prices),max(prices)
    if hi==lo: hi+=1
    pts=[(x0+int(i/(len(prices)-1)*(x1-x0)), y1-int((p-lo)/(hi-lo)*(y1-y0)))
         for i,p in enumerate(prices)]
    for i in range(len(pts)-1): d.line([pts[i],pts[i+1]],fill=color,width=t)

BTC = 67840.0
BTC_H = [65000*(1+sum(random.uniform(-0.008,0.009) for _ in range(j))) for j in range(48)]
BTC_H[-1] = BTC

# ── SLIDE 1: NEWS ─────────────────────────────────────────────────────────────
def slide_news():
    img=Image.new("RGB",(VIDEO_W,VIDEO_H),DARK); d=ImageDraw.Draw(img)
    grad(d,(14,20,42))
    rrect(d,(60,70,560,158),r=20,fill=BLUE)
    d.text((84,84),"📰 КРИПТО-НОВОСТИ",font=font(42),fill=WHITE)
    d.text((VIDEO_W-80,100),datetime.now(timezone.utc).strftime("%d.%m.%Y"),font=font(38),fill=GREY,anchor="ra")
    d.rectangle([(60,178),(VIDEO_W-60,184)],fill=BLUE)
    rrect(d,(60,206,280,268),r=14,fill=PANEL)
    d.text((80,216),"CoinDesk",font=font(40,False),fill=GREY)
    headline="Bitcoin пробил $67 000 — аналитики ждут $80K до конца года"
    y=300
    for line in wrap(headline,26):
        d.text((60,y),line,font=font(62),fill=WHITE); y+=80
    d.rectangle([(60,y+16),(VIDEO_W-60,y+22)],fill=PANEL); y+=50
    body=("Биткоин вырос на 2.4% за сутки и закрепился выше $67 000. "
          "Институциональные инвесторы наращивают позиции — объём ETF фондов "
          "вырос до рекордных $18 млрд за неделю. "
          "Технический анализ указывает на следующую цель $72–74K.")
    for line in wrap(body,32):
        d.text((60,y),line,font=font(44,False),fill=(185,190,210)); y+=60
    py=VIDEO_H-440
    rrect(d,(60,py,VIDEO_W-60,py+300),r=28,fill=PANEL)
    d.text((100,py+20),"Рынок прямо сейчас",font=font(46,False),fill=GREY)
    for i,(sym,price,chg,col) in enumerate([("BTC","$67 840","+2.4%",GREEN),
                                             ("ETH","$3 520","+1.8%",GREEN),
                                             ("SOL","$168","+3.1%",GREEN)]):
        xo=100+i*310
        d.text((xo,py+80),sym,font=font(52),fill=WHITE)
        d.text((xo,py+148),price,font=font(44),fill=col)
        d.text((xo,py+208),chg,font=font(40,False),fill=col)
    d.text((60,VIDEO_H-100),"@cryptosignals  •  не финансовый совет",font=font(36,False),fill=GREY)
    return img

# ── SLIDE 2: SIGNAL ───────────────────────────────────────────────────────────
def slide_signal():
    img=Image.new("RGB",(VIDEO_W,VIDEO_H),DARK); d=ImageDraw.Draw(img)
    grad(d,tuple(int(c*0.3) for c in GREEN))
    rrect(d,(60,70,500,158),r=20,fill=GREEN)
    d.text((84,84),"⚡ СИГНАЛ: LONG",font=font(46),fill=DARK)
    d.text((60,200),"🚀 BTC/USDT",font=font(100),fill=WHITE)
    d.text((60,330),"LONG",font=font(120),fill=GREEN)
    d.rectangle([(60,498),(VIDEO_W-60,504)],fill=GREEN)
    rrect(d,(60,524,VIDEO_W-60,930),r=32,fill=PANEL)
    for i,(lbl,val,col) in enumerate([("Вход","$67 840",WHITE),("Стоп","$65 200",RED),
                                       ("TP 1","$71 000",GREEN),("TP 2","$74 500",GREEN)]):
        y=554+i*92
        d.text((100,y),lbl,font=font(46,False),fill=GREY)
        d.text((VIDEO_W-100,y),val,font=font(52),fill=col,anchor="ra")
    conf=78
    d.text((60,960),f"Уверенность:  {conf}%",font=font(52),fill=WHITE)
    d.rectangle([(60,1028),(VIDEO_W-60,1060)],fill=(40,44,64))
    d.rectangle([(60,1028),(60+int((VIDEO_W-120)*conf/100),1060)],fill=GREEN)
    d.text((60,1090),"Risk / Reward:  2.5x",font=font(52),fill=AMBER)
    spark(d,BTC_H,(60,1200,VIDEO_W-60,1450),color=GREEN,t=5)
    d.rectangle([(60,1198),(VIDEO_W-60,1200)],fill=GREY)
    d.text((60,1480),f"${BTC:,.0f}",font=font(78),fill=WHITE)
    d.text((60,1580),"+2.4% за 24ч",font=font(52),fill=GREEN)
    d.text((60,VIDEO_H-160),"⚠️ Не является финансовым советом",font=font(36,False),fill=GREY)
    d.text((60,VIDEO_H-110),"@cryptosignals  •  фьючерсы",font=font(38,False),fill=GREY)
    return img

# ── SLIDE 3: FORECAST ─────────────────────────────────────────────────────────
def slide_forecast():
    img=Image.new("RGB",(VIDEO_W,VIDEO_H),DARK); d=ImageDraw.Draw(img)
    grad(d,(18,16,36))
    rrect(d,(60,70,700,158),r=20,fill=(80,40,180))
    d.text((84,84),"🔮 ПРОГНОЗ НЕДЕЛИ",font=font(46),fill=WHITE)
    d.rectangle([(60,178),(VIDEO_W-60,184)],fill=(100,60,220))
    d.text((60,220),"BTC — куда идём?",font=font(80),fill=WHITE)
    d.rectangle([(60,340),(VIDEO_W-60,346)],fill=PANEL)
    y=370
    for title,text,col in [
        ("📈 Бычий сценарий",
         "Закрепление выше $68K открывает путь к $72–74K. "
         "Институционалы покупают каждую просадку.",GREEN),
        ("📉 Медвежий сценарий",
         "Если цена уйдёт ниже $65K — возможен откат к $61–62K. "
         "Следи за уровнем $65 200.",RED),
        ("🎯 Ключевые уровни",
         "Поддержка: $65 200 / $63 000\nСопротивление: $70 000 / $74 500",AMBER),
    ]:
        d.text((60,y),title,font=font(54),fill=col); y+=70
        for line in wrap(text,33):
            d.text((60,y),line,font=font(42,False),fill=(190,195,215)); y+=56
        y+=30
    rrect(d,(60,VIDEO_H-360,VIDEO_W-60,VIDEO_H-160),r=28,fill=PANEL)
    d.text((100,VIDEO_H-338),"💡 Совет трейдера",font=font(50),fill=AMBER)
    d.text((100,VIDEO_H-272),"Ставь стоп-лосс ВСЕГДА.",font=font(48),fill=WHITE)
    d.text((100,VIDEO_H-210),"2–5% депозита на сделку — не больше.",font=font(44,False),fill=WHITE)
    d.text((60,VIDEO_H-100),"@cryptosignals  •  не финансовый совет",font=font(36,False),fill=GREY)
    return img

# ── PIPER TTS ──────────────────────────────────────────────────────────────────
voice = PiperVoice.load('/tmp/piper_voices/ru.onnx', config_path='/tmp/piper_voices/ru.onnx.json')

def make_tts(text):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        voice.synthesize_wav(text, wf)
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    with open(path,'wb') as f: f.write(buf.getvalue())
    return path

# ── ASSEMBLE ──────────────────────────────────────────────────────────────────
slides_data = [
    (slide_news(), 7,
     "Биткоин пробил шестьдесят семь тысяч долларов и продолжает рост. "
     "Объём ETF фондов достиг рекорда — восемнадцать миллиардов долларов за неделю. "
     "Аналитики прогнозируют движение к восьмидесяти тысячам до конца года."),
    (slide_signal(), 8,
     "Открываем лонг на Биткоин. "
     "Вход: шестьдесят семь тысяч восемьсот сорок. "
     "Стоп-лосс: шестьдесят пять тысяч двести. "
     "Первая цель: семьдесят одна тысяча долларов. "
     "Уверенность семьдесят восемь процентов. Соотношение прибыли к риску — два и пять."),
    (slide_forecast(), 9,
     "Прогноз на неделю. "
     "Бычий сценарий: закрепление выше шестидесяти восьми тысяч открывает путь к семидесяти четырём. "
     "Медвежий сценарий: уход ниже шестидесяти пяти — откат к шестидесяти двум тысячам. "
     "Главное правило: всегда ставь стоп-лосс и рискуй не больше пяти процентов депозита."),
]

clips, tmps = [], []
for img, dur, speech in slides_data:
    p = make_tts(speech)
    tmps.append(p)
    a = AudioFileClip(p).with_duration(dur)
    c = ImageClip(np.array(img), duration=dur).with_audio(a)
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
