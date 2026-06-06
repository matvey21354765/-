"""Generate short vertical videos (1080x1920) for TikTok from crypto content."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Video dimensions (TikTok vertical format)
VIDEO_W = 1080
VIDEO_H = 1920
FPS = 30
# Seconds per slide
SLIDE_DURATION = 4


def _make_frame(
    text_lines: list[str],
    title: str,
    bg_color: tuple = (10, 10, 20),
    accent: tuple = (0, 200, 120),
) -> "Image":
    """Render a single slide as a PIL Image."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (VIDEO_W, VIDEO_H), bg_color)
    draw = ImageDraw.Draw(img)

    # Gradient overlay (top strip)
    for y in range(200):
        alpha = int(255 * (1 - y / 200))
        r = int(accent[0] * alpha / 255 + bg_color[0] * (255 - alpha) / 255)
        g = int(accent[1] * alpha / 255 + bg_color[1] * (255 - alpha) / 255)
        b = int(accent[2] * alpha / 255 + bg_color[2] * (255 - alpha) / 255)
        draw.line([(0, y), (VIDEO_W, y)], fill=(r, g, b))

    # Try to load a font; fall back to default
    def _font(size: int):
        for name in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]:
            if os.path.exists(name):
                return ImageFont.truetype(name, size)
        return ImageFont.load_default()

    font_title = _font(72)
    font_body = _font(48)
    font_small = _font(38)

    # Title
    draw.text((60, 60), title, font=font_title, fill=(255, 255, 255))

    # Divider
    draw.rectangle([(60, 160), (VIDEO_W - 60, 168)], fill=accent)

    # Body lines
    y_pos = 220
    for line in text_lines:
        wrapped = textwrap.wrap(line, width=32)
        for wline in wrapped:
            draw.text((60, y_pos), wline, font=font_body, fill=(220, 220, 220))
            y_pos += 68
        y_pos += 20

    # Footer
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    draw.text((60, VIDEO_H - 100), f"crypto signals • {now}", font=font_small,
              fill=(120, 120, 140))

    return img


def _pil_to_moviepy_frame(img: "Image") -> "np.ndarray":
    import numpy as np
    return np.array(img)


async def generate_news_video(news_items: list[dict]) -> Optional[str]:
    """
    Build a TikTok video from a list of news dicts (title, desc, source).
    Returns path to the temporary .mp4 file, or None on failure.
    """
    return await asyncio.to_thread(_sync_generate_news_video, news_items)


def _sync_generate_news_video(news_items: list[dict]) -> Optional[str]:
    try:
        from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip
        from PIL import Image
    except ImportError as e:
        logger.error(f"Missing dependency for video generation: {e}")
        return None

    slides = []
    audio_files = []

    for item in news_items[:5]:
        title_line = "📰 Крипто-новости"
        body_lines = [
            item.get("title", "")[:80],
            "",
            item.get("desc", "")[:120] if item.get("desc") else "",
            "",
            f"Источник: {item.get('source', '')}",
        ]
        img = _make_frame(body_lines, title_line)
        frame = _pil_to_moviepy_frame(img)

        tts_text = _tts_text_for_news(item)
        audio_path = _generate_tts(tts_text)

        clip = ImageClip(frame).set_duration(SLIDE_DURATION)
        if audio_path:
            audio_clip = AudioFileClip(audio_path).subclip(0, min(SLIDE_DURATION,
                                       AudioFileClip(audio_path).duration))
            clip = clip.set_audio(audio_clip)
            audio_files.append(audio_path)
        slides.append(clip)

    if not slides:
        return None

    return _export_video(slides, audio_files)


async def generate_signal_video(signal_data: dict) -> Optional[str]:
    """
    Build a TikTok video for a futures trading signal.
    signal_data keys: coin, direction, entry_price, stop_loss,
                      take_profit_1, confidence, reasons (list[str])
    """
    return await asyncio.to_thread(_sync_generate_signal_video, signal_data)


def _sync_generate_signal_video(signal_data: dict) -> Optional[str]:
    try:
        from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip
        from PIL import Image
    except ImportError as e:
        logger.error(f"Missing dependency for video generation: {e}")
        return None

    coin = signal_data.get("coin", "BTC")
    direction = signal_data.get("direction", "LONG")
    accent = (0, 200, 120) if direction == "LONG" else (220, 50, 50)

    slides_data = [
        {
            "title": f"🚀 {coin}/USDT — {direction}",
            "lines": [
                f"Монета: {coin}",
                f"Направление: {direction}",
                f"Уверенность: {signal_data.get('confidence', 0):.0f}%",
                "",
                f"Вход:  ${signal_data.get('entry_price', 0):,.2f}",
                f"Стоп:  ${signal_data.get('stop_loss', 0):,.2f}",
                f"TP1:   ${signal_data.get('take_profit_1', 0):,.2f}",
            ],
        },
        {
            "title": "📊 Причины сигнала",
            "lines": [f"• {r}" for r in (signal_data.get("reasons") or [])[:6]],
        },
        {
            "title": "⚠️ Управление риском",
            "lines": [
                "• Используй не более 2–5% депозита",
                "• Всегда выставляй стоп-лосс",
                "• Фьючерсы = высокий риск",
                "",
                f"RR: {signal_data.get('risk_reward', 0):.1f}x",
            ],
        },
    ]

    slides = []
    audio_files = []

    for sd in slides_data:
        img = _make_frame(sd["lines"], sd["title"], accent=accent)
        frame = _pil_to_moviepy_frame(img)

        tts_text = _tts_text_for_signal(sd, coin, direction)
        audio_path = _generate_tts(tts_text)

        clip = ImageClip(frame).set_duration(SLIDE_DURATION)
        if audio_path:
            try:
                from moviepy.editor import AudioFileClip
                audio_clip = AudioFileClip(audio_path).subclip(
                    0, min(SLIDE_DURATION, AudioFileClip(audio_path).duration))
                clip = clip.set_audio(audio_clip)
                audio_files.append(audio_path)
            except Exception as e:
                logger.warning(f"Audio attach failed: {e}")
        slides.append(clip)

    return _export_video(slides, audio_files)


def _tts_text_for_news(item: dict) -> str:
    title = item.get("title", "")
    desc = item.get("desc", "")[:150]
    return f"Крипто новость. {title}. {desc}"


def _tts_text_for_signal(sd: dict, coin: str, direction: str) -> str:
    title = sd.get("title", "")
    lines = sd.get("lines", [])
    return f"{title}. {'. '.join(l for l in lines if l)}"


def _generate_tts(text: str) -> Optional[str]:
    """Generate TTS audio file, return path or None."""
    try:
        from gtts import gTTS
        tts = gTTS(text=text[:500], lang="ru", slow=False)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        tts.save(path)
        return path
    except Exception as e:
        logger.warning(f"TTS generation failed: {e}")
        return None


def _export_video(slides: list, audio_files: list) -> Optional[str]:
    """Concatenate slides and export to a temp .mp4 file."""
    try:
        from moviepy.editor import concatenate_videoclips
        final = concatenate_videoclips(slides, method="compose")
        fd, out_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        final.write_videofile(
            out_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=out_path + ".temp.m4a",
            remove_temp=True,
            logger=None,
        )
        final.close()
        for p in audio_files:
            try:
                os.remove(p)
            except Exception:
                pass
        return out_path
    except Exception as e:
        logger.error(f"Video export failed: {e}")
        return None
