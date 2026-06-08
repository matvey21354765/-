"""
Generate short vertical TikTok videos (1080×1920) from crypto content.

Each video is a sequence of slides rendered with Pillow, narrated with gTTS,
assembled by MoviePy into an .mp4 file.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
import textwrap
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VIDEO_W = 1080
VIDEO_H = 1920
FPS = 30
SLIDE_DURATION = 5      # seconds per slide
TRANSITION = 0.3        # fade duration (unused — kept for future)

# ── colour palettes ────────────────────────────────────────────────────────────

_DARK   = (10, 12, 22)
_GREEN  = (0, 220, 130)
_RED    = (220, 60, 60)
_AMBER  = (255, 180, 0)
_WHITE  = (240, 240, 250)
_GREY   = (120, 125, 145)
_PANEL  = (20, 24, 40)


# ── font loader ────────────────────────────────────────────────────────────────

def _font(size: int):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _font_regular(size: int):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ── drawing helpers ────────────────────────────────────────────────────────────

def _draw_gradient_bg(draw, color_top, color_bottom=_DARK):
    for y in range(VIDEO_H):
        t = y / VIDEO_H
        r = int(color_top[0] * (1 - t) + color_bottom[0] * t)
        g = int(color_top[1] * (1 - t) + color_bottom[1] * t)
        b = int(color_top[2] * (1 - t) + color_bottom[2] * t)
        draw.line([(0, y), (VIDEO_W, y)], fill=(r, g, b))


def _draw_rounded_rect(draw, xy, radius=24, fill=_PANEL):
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    for cx, cy in [(x0 + radius, y0 + radius), (x1 - radius, y0 + radius),
                   (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)]:
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=fill)


def _wrap(text: str, width: int = 30) -> list[str]:
    return textwrap.wrap(text, width=width) or [""]


# ── spark-line price chart ─────────────────────────────────────────────────────

def _draw_sparkline(draw, prices: list[float], rect, color=_GREEN, thickness=4):
    if len(prices) < 2:
        return
    x0, y0, x1, y1 = rect
    w = x1 - x0
    h = y1 - y0
    lo, hi = min(prices), max(prices)
    if hi == lo:
        hi += 1
    pts = []
    for i, p in enumerate(prices):
        px = x0 + int(i / (len(prices) - 1) * w)
        py = y1 - int((p - lo) / (hi - lo) * h)
        pts.append((px, py))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=color, width=thickness)


# ── slide factories ────────────────────────────────────────────────────────────

def _make_signal_slide_1(signal: dict) -> "Image":
    """Hero slide: coin + direction + entry."""
    from PIL import Image, ImageDraw
    coin = signal["coin"]
    direction = signal["direction"]
    accent = _GREEN if direction == "LONG" else _RED
    emoji = "🚀" if direction == "LONG" else "📉"

    img = Image.new("RGB", (VIDEO_W, VIDEO_H), _DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_bg(draw, tuple(int(c * 0.35) for c in accent), _DARK)

    # Watermark grid lines
    for i in range(0, VIDEO_W, 90):
        draw.line([(i, 0), (i, VIDEO_H)], fill=(255, 255, 255, 8), width=1)

    # Top badge
    _draw_rounded_rect(draw, (60, 80, 460, 160), radius=20, fill=accent)
    draw.text((80, 92), "⚡ ФЬЮЧЕРС СИГНАЛ", font=_font(42), fill=_DARK)

    # Coin + direction
    draw.text((60, 200), f"{emoji} {coin}/USDT", font=_font(110), fill=_WHITE)
    dir_color = accent
    draw.text((60, 340), direction, font=_font(130), fill=dir_color)

    # Divider
    draw.rectangle([(60, 510), (VIDEO_W - 60, 516)], fill=accent)

    # Entry / SL / TP panel
    _draw_rounded_rect(draw, (60, 540, VIDEO_W - 60, 940), radius=32, fill=_PANEL)
    rows = [
        ("Вход",   f"${signal.get('entry_price', 0):,.2f}",  _WHITE),
        ("Стоп",   f"${signal.get('stop_loss', 0):,.2f}",    _RED),
        ("TP1",    f"${signal.get('take_profit_1', 0):,.2f}", _GREEN),
        ("TP2",    f"${signal.get('take_profit_2', 0):,.2f}", _GREEN),
    ]
    for idx, (lbl, val, col) in enumerate(rows):
        y = 570 + idx * 90
        draw.text((100, y), lbl, font=_font_regular(46), fill=_GREY)
        draw.text((VIDEO_W - 100, y), val, font=_font(52), fill=col,
                  anchor="ra")

    # Confidence bar
    conf = signal.get("confidence", 0)
    bar_y = 970
    draw.text((60, bar_y), f"Уверенность:  {conf:.0f}%", font=_font(52), fill=_WHITE)
    draw.rectangle([(60, bar_y + 70), (VIDEO_W - 60, bar_y + 100)], fill=(40, 44, 64))
    bar_w = int((VIDEO_W - 120) * conf / 100)
    draw.rectangle([(60, bar_y + 70), (60 + bar_w, bar_y + 100)], fill=accent)

    # RR
    rr = signal.get("risk_reward", 0)
    draw.text((60, 1110), f"Risk/Reward:  {rr:.1f}x", font=_font(52), fill=_AMBER)

    # Sparkline
    prices = signal.get("price_history", [])
    if prices:
        sp_color = accent
        _draw_sparkline(draw, prices, (60, 1200, VIDEO_W - 60, 1450), color=sp_color, thickness=5)
        draw.rectangle([(60, 1198), (VIDEO_W - 60, 1200)], fill=_GREY)

    # Current price
    cur = signal.get("current_price", signal.get("entry_price", 0))
    chg = signal.get("change_24h", 0.0)
    chg_col = _GREEN if chg >= 0 else _RED
    chg_sym = "+" if chg >= 0 else ""
    draw.text((60, 1480), f"${cur:,.2f}", font=_font(78), fill=_WHITE)
    draw.text((60, 1580), f"{chg_sym}{chg:.2f}% за 24ч", font=_font(52), fill=chg_col)

    # Footer
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    draw.text((60, VIDEO_H - 160), "⚠️ Не является финансовым советом",
              font=_font_regular(36), fill=_GREY)
    draw.text((60, VIDEO_H - 110), f"@cryptosignals  •  {now}",
              font=_font_regular(38), fill=_GREY)

    return img


def _make_signal_slide_2(signal: dict) -> "Image":
    """Reasons + market data slide."""
    from PIL import Image, ImageDraw
    direction = signal["direction"]
    accent = _GREEN if direction == "LONG" else _RED

    img = Image.new("RGB", (VIDEO_W, VIDEO_H), _DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_bg(draw, (18, 22, 38), _DARK)

    draw.text((60, 80), "📊 Причины сигнала", font=_font(72), fill=_WHITE)
    draw.rectangle([(60, 175), (VIDEO_W - 60, 181)], fill=accent)

    reasons = (signal.get("reasons") or [])
    y = 210
    for r in reasons[:6]:
        for line in _wrap(r, 34):
            draw.text((60, y), f"• {line}", font=_font_regular(46), fill=_WHITE)
            y += 62
        y += 10

    # Market indicators panel
    _draw_rounded_rect(draw, (60, 950, VIDEO_W - 60, 1500), radius=32, fill=_PANEL)
    draw.text((100, 970), "Рыночные данные", font=_font(52), fill=_GREY)

    metrics = [
        ("RSI (1h)",   f"{signal.get('rsi_1h', 0):.1f}",   _AMBER),
        ("Funding",    f"{signal.get('funding_rate', 0)*100:.4f}%",  _WHITE),
        ("OI",         _fmt_large(signal.get("open_interest", 0)),   _WHITE),
        ("Fear&Greed", str(signal.get("fear_greed", "—")),  _GREEN),
    ]
    for idx, (lbl, val, col) in enumerate(metrics):
        y2 = 1040 + idx * 110
        draw.text((100, y2), lbl, font=_font_regular(46), fill=_GREY)
        draw.text((VIDEO_W - 100, y2), val, font=_font(52), fill=col, anchor="ra")

    draw.text((60, VIDEO_H - 160), "⚠️ Не является финансовым советом",
              font=_font_regular(36), fill=_GREY)
    draw.text((60, VIDEO_H - 110), "Управляй рисками  •  2–5% депозита",
              font=_font_regular(38), fill=_AMBER)
    return img


def _make_news_slide(item: dict, index: int, total: int) -> "Image":
    """News item slide."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (VIDEO_W, VIDEO_H), _DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_bg(draw, (14, 20, 38), _DARK)

    # Header
    _draw_rounded_rect(draw, (60, 70, 540, 155), radius=20, fill=(0, 100, 200))
    draw.text((82, 84), "📰 КРИПТО-НОВОСТИ", font=_font(40), fill=_WHITE)

    # Counter
    draw.text((VIDEO_W - 80, 100), f"{index}/{total}", font=_font(50),
              fill=_GREY, anchor="ra")

    draw.rectangle([(60, 175), (VIDEO_W - 60, 181)], fill=(0, 140, 255))

    # Source badge
    src = item.get("source", "")
    if src:
        _draw_rounded_rect(draw, (60, 210, 60 + len(src) * 22 + 40, 278),
                           radius=14, fill=_PANEL)
        draw.text((80, 218), src, font=_font_regular(44), fill=_GREY)

    # Title
    title = item.get("title", "")
    y = 310
    for line in _wrap(title, 28):
        draw.text((60, y), line, font=_font(58), fill=_WHITE)
        y += 76

    # Divider
    draw.rectangle([(60, y + 20), (VIDEO_W - 60, y + 24)], fill=_PANEL)

    # Description
    desc = item.get("desc", "")
    if desc:
        y += 50
        for line in _wrap(desc[:280], 34):
            draw.text((60, y), line, font=_font_regular(44), fill=(180, 185, 205))
            y += 60

    # Crypto tickers panel at bottom
    tickers = item.get("tickers", {})
    if tickers:
        panel_y = VIDEO_H - 420
        _draw_rounded_rect(draw, (60, panel_y, VIDEO_W - 60, panel_y + 280),
                           radius=28, fill=_PANEL)
        x_off = 100
        for sym, info in list(tickers.items())[:3]:
            price = info.get("price", 0)
            chg = info.get("change_24h", 0)
            col = _GREEN if chg >= 0 else _RED
            draw.text((x_off, panel_y + 30), sym, font=_font(52), fill=_WHITE)
            draw.text((x_off, panel_y + 100), f"${price:,.0f}", font=_font(44), fill=col)
            draw.text((x_off, panel_y + 160), f"{'+'if chg>=0 else ''}{chg:.1f}%",
                      font=_font_regular(40), fill=col)
            x_off += 310

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    draw.text((60, VIDEO_H - 110), f"@cryptosignals  •  {now}",
              font=_font_regular(38), fill=_GREY)
    return img


def _make_term_slide(title: str, body: str) -> "Image":
    """Daily crypto term slide."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (VIDEO_W, VIDEO_H), _DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_bg(draw, (20, 18, 38), _DARK)

    _draw_rounded_rect(draw, (60, 70, 620, 160), radius=20, fill=(80, 40, 160))
    draw.text((82, 84), "📖 ТЕРМИН ДНЯ", font=_font(46), fill=_WHITE)

    draw.rectangle([(60, 185), (VIDEO_W - 60, 191)], fill=(100, 60, 200))

    short_title = title.replace("📖 Термин дня: ", "")
    draw.text((60, 220), short_title, font=_font(90), fill=_WHITE)

    draw.rectangle([(60, 360), (VIDEO_W - 60, 366)], fill=_PANEL)

    y = 400
    for line in _wrap(body, 32):
        draw.text((60, y), line, font=_font_regular(50), fill=(200, 200, 220))
        y += 70

    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    draw.text((60, VIDEO_H - 110), f"@cryptosignals  •  {now}",
              font=_font_regular(38), fill=_GREY)
    return img


# ── TTS helpers ────────────────────────────────────────────────────────────────

def _tts(text: str) -> Optional[str]:
    try:
        from gtts import gTTS
        tts = gTTS(text=text[:500], lang="ru", slow=False)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        tts.save(path)
        return path
    except Exception as e:
        logger.warning(f"TTS failed: {e}")
        return None


def _tts_signal_slide1(s: dict) -> str:
    d = s["direction"]
    return (
        f"Новый сигнал по {s['coin']}. "
        f"Направление: {d}. "
        f"Вход: {s.get('entry_price', 0):,.0f} долларов. "
        f"Стоп-лосс: {s.get('stop_loss', 0):,.0f}. "
        f"Уверенность: {s.get('confidence', 0):.0f} процентов."
    )


def _tts_signal_slide2(s: dict) -> str:
    reasons = ". ".join((s.get("reasons") or [])[:3])
    return f"Причины сигнала: {reasons}. Используй не более пяти процентов депозита."


def _tts_news(item: dict, idx: int, total: int) -> str:
    return (
        f"Новость {idx} из {total}. "
        f"{item.get('title', '')}. "
        f"{item.get('desc', '')[:200]}"
    )


# ── video export ───────────────────────────────────────────────────────────────

def _pil_to_np(img) -> "np.ndarray":
    import numpy as np
    return np.array(img)


def _build_clip(img, audio_path: Optional[str], duration: float):
    from moviepy.editor import ImageClip, AudioFileClip
    clip = ImageClip(_pil_to_np(img)).set_duration(duration)
    if audio_path and os.path.exists(audio_path):
        try:
            a = AudioFileClip(audio_path)
            a = a.subclip(0, min(duration, a.duration))
            clip = clip.set_audio(a)
        except Exception as e:
            logger.warning(f"Audio attach failed: {e}")
    return clip


def _export(clips: list, tmp_audio: list) -> Optional[str]:
    try:
        from moviepy.editor import concatenate_videoclips
        final = concatenate_videoclips(clips, method="compose")
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        final.write_videofile(
            path, fps=FPS, codec="libx264", audio_codec="aac",
            temp_audiofile=path + ".tmp.m4a", remove_temp=True, logger=None,
        )
        final.close()
        for c in clips:
            try: c.close()
            except Exception: pass
    except Exception as e:
        logger.error(f"Video export error: {e}")
        return None
    finally:
        for p in tmp_audio:
            try: os.remove(p)
            except Exception: pass
    return path


# ── fetch live prices ──────────────────────────────────────────────────────────

async def _fetch_tickers() -> dict:
    """Returns {BTC: {price, change_24h}, ETH: ..., SOL: ...}"""
    try:
        from app.services.binance import fetch_ticker
        result = {}
        for coin in ["BTC", "ETH", "SOL"]:
            try:
                t = await fetch_ticker(coin)
                result[coin] = {
                    "price": float(t.get("lastPrice", 0)),
                    "change_24h": float(t.get("priceChangePercent", 0)),
                }
            except Exception:
                pass
        return result
    except Exception as e:
        logger.warning(f"Ticker fetch failed: {e}")
        return {}


async def _fetch_price_history(coin: str, limit: int = 48) -> list[float]:
    """Return last `limit` 1h close prices for sparkline."""
    try:
        from app.services.binance import _get
        from config.settings import settings
        url = f"{settings.BINANCE_FUTURES_URL}/fapi/v1/klines"
        data = await _get(url, {"symbol": f"{coin}USDT", "interval": "1h", "limit": limit})
        return [float(c[4]) for c in data]
    except Exception as e:
        logger.warning(f"Price history fetch failed: {e}")
        return []


# ── public API ─────────────────────────────────────────────────────────────────

async def generate_news_video(news_items: list[dict]) -> Optional[str]:
    tickers = await _fetch_tickers()
    for item in news_items:
        item["tickers"] = tickers
    return await asyncio.to_thread(_sync_news_video, news_items)


def _sync_news_video(news_items: list[dict]) -> Optional[str]:
    try:
        from moviepy.editor import ImageClip
    except ImportError as e:
        logger.error(f"moviepy not installed: {e}")
        return None

    clips, audios = [], []
    total = len(news_items)
    for i, item in enumerate(news_items, 1):
        img = _make_news_slide(item, i, total)
        tts_text = _tts_news(item, i, total)
        ap = _tts(tts_text)
        if ap:
            audios.append(ap)
        dur = SLIDE_DURATION + 1
        clips.append(_build_clip(img, ap, dur))

    return _export(clips, audios)


async def generate_signal_video(signal) -> Optional[str]:
    """signal can be a Signal ORM object or a plain dict."""
    coin = getattr(signal, "coin", None) or signal.get("coin", "BTC")
    prices = await _fetch_price_history(coin)
    tickers = await _fetch_tickers()

    if hasattr(signal, "__dict__"):
        data = {
            "coin": signal.coin,
            "direction": signal.direction,
            "entry_price": float(signal.entry_price or 0),
            "stop_loss": float(signal.stop_loss or 0),
            "take_profit_1": float(signal.take_profit_1 or 0),
            "take_profit_2": float(signal.take_profit_2 or 0),
            "confidence": float(signal.confidence or 0),
            "risk_reward": float(signal.risk_reward or 0),
            "rsi_1h": float(signal.rsi_1h or 0),
            "funding_rate": float(signal.funding_rate or 0),
            "open_interest": float(signal.open_interest or 0),
            "fear_greed": signal.fear_greed,
            "reasons": (signal.reasons or "").split("\n")[:6],
        }
    else:
        data = dict(signal)

    data["price_history"] = prices
    data["current_price"] = tickers.get(coin, {}).get("price", data["entry_price"])
    data["change_24h"] = tickers.get(coin, {}).get("change_24h", 0.0)

    return await asyncio.to_thread(_sync_signal_video, data)


def _sync_signal_video(data: dict) -> Optional[str]:
    try:
        from moviepy.editor import ImageClip
    except ImportError as e:
        logger.error(f"moviepy not installed: {e}")
        return None

    clips, audios = [], []

    img1 = _make_signal_slide_1(data)
    ap1 = _tts(_tts_signal_slide1(data))
    if ap1: audios.append(ap1)
    clips.append(_build_clip(img1, ap1, SLIDE_DURATION + 1))

    img2 = _make_signal_slide_2(data)
    ap2 = _tts(_tts_signal_slide2(data))
    if ap2: audios.append(ap2)
    clips.append(_build_clip(img2, ap2, SLIDE_DURATION))

    return _export(clips, audios)


async def generate_term_video(title: str, body: str) -> Optional[str]:
    return await asyncio.to_thread(_sync_term_video, title, body)


def _sync_term_video(title: str, body: str) -> Optional[str]:
    try:
        from moviepy.editor import ImageClip
    except ImportError as e:
        logger.error(f"moviepy not installed: {e}")
        return None

    img = _make_term_slide(title, body)
    short_title = title.replace("📖 Термин дня: ", "")
    ap = _tts(f"Термин дня: {short_title}. {body[:400]}")
    clips = [_build_clip(img, ap, SLIDE_DURATION + 3)]
    return _export(clips, [ap] if ap else [])


# ── utils ──────────────────────────────────────────────────────────────────────

def _fmt_large(v: float) -> str:
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.0f}M"
    return f"${v:,.0f}"
