from __future__ import annotations
import asyncio
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Alert state: track last alert sent per type to avoid spam
# key: alert_type -> last triggered timestamp
_last_alert: dict[str, float] = {}
_ALERT_COOLDOWN = 900  # 15 min cooldown per alert type


async def fetch_btc_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Use 1h snapshot data re-structured as df for alert checks."""
    from app.services.binance import fetch_klines
    df5, df15 = await asyncio.gather(
        fetch_klines("BTCUSDT", "1h", 60),
        fetch_klines("BTCUSDT", "1h", 60),
    )
    return df5, df15


def _rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    v = (100 - 100 / (1 + rs)).iloc[-1]
    return round(float(v) if not np.isnan(v) else 50.0, 1)


def _macd(closes: pd.Series) -> dict:
    e12 = closes.ewm(span=12, adjust=False).mean()
    e26 = closes.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return {
        "hist": float(hist.iloc[-1]),
        "prev_hist": float(hist.iloc[-2]),
        "cross_up": float(hist.iloc[-1]) > 0 > float(hist.iloc[-2]),
        "cross_down": float(hist.iloc[-1]) < 0 < float(hist.iloc[-2]),
    }


def _bollinger(closes: pd.Series, period: int = 20) -> dict:
    sma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    price = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    return {
        "upper": float(upper.iloc[-1]),
        "lower": float(lower.iloc[-1]),
        "break_up": prev <= float(upper.iloc[-2]) and price > float(upper.iloc[-1]),
        "break_down": prev >= float(lower.iloc[-2]) and price < float(lower.iloc[-1]),
        "price": price,
    }


def _volume_spike(df: pd.DataFrame) -> dict:
    avg = float(df["volume"].rolling(20).mean().iloc[-1])
    cur = float(df["volume"].iloc[-1])
    ratio = cur / avg if avg > 0 else 1.0
    direction = "🟢 BUY" if float(df["close"].iloc[-1]) > float(df["open"].iloc[-1]) else "🔴 SELL"
    return {"ratio": round(ratio, 1), "spike": ratio >= 2.0, "direction": direction}


def check_alerts(df5: pd.DataFrame, df15: pd.DataFrame) -> list[dict]:
    """Check all 4 alert types. Returns list of triggered alerts."""
    import time
    now = time.monotonic()
    alerts = []

    # Alert 1: RSI экстремум (15м)
    rsi15 = _rsi(df15["close"])
    key1 = "rsi_extreme"
    if (rsi15 >= 75 or rsi15 <= 25) and (now - _last_alert.get(key1, 0)) > _ALERT_COOLDOWN:
        direction = "перекуплен 🔴" if rsi15 >= 75 else "перепродан 🟢"
        emoji = "🔴" if rsi15 >= 75 else "🟢"
        alerts.append({
            "type": key1,
            "title": f"{emoji} RSI Экстремум · BTC 15м",
            "text": (
                f"📊 <b>RSI(14) = {rsi15}</b> — {direction}\n"
                f"Цена: <b>${df15['close'].iloc[-1]:,.0f}</b>\n\n"
                f"{'⚠️ Возможная перегретость — рассмотри шорт' if rsi15 >= 75 else '💡 Зона перепроданности — рассмотри лонг'}"
            ),
        })
        _last_alert[key1] = now

    # Alert 2: MACD кроссовер (15м)
    macd15 = _macd(df15["close"])
    key2 = "macd_cross"
    if (macd15["cross_up"] or macd15["cross_down"]) and (now - _last_alert.get(key2, 0)) > _ALERT_COOLDOWN:
        if macd15["cross_up"]:
            alerts.append({
                "type": key2,
                "title": "🟢 MACD Кросс Вверх · BTC 15м",
                "text": (
                    f"📈 <b>MACD пересёк сигнальную линию снизу вверх</b>\n"
                    f"Цена: <b>${df15['close'].iloc[-1]:,.0f}</b>\n\n"
                    f"💡 Бычий сигнал — импульс набирает силу вверх"
                ),
            })
        else:
            alerts.append({
                "type": key2,
                "title": "🔴 MACD Кросс Вниз · BTC 15м",
                "text": (
                    f"📉 <b>MACD пересёк сигнальную линию сверху вниз</b>\n"
                    f"Цена: <b>${df15['close'].iloc[-1]:,.0f}</b>\n\n"
                    f"⚠️ Медвежий сигнал — импульс разворачивается вниз"
                ),
            })
        _last_alert[key2] = now

    # Alert 3: Bollinger пробой (5м)
    bb5 = _bollinger(df5["close"])
    key3 = "bb_break"
    if (bb5["break_up"] or bb5["break_down"]) and (now - _last_alert.get(key3, 0)) > _ALERT_COOLDOWN:
        if bb5["break_up"]:
            alerts.append({
                "type": key3,
                "title": "⚡ Пробой Bollinger Вверх · BTC 5м",
                "text": (
                    f"📈 <b>Цена пробила верхнюю полосу Bollinger</b>\n"
                    f"Цена: <b>${bb5['price']:,.0f}</b>  Полоса: ${bb5['upper']:,.0f}\n\n"
                    f"⚡ Сильный импульс вверх — следи за объёмом"
                ),
            })
        else:
            alerts.append({
                "type": key3,
                "title": "⚡ Пробой Bollinger Вниз · BTC 5м",
                "text": (
                    f"📉 <b>Цена пробила нижнюю полосу Bollinger</b>\n"
                    f"Цена: <b>${bb5['price']:,.0f}</b>  Полоса: ${bb5['lower']:,.0f}\n\n"
                    f"⚡ Сильный импульс вниз — следи за объёмом"
                ),
            })
        _last_alert[key3] = now

    # Alert 4: Спайк объёма (5м)
    vol5 = _volume_spike(df5)
    key4 = "vol_spike"
    if vol5["spike"] and (now - _last_alert.get(key4, 0)) > _ALERT_COOLDOWN:
        alerts.append({
            "type": key4,
            "title": f"🔥 Спайк Объёма {vol5['ratio']}x · BTC 5м",
            "text": (
                f"📊 <b>Объём в {vol5['ratio']}x выше среднего</b>\n"
                f"Цена: <b>${df5['close'].iloc[-1]:,.0f}</b>  {vol5['direction']}\n\n"
                f"🔥 Крупный игрок в рынке — возможно резкое движение"
            ),
        })
        _last_alert[key4] = now

    return alerts
