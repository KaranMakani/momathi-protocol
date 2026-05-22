"""
Momathi Protocol — Configuration
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
CANDLE_LIMIT = 200  # candles to fetch for EMA computation (5-10x EMA30 period for stability)
MIN_CANDLES = 50  # minimum closed candles required for reliable EMA30

# ── 1H EMA Scan Configuration ──────────────────────────────────
# Curated 2026-05-22 based on Paradex 1H liquidity data.
# Tokens returning <50 candles were removed (insufficient for reliable EMA30).
# Removed tokens tracked in SCAN_WATCHLIST_PENDING below.
SCAN_WATCHLIST = [
    "BTC", "ETH", "BNB", "HYPE", "SOL", "ARB", "LINK", "XRP", "ZEC"
]
# SCAN_WATCHLIST_PENDING = [  # restore if Paradex liquidity improves
#     "NEAR", "TRX", "APT", "AVAX", "DOGE", "OP", "TON"
# ]
SCAN_SPREAD_THRESHOLD = 0.3  # tuned 2026-05-22 — was 0.4, lowered after seeing only 2/9 tokens clean (HYPE, LINK). 0.3 should also catch SOL-like mid-spread cases. Tune up if too many chop signals.
SCAN_SLOPE_LOOKBACK = 5      # candles ago for slope calculation
SCAN_SLOPE_THRESHOLD = 0.05  # % change to consider "up" or "down"

# ── Regime Watcher Configuration ─────────────────────────────────
# Background watcher that re-runs 1H regime check every 15 minutes.
# Tracks state changes per token and alerts only after N consecutive
# confirmations (filters fakeouts). Respects cooldown to prevent spam.
REGIME_WATCHER_ENABLED = True
REGIME_WATCHER_INTERVAL_SECONDS = 900       # 15 min between checks
REGIME_CONFIRMATION_CYCLES = 2              # 2 cycles = 30 min confirm

# Alert control — minimize notification clutter
REGIME_ALERT_ON_ENTER_CLEAN = True          # alert when entering CLEAN
REGIME_ALERT_ON_LEAVE_CLEAN = False         # NO alert on regime loss
REGIME_ALERT_COOLDOWN_HOURS = 4             # don't re-alert same token within this window

REGIME_STATE_FILE = "data/regime_state.json"  # persisted state path


def validate() -> None:
    """
    Validate required environment variables at startup.
    
    Raises:
        ConfigError: If any required environment variables are missing.
    """
    from utils.errors import ConfigError
    
    missing = []
    if not PARADEX_L1_ADDRESS:
        missing.append("PARADEX_L1_ADDRESS")
    if not PARADEX_PRIVATE_KEY:
        missing.append("PARADEX_PRIVATE_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
