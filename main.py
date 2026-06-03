"""
Momathi Bot — Main Entry Point
Starts the Paradex client, trade manager, and Telegram bot.
Runs a background job to auto-update pending orders every minute.
"""
import asyncio
import logging
import sys
import time

from config.settings import PARADEX_L1_ADDRESS, PARADEX_PRIVATE_KEY, PARADEX_ENV, \
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, runtime, validate, \
    REGIME_WATCHER_ENABLED, REGIME_WATCHER_INTERVAL_SECONDS, \
    REGIME_CONFIRMATION_CYCLES, REGIME_ALERT_ON_ENTER_CLEAN, \
    REGIME_ALERT_ON_LEAVE_CLEAN, REGIME_ALERT_COOLDOWN_HOURS, \
    SCAN_WATCHLIST
from exchange.paradex_client import ParadexClient
from trading.trade_manager import TradeManager
from trading.regime_state import (
    load_regime_state, save_regime_state,
    update_token_state, should_alert, mark_alerted,
)
from bot.telegram_bot import MomathiTelegramBot
from utils.logger import setup_logger
from utils.errors import ConfigError
from datetime import datetime, timezone

# ── Logging setup ────────────────────────────────────────────────
setup_logger()
logger = logging.getLogger("momathi.main")

# ── Update interval (seconds) ───────────────────────────────────
ORDER_UPDATE_INTERVAL = 60  # check every 1 minute


async def fill_check_loop(trade_mgr: TradeManager, tg_bot: MomathiTelegramBot):
    """Background loop: detect filled entries every 60s."""
    logger.info("Fill check loop started (60s interval)")
    
    # Thread-safe lock to prevent race conditions in fill detection
    import threading
    fill_lock = threading.Lock()

    while runtime["running"]:
        await asyncio.sleep(60)

        if not runtime["running"]:
            break

        if not trade_mgr.active_trades:
            continue

        # Acquire lock to prevent concurrent fill detection
        if not fill_lock.acquire(blocking=False):
            logger.debug("Fill check skipped — lock already held")
            continue

        try:
            loop = asyncio.get_running_loop()
            filled = await loop.run_in_executor(None, trade_mgr.check_fills)
            for f in filled:
                msg = (
                    f"✅ <b>Entry Filled — {f['coin']} {f['direction']}</b>\n\n"
                    f"📍 Entry: <b>{f['entry']}</b>\n"
                    f"🛑 SL: <b>{f['sl']}</b>\n"
                    f"🎯 TP: <b>{f['tp']}</b>\n\n"
                    f"TP/SL orders placed automatically."
                )
                await tg_bot.notify(msg)

        except Exception as e:
            logger.error("Fill check error: %s", e, exc_info=True)
            # Notify user of critical failure
            await tg_bot.notify(
                f"⚠️ <b>CRITICAL: Fill Check Failed</b>\n\n"
                f"Error: <code>{str(e)}</code>\n\n"
                f"Positions may be unprotected. Please check /status immediately."
            )
        finally:
            fill_lock.release()


async def order_update_loop(trade_mgr: TradeManager, tg_bot: MomathiTelegramBot):
    """Background loop: update pending orders aligned to each trade's candle close."""
    logger.info("Background order update loop started")

    while runtime["running"]:
        if trade_mgr.active_trades:
            tfs = {int(t.get("exec_tf", 5)) for t in trade_mgr.active_trades if not t.get("filled")}
            if not tfs:
                # All trades are filled, no pending orders to update
                await asyncio.sleep(30)
                continue
            min_tf = min(tfs)
        else:
            min_tf = 5

        interval = min_tf * 60
        now = time.time()
        sleep_time = interval - (now % interval)
        await asyncio.sleep(sleep_time + 30)  # Wait 30s after candle boundary for more stable data

        if not runtime["running"]:
            break

        if not trade_mgr.active_trades:
            continue

        # Determine which timeframes' candles just closed.
        # Since we woke up ~30s after a candle boundary, any TF whose candle
        # duration evenly divides into the current timestamp just had a close.
        now2 = time.time()
        closed_tfs = set()
        for t in trade_mgr.active_trades:
            if t.get("filled"):
                continue
            tf_sec = int(t.get("exec_tf", 5)) * 60
            # We are 30s past boundary: check if we're within the first 60s of a new candle
            remainder = now2 % tf_sec
            if remainder < 60:  # generous window since we woke up aligned to boundary
                closed_tfs.add(t.get("exec_tf", "5"))

        if not closed_tfs:
            logger.debug("No candle boundary hit — skipping update cycle")
            continue

        logger.info(
            "Candle closed for TF(s): %s — running order update + trailing SL check...",
            ", ".join(f"{tf}m" for tf in sorted(closed_tfs)),
        )
        try:
            loop = asyncio.get_running_loop()
            updates = await loop.run_in_executor(None, trade_mgr.update_pending_orders, closed_tfs)
            for u in updates:
                # Differentiate notification: entry update vs TP/SL trailing
                is_tpsl_update = abs(u.get('old_entry', 0) - u.get('new_entry', 0)) < 0.01
                size_note = ""
                if abs(u.get('new_size', 0) - u.get('old_size', 0)) > 0.0001:
                    size_note = f"\n📦 Size: {u['old_size']:.6f} → <b>{u['new_size']:.6f}</b>"
                
                if is_tpsl_update:
                    msg = (
                        f"🔄 <b>TP/SL Updated — {u['coin']} {u['direction']}</b>\n\n"
                        f"🛑 SL: {u['old_sl']:.2f} → <b>{u['new_sl']:.2f}</b>\n"
                        f"🎯 TP: {u['old_tp'] if 'old_tp' in u else 'N/A'} → <b>{u['new_tp']:.2f}</b>"
                        f"{size_note}"
                    )
                else:
                    msg = (
                        f"🔄 <b>Orders Updated — {u['coin']} {u['direction']}</b>\n\n"
                        f"📍 Entry: {u['old_entry']:.2f} → <b>{u['new_entry']:.2f}</b>\n"
                        f"🛑 SL: {u['old_sl']:.2f} → <b>{u['new_sl']:.2f}</b>\n"
                        f"🎯 TP: <b>{u['new_tp']:.2f}</b>"
                        f"{size_note}"
                    )
                await tg_bot.notify(msg)

        except Exception as e:
            logger.error("Order update error: %s", e, exc_info=True)
            # Notify user of critical failure
            await tg_bot.notify(
                f"⚠️ <b>CRITICAL: Order Update Failed</b>\n\n"
                f"Error: <code>{str(e)}</code>\n\n"
                f"Pending orders may be stale. Please check /status."
            )


def _derive_state_from_scan(scan_result: dict, token: str) -> str:
    """
    Map the scan_regime output for a single token into one of:
    CLEAN_LONG, CLEAN_SHORT, TANGLED, INSUFFICIENT_DATA.
    
    Uses the existing scan_result structure (long_bias/short_bias/tangled/errors lists).
    """
    # Check if token is in errors (insufficient data)
    for err in scan_result.get("errors", []):
        if err.get("coin") == token:
            return "INSUFFICIENT_DATA"
    
    # Check if token is in long_bias
    for item in scan_result.get("long_bias", []):
        if item.get("coin") == token:
            return "CLEAN_LONG"
    
    # Check if token is in short_bias
    for item in scan_result.get("short_bias", []):
        if item.get("coin") == token:
            return "CLEAN_SHORT"
    
    # Check if token is in tangled
    if token in scan_result.get("tangled", []):
        return "TANGLED"
    
    # Fallback: token not found in any category
    return "INSUFFICIENT_DATA"


async def regime_watcher_loop(trade_mgr: TradeManager, telegram_bot: MomathiTelegramBot):
    """Background loop: monitor 1H regime changes and send smart alerts."""
    if not REGIME_WATCHER_ENABLED:
        logger.info("Regime watcher disabled via config")
        return

    logger.info(
        f"Regime watcher started: interval={REGIME_WATCHER_INTERVAL_SECONDS}s, "
        f"confirmation_cycles={REGIME_CONFIRMATION_CYCLES}, "
        f"cooldown_hours={REGIME_ALERT_COOLDOWN_HOURS}"
    )

    while runtime["running"]:
        try:
            state = load_regime_state()
            scan_result = trade_mgr.scan_regime()
            now = datetime.now(timezone.utc)

            # Collect all alerts for this cycle
            alerts_to_send = []

            for token in SCAN_WATCHLIST:
                new_state = _derive_state_from_scan(scan_result, token)

                state = update_token_state(state, token, new_state, now)

                should, alert_type = should_alert(
                    state, token,
                    confirmation_cycles=REGIME_CONFIRMATION_CYCLES,
                    cooldown_hours=REGIME_ALERT_COOLDOWN_HOURS,
                    alert_on_enter=REGIME_ALERT_ON_ENTER_CLEAN,
                    alert_on_leave=REGIME_ALERT_ON_LEAVE_CLEAN,
                    now_utc=now,
                )

                if should:
                    alerts_to_send.append({
                        "token": token,
                        "alert_type": alert_type,
                        "state": state[token],
                    })
                    state = mark_alerted(state, token, now)

            # Send consolidated alert if any tokens triggered
            if alerts_to_send:
                await telegram_bot.send_consolidated_regime_alert(alerts_to_send)

            save_regime_state(state)

        except Exception as e:
            logger.exception("regime_watcher_loop error (continuing)")
            # Notify user of regime watcher failure
            await telegram_bot.notify(
                f"⚠️ <b>Regime Watcher Error</b>\n\n"
                f"Error: <code>{str(e)}</code>\n\n"
                f"Regime monitoring temporarily paused."
            )

        # Respect bot shutdown
        for _ in range(REGIME_WATCHER_INTERVAL_SECONDS):
            if not runtime["running"]:
                break
            await asyncio.sleep(1)


async def post_init(app):
    """Called after the Telegram app is initialized — set bot commands & start bg task."""
    tg_bot = app.bot_data.get("momathi_bot")
    trade_mgr = app.bot_data.get("trade_mgr")

    if tg_bot:
        await tg_bot.set_commands()

    if tg_bot and trade_mgr:
        # ═══════════════════════════════════════════════════════════
        # STARTUP RECONCILIATION: Sync local state with Paradex
        # ═══════════════════════════════════════════════════════════
        logger.info("Starting reconciliation with Paradex...")
        try:
            loop = asyncio.get_running_loop()
            reconciliation_result = await loop.run_in_executor(
                None, trade_mgr.reconcile_with_exchange
            )
            
            if reconciliation_result["changes_made"]:
                logger.info(
                    "Reconciliation complete: %d trades removed, %d orphaned positions found",
                    reconciliation_result["trades_removed"],
                    reconciliation_result["orphaned_positions"],
                )
                
                # Notify user of reconciliation results
                msg = (
                    f"🔄 <b>Startup Reconciliation Complete</b>\n\n"
                    f"✅ Trades loaded from disk: <b>{len(trade_mgr.active_trades)}</b>\n"
                    f"🗑️ Stale trades removed: <b>{reconciliation_result['trades_removed']}</b>\n"
                )
                if reconciliation_result["orphaned_positions"] > 0:
                    msg += f"⚠️ Orphaned positions found: <b>{reconciliation_result['orphaned_positions']}</b>\n\n"
                    msg += "Use /close_all to close any unexpected positions."
                else:
                    msg += "✅ No orphaned positions detected."
                
                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode="HTML",
                )
            else:
                logger.info("Reconciliation complete: state already synchronized")
                
        except Exception as e:
            logger.error("Reconciliation failed: %s", e, exc_info=True)
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"⚠️ <b>Reconciliation Warning</b>\n\n"
                    f"Failed to sync with Paradex: <code>{str(e)}</code>\n\n"
                    f"Please verify /status manually."
                ),
                parse_mode="HTML",
            )
        
        # Start background loops
        asyncio.create_task(fill_check_loop(trade_mgr, tg_bot))
        asyncio.create_task(order_update_loop(trade_mgr, tg_bot))
        if REGIME_WATCHER_ENABLED:
            asyncio.create_task(regime_watcher_loop(trade_mgr, tg_bot))

    chat_id = TELEGRAM_CHAT_ID
    if chat_id:
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                        "🍅 <b>Momathi Bot Started!</b>\n\n"
                f"💰 Risk: <b>${runtime['risk_usd']:.2f}</b>\n"
                f"📊 Strategy: EMA 8/30 (5m or 15m)\n"
                f"🔄 Auto-update: every 5m candle (sync)\n"
                f"⚡ Fill check: every 60s\n\n"
                "Trade: /<b>coin</b> <b>direction</b> <b>timeframe</b>\n"
                "Example: /btc long 5\n\n"
                "Send /start for command list."
            ),
            parse_mode="HTML",
        )


def main():
    """Entry point — initialize everything and start the bot."""
    print(
        r"""
  _____                    _   _     _ 
 |_   _|__  _ __ ___   __ | |_| |__ (_)
   | |/ _ \| '_ ` _ \ / _`| __| '_ \| |
   | | (_) | | | | | | (_| | |_| | | | |
   |_|\___/|_| |_| |_|\__,_|\__|_| |_|_|
                                         
    Paradex EMA Trading Bot
    """
    )

    try:
        validate()
    except ConfigError as e:
        logger.error(str(e))
        logger.error("Please fill in your .env file and try again.")
        sys.exit(1)

    logger.info("Initializing Paradex client...")
    paradex_client = ParadexClient()

    logger.info("Initializing Trade Manager...")
    trade_mgr = TradeManager(paradex_client)

    logger.info("Building Telegram bot...")
    tg_bot = MomathiTelegramBot(trade_mgr)
    app = tg_bot.build()

    app.bot_data["momathi_bot"] = tg_bot
    app.bot_data["trade_mgr"] = trade_mgr
    app.post_init = post_init

    logger.info("Starting Momathi bot — polling Telegram...")
    app.run_polling(drop_pending_updates=True)

    logger.info("Momathi bot stopped.")


if __name__ == "__main__":
    main()
