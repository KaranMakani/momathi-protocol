"""
Momathi Bot — Trade Manager
Orchestrates trade execution, position sizing, and PnL tracking.
"""
import json
import logging
import os
from datetime import datetime

import config
from paradex_client import ParadexClient
from strategy import validate_signal, get_ema30, get_mark_price

logger = logging.getLogger("momathi.trade_mgr")

TRADES_FILE = "active_trades.json"


class TradeManager:
    """Manages trade lifecycle: sizing, entry, TP/SL, and position tracking."""

    def __init__(self, client: ParadexClient):
        self.client = client
        self.active_trades: list[dict] = []
        self._load_trades()

    # ── Persistence ──────────────────────────────────────────────

    def _save_trades(self):
        """Persist active_trades to disk so the bot survives restarts."""
        try:
            with open(TRADES_FILE, "w") as f:
                json.dump(self.active_trades, f, indent=2, default=str)
            logger.debug("Saved %d active trade(s) to %s", len(self.active_trades), TRADES_FILE)
        except Exception as e:
            logger.error("Failed to save trades: %s", e)

    def _load_trades(self):
        """Load active_trades from disk on startup (if the file exists)."""
        if not os.path.exists(TRADES_FILE):
            return
        try:
            with open(TRADES_FILE) as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                self.active_trades = data
                logger.info(
                    "Restored %d active trade(s) from %s",
                    len(self.active_trades), TRADES_FILE,
                )
            else:
                logger.info("No active trades found in %s", TRADES_FILE)
        except Exception as e:
            logger.error("Failed to load trades from disk: %s", e)

    # ── Position validation ────────────────────────────────────

    def _position_still_open(self, coin: str) -> bool:
        """Check if we actually have an open position for this coin on the exchange."""
        try:
            positions = self.client.get_positions()
            return any(p["coin"] == coin for p in positions)
        except Exception as e:
            logger.warning("_position_still_open: failed to fetch positions for %s: %s", coin, e)
            return True  # assume open to avoid accidentally cancelling nothing

    def _cleanup_closed_trade(self, trade: dict):
        """Remove a trade whose position no longer exists (SL/TP hit on exchange)."""
        coin = trade["coin"]
        direction = trade["direction"]
        if trade in self.active_trades:
            self.active_trades.remove(trade)
            self._save_trades()
            logger.info(
                "🗑️ Trade %s %s removed — position no longer exists (SL/TP hit on exchange)",
                direction, coin,
            )

    # ── Position sizing ──────────────────────────────────────────

    def calculate_size(self, entry: float, sl: float, risk_usd: float = None) -> float:
        """
        Calculate position size based on fixed USD risk.
        size = risk_usd / |entry - sl|
        """
        risk = risk_usd or config.runtime["risk_usd"]
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            return 0.0
        size = risk / risk_per_unit
        return size

    # ── Execute trade ────────────────────────────────────────────

    def execute_trade(self, coin: str, direction: str, levels: dict, exec_tf: str = "5") -> dict:
        """
        Execute a trade:
        1. Calculate size from risk
        2. Place limit entry order
        3. TP/SL will be placed by the background loop once filled (Paradex requirement)
        """
        entry = levels["entry"]
        sl = levels["sl"]
        tp = levels["tp"]
        risk_usd = config.runtime["risk_usd"]

        size = self.calculate_size(entry, sl, risk_usd)
        is_buy = direction.upper() == "LONG"

        logger.info(
            "Executing %s %s | Entry=%.2f SL=%.2f TP=%.2f | Size=%.6f Risk=$%.2f",
            direction, coin, entry, sl, tp, size, risk_usd,
        )

        # 1. Place limit entry order
        entry_result = self.client.place_limit_order(coin, is_buy, size, entry)

        entry_oid = None
        filled_immediately = False
        if entry_result.get("status") == "ok":
            statuses = entry_result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                s = statuses[0]
                if "resting" in s:
                    entry_oid = s["resting"]["oid"]
                elif "filled" in s:
                    entry_oid = s["filled"]["oid"]
                    filled_immediately = True

        # ── Pyramid fields ──────────────────────────────────────
        # 1:1 RR level: the price target that ARMS the pyramid
        risk = abs(entry - sl)
        pyramid_level = round(entry + risk, 6) if is_buy else round(entry - risk, 6)
        pyramid_size  = round(size * config.PYRAMID_ADD_PCT, 6)

        trade = {
            "coin": coin,
            "direction": direction.upper(),
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "size": size,
            "risk_usd": risk_usd,
            "is_buy": is_buy,
            "exec_tf": exec_tf,
            "entry_oid": entry_oid,
            "filled": filled_immediately,
            "sl_oid": None,
            "tp_oid": None,
            "timestamp": datetime.now().isoformat(),
            # ── Pyramid state ───────────────────────────────────
            "pyramid_level":  pyramid_level,  # price where 1:1 RR is hit
            "pyramid_size":   pyramid_size,   # size of the add-on order
            "pyramid_armed":  False,           # True once 1:1 RR is crossed
            "pyramid_done":   False,           # True after add is placed (one-time)
            "trailing_sl":    False,           # True after pyramid fires (SL trails EMA30)
        }

        # If filled immediately, place TP/SL right now
        if filled_immediately:
            logger.info("Entry filled immediately, placing TP/SL triggers")
            self._place_tpsl(trade)

        self.active_trades.append(trade)
        self._save_trades()
        logger.info(
            "Pyramid config | Level(1:1 RR)=%.4f | AddSize=%.6f | Trigger=%.3f%%",
            pyramid_level, pyramid_size, config.PYRAMID_TRIGGER_PCT * 100,
        )
        return trade

    def _place_tpsl(self, trade: dict):
        """Helper to place TP and SL trigger orders once a position exists."""
        coin = trade["coin"]
        is_buy = trade["is_buy"]
        size = trade["size"]
        sl = trade["sl"]
        tp = trade["tp"]

        # Place SL
        sl_res = self.client.place_trigger_order(
            coin, is_buy=not is_buy, size=size, trigger_px=sl, tpsl="sl", reduce_only=True
        )
        trade["sl_oid"] = sl_res.get("oid")

        # Place TP
        tp_res = self.client.place_trigger_order(
            coin, is_buy=not is_buy, size=size, trigger_px=tp, tpsl="tp", reduce_only=True
        )
        trade["tp_oid"] = tp_res.get("oid")
        
        logger.info("Placed TP/SL for %s: SL_OID=%s, TP_OID=%s", coin, trade["sl_oid"], trade["tp_oid"])

    # ── Fast fill check (60s loop) ─────────────────────────────

    def check_fills(self) -> list[dict]:
        """
        Lightweight fill detector: check if any pending entry orders have
        been filled, and place TP/SL immediately.  Also cleans up filled
        trades whose positions have since closed (TP/SL hit by the exchange).
        Called every ~60s from a dedicated background loop.

        Returns a list of newly-filled trade summaries.
        """
        filled_trades = []
        needs_save = False

        # ── 0. Cleanup: remove filled trades with no open position ──────
        # This handles the case where TP or SL was triggered on the exchange
        # but we never received an explicit notification.
        try:
            all_positions = self.client.get_positions()
            open_coins = {p["coin"] for p in all_positions}
            closed = [
                t for t in self.active_trades
                if t.get("filled") and t["coin"] not in open_coins
            ]
            for t in closed:
                logger.info(
                    "✅ Closed trade detected — %s %s removed (TP/SL hit or manually closed)",
                    t["direction"], t["coin"],
                )
                self.active_trades.remove(t)
                needs_save = True
        except Exception as e:
            logger.warning("Closed-trade cleanup error: %s", e)

        # ── 1. Fill detection for pending entries ───────────────────────
        open_orders = self.client.get_open_orders()

        for trade in self.active_trades[:]:
            if trade.get("filled"):
                continue

            coin = trade["coin"]
            direction = trade["direction"]
            entry_oid = trade.get("entry_oid")

            # Guard: if we never received a valid OID, we cannot track this order.
            # Remove it immediately to prevent a silent re-entry on the next loop.
            if not entry_oid:
                logger.warning(
                    "Fill check: %s %s has no entry OID — removing untrackable trade",
                    direction, coin,
                )
                self.active_trades.remove(trade)
                needs_save = True
                continue

            entry_still_open = any(
                o.get("oid") == entry_oid for o in open_orders
            )

            if not entry_still_open:
                # Check if a position now exists (order was filled)
                positions = self.client.get_positions()
                has_position = any(p["coin"] == coin for p in positions)

                if has_position:
                    trade["filled"] = True
                    logger.info(
                        "Fill check: %s %s filled → placing TP/SL", direction, coin
                    )
                    self._place_tpsl(trade)
                    filled_trades.append({
                        "coin": coin,
                        "direction": direction,
                        "entry": trade["entry"],
                        "sl": trade["sl"],
                        "tp": trade["tp"],
                    })
                    needs_save = True
                else:
                    # Order was cancelled externally (e.g. /close_all) — remove from tracking
                    logger.info(
                        "Fill check: %s %s entry order gone (cancelled externally), removing",
                        direction, coin,
                    )
                    self.active_trades.remove(trade)
                    needs_save = True

        if needs_save:
            self._save_trades()

        return filled_trades

    # ── Auto-update pending orders ───────────────────────────────

    def update_pending_orders(self, closed_tfs: set = None) -> list[dict]:
        """
        Re-fetch EMAs and update unfilled limit entry + SL + TP orders
        to the latest 8 EMA / 15 EMA levels.

        Args:
            closed_tfs: Set of exec_tf values ("5", "15") whose candles just closed.
                        If None, processes all trades (backward-compatible).

        Returns a list of update summaries (one per updated trade).
        """
        updates = []

        for trade in self.active_trades[:]:
            # Skip trades whose candle hasn't just closed
            if closed_tfs is not None and trade.get("exec_tf", "5") not in closed_tfs:
                continue

            if trade.get("filled"):
                continue

            coin = trade["coin"]
            direction = trade["direction"]

            # Check if entry order has been filled
            open_orders = self.client.get_open_orders()
            entry_oid = trade.get("entry_oid")
            entry_still_open = any(
                o.get("oid") == entry_oid for o in open_orders
            ) if entry_oid else False

            if not entry_still_open:
                # Check if we have a position (order filled)
                positions = self.client.get_positions()
                has_position = any(p["coin"] == coin for p in positions)
                if has_position:
                    trade["filled"] = True
                    logger.info("Trade %s %s filled, placing TP/SL now", direction, coin)
                    self._place_tpsl(trade)
                    self._save_trades()
                    continue
                else:
                    # Order is gone AND no position — cancelled externally (or OID never set).
                    # NEVER fall through to re-place: that would create an autonomous entry.
                    logger.info(
                        "Trade %s %s entry order gone (no position, OID=%s) — removing",
                        direction, coin, entry_oid,
                    )
                    self.active_trades.remove(trade)
                    self._save_trades()
                    continue

            # Re-fetch EMAs using the trade's execution timeframe
            exec_tf = trade.get("exec_tf", "5")
            result = validate_signal(direction, coin, exec_tf)
            if not result.get("valid"):
                logger.info("Signal no longer valid for %s %s, keeping existing orders", direction, coin)
                continue

            new_levels = result["levels"]
            old_entry = float(trade["entry"])
            old_sl = float(trade["sl"])
            new_entry = float(new_levels["entry"])
            new_sl = float(new_levels["sl"])
            new_tp = float(new_levels["tp"])

            # Skip only if levels haven't changed beyond the exchange tick size.
            # Using tick-size instead of a fixed percentage ensures updates fire
            # correctly regardless of the asset's price (BTC at $77K vs DOGE at $0.20).
            tick = self.client.get_tick_size(coin)
            entry_delta = abs(new_entry - old_entry)
            sl_delta = abs(new_sl - old_sl)
            if entry_delta < tick and sl_delta < tick:
                logger.info(
                    "Levels unchanged for %s %s (delta: entry=%.4f sl=%.4f tick=%.4f), skipping",
                    direction, coin, entry_delta, sl_delta, tick,
                )
                continue

            logger.info(
                "Updating %s %s | Entry: %.2f->%.2f | SL: %.2f->%.2f | TP: %.2f->%.2f",
                direction, coin, old_entry, new_entry, old_sl, new_sl, float(trade["tp"]), new_tp,
            )

            # Cancel everything and re-place entry
            self.client.cancel_all_orders(coin)

            is_buy = trade["is_buy"]

            # Recalculate position size for the NEW levels to maintain risk_usd.
            # Without this, a widening EMA gap makes the old size risk MORE than
            # $10, and a narrowing gap risks less — breaking the fixed-risk model.
            new_size = self.calculate_size(new_entry, new_sl, config.runtime["risk_usd"])
            old_size = float(trade["size"])

            if abs(new_size - old_size) / old_size > 0.01:
                logger.info(
                    "Size recalculated for %s %s | %.6f -> %.6f (risk_per_unit: %.2f -> %.2f)",
                    direction, coin, old_size, new_size,
                    abs(old_entry - old_sl), abs(new_entry - new_sl),
                )

            # Re-place entry limit order with corrected size
            entry_result = self.client.place_limit_order(coin, is_buy, new_size, new_entry)
            new_entry_oid = None
            if entry_result.get("status") == "ok":
                statuses = entry_result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses:
                    s = statuses[0]
                    if "resting" in s:
                        new_entry_oid = s["resting"]["oid"]
                    elif "filled" in s:
                        new_entry_oid = s["filled"]["oid"]
                        trade["filled"] = True
                        self._place_tpsl(trade)

            # Note: We do NOT place TP/SL here while trade["filled"] is False (Paradex requirement)

            # If the new entry order came back with no OID, remove the trade rather than
            # leaving it OID-less — a None OID would cause yet another silent re-entry.
            if not new_entry_oid and not trade.get("filled"):
                logger.warning(
                    "Re-placed order for %s %s returned no OID — removing trade to prevent ghost re-entry",
                    direction, coin,
                )
                self.active_trades.remove(trade)
                self._save_trades()
                continue

            # Update trade record
            trade["entry"] = new_entry
            trade["sl"] = new_sl
            trade["tp"] = new_tp
            trade["size"] = new_size
            trade["entry_oid"] = new_entry_oid
            trade["last_updated"] = datetime.now().isoformat()

            # Re-calculate pyramid fields for the new size/levels
            risk_dist = abs(new_entry - new_sl)
            trade["pyramid_level"] = round(new_entry + risk_dist, 6) if is_buy else round(new_entry - risk_dist, 6)
            trade["pyramid_size"] = round(new_size * config.PYRAMID_ADD_PCT, 6)

            self._save_trades()

            updates.append({
                "coin": coin,
                "direction": direction,
                "old_entry": old_entry,
                "new_entry": new_entry,
                "old_sl": old_sl,
                "new_sl": new_sl,
                "new_tp": new_tp,
                "old_size": old_size,
                "new_size": new_size,
            })

        return updates

    # ── Pyramid ──────────────────────────────────────────────────

    def check_pyramid(self) -> list[dict]:
        """
        Called every 60 s from fill_check_loop.

        Phase 1 — ARM:  For filled trades whose pyramid is not yet armed,
                        check whether the current mark price has crossed
                        the 1:1 RR level.  If so, set pyramid_armed=True
                        and return an 'armed' event for Telegram.

        Phase 2 — FIRE: For armed (but not yet done) trades, fetch the
                        latest EMA30 on the execution TF.  If EMA30 is
                        within PYRAMID_TRIGGER_PCT of the original entry
                        price, call _fire_pyramid_add().

        Returns a list of event dicts for Telegram notification.
        """
        if not config.PYRAMID_ENABLED:
            return []

        events = []

        for trade in self.active_trades[:]:
            # Only consider filled trades that haven't been pyramided yet
            if not trade.get("filled") or trade.get("pyramid_done"):
                continue

            coin       = trade["coin"]
            direction  = trade["direction"]
            is_buy     = trade["is_buy"]
            exec_tf    = trade.get("exec_tf", "5")
            entry      = float(trade["entry"])

            # Verify position still exists before pyramid arm/fire
            if not self._position_still_open(coin):
                self._cleanup_closed_trade(trade)
                continue

            # ── Phase 1: ARM at 1:1 RR ────────────────────────────
            if not trade.get("pyramid_armed"):
                mark = get_mark_price(coin)
                if mark is None:
                    logger.warning("check_pyramid: could not get mark price for %s", coin)
                    continue

                lvl = float(trade["pyramid_level"])
                armed = (is_buy and mark >= lvl) or (not is_buy and mark <= lvl)

                if armed:
                    trade["pyramid_armed"] = True
                    self._save_trades()
                    logger.info(
                        "🔺 Pyramid ARMED — %s %s | mark=%.4f crossed 1:1 RR=%.4f",
                        direction, coin, mark, lvl,
                    )
                    events.append({
                        "type":          "armed",
                        "coin":          coin,
                        "direction":     direction,
                        "pyramid_level": lvl,
                        "mark_price":    mark,
                    })

            # ── Phase 2: FIRE when EMA30 reaches original entry ───
            else:
                ema30 = get_ema30(coin, exec_tf)
                if ema30 is None:
                    logger.warning("check_pyramid: could not get EMA30 for %s", coin)
                    continue

                proximity = abs(ema30 - entry) / entry

                logger.debug(
                    "Pyramid proximity check %s %s | EMA30=%.4f Entry=%.4f Prox=%.4f%%",
                    direction, coin, ema30, entry, proximity * 100,
                )

                if proximity < config.PYRAMID_TRIGGER_PCT:
                    logger.info(
                        "🔥 Pyramid TRIGGER — %s %s | EMA30=%.4f Entry=%.4f (%.3f%%)",
                        direction, coin, ema30, entry, proximity * 100,
                    )
                    result = self._fire_pyramid_add(trade, ema30)
                    if result:
                        events.append({
                            "type":      "fired",
                            "coin":      coin,
                            "direction": direction,
                            **result,
                        })

        return events

    def _fire_pyramid_add(self, trade: dict, ema30: float) -> dict | None:
        """
        Execute the pyramid add (ONE-TIME only — pyramid_done prevents re-entry):
        1. Cancel old SL/TP (they have the wrong size for the new position).
        2. Place market order for pyramid_size.
        3. Update total size in the trade dict.
        4. Place new SL at EMA30 ± buffer (for full position size).
        5. Squeeze TP 15% closer to current price.
        6. Enable trailing SL.

        Returns a result dict for Telegram notification, or None on failure.
        """
        coin         = trade["coin"]
        is_buy       = trade["is_buy"]
        pyramid_size = float(trade["pyramid_size"])
        old_size     = float(trade["size"])

        # Verify position still exists before placing any orders
        if not self._position_still_open(coin):
            self._cleanup_closed_trade(trade)
            return None

        # ── 1. Cancel old SL/TP BEFORE the market add ─────────────
        # Paradex auto-cancels trigger orders when position size changes,
        # but we cancel explicitly first to avoid ghost orders on the dashboard.
        old_sl_oid = trade.get("sl_oid")
        old_tp_oid = trade.get("tp_oid")
        if old_sl_oid:
            self.client.cancel_order(old_sl_oid)
            trade["sl_oid"] = None
        if old_tp_oid:
            self.client.cancel_order(old_tp_oid)
            trade["tp_oid"] = None

        # ── 2. Place market add ───────────────────────────────────
        add_result = self.client.place_market_order(coin, is_buy, pyramid_size)
        if add_result.get("status") != "ok":
            logger.error("Pyramid add FAILED for %s: %s", coin, add_result)
            # Re-place SL/TP at original size since add failed
            self._place_tpsl(trade)
            return None
        logger.info("Pyramid market add OK for %s | size=%.6f", coin, pyramid_size)

        # ── 3. Update total tracked size (so SL covers full position) ──
        total_size       = old_size + pyramid_size
        trade["size"]    = total_size

        # ── 4. Place new SL at EMA30 for the full position ────────
        buffer   = ema30 * config.PYRAMID_SL_BUFFER
        new_sl   = (ema30 - buffer) if is_buy else (ema30 + buffer)
        sl_res = self.client.place_trigger_order(
            coin,
            is_buy=not is_buy,
            size=total_size,
            trigger_px=new_sl,
            tpsl="sl",
            reduce_only=True,
        )
        trade["sl"]     = new_sl
        trade["sl_oid"] = sl_res.get("oid")
        logger.info("Pyramid SL placed for %s | new_sl=%.4f size=%.6f OID=%s",
                     coin, new_sl, total_size, trade["sl_oid"])

        # ── 5. Squeeze TP closer to current price ─────────────────
        new_tp = trade["tp"]   # default: unchanged (in case mark price fails)
        current_price = get_mark_price(coin)
        if current_price:
            original_tp    = float(trade["tp"])
            remaining_dist = abs(original_tp - current_price)
            squeeze        = remaining_dist * config.PYRAMID_TP_SQUEEZE
            new_tp  = original_tp - squeeze if is_buy else original_tp + squeeze

            tp_res = self.client.place_trigger_order(
                coin,
                is_buy=not is_buy,
                size=total_size,
                trigger_px=new_tp,
                tpsl="tp",
                reduce_only=True,
            )
            trade["tp"]     = new_tp
            trade["tp_oid"] = tp_res.get("oid")
            logger.info(
                "TP squeezed for %s: %.4f → %.4f (current=%.4f)",
                coin, original_tp, new_tp, current_price,
            )

        # ── 6. Enable trailing SL (ONE-TIME add done, now just trail) ──
        trade["pyramid_done"] = True
        trade["trailing_sl"]  = True
        self._save_trades()

        logger.info(
            "✅ Pyramid complete for %s %s | Added=%.6f | New SL=%.4f | New TP=%.4f | Trailing=ON",
            trade["direction"], coin, pyramid_size, new_sl, new_tp,
        )
        return {
            "pyramid_size":  pyramid_size,
            "new_sl":        new_sl,
            "new_tp":        new_tp,
            "current_price": current_price,
            "ema30":         ema30,
        }

    def _replace_sl(self, trade: dict, new_sl_px: float):
        """
        Cancel the existing SL trigger order and place a new one at new_sl_px.
        Covers the full current trade["size"] (call AFTER updating size).

        Verifies:
        1. Position still exists on the exchange (SL/TP not already hit).
        2. Old SL order still exists before cancelling (no ORDER_ID_NOT_FOUND).
        """
        coin   = trade["coin"]
        is_buy = trade["is_buy"]
        size   = float(trade["size"])  # uses updated total size

        # ── GUARD: Position must still exist ──────────────────────
        if not self._position_still_open(coin):
            logger.warning(
                "_replace_sl: %s position gone — SL/TP already hit, skipping replacement",
                coin,
            )
            self._cleanup_closed_trade(trade)
            return

        # Cancel the old SL — verify it still exists on the exchange first
        # to prevent ORDER_ID_NOT_FOUND spam and ghost order accumulation.
        old_oid = trade.get("sl_oid")
        if old_oid:
            # Check if this order is still open before cancelling
            still_open = any(
                o.get("oid") == str(old_oid)
                for o in self.client.get_open_orders()
            )
            if still_open:
                self.client.cancel_order(old_oid)
                logger.debug("Cancelled old SL %s for %s", old_oid, coin)
            else:
                logger.debug(
                    "Old SL %s for %s already gone from exchange — skipping cancel",
                    old_oid, coin,
                )
            trade["sl_oid"] = None  # clear before placing new one

        # Place the replacement SL
        sl_res = self.client.place_trigger_order(
            coin,
            is_buy=not is_buy,
            size=size,
            trigger_px=new_sl_px,
            tpsl="sl",
            reduce_only=True,
        )
        trade["sl"]     = new_sl_px
        trade["sl_oid"] = sl_res.get("oid")
        logger.info(
            "SL replaced for %s | new_sl=%.4f OID=%s",
            coin, new_sl_px, trade["sl_oid"],
        )

    def update_trailing_sl(self, closed_tfs: set = None) -> list[dict]:
        """
        Called at each relevant candle close (order_update_loop).

        For every filled trade with trailing_sl=True, fetch the latest EMA30
        and move the SL to EMA30 ± buffer — but ONLY in the trade's favour
        (ratchet behaviour: LONG SL can only move up, SHORT SL can only move down).

        Args:
            closed_tfs: Set of exec_tf values ("5", "15") whose candles just closed.
                        If None, processes all trades (backward-compatible).

        Returns a list of update dicts for Telegram notification.
        """
        if not config.PYRAMID_ENABLED:
            return []

        updates = []

        for trade in self.active_trades[:]:
            # Skip trades whose candle hasn't just closed
            if closed_tfs is not None and trade.get("exec_tf", "5") not in closed_tfs:
                continue

            if not trade.get("filled") or not trade.get("trailing_sl"):
                continue

            # Verify position still exists on the exchange before doing anything
            coin    = trade["coin"]
            if not self._position_still_open(coin):
                self._cleanup_closed_trade(trade)
                continue

            is_buy  = trade["is_buy"]
            exec_tf = trade.get("exec_tf", "5")
            old_sl  = float(trade["sl"])

            ema30 = get_ema30(coin, exec_tf)
            if ema30 is None:
                logger.warning("update_trailing_sl: no EMA30 for %s, skipping", coin)
                continue

            buffer = ema30 * config.PYRAMID_SL_BUFFER
            new_sl = (ema30 - buffer) if is_buy else (ema30 + buffer)

            # Ratchet: only move SL in the winning direction
            if is_buy and new_sl <= old_sl:
                logger.debug(
                    "Trailing SL skip %s (LONG): new_sl %.4f <= old_sl %.4f",
                    coin, new_sl, old_sl,
                )
                continue
            if not is_buy and new_sl >= old_sl:
                logger.debug(
                    "Trailing SL skip %s (SHORT): new_sl %.4f >= old_sl %.4f",
                    coin, new_sl, old_sl,
                )
                continue

            logger.info(
                "📈 Trailing SL %s %s | %.4f → %.4f (EMA30=%.4f)",
                trade["direction"], coin, old_sl, new_sl, ema30,
            )
            self._replace_sl(trade, new_sl)
            self._save_trades()

            updates.append({
                "coin":      coin,
                "direction": trade["direction"],
                "old_sl":    old_sl,
                "new_sl":    new_sl,
                "ema30":     ema30,
            })

        return updates

    # ── PnL ──────────────────────────────────────────────────────

    def get_pnl(self) -> list:
        """Get unrealized PnL for all open positions."""
        positions = self.client.get_positions()
        pnl_list = []
        for pos in positions:
            pnl_list.append({
                "coin": pos["coin"],
                "size": pos["size"],
                "entry_px": pos["entry_px"],
                "unrealized_pnl": pos["unrealized_pnl"],
            })
        return pnl_list

    # ── Close all ────────────────────────────────────────────────

    def close_all(self) -> dict:
        """Close all positions and cancel all orders."""
        results = self.client.close_all_positions()
        self.active_trades.clear()
        self._save_trades()
        return {
            "closed": len(results),
            "results": results,
        }

    # ── Status ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return positions and open orders."""
        return {
            "positions": self.client.get_positions(),
            "open_orders": self.client.get_open_orders(),
            "tracked_trades": len(self.active_trades),
        }
