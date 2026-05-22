"""
Momathi Protocol — Regime Filters
1H EMA regime classification for token watchlist scanning.
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from config.settings import (
    SCAN_WATCHLIST, SCAN_SLOPE_LOOKBACK, SCAN_SPREAD_THRESHOLD, SCAN_SLOPE_THRESHOLD, MIN_CANDLES
)
from exchange.paradex_client import ParadexClient
from strategy.ema_setup import fetch_candles, compute_emas

logger = logging.getLogger("momathi.strategy.filters")


def scan_1h_regime(paradex_client: ParadexClient, coins: list = None) -> dict:
    """
    Scan a watchlist on 1H timeframe and classify each token as:
    - CLEAN LONG BIAS (EMA8 > EMA15 > EMA30, all slopes up, spread >= 0.4%)
    - CLEAN SHORT BIAS (EMA8 < EMA15 < EMA30, all slopes down, spread >= 0.4%)
    - TANGLED (everything else — skip)
    
    Args:
        paradex_client: Authenticated ParadexClient for candle fetching
        coins: Optional list of coins to scan (defaults to SCAN_WATCHLIST)
    
    Returns:
        Dict with 'long_bias', 'short_bias', 'tangled', 'errors', 'timestamp', 'last_candle_close'.
    """
    if coins is None:
        coins = SCAN_WATCHLIST
    
    result = {
        "long_bias": [],
        "short_bias": [],
        "tangled": [],
        "errors": [],
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "last_candle_close": None,
    }
    
    for coin in coins:
        try:
            # Fetch 1H candles (need at least MIN_CANDLES for reliable EMAs)
            df = fetch_candles(coin, resolution="60", paradex_client=paradex_client)
            if df.empty or len(df) < MIN_CANDLES:
                result["errors"].append({
                    "coin": coin,
                    "reason": f"Insufficient data (got {len(df)} candles, need {MIN_CANDLES})"
                })
                continue
            
            # Compute EMAs
            df = compute_emas(df)
            
            # Get current and historical values
            current = df.iloc[-1]
            past_idx = -1 - SCAN_SLOPE_LOOKBACK
            past = df.iloc[past_idx]
            
            # Calculate spread
            current_price = float(current["close"])
            ema8 = float(current["ema8"])
            ema15 = float(current["ema15"])
            ema30 = float(current["ema30"])
            
            spread_pct = abs(ema8 - ema30) / current_price * 100
            
            # Calculate slopes
            def get_slope(current_val, past_val):
                if past_val == 0:
                    return "flat"
                pct_change = (current_val - past_val) / past_val * 100
                if pct_change > SCAN_SLOPE_THRESHOLD:
                    return "up"
                elif pct_change < -SCAN_SLOPE_THRESHOLD:
                    return "down"
                else:
                    return "flat"
            
            ema8_slope = get_slope(ema8, float(past["ema8"]))
            ema15_slope = get_slope(ema15, float(past["ema15"]))
            ema30_slope = get_slope(ema30, float(past["ema30"]))
            
            # Get last closed candle time
            if result["last_candle_close"] is None:
                ts = current["timestamp"].timestamp()
                result["last_candle_close"] = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
            
            # Classification logic
            is_clean_long = (
                ema8 > ema15 > ema30 and
                ema8_slope == "up" and ema15_slope == "up" and ema30_slope == "up" and
                spread_pct >= SCAN_SPREAD_THRESHOLD
            )
            
            is_clean_short = (
                ema8 < ema15 < ema30 and
                ema8_slope == "down" and ema15_slope == "down" and ema30_slope == "down" and
                spread_pct >= SCAN_SPREAD_THRESHOLD
            )
            
            if is_clean_long:
                result["long_bias"].append({
                    "coin": coin,
                    "spread_pct": round(spread_pct, 2)
                })
            elif is_clean_short:
                result["short_bias"].append({
                    "coin": coin,
                    "spread_pct": round(spread_pct, 2)
                })
            else:
                result["tangled"].append(coin)
                
        except Exception as e:
            import traceback
            logger.error(f"scan_1h_regime error for {coin}: {e}\n{traceback.format_exc()}")
            result["errors"].append({
                "coin": coin,
                "reason": str(e)
            })
    
    # Sort by spread (largest first)
    result["long_bias"].sort(key=lambda x: x["spread_pct"], reverse=True)
    result["short_bias"].sort(key=lambda x: x["spread_pct"], reverse=True)
    
    return result
