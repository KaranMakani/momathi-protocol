"""
Momathi Bot — Main Entry Point
Starts the Paradex client, trade manager, and Telegram bot.
Runs a background job to auto-update pending orders every minute.
"""
import asyncio
import logging
import sys
import time

import config
from paradex_client import ParadexClient
from trade_manager import TradeManager
from telegram_bot import MomathiTelegramBot

# ── Logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("momathi.log"),
    ],
)
logger = logging.getLogger("momathi.main")

# ── Update interval (seconds) ───────────────────────────────────
ORDER_UPDATE_INTERVAL = 60  # check every 1 minute


def validate_config():
    """Ensure all required env vars are set."""
    missing = []
    if not config.PARADEX_L1_ADDRESS:
        missing.append("PARADEX_L1_ADDRESS")
    if not config.PARADEX_PRIVATE_KEY:
        missing.append("PARADEX_PRIVATE_KEY")
    if not config.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error("Missing environment variables: %s", ", ".join(missing))
        logger.error("Please fill in your .env file and try again.")
        sys.exit(1)


async def fill_check_loop(trade_mgr: TradeManager, tg_bot: MomathiTelegramBot):
    """Background loop: detect filled entries + manage pyramid every 60s."""
    logger.info("Fill check loop started (60s interval)")

    while config.runtime["running"]:
        await asyncio.sleep(60)

        if not config.runtime["running"]:
            break

        # Only run if there are tracked trades
        if not trade_mgr.active_trades:
            continue

        try:
            loop = asyncio.get_running_loop()

            # ── 1. Fill detection ────────────────────────────────────────
            unfilled = [t for t in trade_mgr.active_trades if not t.get("filled")]
            if unfilled:
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

            # ── 2. Pyramid check (Phase 1: arm / Phase 2: fire) ───────
            pyramid_events = await loop.run_in_executor(None, trade_mgr.check_pyramid)
            for ev in pyramid_events:
                if ev["type"] == "armed":
                    msg = (
                        f"🔺 <b>Pyramid Armed — {ev['coin']} {ev['direction']}</b>\n\n"
                        f"💰 1:1 RR Level: <b>{ev['pyramid_level']:.4f}</b>\n"
                        f"📈 Mark Price: <b>{ev['mark_price']:.4f}</b>\n\n"
                        f"Watching EMA30 → will add when EMA30 reaches original entry."
                    )
                elif ev["type"] == "fired":
                    cp = ev.get("current_price")
                    cp_str = f"{cp:.4f}" if cp else "N/A"
                    msg = (
                        f"🔥 <b>Pyramid Fired! — {ev['coin']} {ev['direction']}</b>\n\n"
                        f"➕ Added: <b>{ev['pyramid_size']:.6f}</b>\n"
                        f"📊 EMA30: <b>{ev['ema30']:.4f}</b>\n"
                        f"📈 Price: <b>{cp_str}</b>\n"
                        f"🛑 New SL: <b>{ev['new_sl']:.4f}</b> (EMA30 trailing ON)\n"
                        f"🎯 New TP: <b>{ev['new_tp']:.4f}</b> (squeezed 15%)"
                    )
                else:
                    continue
                await tg_bot.notify(msg)

        except Exception as e:
            logger.error("Fill/pyramid check error: %s", e, exc_info=True)


async def order_update_loop(trade_mgr: TradeManager, tg_bot: MomathiTelegramBot):
    """Background loop: update pending orders + trail SL aligned to each trade's candle close.

    Calculates the next relevant candle boundary across all active trades
    (e.g. if only 15m trades exist, sleeps until the next 15m boundary;
    if 5m trades exist too, wakes at the 5m boundaries).
    """
    logger.info("Background order update loop started")

    while config.runtime["running"]:
        # Determine the smallest candle interval among active trades
        # so we wake up at the right time.
        if trade_mgr.active_trades:
            tfs = {int(t.get("exec_tf", 5)) for t in trade_mgr.active_trades}
            min_tf = min(tfs) if tfs else 5
        else:
            min_tf = 5

        interval = min_tf * 60  # seconds

        # Seconds until the next candle boundary for the smallest TF
        now = time.time()
        sleep_time = interval - (now % interval)

        # Wake up 3 seconds after the candle closes for fresh data
        await asyncio.sleep(sleep_time + 3)

        if not config.runtime["running"]:
            break

        if not trade_mgr.active_trades:
            continue

        # Determine which timeframes just closed a candle
        now2 = time.time()
        closed_tfs = set()
        for t in trade_mgr.active_trades:
            tf_sec = int(t.get("exec_tf", 5)) * 60
            # A candle just closed if we're within ~10s past a boundary
            if (now2 % tf_sec) < 10:
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

            # ── 1. Update unfilled entry orders with latest EMA levels ───
            updates = await loop.run_in_executor(None, trade_mgr.update_pending_orders, closed_tfs)
            for u in updates:
                size_note = ""
                if abs(u.get('new_size', 0) - u.get('old_size', 0)) > 0.0001:
                    size_note = f"\n📦 Size: {u['old_size']:.6f} → <b>{u['new_size']:.6f}</b>"
                msg = (
                    f"🔄 <b>Orders Updated — {u['coin']} {u['direction']}</b>\n\n"
                    f"📍 Entry: {u['old_entry']:.2f} → <b>{u['new_entry']:.2f}</b>\n"
                    f"🛑 SL: {u['old_sl']:.2f} → <b>{u['new_sl']:.2f}</b>\n"
                    f"🎯 TP: <b>{u['new_tp']:.2f}</b>"
                    f"{size_note}"
                )
                await tg_bot.notify(msg)

            # ── 2. Trail SL at EMA30 for pyramided trades ─────────────
            trailing_updates = await loop.run_in_executor(None, trade_mgr.update_trailing_sl, closed_tfs)
            for t in trailing_updates:
                msg = (
                    f"📈 <b>Trailing SL Moved — {t['coin']} {t['direction']}</b>\n\n"
                    f"🛑 SL: {t['old_sl']:.4f} → <b>{t['new_sl']:.4f}</b>\n"
                    f"📊 EMA30: <b>{t['ema30']:.4f}</b>"
                )
                await tg_bot.notify(msg)

        except Exception as e:
            logger.error("Order update error: %s", e, exc_info=True)


async def post_init(app):
    """Called after the Telegram app is initialized — set bot commands & start bg task."""
    tg_bot = app.bot_data.get("momathi_bot")
    trade_mgr = app.bot_data.get("trade_mgr")

    if tg_bot:
        await tg_bot.set_commands()

    # Start background loops
    if tg_bot and trade_mgr:
        asyncio.create_task(fill_check_loop(trade_mgr, tg_bot))    # 60s fill detection
        asyncio.create_task(order_update_loop(trade_mgr, tg_bot))  # 5m EMA level updates

    chat_id = config.TELEGRAM_CHAT_ID
    if chat_id:
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                        "🍅 <b>Momathi Bot Started!</b>\n\n"
                f"💰 Risk: <b>${config.runtime['risk_usd']:.2f}</b>\n"
                f"📊 Strategy: EMA 8/30 (5m or 15m)\n"
                f"🔺 Pyramid: {'ON' if config.PYRAMID_ENABLED else 'OFF'} "
                f"(add {int(config.PYRAMID_ADD_PCT*100)}% @ EMA30→entry, trail SL)\n"
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

    validate_config()

    logger.info("Initializing Paradex client...")
    paradex_client = ParadexClient()

    logger.info("Initializing Trade Manager...")
    trade_mgr = TradeManager(paradex_client)

    # Set authenticated client for strategy candle/BBO fetching
    from strategy import set_paradex_client
    set_paradex_client(paradex_client)

    logger.info("Building Telegram bot...")
    tg_bot = MomathiTelegramBot(trade_mgr)
    app = tg_bot.build()

    # Store references for post_init
    app.bot_data["momathi_bot"] = tg_bot
    app.bot_data["trade_mgr"] = trade_mgr

    # Register post-init callback
    app.post_init = post_init

    logger.info("Starting Momathi bot — polling Telegram...")
    app.run_polling(drop_pending_updates=True)

    logger.info("Momathi bot stopped.")


if __name__ == "__main__":
    main()
