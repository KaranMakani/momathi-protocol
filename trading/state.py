"""
Momathi Protocol — Trade State Persistence
Handles loading and saving active_trades.json.
"""
import json
import logging
import os

logger = logging.getLogger("momathi.trading.state")

# Consolidated path: always use data/ folder
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_FILE = os.path.join(BASE_DIR, "data", "active_trades.json")


def load_trades() -> list:
    """
    Load active trades from disk on startup.
    
    Returns:
        List of active trade dicts (empty if file doesn't exist or error).
    """
    if not os.path.exists(TRADES_FILE):
        return []
    
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            logger.info(
                "Restored %d active trade(s) from %s",
                len(data), TRADES_FILE,
            )
            return data
        else:
            logger.info("No active trades found in %s", TRADES_FILE)
            return []
    except Exception as e:
        logger.error("Failed to load trades from disk: %s", e)
        return []


def save_trades(trades: list) -> None:
    """
    Persist active trades to disk.
    
    Args:
        trades: List of active trade dicts to save.
    """
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2, default=str)
        logger.debug("Saved %d active trade(s) to %s", len(trades), TRADES_FILE)
    except Exception as e:
        logger.error("Failed to save trades: %s", e)
