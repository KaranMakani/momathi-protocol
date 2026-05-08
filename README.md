# Rajathi — Paradex Silver Trading Bot

> **Rajathi** (Hindi: रजत + अठी) — "Silver Watchman". A silver-focused EMA trading bot for Paradex, built on the same architecture as Tomathi (BTC bot).

Rajathi is a **Telegram-controlled** algorithmic trading bot that executes EMA crossover strategies on Paradex DEX with automated position sizing, pyramid add-ons, and trailing stop-losses — designed specifically for **SILVER (XAG)** perpetual futures.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [File Structure & Responsibilities](#file-structure--responsibilities)
3. [Configuration Reference](#configuration-reference)
4. [Strategy Engine API](#strategy-engine-api)
5. [Paradex Client API](#paradex-client-api)
6. [Trade Manager API](#trade-manager-api)
7. [Telegram Bot API](#telegram-bot-api)
8. [Trade Lifecycle & State Machine](#trade-lifecycle--state-machine)
9. [Pyramid System Deep-Dive](#pyramid-system-deep-dive)
10. [Background Loops](#background-loops)
11. [Persistence Format](#persistence-format)
12. [Safety Guards](#safety-guards)
13. [Quick Start: Build Your Bot](#quick-start-build-your-bot)
14. [Customizing for Silver](#customizing-for-silver)
15. [Environment Variables](#environment-variables)

---

## Architecture Overview

```
┌──────────────┐     ┌────────────────┐     ┌──────────────────┐
│  Telegram Bot │────▶│  Trade Manager │────▶│  Paradex Client  │
│  (telegram_bot│     │  (trade_manager│     │  (paradex_client │
│   .py)        │     │   .py)         │     │   .py)           │
└──────┬───────┘     └───────┬────────┘     └────────┬─────────┘
       │                     │                       │
       │              ┌──────┴──────┐                │
       │              │   Strategy   │                │
       │              │  (strategy.py│                │
       │              └─────────────┘                │
       │                                             │
┌──────┴───────┐                            ┌───────┴─────────┐
│    config.py │                            │  Paradex DEX    │
│  (.env vars) │                            │  (API/SDK)      │
└──────────────┘                            └─────────────────┘
```

**Data flow:**
1. User sends `/xag long 5` via Telegram
2. `telegram_bot.py` parses command → calls `strategy.validate_signal()`
3. `strategy.py` fetches 1H candles for trend filter, then exec-TF candles for levels
4. If valid → `trade_manager.execute_trade()` places limit entry on Paradex
5. Background loops handle fill detection, order updates, pyramid, and trailing SL

---

## File Structure & Responsibilities

| File | Purpose |
|------|---------|
| `main.py` | Entry point. Initializes all components, starts background loops, runs Telegram polling |
| `config.py` | Loads `.env`, provides mutable runtime settings (risk, coin, strategy params) |
| `strategy.py` | **Strategy engine**: EMA computation, trend detection, level calculation, signal validation |
| `paradex_client.py` | **Exchange adapter**: all Paradex API calls (orders, positions, balance, market data) |
| `trade_manager.py` | **Core brain**: position sizing, trade execution, fill detection, order updates, pyramid, trailing SL, persistence |
| `telegram_bot.py` | **UI layer**: Telegram commands, auth, notifications |
| `active_trades.json` | Persisted trade state (survives restarts) |
| `.env` | Secrets and defaults |

---

## Configuration Reference

All settings live in `config.py`. Runtime-mutable values are in `config.runtime`.

### Static Parameters

```python
# Strategy EMA periods
EMA_FAST = 8       # Fast EMA (entry line)
EMA_SLOW = 30      # Slow EMA (SL / trend line)
CANDLE_LIMIT = 100  # Number of candles to fetch for EMA computation

# Pyramid system
PYRAMID_ENABLED     = True    # Master on/off switch
PYRAMID_ADD_PCT     = 0.15    # Size of add = 15% of base position size
PYRAMID_TRIGGER_PCT = 0.003   # Fire add when EMA30 is within 0.3% of original entry
PYRAMID_SL_BUFFER   = 0.0003  # New SL placed 0.03% below/above EMA30
PYRAMID_TP_SQUEEZE  = 0.15    # Pull TP 15% closer to current price after add
```

### Runtime-Mutable Parameters

```python
runtime = {
    "risk_usd": float(os.getenv("DEFAULT_RISK_USD", "10")),
    "coin": os.getenv("DEFAULT_COIN", "BTC"),
    "running": True,  # Set to False via /stop_bot
}
```

Change at runtime via Telegram: `/set_risk 15`

---

## Strategy Engine API

**File:** `strategy.py`

### Functions

#### `fetch_candles(coin, resolution) -> pd.DataFrame`
Fetches OHLCV candles from Paradex.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `coin` | `str` | `config.runtime["coin"]` | Trading pair base (e.g. `"XAG"`) |
| `resolution` | `str` | `"5"` | Candle TF: `"5"` = 5m, `"15"` = 15m, `"60"` = 1H |

**Returns:** DataFrame with columns `timestamp, open, high, low, close, volume`

#### `compute_emas(df) -> pd.DataFrame`
Adds `ema8` and `ema30` columns to the DataFrame based on `config.EMA_FAST` and `config.EMA_SLOW`.

#### `get_ema30(coin, exec_tf) -> float | None`
Lightweight: fetches just the latest EMA30 value. Used by pyramid checker every 60s.

#### `get_mark_price(coin) -> float | None`
Returns current mid-price. Tries BBO endpoint first, falls back to last 5m candle close.

#### `get_trend(df) -> str`
Returns `"LONG"` if EMA8 > EMA30, `"SHORT"` otherwise.

#### `calculate_levels(ema8, ema30, direction) -> dict`
Computes entry, SL, TP from EMA values.

```python
# LONG:  entry=EMA8, sl=EMA30, tp=entry + 3*risk
# SHORT: entry=EMA8, sl=EMA30, tp=entry - 3*risk

{
    "entry": float,        # = EMA8
    "sl": float,           # = EMA30
    "tp": float,           # 1:3 risk-reward
    "risk_per_unit": float, # |entry - sl|
}
```

#### `validate_signal(direction, coin, exec_tf) -> dict`
**The main entry point for signal validation.** Called by Telegram bot and order update loop.

1. Fetches 1H candles → determines trend (higher-timeframe filter)
2. Rejects if direction doesn't align with 1H trend
3. Fetches exec_tf candles → computes entry/SL/TP levels
4. Returns validation result

```python
# Valid signal:
{
    "valid": True,
    "trend": "LONG",          # 1H trend
    "levels": {...},          # calculate_levels() output
    "ema8": float,            # exec_tf EMA8
    "ema30": float,           # exec_tf EMA30
    "exec_tf": "5",           # execution timeframe
}

# Invalid signal:
{
    "valid": False,
    "reason": "❌ Signal REJECTED — SHORT signal against LONG trend (1H)\n...",
    "trend": "LONG",
    "ema8": float,
    "ema30": float,
}
```

---

## Paradex Client API

**File:** `paradex_client.py`  
**Class:** `ParadexClient`

### Initialization

```python
client = ParadexClient()
# Reads PARADEX_L1_ADDRESS, PARADEX_PRIVATE_KEY, PARADEX_ENV from config
# Auto-authenticates with Paradex on init
```

### Order Methods

#### `place_limit_order(coin, is_buy, size, price) -> dict`
Places a GTC limit entry order.

```python
result = client.place_limit_order("XAG", is_buy=True, size=0.5, price=32.50)
# Returns:
{
    "status": "ok" | "error",
    "oid": "1777369808403020170922890000",  # Paradex order ID
    "raw": {...},                            # Full API response
    "response": {"data": {"statuses": [{"resting": {"oid": "..."}}]}}
}
```

#### `place_trigger_order(coin, is_buy, size, trigger_px, tpsl, reduce_only=True) -> dict`
Places a TP or SL trigger order (stop-market).

| Param | Type | Description |
|-------|------|-------------|
| `coin` | `str` | e.g. `"XAG"` |
| `is_buy` | `bool` | `True` to buy (close short), `False` to sell (close long) |
| `size` | `float` | Position size to close |
| `trigger_px` | `float` | Trigger price |
| `tpsl` | `str` | `"tp"` or `"sl"` |
| `reduce_only` | `bool` | Always `True` for TP/SL |

```python
result = client.place_trigger_order("XAG", is_buy=False, size=0.5,
                                     trigger_px=31.50, tpsl="sl", reduce_only=True)
# Returns:
{"status": "ok", "oid": "...", "raw": {...}}
```

#### `place_market_order(coin, is_buy, size) -> dict`
Places an IOC market order. Used for pyramid add-ons.

```python
result = client.place_market_order("XAG", is_buy=True, size=0.1)
# Returns:
{"status": "ok", "oid": "...", "raw": {...}}
```

### Cancel Methods

#### `cancel_order(oid) -> bool`
Cancel a single order by OID. Returns `True` on success.

#### `cancel_all_orders(coin) -> list`
Cancel every open order for a specific coin.

#### `cancel_all() -> list`
Cancel ALL open orders across every coin.

### Query Methods

#### `get_positions() -> list[dict]`
Returns all open positions.

```python
[{
    "coin": "XAG",
    "symbol": "XAG-USD-PERP",
    "size": 0.5,              # positive=long, negative=short
    "entry_px": 32.50,
    "unrealized_pnl": 1.25,
    "liquidation_px": 28.00,
    "margin_used": 0.0,
}]
```

#### `get_open_orders() -> list[dict]`
Returns all resting/open orders.

```python
[{
    "oid": "1777...",
    "coin": "XAG",
    "side": "b",    # "b"=buy, "a"=ask/sell
    "sz": 0.5,
    "limitPx": 32.50,
}]
```

#### `get_balance() -> dict`
Returns account equity and margin info.

```python
{
    "account_value": 100.00,
    "total_margin_used": 15.00,
    "withdrawable": 85.00,
}
```

#### `get_tick_size(coin) -> float`
Returns the price tick size for a market (e.g. `0.01` for XAG).

### Position Close Methods

#### `close_position(coin) -> dict | None`
Market-closes a specific position.

#### `close_all_positions() -> list`
Closes every open position and cancels all orders.

### Internal Helpers

| Method | Purpose |
|--------|---------|
| `_coin_to_symbol(coin)` | `"XAG"` → `"XAG-USD-PERP"` |
| `_symbol_to_coin(symbol)` | `"XAG-USD-PERP"` → `"XAG"` |
| `_round_size(size, symbol)` | Rounds size to market's step_size (Decimal) |
| `_round_price(price, symbol)` | Rounds price to market's tick_size (Decimal) |
| `_get_markets()` | Fetches and caches market metadata |
| `_get_market_info(symbol)` | Returns metadata dict for a specific market |
| `_get_field(obj, field, default)` | Safely gets dict key or object attribute |

---

## Trade Manager API

**File:** `trade_manager.py`  
**Class:** `TradeManager`

### Initialization

```python
tm = TradeManager(paradex_client)
# Automatically loads active_trades.json on init
```

### Core Methods

#### `execute_trade(coin, direction, levels, exec_tf="5") -> dict`
Executes a trade after signal validation. This is the main entry point called from Telegram.

1. Calculates size from fixed-risk model
2. Places limit entry order
3. Sets up pyramid fields
4. If filled immediately → places TP/SL

```python
trade = tm.execute_trade("XAG", "LONG", levels, exec_tf="5")
# Returns the trade dict (see Persistence Format below)
```

#### `check_fills() -> list[dict]`
Detects if pending entry orders have been filled. Called every 60s by `fill_check_loop`.

1. Cleans up filled trades with no open position (SL/TP already hit on exchange)
2. Checks if entry order still exists in open orders
3. If gone → verifies position exists → marks as filled → places TP/SL

```python
filled = tm.check_fills()
# Returns:
[{"coin": "XAG", "direction": "LONG", "entry": 32.50, "sl": 31.80, "tp": 34.60}]
```

#### `update_pending_orders(closed_tfs=None) -> list[dict]`
Re-fetches EMAs and updates unfilled entry orders with new levels. Called on each candle close.

1. Skips trades whose TF hasn't just closed
2. Checks if entry is still open (detects fills)
3. Re-validates signal on current EMAs
4. Tick-size threshold check (skip if delta < tick)
5. Cancels all orders, recalculates size, re-places entry
6. Recalculates pyramid fields for new levels/size

```python
updates = tm.update_pending_orders(closed_tfs={"5"})
# Returns:
[{
    "coin": "XAG", "direction": "LONG",
    "old_entry": 32.50, "new_entry": 32.55,
    "old_sl": 31.80, "new_sl": 31.85,
    "new_tp": 34.75,
    "old_size": 0.5, "new_size": 0.48,
}]
```

#### `check_pyramid() -> list[dict]`
Pyramid arm/fire check. Called every 60s by `fill_check_loop`.

```python
events = tm.check_pyramid()
# Returns events:
[
    {"type": "armed", "coin": "XAG", "direction": "LONG",
     "pyramid_level": 33.20, "mark_price": 33.25},
    {"type": "fired", "coin": "XAG", "direction": "LONG",
     "pyramid_size": 0.075, "new_sl": 32.80, "new_tp": 34.40,
     "current_price": 33.10, "ema30": 32.79},
]
```

#### `update_trailing_sl(closed_tfs=None) -> list[dict]`
Trails SL at EMA30 for pyramided trades. Called on each candle close.

```python
updates = tm.update_trailing_sl(closed_tfs={"5"})
# Returns:
[{"coin": "XAG", "direction": "LONG",
  "old_sl": 32.50, "new_sl": 32.80, "ema30": 32.81}]
```

### Utility Methods

#### `calculate_size(entry, sl, risk_usd=None) -> float`
Fixed-risk position sizing: `size = risk_usd / |entry - sl|`

#### `get_pnl() -> list[dict]`
Returns unrealized PnL for all open positions.

#### `get_status() -> dict`
Returns positions, open orders, and tracked trade count.

#### `close_all() -> dict`
Closes all positions and cancels all orders. Clears `active_trades`.

### Internal Methods

| Method | Purpose |
|--------|---------|
| `_place_tpsl(trade)` | Place TP and SL trigger orders for a trade |
| `_replace_sl(trade, new_sl_px)` | Cancel old SL + place new one (with position existence guard) |
| `_fire_pyramid_add(trade, ema30)` | Execute pyramid add (cancel old SL/TP, market add, new SL/TP, trail) |
| `_position_still_open(coin)` | Check if position exists on exchange |
| `_get_actual_position_size(coin)` | Fetch real position size from exchange |
| `_cleanup_closed_trade(trade)` | Remove trade whose position was closed on exchange |
| `_save_trades()` | Persist `active_trades` to `active_trades.json` |
| `_load_trades()` | Load `active_trades` from disk on startup |

---

## Telegram Bot API

**File:** `telegram_bot.py`  
**Class:** `TomathiTelegramBot` (rename to `RajathiTelegramBot` for silver)

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + command list |
| `/<coin> <direction> <tf>` | Execute trade: `/xag long 5` |
| `/status` | View open positions & orders |
| `/balance` | Account balance |
| `/pnl` | Unrealized PnL |
| `/set_risk <amount>` | Set USD risk per trade |
| `/get_risk` | Show current risk |
| `/close_all` | Close all positions & cancel orders |
| `/stop_bot` | Shutdown the bot |

### Trade Command Flow

```
User: /xag long 5
  1. Parse: coin=XAG, direction=LONG, exec_tf=5
  2. Reply: "🔍 Validating LONG signal for XAG on 5m..."
  3. Call: validate_signal("LONG", "XAG", "5")
  4. If invalid → reply with rejection reason
  5. If valid → reply with levels, then:
  6. Call: trade_manager.execute_trade("XAG", "LONG", levels, "5")
  7. Reply: "🍅 Trade Placed — XAG LONG (5m) ..."
```

### Notifications

Background loops send proactive Telegram notifications for:

| Event | Format |
|-------|--------|
| Entry filled | `✅ Entry Filled — XAG LONG` |
| Order updated | `🔄 Orders Updated — XAG LONG` |
| Pyramid armed | `🔺 Pyramid Armed — XAG LONG` |
| Pyramid fired | `🔥 Pyramid Fired! — XAG LONG` |
| SL trailed | `📈 Trailing SL Moved — XAG LONG` |

### Auth

All commands are restricted to `TELEGRAM_CHAT_ID` via the `@auth` decorator.

---

## Trade Lifecycle & State Machine

```
                    ┌──────────┐
   execute_trade()  │ PENDING  │ entry_oid set, filled=False
   ────────────────▶│ ENTRY    │ TP/SL NOT placed yet
                    └────┬─────┘
                         │ check_fills() detects entry filled
                         ▼
                    ┌──────────┐
                    │  FILLED  │ filled=True, sl_oid + tp_oid set
                    │          │ pyramid_armed=False
                    └────┬─────┘
                         │ mark price crosses 1:1 RR level
                         ▼
                    ┌──────────┐
                    │  ARMED   │ pyramid_armed=True
                    │          │ Watching EMA30 → entry
                    └────┬─────┘
                         │ EMA30 within 0.3% of entry
                         ▼
                    ┌──────────┐
                    │ PYRAMID  │ pyramid_done=True, trailing_sl=True
                    │ + TRAIL  │ SL trails EMA30 every candle
                    └────┬─────┘
                         │ SL or TP hit on exchange
                         ▼
                    ┌──────────┐
                    │ CLOSED   │ Removed from active_trades
                    └──────────┘
```

**Key state transitions:**
- `PENDING → FILLED`: `check_fills()` (60s loop)
- `FILLED → ARMED`: `check_pyramid()` Phase 1 (60s loop)
- `ARMED → PYRAMID+TRAIL`: `check_pyramid()` Phase 2 (60s loop)
- `ANY → CLOSED`: `check_fills()` cleanup OR `_position_still_open()` guard

---

## Pyramid System Deep-Dive

The pyramid is a **one-time add-on** that increases position size after the trade moves 1:1 in your favor.

### Phase 1: ARM (1:1 RR Level)

```
Condition: mark_price >= entry + |entry - sl|  (for LONG)
Action:    Set pyramid_armed = True
Trigger:   Every 60s in check_pyramid()
```

### Phase 2: FIRE (EMA30 Reaches Entry)

```
Condition: |EMA30 - entry| / entry < 0.3%  (PYRAMID_TRIGGER_PCT)
Action:    1. Cancel old SL/TP (with existence check)
           2. Market add: size = base_size × 15%  (PYRAMID_ADD_PCT)
           3. New SL = EMA30 ± 0.03% buffer  (PYRAMID_SL_BUFFER)
           4. New TP = original_TP - 15% of remaining distance  (PYRAMID_TP_SQUEEZE)
           5. Enable trailing SL
Trigger:   Every 60s in check_pyramid()
```

### Phase 3: TRAIL (Ratchet SL at EMA30)

```
Condition: New SL is MORE favorable than old SL
           LONG:  new_sl > old_sl
           SHORT: new_sl < old_sl
Action:    Cancel old SL, place new one at EMA30 ± buffer
Trigger:   Every candle close in update_trailing_sl()
```

---

## Background Loops

Two async loops run in `main.py`:

### 1. Fill Check Loop (60s interval)

```
Every 60 seconds:
  1. check_fills()      — detect filled entries, place TP/SL
  2. check_pyramid()    — arm/fire pyramid adds
```

### 2. Order Update Loop (candle-aligned)

```
Sleeps until next candle boundary + 3 seconds:
  1. Determine which TFs just closed a candle
  2. update_pending_orders(closed_tfs)  — refresh EMA levels for unfilled entries
  3. update_trailing_sl(closed_tfs)     — trail SL at EMA30 for pyramided trades
```

The loop automatically aligns to the smallest active timeframe (5m or 15m).

---

## Persistence Format

**File:** `active_trades.json`

```json
[
  {
    "coin": "XAG",
    "direction": "LONG",
    "entry": 32.50,
    "sl": 31.80,
    "tp": 34.60,
    "size": 0.5,
    "risk_usd": 10.0,
    "is_buy": true,
    "exec_tf": "5",
    "entry_oid": "1777369808403020170922890000",
    "filled": true,
    "sl_oid": "1777369813560201709224470003",
    "tp_oid": "1777369815120201709225810000",
    "timestamp": "2026-04-28T15:15:07.123456",
    "pyramid_level": 33.20,
    "pyramid_size": 0.075,
    "pyramid_armed": false,
    "pyramid_done": false,
    "trailing_sl": false,
    "last_updated": "2026-04-28T15:20:08.654321"
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `coin` | str | Base currency (`"XAG"`) |
| `direction` | str | `"LONG"` or `"SHORT"` |
| `entry` | float | Entry price (EMA8 at signal time) |
| `sl` | float | Current SL price (EMA30 or trailed) |
| `tp` | float | Current TP price |
| `size` | float | Position size (units) |
| `risk_usd` | float | USD risk for this trade |
| `is_buy` | bool | True = LONG |
| `exec_tf` | str | `"5"` or `"15"` |
| `entry_oid` | str\|null | Paradex order ID for the limit entry |
| `filled` | bool | Whether the entry has been filled |
| `sl_oid` | str\|null | Paradex order ID for the SL trigger |
| `tp_oid` | str\|null | Paradex order ID for the TP trigger |
| `timestamp` | str | ISO format creation time |
| `pyramid_level` | float | 1:1 RR price level |
| `pyramid_size` | float | Size of the add-on order |
| `pyramid_armed` | bool | True once 1:1 RR is crossed |
| `pyramid_done` | bool | True after add is placed (one-time) |
| `trailing_sl` | bool | True = SL trails EMA30 every candle |
| `last_updated` | str\|null | ISO format of last level update |

---

## Safety Guards

The bot includes multiple safety checks to prevent orphan orders and phantom trades:

### 1. Position Existence Check (`_position_still_open`)
Called **before** every order-placement action:
- `check_pyramid()` — before arm/fire
- `_fire_pyramid_add()` — before placing pyramid orders
- `_replace_sl()` — before placing replacement SL
- `update_trailing_sl()` — before trailing

If the position is gone (SL/TP hit on exchange), the trade is auto-removed.

### 2. Actual Size Verification (`_get_actual_position_size`)
`_replace_sl()` fetches the real position size from the exchange and corrects `trade["size"]` if stale. This prevents duplicate SL orders with wrong sizes when `update_levels` and `update_trailing_sl` run in the same tick.

### 3. Order Existence Check Before Cancel
Both `_replace_sl()` and `_fire_pyramid_add()` verify an order still exists on the exchange before cancelling it. This eliminates `ORDER_ID_NOT_FOUND` spam when Paradex auto-cancels trigger orders.

### 4. Closed Trade Cleanup (`check_fills`)
Every 60s, the fill check loop removes trades whose position no longer exists on the exchange — handles the case where SL/TP is triggered between loops.

### 5. Tick-Size Threshold
`update_pending_orders()` only replaces orders when the EMA level has changed by at least one tick size. Prevents unnecessary API calls and order churn.

### 6. No-OID Trade Removal
If an entry order returns no OID, the trade is immediately removed to prevent silent re-entries.

---

## Quick Start: Build Your Bot

### Step 1: Clone & Set Up

```bash
cp -r tomathi/ rajathi/
cd rajathi/
rm active_trades.json 2>/dev/null
rm rajathi.log 2>/dev/null
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Rename Everything

| From | To |
|------|-----|
| `tomathi.log` | `rajathi.log` |
| `"tomathi."` logger prefix | `"rajathi."` |
| `TomathiTelegramBot` | `RajathiTelegramBot` |
| `Tomathi Bot` in strings | `Rajathi Bot` |
| 🍅 emoji | 🥈 emoji |
| `TRADES_FILE = "active_trades.json"` | `TRADES_FILE = "rajathi_trades.json"` |

### Step 3: Configure for Silver

Edit `config.py`:
```python
runtime = {
    "risk_usd": float(os.getenv("DEFAULT_RISK_USD", "10")),
    "coin": os.getenv("DEFAULT_COIN", "XAG"),
    "running": True,
}
```

Edit `.env`:
```env
DEFAULT_COIN=XAG
# Use a DIFFERENT Telegram bot token for Rajathi!
TELEGRAM_BOT_TOKEN=<new_bot_token>
TELEGRAM_CHAT_ID=<your_chat_id>
```

### Step 4: Customize Strategy

Edit `config.py` EMA periods:
```python
EMA_FAST = 12      # Your silver fast EMA
EMA_SLOW = 36      # Your silver slow EMA
```

Edit `strategy.py` `_price_precision()` for silver:
```python
def _price_precision(price: float) -> int:
    if price >= 1000:
        return 2    # BTC, ETH
    elif price >= 10:
        return 3    # XAG (silver ~$30-40)
    elif price >= 1:
        return 4
    else:
        return 5
```

### Step 5: Run

```bash
source .venv/bin/activate
python main.py
```

Then in Telegram: `/xag long 5`

---

## Customizing for Silver

### What to change for a different EMA strategy

1. **EMA Periods** (`config.py`):
   ```python
   EMA_FAST = 12   # was 8
   EMA_SLOW = 36   # was 30
   ```

2. **Risk-Reward Ratio** (`strategy.py` → `calculate_levels`):
   ```python
   # Default is 1:3 RR. To change to 1:2:
   tp = entry + (2 * risk)   # was 3 * risk
   ```

3. **Pyramid Parameters** (`config.py`):
   ```python
   PYRAMID_ADD_PCT     = 0.20   # 20% add instead of 15%
   PYRAMID_TRIGGER_PCT = 0.005  # 0.5% instead of 0.3%
   PYRAMID_SL_BUFFER   = 0.001  # 0.1% buffer instead of 0.03%
   PYRAMID_TP_SQUEEZE  = 0.10   # 10% squeeze instead of 15%
   ```

4. **Trend Filter** (`strategy.py` → `validate_signal`):
   - Currently uses 1H EMA8/30 for trend direction
   - Change the `resolution="60"` call to a different TF if desired

5. **Price Precision** (`strategy.py` → `_price_precision`):
   - Silver trades ~$30-40, so the `>= 10` branch (3 decimals) applies
   - Adjust if Paradex tick size requires different precision

### What NOT to change

- `paradex_client.py` — Exchange adapter, same for all coins
- `trade_manager.py` — Core logic is coin-agnostic
- `telegram_bot.py` — Only rename class/strings, logic stays the same
- Safety guards — They protect all assets equally

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PARADEX_L1_ADDRESS` | Yes | — | Your Paradex L1 wallet address |
| `PARADEX_PRIVATE_KEY` | Yes | — | Your Paradex private key |
| `PARADEX_ENV` | No | `TESTNET` | `MAINNET` or `TESTNET` |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram chat ID |
| `DEFAULT_RISK_USD` | No | `10` | USD risk per trade |
| `DEFAULT_COIN` | No | `BTC` | Default trading pair base |

---

## Running Both Bots Simultaneously

Since each bot is fully independent, just run them in separate terminals:

```bash
# Terminal 1: Tomathi (BTC)
cd ~/tomathi/
source .venv/bin/activate
python main.py

# Terminal 2: Rajathi (Silver)
cd ~/rajathi/
source .venv/bin/activate
python main.py
```

**Important:** Each bot must have its own:
- `.env` file (different `TELEGRAM_BOT_TOKEN`, same or different `PARADEX_L1_ADDRESS`)
- `active_trades.json` (separate trade tracking)
- `.venv/` (separate virtual environment)
- Log file (separate `rajathi.log` vs `tomathi.log`)

They CAN share the same Paradex account — just don't trade the same coin on both bots.

---

*Built with ❤️ for Paradex. Rajathi — your silver watchman.* 🥈
