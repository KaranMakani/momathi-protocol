"""
Momathi Protocol — Strategy Engine
Computes EMAs, detects trend, validates signals, and calculates trade levels.
"""
import logging
import time
import requests
import pandas as pd

import config

logger = logging.getLogger("momathi.strategy")

# ── Paradex API base URL (authenticated) ─────────────────────────
_PARADEX_API_URL = "https://api.prod.paradex.trade/v1" if config.PARADEX_ENV == "PROD" else "https://api.testnet.paradex.trade/v1"

# Global reference to authenticated ParadexClient (set by main.py)
_paradex_client = None


def set_paradex_client(client):
    """Set the authenticated ParadexClient for candle/BBO fetching."""
    global _paradex_client
    _paradex_client = client


def _get_auth_headers():
    """Get auth headers from the ParadexClient if available."""
    if _paradex_client and hasattr(_paradex_client, 'client'):
        # paradex-py stores auth headers in the internal client
        try:
            return _paradex_client.client.api_client.account.auth_headers()
        except Exception:
            pass
    return {}


def fetch_candles(coin: str = None, resolution: str = "5") -> pd.DataFrame:
    """
    Fetch OHLCV candles for the given coin from Paradex REST API.
    Args:
        coin: Trading pair (e.g. "BTC")
        resolution: Candle resolution ("5" = 5min, "15" = 15min, "60" = 1H)
    Returns a DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    coin = coin or config.runtime["coin"]
    symbol = f"{coin}-USD-PERP"
    res_labels = {"5": "5m", "15": "15m", "60": "1H"}
    res_label = res_labels.get(resolution, f"{resolution}m")
    logger.info("Fetching %s candles for %s", res_label, symbol)

    # Calculate timestamps based on resolution
    res_minutes = int(resolution)
    end_at = int(time.time() * 1000)
    start_at = end_at - (config.CANDLE_LIMIT * res_minutes * 60 * 1000)

    try:
        resp = requests.get(
            f"{_PARADEX_API_URL}/candles",
            params={
                "symbol": symbol,
                "resolution": resolution,
                "start_at": start_at,
                "end_at": end_at,
            },
            headers=_get_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        df = pd.DataFrame(results, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if df.empty:
            logger.warning("Empty candles returned for %s", symbol)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        # Convert values to float
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        # Convert timestamp
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        # Sort chronologically
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    except Exception as e:
        logger.error("Error fetching candles from Paradex: %s", e)
        raise


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA 8 and 30 columns to the DataFrame."""
    df["ema8"] = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()
    return df


def get_ema30(coin: str, exec_tf: str = "5") -> float | None:
    """
    Lightweight: fetch just the latest EMA30 on exec_tf.
    No trend check, no full validate_signal overhead.
    Used by the pyramid checker every 60 s.
    """
    try:
        df = fetch_candles(coin, resolution=exec_tf)
        if df.empty:
            return None
        df = compute_emas(df)
        return float(df.iloc[-1]["ema30"])
    except Exception as e:
        logger.error("get_ema30 failed for %s (%smin): %s", coin, exec_tf, e)
        return None


def get_mark_price(coin: str) -> float | None:
    """
    Return the current approximate mid-price for a coin.
    Tries the BBO (best-bid/offer) endpoint first; falls back to
    the last 5m candle close if unavailable.
    """
    symbol = f"{coin}-USD-PERP"
    try:
        resp = requests.get(
            f"{_PARADEX_API_URL}/bbo",
            params={"symbol": symbol},
            headers=_get_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        bbo = resp.json()
        bid = float(bbo.get("bid") or 0)
        ask = float(bbo.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
    except Exception:
        pass  # fall through to candle fallback

    try:
        df = fetch_candles(coin, resolution="5")
        if not df.empty:
            return float(df.iloc[-1]["close"])
    except Exception as e:
        logger.error("get_mark_price fallback failed for %s: %s", coin, e)
    return None




def get_trend(df: pd.DataFrame) -> str:
    """
    Determine the trend based on 8 EMA vs 30 EMA.
    Returns 'LONG' if 8 EMA > 30 EMA, 'SHORT' if 8 EMA < 30 EMA.
    """
    latest = df.iloc[-1]
    if latest["ema8"] > latest["ema30"]:
        return "LONG"
    else:
        return "SHORT"


def _price_precision(price: float) -> int:
    """Determine the number of decimal places needed for a given price level."""
    if price >= 1000:
        return 2     # BTC, ETH
    elif price >= 10:
        return 3     # SOL, AVAX
    elif price >= 1:
        return 4     # NEAR, SUI
    else:
        return 5     # DOGE, SHIB


def calculate_levels(ema8: float, ema30: float, direction: str) -> dict:
    """
    Calculate entry, SL, and TP levels.
    - Entry: exact EMA 8
    - SL: exact EMA 30
    - TP: 1:3 risk-reward from entry
    """
    decimals = _price_precision(ema8)

    if direction == "LONG":
        entry = ema8
        sl = ema30
        risk = abs(entry - sl)
        tp = entry + (3 * risk)
    else:  # SHORT
        entry = ema8
        sl = ema30
        risk = abs(sl - entry)
        tp = entry - (3 * risk)

    return {
        "entry": round(entry, decimals),
        "sl": round(sl, decimals),
        "tp": round(tp, decimals),
        "risk_per_unit": round(risk, decimals),
    }


def validate_signal(direction: str, coin: str = None, exec_tf: str = "5") -> dict:
    """
    Validate whether a trade signal is allowed.
    1. Fetch 1H candles → determine trend (higher-timeframe filter)
    2. Check if direction aligns with 1H trend
    3. Fetch exec_tf candles → compute entry/SL/TP levels
    4. If valid, return trade levels; if not, return rejection reason

    Args:
        direction: "LONG" or "SHORT"
        coin: Trading pair (e.g. "BTC")
        exec_tf: Execution timeframe resolution ("5" or "15")

    Returns:
        {
            "valid": bool,
            "reason": str (only if invalid),
            "trend": str,
            "levels": dict (only if valid),
            "ema8": float,
            "ema30": float,
            "exec_tf": str,
        }
    """
    coin = coin or config.runtime["coin"]
    tf_label = "5m" if exec_tf == "5" else "15m"

    # ── Step 1: Fetch 1H candles for TREND determination ──
    try:
        df_1h = fetch_candles(coin, resolution="60")
        df_1h = compute_emas(df_1h)
    except Exception as e:
        logger.error("Failed to fetch 1H candles: %s", e)
        return {"valid": False, "reason": f"Data error (1H): {e}"}

    trend = get_trend(df_1h)
    latest_1h = df_1h.iloc[-1]
    trend_ema8 = latest_1h["ema8"]
    trend_ema30 = latest_1h["ema30"]

    logger.info(
        "1H Trend — Direction: %s | Trend: %s | EMA8=%.4f EMA30=%.4f",
        direction, trend, trend_ema8, trend_ema30,
    )

    # Trend alignment check (1H)
    if direction.upper() != trend:
        return {
            "valid": False,
            "reason": (
                f"❌ Signal REJECTED — {direction.upper()} signal against {trend} trend (1H)\n"
                f"1H EMA8={trend_ema8:.4f} | EMA30={trend_ema30:.4f}"
            ),
            "trend": trend,
            "ema8": trend_ema8,
            "ema30": trend_ema30,
        }

    # ── Step 2: Fetch exec_tf candles for ENTRY/SL/TP levels ──
    try:
        df_exec = fetch_candles(coin, resolution=exec_tf)
        df_exec = compute_emas(df_exec)
    except Exception as e:
        logger.error("Failed to fetch %s candles: %s", tf_label, e)
        return {"valid": False, "reason": f"Data error ({tf_label}): {e}"}

    latest = df_exec.iloc[-1]
    ema8 = latest["ema8"]
    ema30 = latest["ema30"]

    logger.info(
        "%s Levels — EMA8=%.4f EMA30=%.4f",
        tf_label, ema8, ema30,
    )

    levels = calculate_levels(ema8, ema30, direction.upper())

    # Sanity check: risk must be positive
    if levels["risk_per_unit"] <= 0:
        return {
            "valid": False,
            "reason": "❌ Invalid EMA levels — risk per unit is zero or negative.",
            "trend": trend,
        }

    decimals = _price_precision(ema8)
    return {
        "valid": True,
        "trend": trend,
        "levels": levels,
        "ema8": round(ema8, decimals),
        "ema30": round(ema30, decimals),
        "exec_tf": exec_tf,
    }
