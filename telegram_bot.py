"""
Momathi Bot — Telegram Bot
All Telegram commands and notification system.
"""
import logging
import os
import signal
import asyncio
from functools import wraps

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
from strategy import validate_signal
from trade_manager import TradeManager

logger = logging.getLogger("momathi.telegram")


def auth(func):
    """Decorator to restrict commands to the authorized chat only."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Handle both (update, context) and (self, update, context)
        if len(args) >= 2 and isinstance(args[-2], Update):
            update = args[-2]
        elif len(args) >= 1 and isinstance(args[0], Update):
            update = args[0]
        else:
            return await func(*args, **kwargs)
        chat_id = str(update.effective_chat.id)
        allowed = config.TELEGRAM_CHAT_ID
        if allowed and chat_id != allowed:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(*args, **kwargs)
    return wrapper


class MomathiTelegramBot:
    """Telegram interface for Momathi trading bot."""

    def __init__(self, trade_manager: TradeManager):
        self.tm = trade_manager
        self.app: Application = None

    def build(self) -> Application:
        """Build the Telegram application with all handlers."""
        self.app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

        handlers = [
            ("start", self.cmd_start),
            ("status", self.cmd_status),
            ("balance", self.cmd_balance),
            ("pnl", self.cmd_pnl),
            ("set_risk", self.cmd_set_risk),
            ("get_risk", self.cmd_get_risk),
            ("close_all", self.cmd_close_all),
            ("stop_bot", self.cmd_stop_bot),
        ]
        for name, callback in handlers:
            self.app.add_handler(CommandHandler(name, callback))

        # Generic handler for /<coin> <direction> <tf> commands
        self.app.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(r'^/') & ~filters.COMMAND,
            self.cmd_trade
        ))
        # Fallback: catch unknown /commands as potential trade commands
        self.app.add_handler(MessageHandler(
            filters.COMMAND,
            self.cmd_trade_fallback
        ))

        # Global error handler
        self.app.add_error_handler(self._error_handler)

        return self.app

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Log errors and notify the user."""
        logger.error("Telegram error: %s", context.error, exc_info=context.error)
        if update and hasattr(update, "effective_chat") and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"⚠️ An error occurred: {context.error}",
                )
            except Exception:
                pass

    async def set_commands(self):
        """Register bot commands in Telegram menu."""
        commands = [
            BotCommand("start", "Welcome & command list"),
            BotCommand("status", "View open positions & orders"),
            BotCommand("balance", "Account balance"),
            BotCommand("pnl", "Unrealized PnL"),
            BotCommand("set_risk", "Set risk: /set_risk 10"),
            BotCommand("get_risk", "Show current risk"),
            BotCommand("close_all", "Close all positions & orders"),
            BotCommand("stop_bot", "Shutdown the bot"),
        ]
        await self.app.bot.set_my_commands(commands)

    # ── Notification helper ──────────────────────────────────────

    async def notify(self, text: str):
        """Send a notification message to the authorized chat."""
        if self.app and config.TELEGRAM_CHAT_ID:
            await self.app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )

    # ── Commands ─────────────────────────────────────────────────

    @auth
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            "🍅 <b>Momathi Trading Bot</b>\n\n"
            "📋 <b>Trade Commands:</b>\n"
            "/<b>coin</b> <b>direction</b> <b>timeframe</b>\n"
            "Examples:\n"
            "  /btc long 5  — BTC long on 5m\n"
            "  /near short 15  — NEAR short on 15m\n\n"
            "📋 <b>Other Commands:</b>\n"
            "/status — Open positions &amp; orders\n"
            "/balance — Account balance\n"
            "/pnl — Unrealized PnL\n"
            "/set_risk [amount] — Set USD risk per trade\n"
            "/get_risk — Show current risk\n"
            "/close_all — Close everything\n"
            "/stop_bot — Shutdown bot\n\n"
            f"💰 Risk: <b>${config.runtime['risk_usd']}</b>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    def _parse_trade_command(self, text: str) -> tuple:
        """
        Parse trade command: /<coin> <direction> <timeframe>
        Returns (coin, direction, exec_tf) or raises ValueError.
        """
        parts = text.strip().split()
        if len(parts) < 3:
            raise ValueError("Not enough arguments")

        coin = parts[0].lstrip("/").upper()
        direction = parts[1].upper()
        tf = parts[2]

        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"Invalid direction: {direction}")
        if tf not in ("5", "15"):
            raise ValueError(f"Invalid timeframe: {tf}")

        return coin, direction, tf

    @auth
    async def cmd_trade_fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Catch unknown /commands and try to parse as trade commands."""
        await self._handle_trade(update, context)

    @auth
    async def cmd_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle generic trade messages."""
        await self._handle_trade(update, context)

    async def _handle_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /<coin> <direction> <tf> trade commands."""
        text = update.message.text
        try:
            coin, direction, exec_tf = self._parse_trade_command(text)
        except ValueError:
            await update.message.reply_text(
                "⚠️ Usage: /<b>coin</b> <b>direction</b> <b>timeframe</b>\n"
                "Examples:\n"
                "  /btc long 5\n"
                "  /near short 15",
                parse_mode="HTML",
            )
            return

        tf_label = "5m" if exec_tf == "5" else "15m"
        await update.message.reply_text(
            f"🔍 Validating {direction} signal for {coin} on {tf_label}..."
        )

        # Run validation in thread to avoid blocking
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, validate_signal, direction, coin, exec_tf
        )

        if not result["valid"]:
            await update.message.reply_text(result["reason"])
            return

        # Signal is valid — execute trade
        levels = result["levels"]
        await update.message.reply_text(
            f"✅ Signal VALID — {direction} aligns with {result['trend']} trend\n\n"
            f"⏱️ Entry TF: {tf_label}\n"
            f"📊 EMA8={result['ema8']} | EMA30={result['ema30']}\n"
            f"📍 Entry: {levels['entry']}\n"
            f"🛑 SL: {levels['sl']}\n"
            f"🎯 TP: {levels['tp']}\n"
            f"💰 Risk: ${float(config.runtime['risk_usd']):.2f}\n\n"
            f"⏳ Placing orders..."
        )

        try:
            trade = await loop.run_in_executor(
                None, self.tm.execute_trade, coin, direction, levels, exec_tf
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Order failed: {e}")
            logger.error("Trade execution error: %s", e, exc_info=True)
            return

        # Format result
        entry_status = "ok" if trade.get("entry_oid") else "error"
        sl_status = "ok" if trade.get("sl_oid") else "pending fill"
        tp_status = "ok" if trade.get("tp_oid") else "pending fill"

        msg = (
            f"🍅 <b>Trade Placed — {coin} {direction} ({tf_label})</b>\n\n"
            f"📍 Entry: <b>{levels['entry']}</b> (limit)\n"
            f"🛑 SL: <b>{levels['sl']}</b>\n"
            f"🎯 TP: <b>{levels['tp']}</b>\n"
            f"📦 Size: <b>{float(trade['size']):.6f}</b>\n"
            f"💰 Risk: <b>${float(trade['risk_usd']):.2f}</b>\n\n"
            f"Entry order: {entry_status}\n"
            f"SL order: {sl_status}\n"
            f"TP order: {tp_status}"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    @auth
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(None, self.tm.get_status)

        positions = status["positions"]
        orders = status["open_orders"]

        msg = "📊 <b>Status</b>\n\n"

        if positions:
            msg += "<b>Positions:</b>\n"
            for p in positions:
                side = "LONG" if p["size"] > 0 else "SHORT"
                msg += (
                    f"• {p['coin']} {side} | Size: {abs(p['size']):.4f} | "
                    f"Entry: {p['entry_px']:.2f} | PnL: ${p['unrealized_pnl']:.2f}\n"
                )
        else:
            msg += "No open positions\n"

        msg += f"\n<b>Open Orders:</b> {len(orders)}\n"
        for o in orders[:10]:  # limit display
            side = "BUY" if o.get("side", "").lower() == "b" else "SELL"
            msg += f"• {o.get('coin', '?')} {side} | Sz: {o.get('sz', '?')} | Px: {o.get('limitPx', '?')}\n"

        msg += f"\n<b>Tracked Trades:</b> {status['tracked_trades']}"
        await update.message.reply_text(msg, parse_mode="HTML")

    @auth
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loop = asyncio.get_running_loop()
        bal = await loop.run_in_executor(None, self.tm.client.get_balance)
        msg = (
            f"💰 <b>Account Balance</b>\n\n"
            f"Account Value: <b>${bal['account_value']:.2f}</b>\n"
            f"Margin Used: <b>${bal['total_margin_used']:.2f}</b>\n"
            f"Withdrawable: <b>${bal['withdrawable']:.2f}</b>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    @auth
    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        loop = asyncio.get_running_loop()
        pnl_list = await loop.run_in_executor(None, self.tm.get_pnl)

        if not pnl_list:
            await update.message.reply_text("📈 No open positions.")
            return

        msg = "📈 <b>PnL</b>\n\n"
        total = 0
        for p in pnl_list:
            side = "LONG" if p["size"] > 0 else "SHORT"
            emoji = "🟢" if p["unrealized_pnl"] >= 0 else "🔴"
            msg += (
                f"{emoji} {p['coin']} {side} | Entry: {p['entry_px']:.2f} | "
                f"PnL: <b>${p['unrealized_pnl']:.2f}</b>\n"
            )
            total += p["unrealized_pnl"]

        msg += f"\n<b>Total Unrealized PnL: ${total:.2f}</b>"
        await update.message.reply_text(msg, parse_mode="HTML")

    @auth
    async def cmd_set_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("⚠️ Usage: /set_risk [amount]\nExample: /set_risk 10")
            return
        try:
            amount = float(context.args[0])
            if amount <= 0:
                raise ValueError
            config.runtime["risk_usd"] = amount
            await update.message.reply_text(f"✅ Risk set to <b>${amount:.2f}</b> per trade.", parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("⚠️ Please provide a valid positive number.")

    @auth
    async def cmd_get_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"💰 Current risk: <b>${config.runtime['risk_usd']:.2f}</b> per trade.",
            parse_mode="HTML",
        )



    @auth
    async def cmd_close_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Closing all positions and cancelling orders...")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self.tm.close_all)
            await update.message.reply_text(
                f"✅ Closed <b>{result['closed']}</b> position(s). All orders cancelled.",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error closing: {e}")

    @auth
    async def cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🛑 Shutting down Momathi bot... Goodbye! 🍅")
        config.runtime["running"] = False
        # Stop the application gracefully
        asyncio.get_event_loop().call_later(1, lambda: os.kill(os.getpid(), signal.SIGINT))
