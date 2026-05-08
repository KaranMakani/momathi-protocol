"""
Momathi Bot — Paradex Exchange Client
Thin wrapper around the paradex-py SDK for order management.
"""
import logging
import math
from decimal import Decimal

from paradex_py import Paradex
from paradex_py.environment import Environment
from paradex_py.common.order import Order, OrderType, OrderSide

import config

logger = logging.getLogger("momathi.paradex_client")


class ParadexClient:
    """Manages all interactions with the Paradex DEX."""

    def __init__(self):
        env = "testnet" if config.PARADEX_ENV == "TESTNET" else "prod"
        self.client = Paradex(
            env="testnet" if config.PARADEX_ENV == "TESTNET" else "prod",
            l1_address=config.PARADEX_L1_ADDRESS,
            l2_private_key=config.PARADEX_PRIVATE_KEY,
            
        )
        logger.info("ParadexClient initialized for L1 Address %s on %s", config.PARADEX_L1_ADDRESS, env)

    # ── Helpers ──────────────────────────────────────────────────

    def _get_markets(self) -> dict:
        """Get and cache markets metadata."""
        if not hasattr(self, "_markets_cache"):
            self._markets_cache = self.client.api_client.fetch_markets()
        return self._markets_cache

    def _get_market_info(self, symbol: str) -> dict | None:
        markets = self._get_markets()
        results = markets.get("results", []) if isinstance(markets, dict) else markets
        for market in results:
            # Handle both dict and object-style access
            mkt_symbol = market.get("symbol") if isinstance(market, dict) else getattr(market, "symbol", None)
            if mkt_symbol == symbol:
                return market
        return None

    def _coin_to_symbol(self, coin: str) -> str:
        """Convert 'BTC' to 'BTC-USD-PERP'"""
        return f"{coin}-USD-PERP"

    def _symbol_to_coin(self, symbol: str) -> str:
        """Convert 'BTC-USD-PERP' to 'BTC'"""
        if symbol and symbol.endswith("-USD-PERP"):
            return symbol.split("-")[0]
        return symbol

    def _get_field(self, obj, field: str, default=None):
        """Safely get a field from either a dict or an object."""
        if isinstance(obj, dict):
            return obj.get(field, default)
        return getattr(obj, field, default)

    def _round_size(self, size: float, symbol: str) -> Decimal:
        """Round size to the market's asset step_size."""
        info = self._get_market_info(symbol)
        step_str = self._get_field(info, "order_size_increment") if info else None
        if not step_str:
            step_str = self._get_field(info, "asset_step_size") if info else None
        
        if step_str:
            step_size = Decimal(str(step_str))
            sz = Decimal(str(size))
            # Round down to nearest step
            rounded = (sz // step_size) * step_size
            logger.debug("Size rounding: %s -> %s (step=%s)", size, rounded, step_size)
            return rounded
        # Safe fallback: round to 1 decimal place
        logger.warning("No step_size found for %s, using fallback rounding", symbol)
        return Decimal(str(math.floor(size * 10) / 10))

    def _round_price(self, price: float, symbol: str) -> Decimal:
        """Round price to the market's price tick_size."""
        info = self._get_market_info(symbol)
        tick_str = self._get_field(info, "price_tick_size") if info else None
        
        if tick_str:
            tick_size = Decimal(str(tick_str))
            px = Decimal(str(price))
            # Round to nearest tick
            rounded = round(px / tick_size) * tick_size
            return rounded
        # Safe fallback
        return Decimal(str(round(price, 2)))

    def get_tick_size(self, coin: str) -> float:
        """Return the price tick_size for a coin's market (e.g. 0.1 for BTC)."""
        symbol = self._coin_to_symbol(coin)
        info = self._get_market_info(symbol)
        tick_str = self._get_field(info, "price_tick_size") if info else None
        if tick_str:
            return float(tick_str)
        # Fallback: estimate from price magnitude
        mark = None
        try:
            mark = float(self.client.api_client.fetch_bbo(symbol=symbol).get("mid", 0))
        except Exception:
            pass
        if mark and mark >= 1000:
            return 0.10
        elif mark and mark >= 10:
            return 0.01
        elif mark and mark >= 1:
            return 0.001
        return 0.01  # safe default

    # ── Orders ───────────────────────────────────────────────────

    def place_limit_order(self, coin: str, is_buy: bool, size: float, price: float) -> dict:
        """Place a GTC limit order. Returns normalized response."""
        symbol = self._coin_to_symbol(coin)
        sz = self._round_size(size, symbol)
        px = self._round_price(price, symbol)
        
        if sz <= 0:
            return {"status": "error", "msg": f"Size too small: {size} → {sz}"}

        side = OrderSide.Buy if is_buy else OrderSide.Sell
        logger.info("LIMIT %s %s | size=%s price=%s", side.name, symbol, sz, px)
        
        order = Order(
            market=symbol,
            order_type=OrderType.Limit,
            order_side=side,
            size=sz,
            limit_price=px,
            instruction="GTC",
            reduce_only=False,
        )

        try:
            result = self.client.api_client.submit_order(order=order)
            logger.info("Order result: %s", result)
            
            # Normalize to look somewhat like old Hyperliquid response for the trade manager
            oid = result.get("id")
            return {
                "status": "ok" if oid else "error",
                "oid": oid,
                "raw": result,
                "response": {
                    "data": {
                        "statuses": [{"resting": {"oid": oid}} if oid else {}]
                    }
                }
            }
        except Exception as e:
            logger.error("Failed to place limit order: %s", e)
            return {"status": "error", "msg": str(e)}

    def place_trigger_order(
        self, coin: str, is_buy: bool, size: float, trigger_px: float, tpsl: str, reduce_only: bool = True
    ) -> dict:
        """
        Place a trigger order (TP or SL).
        tpsl: "tp" or "sl"
        is_buy: True to buy (close short), False to sell (close long)
        """
        symbol = self._coin_to_symbol(coin)
        sz = self._round_size(size, symbol)
        px = self._round_price(trigger_px, symbol)
        
        if sz <= 0:
            return {"status": "error", "msg": f"Size too small: {size} → {sz}"}

        side = OrderSide.Buy if is_buy else OrderSide.Sell
        o_type = OrderType.TakeProfitMarket if tpsl.lower() == "tp" else OrderType.StopLossMarket
        
        logger.info("TRIGGER %s %s %s | size=%s trigger=%s reduce_only=%s", tpsl.upper(), side.name, symbol, sz, px, reduce_only)
        
        order = Order(
            market=symbol,
            order_type=o_type,
            order_side=side,
            size=sz,
            trigger_price=px,
            reduce_only=reduce_only,
        )

        try:
            result = self.client.api_client.submit_order(order=order)
            logger.info("Trigger result: %s", result)
            
            oid = result.get("id")
            return {
                "status": "ok" if oid else "error",
                "oid": oid,
                "raw": result
            }
        except Exception as e:
            logger.error("Failed to place trigger order: %s", e)
            return {"status": "error", "msg": str(e)}
            
    def place_batch_orders(self, orders_data: list) -> dict:
        """
        Advanced: Place multiple orders in one call. 
        orders_data formatting depends on internal use (we can build this if TradeManager needs it).
        """
        pass # Optional optimization later

    def cancel_all_orders(self, coin: str) -> list:
        """Cancel every open order for a coin."""
        symbol = self._coin_to_symbol(coin)
        try:
            res = self.client.api_client.cancel_all_orders({"market": symbol})
            logger.info("Cancelled all orders for %s: %s", symbol, res)
            return res if isinstance(res, list) else [res]
        except Exception as e:
            logger.warning("Failed to cancel orders for %s: %s", symbol, e)
            return []

    def cancel_all(self) -> list:
        """Cancel ALL open orders across every coin."""
        try:
            res = self.client.api_client.cancel_all_orders()
            logger.info("Cancelled ALL orders: %s", res)
            return res if isinstance(res, list) else [res]
        except Exception as e:
            logger.warning("Failed to cancel ALL orders: %s", e)
            return []

    def cancel_order(self, oid: str) -> bool:
        """
        Cancel a single open order by its OID.
        Returns True if successful, False otherwise.
        Used for targeted SL/TP replacement without touching other orders.
        """
        if not oid:
            return False
        try:
            res = self.client.api_client.cancel_order({"id": str(oid)})
            logger.info("Cancelled order %s: %s", oid, res)
            return True
        except Exception as e:
            logger.warning("Failed to cancel order %s: %s", oid, e)
            return False

    def place_market_order(self, coin: str, is_buy: bool, size: float) -> dict:
        """
        Place a market order (IOC) to add to an existing position.
        Used for the pyramid add-on after 1:1 RR is hit.
        reduce_only=False so it ADDS to the position, not closes it.
        """
        symbol = self._coin_to_symbol(coin)
        sz = self._round_size(size, symbol)

        if sz <= 0:
            return {"status": "error", "msg": f"Size too small: {size} → {sz}"}

        side = OrderSide.Buy if is_buy else OrderSide.Sell
        logger.info("MARKET ADD %s %s | size=%s", side.name, symbol, sz)

        order = Order(
            market=symbol,
            order_type=OrderType.Market,
            order_side=side,
            size=sz,
            instruction="IOC",
            reduce_only=False,
        )

        try:
            result = self.client.api_client.submit_order(order=order)
            logger.info("Market add result: %s", result)
            oid = result.get("id")
            return {
                "status": "ok" if oid else "error",
                "oid": oid,
                "raw": result,
            }
        except Exception as e:
            logger.error("Failed to place market order: %s", e)
            return {"status": "error", "msg": str(e)}



    def get_positions(self) -> list:
        """Return list of open positions with normalized details."""
        try:
            response = self.client.api_client.fetch_positions()
            raw_positions = response.get("results", []) if isinstance(response, dict) else response
            
            positions = []
            for pos in raw_positions:
                size = float(pos.get("size", 0))
                if size != 0:
                    positions.append({
                        "coin": self._symbol_to_coin(pos.get("market")),
                        "symbol": pos.get("market"),
                        "size": size,
                        "entry_px": float(pos.get("average_entry_price", 0)),
                        "unrealized_pnl": float(pos.get("unrealized_pnl", 0)),
                        "liquidation_px": float(pos.get("liquidation_price", 0)) if pos.get("liquidation_price") else None,
                        "margin_used": 0.0, # Not directly in position on Paradex usually
                    })
            return positions
        except Exception as e:
            logger.error("Failed to fetch positions: %s", e)
            return []

    def get_balance(self) -> dict:
        """Return account equity and available margin."""
        try:
            summary = self.client.api_client.fetch_account_summary()
            
            # Helper to safely parse strings to float
            def parse_float(val):
                return float(val) if val else 0.0
                
            account_value = parse_float(getattr(summary, "account_value", 0))
            total_collat = parse_float(getattr(summary, "total_collateral", 0))
            free_collat = parse_float(getattr(summary, "free_collateral", 0))
            
            return {
                "account_value": account_value,
                "total_margin_used": total_collat - free_collat,
                "withdrawable": free_collat, 
            }
        except Exception as e:
            logger.error("Failed to fetch balance: %s", e)
            return {"account_value": 0, "total_margin_used": 0, "withdrawable": 0}

    def get_open_orders(self) -> list:
        """Return all currently open/resting orders."""
        try:
            res = self.client.api_client.fetch_orders()
            raw_orders = res.get("results", []) if isinstance(res, dict) else res
            
            # Normalize to match expected format for trade manager
            orders = []
            for o in raw_orders:
                orders.append({
                    "oid": o.get("id"),
                    "coin": self._symbol_to_coin(o.get("market")),
                    "side": "b" if o.get("side") == "BUY" else "a", # 'b' = buy, 'a' = ask/sell
                    "sz": float(o.get("size", 0)),
                    "limitPx": float(o.get("price", 0)),
                })
            return orders
        except Exception as e:
            logger.error("Failed to fetch open orders: %s", e)
            return []

    # ── Close positions ──────────────────────────────────────────

    def close_position(self, coin: str) -> dict | None:
        """Market-close a specific position."""
        symbol = self._coin_to_symbol(coin)
        positions = self.get_positions()
        
        for pos in positions:
            if pos["coin"] == coin:
                sz = abs(pos["size"])
                is_buy = pos["size"] < 0  # if short, buy to close
                side = OrderSide.Buy if is_buy else OrderSide.Sell
                
                logger.info("Closing position %s %s size=%s", side.name, symbol, sz)
                
                order = Order(
                    market=symbol,
                    order_type=OrderType.Market,
                    order_side=side,
                    size=Decimal(str(sz)),
                    instruction="IOC",
                    reduce_only=True,
                )
                
                try:
                    result = self.client.api_client.submit_order(order=order)
                    logger.info("Close position %s: %s", coin, result)
                    return result
                except Exception as e:
                    logger.error("Failed to close position %s: %s", coin, e)
                    return None
        return None

    def close_all_positions(self) -> list:
        """Market-close every open position and cancel all orders."""
        self.cancel_all()
        positions = self.get_positions()
        results = []
        for pos in positions:
            res = self.close_position(pos["coin"])
            if res:
                results.append(res)
        return results
