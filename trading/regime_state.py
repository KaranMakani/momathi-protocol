"""
Momathi Protocol — Regime State Persistence
Handles loading and saving data/regime_state.json for the background regime watcher.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("momathi.trading.regime_state")

from config.settings import REGIME_STATE_FILE


def load_regime_state() -> dict:
    """
    Load regime state from disk on startup.
    
    Returns:
        Dict of token -> state (empty if file doesn't exist or error).
    """
    if not os.path.exists(REGIME_STATE_FILE):
        return {}
    
    try:
        with open(REGIME_STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            logger.info(
                "Restored regime state for %d token(s) from %s",
                len(data), REGIME_STATE_FILE,
            )
            return data
        else:
            logger.info("No regime state found in %s", REGIME_STATE_FILE)
            return {}
    except Exception as e:
        logger.error("Failed to load regime state from disk: %s", e)
        return {}


def save_regime_state(state: dict) -> None:
    """
    Persist regime state to disk atomically.
    
    Args:
        state: Dict of token -> state to save.
    """
    try:
        tmp_file = REGIME_STATE_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_file, REGIME_STATE_FILE)
        logger.debug("Saved regime state for %d token(s) to %s", len(state), REGIME_STATE_FILE)
    except Exception as e:
        logger.error("Failed to save regime state: %s", e)


def update_token_state(state: dict, token: str, new_state: str,
                       now_utc: datetime) -> dict:
    """
    Update the regime state for a single token.
    
    Args:
        state: Current regime state dict (mutated in place).
        token: Token symbol (e.g., "BTC").
        new_state: New regime state (CLEAN_LONG, CLEAN_SHORT, TANGLED, INSUFFICIENT_DATA).
        now_utc: Current UTC timestamp.
    
    Returns:
        The mutated state dict.
    """
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    if token not in state:
        # First time seeing this token
        state[token] = {
            "current_state": new_state,
            "previous_state": None,
            "consecutive_count": 1,
            "last_alert_state": None,
            "last_alert_at": None,
            "last_check_utc": now_iso,
        }
    else:
        entry = state[token]
        if entry["current_state"] == new_state:
            # Same state — increment consecutive count
            entry["consecutive_count"] += 1
        else:
            # State changed — reset counter
            entry["previous_state"] = entry["current_state"]
            entry["current_state"] = new_state
            entry["consecutive_count"] = 1
        
        entry["last_check_utc"] = now_iso
    
    return state


def should_alert(state: dict, token: str, confirmation_cycles: int,
                 cooldown_hours: int, alert_on_enter: bool,
                 alert_on_leave: bool, now_utc: datetime) -> tuple:
    """
    Determine if a regime change alert should be sent.
    
    Returns:
        (True, "ENTERED_CLEAN") or (True, "LEFT_CLEAN") when all conditions met.
        (False, None) otherwise.
    """
    if token not in state:
        return False, None
    
    entry = state[token]
    current = entry["current_state"]
    last_alert = entry.get("last_alert_state")
    consecutive = entry["consecutive_count"]
    
    # Condition 1: State must have changed since last alert
    if current == last_alert:
        return False, None
    
    # Condition 2: Must have enough consecutive confirmations
    if consecutive < confirmation_cycles:
        return False, None
    
    # Condition 3: INSUFFICIENT_DATA never triggers alerts
    if current == "INSUFFICIENT_DATA":
        return False, None
    
    # Condition 4: Cooldown check
    last_alert_at = entry.get("last_alert_at")
    if last_alert_at is not None:
        try:
            last_dt = datetime.fromisoformat(last_alert_at.replace("Z", "+00:00"))
            elapsed = (now_utc - last_dt).total_seconds() / 3600
            if elapsed < cooldown_hours:
                return False, None
        except (ValueError, TypeError) as e:
            logger.warning("should_alert: failed to parse last_alert_at for %s: %s", token, e)
            # Proceed if we can't parse (treat as expired cooldown)
    
    # Condition 5: Determine alert type and check if enabled
    is_clean = current in ("CLEAN_LONG", "CLEAN_SHORT")
    was_clean = entry.get("previous_state") in ("CLEAN_LONG", "CLEAN_SHORT")
    
    if is_clean and alert_on_enter:
        return True, "ENTERED_CLEAN"
    elif not is_clean and was_clean and alert_on_leave:
        return True, "LEFT_CLEAN"
    
    return False, None


def mark_alerted(state: dict, token: str, now_utc: datetime) -> dict:
    """
    Mark a token as having been alerted.
    
    Sets last_alert_state = current_state and last_alert_at = now_utc.
    
    Args:
        state: Current regime state dict (mutated in place).
        token: Token symbol.
        now_utc: Current UTC timestamp.
    
    Returns:
        The mutated state dict.
    """
    if token not in state:
        return state
    
    entry = state[token]
    entry["last_alert_state"] = entry["current_state"]
    entry["last_alert_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    return state
