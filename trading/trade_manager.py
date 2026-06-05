"""
Momathi Bot — Trade Manager
Orchestrates trade execution, position sizing, and PnL tracking.
"""
import json
import logging
import os
from datetime import datetime

from config.settings import runtime
from exchange.paradex_client import ParadexClient
from strategy.ema_setup import validate_signal, get_ema30, get_mark_price
from trading.state import load_trades, save_trades, TRADES_FILE

logger = logging.getLogger("momathi.trade_mgr")


class TradeManager:
    """Manages trade lifecycle: sizing, entry, TP/SL, and position tracking."""

    def __init__(self, client: ParadexClient):
        self.client = client
        self.active_trades: list[dict] = []
        self.active_trades = load_trades()

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
            save_trades(self.active_trades)
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
        risk = risk_usd or runtime["risk_usd"]
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
        risk_usd = runtime["risk_usd"]

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
        }

        # If filled immediately, place TP/SL right now
        if filled_immediately:
            logger.info("Entry filled immediately, placing TP/SL triggers")
            try:
                self._place_tpsl(trade)
            except Exception as e:
                logger.error("CRITICAL: Failed to place TP/SL for %s %s: %s", coin, direction, e)
                # Keep trade tracked so we can retry in check_fills loop

        self.active_trades.append(trade)
        save_trades(self.active_trades)
        return trade

    def _place_tpsl(self, trade: dict):
        """Helper to place TP and SL trigger orders once a position exists."""
        coin = trade["coin"]
        is_buy = trade["is_buy"]
        size = trade["size"]
        sl = trade["sl"]
        tp = trade["tp"]

        logger.info("Attempting to place TP/SL for %s %s | size=%.6f | SL=%.2f | TP=%.2f", coin, "LONG" if is_buy else "SHORT", size, sl, tp)

        # Place SL
        logger.info("Placing SL for %s...", coin)
        sl_res = self.client.place_trigger_order(
            coin, is_buy=not is_buy, size=size, trigger_px=sl, tpsl="sl", reduce_only=True
        )
        logger.info("SL result for %s: %s", coin, sl_res)
        
        if sl_res.get("status") != "ok":
            logger.error("FAILED to place SL for %s %s: %s", coin, trade["direction"], sl_res)
            raise Exception(f"SL placement failed: {sl_res.get('msg', sl_res)}")
        
        trade["sl_oid"] = sl_res.get("oid")

        # Place TP
        logger.info("Placing TP for %s...", coin)
        tp_res = self.client.place_trigger_order(
            coin, is_buy=not is_buy, size=size, trigger_px=tp, tpsl="tp", reduce_only=True
        )
        logger.info("TP result for %s: %s", coin, tp_res)
        
        if tp_res.get("status") != "ok":
            logger.error("FAILED to place TP for %s %s: %s", coin, trade["direction"], tp_res)
            raise Exception(f"TP placement failed: {tp_res.get('msg', tp_res)}")
        
        trade["tp_oid"] = tp_res.get("oid")
        
        logger.info("\u2705 Successfully placed TP/SL for %s %s: SL_OID=%s, TP_OID=%s", 
                    coin, trade["direction"], trade["sl_oid"], trade["tp_oid"])
    
    def _update_tpsl_for_filled_trade(self, trade: dict) -> dict | None:
        """
        Update TP/SL trigger orders for a filled trade to trail the latest EMA levels.
            
        1. Re-fetch EMAs to get new SL/TP levels
        2. Cancel old SL and TP trigger orders
        3. Place new SL and TP trigger orders at updated levels
            
        Returns update summary dict, or None if levels unchanged.
        """
        coin = trade["coin"]
        direction = trade["direction"]
        exec_tf = trade.get("exec_tf", "5")
        old_sl = float(trade["sl"])
        old_tp = float(trade["tp"])
    
        # Re-fetch EMAs to get latest levels
        result = validate_signal(direction, coin, exec_tf, paradex_client=self.client)
        if not result.get("valid"):
            logger.debug("Signal no longer valid for filled %s %s, keeping existing TP/SL", direction, coin)
            return None
    
        new_levels = result["levels"]
        new_sl = float(new_levels["sl"])
        new_tp = float(new_levels["tp"])
    
        # Check if SL/TP levels have actually changed
        tick = self.client.get_tick_size(coin)
        sl_delta = abs(new_sl - old_sl)
        tp_delta = abs(new_tp - old_tp)
        if sl_delta < tick and tp_delta < tick:
            logger.debug("TP/SL unchanged for %s %s (sl_delta=%.4f tp_delta=%.4f tick=%.4f)", direction, coin, sl_delta, tp_delta, tick)
            return None
    
        logger.info(
            "Updating TP/SL for %s %s | SL: %.2f->%.2f | TP: %.2f->%.2f",
            direction, coin, old_sl, new_sl, old_tp, new_tp,
        )
    
        # Cancel old TP/SL trigger orders
        sl_oid = trade.get("sl_oid")
        tp_oid = trade.get("tp_oid")
        if sl_oid:
            self.client.cancel_order(str(sl_oid))
            logger.info("Cancelled old SL %s for %s", sl_oid, coin)
        if tp_oid:
            self.client.cancel_order(str(tp_oid))
            logger.info("Cancelled old TP %s for %s", tp_oid, coin)
    
        # Update trade record with new levels before placing new orders
        trade["sl"] = new_sl
        trade["tp"] = new_tp
    
        # Place new TP/SL at updated levels
        try:
            self._place_tpsl(trade)
        except Exception as e:
            logger.error("CRITICAL: Failed to place updated TP/SL for %s %s: %s", direction, coin, e)
            # Position may be unprotected — re-raise so caller can notify user
            raise
    
        save_trades(self.active_trades)
    
        return {
            "coin": coin,
            "direction": direction,
            "old_entry": float(trade["entry"]),  # entry doesn't change for filled trades
            "new_entry": float(trade["entry"]),
            "old_sl": old_sl,
            "new_sl": new_sl,
            "new_tp": new_tp,
            "old_size": float(trade["size"]),
            "new_size": float(trade["size"]),
        }

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
        logger.info("Fill check: %d open orders, %d active trades to check", len(open_orders), len(self.active_trades))

        for trade in self.active_trades[:]:
            if trade.get("filled"):
                logger.info("Fill check: skipping %s %s (already marked filled)", trade["direction"], trade["coin"])
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

            logger.info("Fill check: %s %s | entry_oid=%s | checking if still open...", direction, coin, entry_oid)
            entry_still_open = any(
                str(o.get("oid")) == str(entry_oid) for o in open_orders
            )
            logger.info("Fill check: %s %s | entry_still_open=%s", direction, coin, entry_still_open)

            if not entry_still_open:
                # Check if a position now exists (order was filled)
                positions = self.client.get_positions()
                logger.info("Fill check: %s %s | positions found: %s", direction, coin, [p["coin"] for p in positions])
                has_position = any(p["coin"] == coin for p in positions)
                logger.info("Fill check: %s %s | has_position=%s", direction, coin, has_position)

                if has_position:
                    trade["filled"] = True
                    logger.info(
                        "Fill check: %s %s filled → placing TP/SL", direction, coin
                    )
                    try:
                        self._place_tpsl(trade)
                        filled_trades.append({
                            "coin": coin,
                            "direction": direction,
                            "entry": trade["entry"],
                            "sl": trade["sl"],
                            "tp": trade["tp"],
                        })
                    except Exception as e:
                        logger.error(
                            "CRITICAL: Fill check failed to place TP/SL for %s %s: %s — will retry next cycle",
                            direction, coin, e,
                        )
                        # Don't add to filled_trades (no notification yet), keep trade for retry
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
            save_trades(self.active_trades)

        return filled_trades

    # ── Auto-update pending orders ───────────────────────────────

    def update_pending_orders(self, closed_tfs: set = None) -> list[dict]:
        """
        Re-fetch EMAs and update unfilled limit entry + SL + TP orders
        to the latest 8 EMA / 30 EMA levels.

        For UNFILLED trades: cancels and re-places the entry limit order.
        For FILLED trades: cancels and re-places the TP/SL trigger orders
        to trail the latest EMA levels.

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

            coin = trade["coin"]
            direction = trade["direction"]

            # ═══════════════════════════════════════════════════════════
            # FILLED TRADES: Update TP/SL trigger orders to latest EMAs
            # ═══════════════════════════════════════════════════════════
            if trade.get("filled"):
                try:
                    update_result = self._update_tpsl_for_filled_trade(trade)
                    if update_result:
                        updates.append(update_result)
                except Exception as e:
                    logger.error("Failed to update TP/SL for %s %s: %s", direction, coin, e)
                continue

            # ═══════════════════════════════════════════════════════════
            # UNFILLED TRADES: Update entry limit order + check for fills
            # ═══════════════════════════════════════════════════════════

            # Check if entry order has been filled
            open_orders = self.client.get_open_orders()
            entry_oid = trade.get("entry_oid")
            entry_still_open = any(
                str(o.get("oid")) == str(entry_oid) for o in open_orders
            ) if entry_oid else False

            if not entry_still_open:
                # Check if we have a position (order filled)
                positions = self.client.get_positions()
                has_position = any(p["coin"] == coin for p in positions)
                if has_position:
                    trade["filled"] = True
                    logger.info("Trade %s %s filled, placing TP/SL now", direction, coin)
                    try:
                        self._place_tpsl(trade)
                    except Exception as e:
                        logger.error("CRITICAL: update_pending_orders failed to place TP/SL for %s %s: %s", direction, coin, e)
                    save_trades(self.active_trades)
                    continue
                else:
                    # Order is gone AND no position — cancelled externally (or OID never set).
                    # NEVER fall through to re-place: that would create an autonomous entry.
                    logger.info(
                        "Trade %s %s entry order gone (no position, OID=%s) — removing",
                        direction, coin, entry_oid,
                    )
                    self.active_trades.remove(trade)
                    save_trades(self.active_trades)
                    continue

            # Re-fetch EMAs using the trade's execution timeframe
            exec_tf = trade.get("exec_tf", "5")
            result = validate_signal(direction, coin, exec_tf, paradex_client=self.client)
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
            new_size = self.calculate_size(new_entry, new_sl, runtime["risk_usd"])
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
                        # Entry filled immediately — place TP/SL now
                        trade["sl"] = new_sl
                        trade["tp"] = new_tp
                        trade["size"] = new_size
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
                save_trades(self.active_trades)
                continue

            # Update trade record
            trade["entry"] = new_entry
            trade["sl"] = new_sl
            trade["tp"] = new_tp
            trade["size"] = new_size
            trade["entry_oid"] = new_entry_oid
            trade["last_updated"] = datetime.now().isoformat()

            save_trades(self.active_trades)

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
        save_trades(self.active_trades)
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

    # ── Signal validation ────────────────────────────────────────

    def validate_signal(self, direction: str, coin: str = None, exec_tf: str = "5") -> dict:
        """Validate trade signal using strategy engine."""
        return validate_signal(direction, coin, exec_tf, paradex_client=self.client)

    # ── Regime scan ──────────────────────────────────────────────

    def scan_regime(self) -> dict:
        """Scan 1H EMA regime for watchlist tokens."""
        from strategy.filters import scan_1h_regime
        return scan_1h_regime(paradex_client=self.client)

    # ── Startup reconciliation ───────────────────────────────────

    def reconcile_with_exchange(self) -> dict:
        """
        Reconcile local active_trades state with actual Paradex positions/orders.
        
        Removes trades from local state that:
        1. Have been filled but position no longer exists (SL/TP already hit)
        2. Have orders that were cancelled externally
        3. Have entry_oid=None (untrackable)
        
        Returns dict with reconciliation statistics.
        """
        logger.info("Starting reconciliation with Paradex...")
        
        if not self.active_trades:
            logger.info("No active trades to reconcile")
            return {
                "changes_made": False,
                "trades_removed": 0,
                "orphaned_positions": 0,
            }
        
        # Fetch current Paradex state
        open_orders = self.client.get_open_orders()
        open_positions = self.client.get_positions()
        
        open_order_oids = {str(o["oid"]) for o in open_orders}
        open_position_coins = {p["coin"] for p in open_positions}
        
        trades_to_remove = []
        
        for trade in self.active_trades:
            entry_oid = trade.get("entry_oid")
            coin = trade["coin"]
            filled = trade.get("filled", False)
            
            # Case 1: Trade marked as filled, but position no longer exists
            if filled and coin not in open_position_coins:
                logger.info(
                    "Reconciliation: %s %s marked as filled but position closed — removing",
                    trade["direction"], coin,
                )
                trades_to_remove.append(trade)
                continue
            
            # Case 2: Trade NOT filled, but entry_oid not in open orders (cancelled externally)
            if not filled and entry_oid and str(entry_oid) not in open_order_oids:
                # Check if position exists (order may have filled but fill detection missed it)
                if coin not in open_position_coins:
                    logger.info(
                        "Reconciliation: %s %s order %s not found and no position — removed externally",
                        trade["direction"], coin, entry_oid,
                    )
                    trades_to_remove.append(trade)
                    continue
                else:
                    # Position exists but trade not marked as filled — fix it
                    logger.warning(
                        "Reconciliation: %s %s position exists but trade not marked as filled — fixing",
                        trade["direction"], coin,
                    )
                    trade["filled"] = True
                    # Place TP/SL if not already done
                    if not trade.get("sl_oid") or not trade.get("tp_oid"):
                        try:
                            self._place_tpsl(trade)
                            logger.info("Reconciliation: TP/SL placed for orphaned position %s", coin)
                        except Exception as e:
                            logger.error("Reconciliation: Failed to place TP/SL for %s: %s", coin, e)
        
        # Remove stale trades
        for trade in trades_to_remove:
            self.active_trades.remove(trade)
        
        # Check for orphaned positions (positions not in active_trades)
        tracked_coins = {t["coin"] for t in self.active_trades}
        orphaned_positions = open_position_coins - tracked_coins
        
        if orphaned_positions:
            logger.warning(
                "Reconciliation: Found %d orphaned position(s): %s",
                len(orphaned_positions), ", ".join(orphaned_positions),
            )
        
        # Save reconciled state
        if trades_to_remove:
            save_trades(self.active_trades)
            logger.info(
                "Reconciliation complete: removed %d stale trade(s), %d active trade(s) remaining",
                len(trades_to_remove), len(self.active_trades),
            )
        
        return {
            "changes_made": len(trades_to_remove) > 0,
            "trades_removed": len(trades_to_remove),
            "orphaned_positions": len(orphaned_positions),
            "orphaned_coins": list(orphaned_positions),
        }
