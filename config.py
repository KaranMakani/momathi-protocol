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

# ── Pyramid settings (add-on after 1:1 RR) ──────────────────────
# After entry fills and price hits the 1:1 RR level, the bot monitors
# EMA30 on the execution TF. When EMA30 rises (LONG) or falls (SHORT)
# within PYRAMID_TRIGGER_PCT of the original entry price, a market add
# is fired. The SL then trails at EMA30 every candle.
PYRAMID_ENABLED     = True   # master on/off switch
PYRAMID_ADD_PCT     = 0.15   # size of add = 15% of base position size
PYRAMID_TRIGGER_PCT = 0.003  # fire add when EMA30 is within 0.3% of original entry
PYRAMID_SL_BUFFER   = 0.0003 # new SL placed 0.03% below (LONG) / above (SHORT) EMA30
PYRAMID_TP_SQUEEZE  = 0.15   # pull TP 15% closer to current price after the add
