"""
Momathi Bot — Configuration
Loads .env and provides mutable runtime settings.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Paradex credentials ───────────────────────────────────────
PARADEX_L1_ADDRESS = os.getenv("PARADEX_L1_ADDRESS", "")
PARADEX_PRIVATE_KEY = os.getenv("PARADEX_PRIVATE_KEY", "")
PARADEX_ENV = os.getenv("PARADEX_ENV", "TESTNET").upper() # "PROD" or "TESTNET"

# ── Telegram credentials ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Runtime settings (mutable at runtime via Telegram) ──────────
runtime = {
    "risk_usd": float(os.getenv("DEFAULT_RISK_USD", "10")),
    "coin": os.getenv("DEFAULT_COIN", "BTC"),
    "running": True,
}

# ── Strategy parameters ─────────────────────────────────────────
EMA_FAST = 8      # Entry EMA
EMA_SLOW = 30     # SL / Trend EMA
CANDLE_LIMIT = 100  # candles to fetch for EMA computation

# ── 1H EMA Scan Configuration ──────────────────────────────────
SCAN_WATCHLIST = [
    "BTC", "ETH", "BNB", "HYPE", "NEAR", "SOL", "TRX",
    "APT", "ARB", "AVAX", "DOGE", "LINK", "OP", "XRP", "ZEC", "TON"
]
SCAN_SPREAD_THRESHOLD = 0.4  # minimum spread % for CLEAN classification
SCAN_SLOPE_LOOKBACK = 5      # candles ago for slope calculation
SCAN_SLOPE_THRESHOLD = 0.05  # % change to consider "up" or "down"
