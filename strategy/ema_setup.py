"""
Momathi Protocol — Strategy Engine
Computes EMAs, detects trend, validates signals, and calculates trade levels.
"""
import logging
import time
import requests
import pandas as pd
from datetime import datetime
from typing import Optional

from config.settings import (
    PARADEX_ENV, EMA_FAST, EMA_SLOW, CANDLE_LIMIT, MIN_CANDLES, runtime
)
from exchange.paradex_client import ParadexClient

logger = logging.getLogger("momathi.strategy")

# ── Paradex API base URL (authenticated) ─────────────────────────
_is_prod = PARADEX_ENV in ("PROD", "MAINNET")
_PARADEX_API_URL = "https://api.prod.paradex.trade/v1" if _is_prod else "https://api.testnet.paradex.trade/v1"


def _get_auth_headers(paradex_client: ParadexClient) -> dict:
    """Get JWT Bearer token from the ParadexClient for authenticated API requests."""
    if paradex_client and hasattr(paradex_client, 'client'):
        try:
            px_client = paradex_client.client
            if hasattr(px_client, 'account') and px_client.account:
                # Get JWT token from account (set after onboarding/auth)
                jwt_token = getattr(px_client.account, 'jwt_token', None)
                if jwt_token:
                    logger.info("JWT token available for API requests")
                    return {"Authorization": f"Bearer {jwt_token}"}
                else:
                    logger.warning("No JWT token in account — need to call auth() first")
            else:
                logger.warning("ParadexClient has no account initialized")
        except Exception as e:
            logger.warning("Failed to get JWT token: %s", e, exc_info=True)
    else:
        logger.warning("ParadexClient not set or missing client attribute")
    return {}


def fetch_candles(coin: str = None, resolution: str = "5", paradex_client: ParadexClient = None) -> pd.DataFrame:
    """
    Fetch OHLCV candles for the given coin from Paradex REST API.
    
    Args:
        coin: Trading pair (e.g. "BTC")
        resolution: Candle resolution ("5" = 5min, "15" = 15min, "60" = 1H)
        paradex_client: Authenticated ParadexClient for JWT token
    
    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    coin = coin or runtime["coin"]
    symbol = f"{coin}-USD-PERP"
    res_labels = {"5": "5m", "15": "15m", "60": "1H"}
    res_label = res_labels.get(resolution, f"{resolution}m")
    logger.info("Fetching %s candles for %s", res_label, symbol)

    # Calculate timestamps based on resolution
    res_minutes = int(resolution)
    end_at = int(time.time() * 1000)
    start_at = end_at - (CANDLE_LIMIT * res_minutes * 60 * 1000)
    
    # Diagnostic logging
    from datetime import datetime as dt
    logger.info(
        f"fetch_candles request: {symbol} {res_label} | "
        f"end_at={end_at} ({dt.utcfromtimestamp(end_at/1000).strftime('%Y-%m-%d %H:%M:%S UTC')}) | "
        f"start_at={start_at} ({dt.utcfromtimestamp(start_at/1000).strftime('%Y-%m-%d %H:%M:%S UTC')}) | "
        f"api_url={_PARADEX_API_URL}/markets/klines"
    )

    try:
        resp = requests.get(
            f"{_PARADEX_API_URL}/markets/klines",
            params={
                "symbol": symbol,
                "resolution": resolution,
                "start_at": start_at,
                "end_at": end_at,
            },
            headers=_get_auth_headers(paradex_client),
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
        
        # Drop the currently-forming (live) candle — only use closed candles
        df = df.iloc[:-1]
        
        logger.info(f"fetch_candles: {symbol} {res_label} returned {len(df)} candles, first={df.iloc[0]['timestamp']}, last={df.iloc[-1]['timestamp']}")
        
        return df

    except Exception as e:
        logger.error("Error fetching candles from Paradex: %s", e)
        raise


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add EMA 8, 15, and 30 columns to the DataFrame.
    
    Args:
        df: DataFrame with 'close' column
    
    Returns:
        DataFrame with added 'ema8', 'ema15', 'ema30' columns.
    """
    df["ema8"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema15"] = df["close"].ewm(span=15, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    
    logger.info(f"compute_emas: input_len={len(df)}, ema8_first={df['ema8'].iloc[0]}, ema8_last={df['ema8'].iloc[-1]}, ema30_first={df['ema30'].iloc[0]}, ema30_last={df['ema30'].iloc[-1]}")
    
    return df


def get_ema30(coin: str, exec_tf: str = "5", paradex_client: ParadexClient = None) -> Optional[float]:
    """
    Lightweight: fetch just the latest EMA30 on exec_tf.
    No trend check, no full validate_signal overhead.
    
    Args:
        coin: Trading pair (e.g. "BTC")
        exec_tf: Execution timeframe ("5" or "15")
        paradex_client: Authenticated ParadexClient
    
    Returns:
        EMA30 value or None if failed.
    """
    try:
        df = fetch_candles(coin, resolution=exec_tf, paradex_client=paradex_client)
        if df.empty:
            return None
        df = compute_emas(df)
        return float(df.iloc[-1]["ema30"])
    except Exception as e:
        logger.error("get_ema30 failed for %s (%smin): %s", coin, exec_tf, e)
        return None


def get_mark_price(coin: str, paradex_client: ParadexClient = None) -> Optional[float]:
    """
    Return the current approximate price for a coin using latest 5m kline close.
    
    Args:
        coin: Trading pair (e.g. "BTC")
        paradex_client: Authenticated ParadexClient
    
    Returns:
        Mark price or None if failed.
    """
    try:
        df = fetch_candles(coin, resolution="5", paradex_client=paradex_client)
        if not df.empty:
            return float(df.iloc[-1]["close"])
    except Exception as e:
        logger.warning("Failed to get mark price for %s: %s", coin, e)
    return None


def get_trend(df: pd.DataFrame) -> str:
    """
    Determine the trend based on 8 EMA vs 30 EMA.
    
    Args:
        df: DataFrame with 'ema8' and 'ema30' columns
    
    Returns:
        'LONG' if 8 EMA > 30 EMA, 'SHORT' if 8 EMA < 30 EMA.
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
    
    Args:
        ema8: EMA 8 value
        ema30: EMA 30 value
        direction: "LONG" or "SHORT"
    
    Returns:
        Dict with 'entry', 'sl', 'tp', 'risk_per_unit'.
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


def validate_signal(direction: str, coin: str = None, exec_tf: str = "5", paradex_client: ParadexClient = None) -> dict:
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
        paradex_client: Authenticated ParadexClient
    
    Returns:
        Dict with 'valid', optional 'reason', 'trend', optional 'levels', 'ema8', 'ema30', 'exec_tf'.
    """
    coin = coin or runtime["coin"]
    tf_label = "5m" if exec_tf == "5" else "15m"

    # ── Step 1: Fetch 1H candles for TREND determination ──
    try:
        df_1h = fetch_candles(coin, resolution="60", paradex_client=paradex_client)
        if len(df_1h) < MIN_CANDLES:
            logger.warning(
                f"validate_signal: insufficient 1H candles for {coin} "
                f"(got {len(df_1h)}, need {MIN_CANDLES}) — rejecting trade"
            )
            return {
                "valid": False,
                "reason": f"❌ Insufficient 1H data for {coin} (got {len(df_1h)} candles, need {MIN_CANDLES})"
            }
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
        df_exec = fetch_candles(coin, resolution=exec_tf, paradex_client=paradex_client)
        if len(df_exec) < MIN_CANDLES:
            logger.warning(
                f"validate_signal: insufficient {tf_label} candles for {coin} "
                f"(got {len(df_exec)}, need {MIN_CANDLES}) — rejecting trade"
            )
            return {
                "valid": False,
                "reason": f"❌ Insufficient {tf_label} data for {coin} (got {len(df_exec)} candles, need {MIN_CANDLES})"
            }
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
