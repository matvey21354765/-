from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    BOT_TOKEN: str
    GROQ_API_KEY: str

    DATABASE_URL: str = "postgresql+asyncpg://postgres:2004@localhost:5432/dao_signals"
    POSTGRES_PASSWORD: str = "2004"

    BINANCE_SPOT_URL: str = "https://api.binance.com"
    BINANCE_FUTURES_URL: str = "https://fapi.binance.com"

    TRIAL_DAYS: int = 3
    PRICE_1M: int = 29
    PRICE_3M: int = 69
    PRICE_6M: int = 119
    VIRTUAL_DEPOSIT: float = 10000.0
    RISK_PER_TRADE_PCT: float = 2.0
    MIN_CONFIDENCE: float = 55.0
    MIN_SIGNAL_RATING: int = 5
    MIN_RR: float = 1.8
    SIGNAL_INTERVAL_MINUTES: int = 60
    SIGNAL_EXPIRE_DAYS: int = 7

    COINS: List[str] = ["BTC", "ETH", "SOL"]
    ADMIN_IDS: List[int] = [749256529]
    LOG_LEVEL: str = "INFO"


settings = Settings()
