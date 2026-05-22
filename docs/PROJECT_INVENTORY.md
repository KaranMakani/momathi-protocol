# Momathi Protocol — Project Inventory

**Generated:** 2026-05-22  
**Type:** Read-only audit documentation (NO code changes)  
**Scope:** Complete feature, function, and behavior map

---

## SECTION 1 — USER-FACING FEATURES

### Telegram Commands

#### `/start`
- **Description:** Displays welcome message with full command list and current risk setting.
- **Arguments:** None.
- **Auth:** Yes — `@auth` decorator checks `TELEGRAM_CHAT_ID`.
- **Calls:** None (reads `runtime["risk_usd"]` for display).
- **Side effects:** None.

#### `/scan`
- **Description:** Runs a 1H EMA regime scan across all tokens in `SCAN_WATCHLIST`, classifying each as LONG BIAS, SHORT BIAS, or TANGLED.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** `trade_mgr.scan_regime()` → `strategy.filters.scan_1h_regime()` → `ema_setup.fetch_candles()` (1H per token) + `ema_setup.compute_emas()`.
- **Side effects:** None (read-only scan).

#### `/status`
- **Description:** Shows open positions (coin, side, size, entry price, unrealized PnL), open orders (up to 10), and number of tracked trades.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** `trade_mgr.get_status()` → `client.get_positions()` + `client.get_open_orders()`.
- **Side effects:** None.

#### `/balance`
- **Description:** Displays account value, margin used, and withdrawable collateral.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** `trade_mgr.client.get_balance()` → `client.api_client.fetch_account_summary()`.
- **Side effects:** None.

#### `/pnl`
- **Description:** Shows unrealized PnL for each open position with total PnL.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** `trade_mgr.get_pnl()` → `client.get_positions()`.
- **Side effects:** None.

#### `/set_risk [amount]`
- **Description:** Sets the USD risk per trade (used for position sizing).
- **Arguments:** Required — positive float (e.g., `/set_risk 10`).
- **Auth:** Yes.
- **Calls:** None (directly mutates `runtime["risk_usd"]`).
- **Side effects:** Mutates `runtime["risk_usd"]` in-memory only (not persisted).

#### `/get_risk`
- **Description:** Displays the current USD risk per trade.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** None (reads `runtime["risk_usd"]`).
- **Side effects:** None.

#### `/close_all`
- **Description:** Market-closes all open positions and cancels all orders across all coins. Clears all tracked trades.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** `trade_mgr.close_all()` → `client.close_all_positions()` → `client.cancel_all()` + `client.close_position()` per coin.
- **Side effects:** Writes to `active_trades.json` (clears the list).

#### `/stop_bot`
- **Description:** Gracefully shuts down the bot.
- **Arguments:** None.
- **Auth:** Yes.
- **Calls:** None (sets `runtime["running"] = False`, schedules `SIGINT` after 1s).
- **Side effects:** Stops all background loops, terminates process.

#### `/<coin> <direction> <timeframe>` (Trade Command)
- **Description:** Places a limit entry order with auto-placed TP/SL once filled. Validates signal against 1H trend first.
- **Arguments:** Required — coin (any string), direction (`long` or `short`), timeframe (`5` or `15`). Example: `/btc long 5`.
- **Auth:** Yes.
- **Calls:** `trade_mgr.validate_signal()` → `ema_setup.validate_signal()` → if valid → `trade_mgr.execute_trade()` → `client.place_limit_order()`.
- **Side effects:** Writes new trade to `active_trades.json`.

### Non-Command User-Facing Behavior

#### Startup Message
- **Trigger:** Bot initialization (`post_init()` in main.py).
- **Content:** Welcome message with risk amount, strategy description (EMA 8/30), auto-update interval, fill check interval, and usage example.
- **Sent to:** `TELEGRAM_CHAT_ID`.

#### Background Notifications

1. **Fill Notifications** (from `fill_check_loop`):
   - Triggered when a pending entry order is filled.
   - Content: Coin, direction, entry price, SL, TP, confirmation that TP/SL placed.

2. **Order Update Notifications** (from `order_update_loop`):
   - Triggered when pending orders are updated to new EMA levels at candle boundaries.
   - Content: Coin, direction, old/new entry, old/new SL, new TP, optionally size change.

3. **Regime Alerts** (from `regime_watcher_loop`):
   - Triggered when a token enters a CLEAN regime (ENTERED_CLEAN) — if `REGIME_ALERT_ON_ENTER_CLEAN = True`.
   - Triggered when a token leaves a CLEAN regime (LEFT_CLEAN) — if `REGIME_ALERT_ON_LEAVE_CLEAN = False` (currently disabled).
   - Content: Token, state transition, consecutive confirmation count, eligibility note.

#### Error Messages
- **Source:** `_error_handler()` in telegram_bot.py.
- **Trigger:** Any unhandled exception in Telegram command processing.
- **Content:** "⚠️ An error occurred: {error_message}".

---

## SECTION 2 — BACKGROUND LOOPS

All three loops are created as `asyncio.create_task()` in `post_init()` (main.py, lines 212-215).

### 1. fill_check_loop

- **File:** `main.py`, lines 36-63.
- **Interval:** Every 60 seconds (hardcoded `await asyncio.sleep(60)`).
- **What it does each iteration:**
  1. Skips if `trade_mgr.active_trades` is empty.
  2. Calls `trade_mgr.check_fills()` via `run_in_executor` (runs in thread to avoid blocking event loop).
  3. For each newly-filled trade, sends Telegram notification via `tg_bot.notify()`.
  4. Catches all exceptions, logs error, continues.
- **Functions called:** `trade_mgr.check_fills()` → internally calls `client.get_positions()`, `client.get_open_orders()`, `_place_tpsl()`, `save_trades()`.
- **State read:** `trade_mgr.active_trades`, `runtime["running"]`.
- **State written:** `trade_mgr.active_trades` (marks trades as filled, removes cancelled trades), `active_trades.json` (via `save_trades()`).
- **Failure behavior:** Catches `Exception`, logs with traceback, continues loop. Does NOT alert user.
- **Can it be disabled?** No — always runs if `active_trades` is non-empty. No config flag.

### 2. order_update_loop

- **File:** `main.py`, lines 66-120.
- **Interval:** Aligns to candle boundaries. Computes `min_tf` across all active trades (default 5m), sleeps until next `min_tf` candle close + 3 seconds.
- **What it does each iteration:**
  1. Determines which timeframes just had candles close (checks `now2 % tf_sec < 10`).
  2. Skips if no candle boundary hit.
  3. Calls `trade_mgr.update_pending_orders(closed_tfs)` via `run_in_executor`.
  4. For each updated trade, sends Telegram notification with old/new levels.
  5. Catches all exceptions, logs error, continues.
- **Functions called:** `trade_mgr.update_pending_orders()` → internally calls `client.get_open_orders()`, `client.get_positions()`, `validate_signal()`, `client.cancel_all_orders()`, `client.place_limit_order()`, `_place_tpsl()`, `save_trades()`, `client.get_tick_size()`.
- **State read:** `trade_mgr.active_trades`, `runtime["running"]`.
- **State written:** `trade_mgr.active_trades` (updates entry/SL/TP/size/OID), `active_trades.json` (via `save_trades()`).
- **Failure behavior:** Catches `Exception`, logs with traceback, continues loop. Does NOT alert user.
- **Can it be disabled?** No — always runs if `active_trades` is non-empty. No config flag.

### 3. regime_watcher_loop

- **File:** `main.py`, lines 153-200.
- **Interval:** Every 900 seconds (15 minutes), configurable via `REGIME_WATCHER_INTERVAL_SECONDS`.
- **What it does each iteration:**
  1. Loads regime state from disk via `load_regime_state()`.
  2. Calls `trade_mgr.scan_regime()` → `strategy.filters.scan_1h_regime()`.
  3. For each token in `SCAN_WATCHLIST`:
     - Derives state from scan result via `_derive_state_from_scan()`.
     - Updates token state via `update_token_state()`.
     - Checks if should alert via `should_alert()`.
     - If yes, sends regime alert via `telegram_bot.send_regime_alert()`, then marks alerted via `mark_alerted()`.
  4. Saves regime state via `save_regime_state()`.
  5. Catches all exceptions, logs, continues.
- **Functions called:** `load_regime_state()`, `trade_mgr.scan_regime()`, `scan_1h_regime()`, `_derive_state_from_scan()`, `update_token_state()`, `should_alert()`, `send_regime_alert()`, `mark_alerted()`, `save_regime_state()`.
- **State read:** `runtime["running"]`, `data/regime_state.json` (via `load_regime_state()`).
- **State written:** `data/regime_state.json` (via `save_regime_state()`).
- **Failure behavior:** Catches `Exception`, logs with traceback, continues loop. Does NOT alert user.
- **Can it be disabled?** Yes — set `REGIME_WATCHER_ENABLED = False` in config.

---

## SECTION 3 — MODULE INVENTORY

### main.py

- **Responsibility:** Application entry point — initializes all components, starts Telegram bot, and manages three background loops.
- **Public functions:**
  - `main()` → None
    - Description: Entry point — validates config, creates ParadexClient, TradeManager, MomathiTelegramBot, starts polling.
    - Called by: `if __name__ == "__main__"` block.
    - Calls: `validate()`, `ParadexClient.__init__()`, `TradeManager.__init__()`, `MomathiTelegramBot.build()`, `app.run_polling()`.
  - `post_init(app)` → None (async)
    - Description: Called after Telegram app init — registers bot commands and starts background tasks.
    - Called by: Telegram framework (assigned to `app.post_init`).
    - Calls: `tg_bot.set_commands()`, `asyncio.create_task()` for each loop, `app.bot.send_message()`.
  - `fill_check_loop(trade_mgr, tg_bot)` → None (async)
    - Description: Background loop detecting filled entries every 60s.
    - Called by: `post_init()`.
    - Calls: `trade_mgr.check_fills()`, `tg_bot.notify()`.
  - `order_update_loop(trade_mgr, tg_bot)` → None (async)
    - Description: Background loop updating pending orders at candle boundaries.
    - Called by: `post_init()`.
    - Calls: `trade_mgr.update_pending_orders()`, `tg_bot.notify()`.
  - `regime_watcher_loop(trade_mgr, telegram_bot)` → None (async)
    - Description: Background loop monitoring 1H regime changes.
    - Called by: `post_init()` (if `REGIME_WATCHER_ENABLED`).
    - Calls: `load_regime_state()`, `trade_mgr.scan_regime()`, `_derive_state_from_scan()`, `update_token_state()`, `should_alert()`, `telegram_bot.send_regime_alert()`, `mark_alerted()`, `save_regime_state()`.
- **Private functions:**
  - `_derive_state_from_scan(scan_result, token)` → str [internal]
    - Description: Maps scan_regime output for a single token into CLEAN_LONG, CLEAN_SHORT, TANGLED, or INSUFFICIENT_DATA.
    - Called by: `regime_watcher_loop()`.
    - Calls: None.
- **Module-level constants/state:**
  - `ORDER_UPDATE_INTERVAL = 60` (UNUSED — dead code)
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `asyncio`, `logging`, `sys`, `time`, `datetime`
  - Project modules: `config.settings`, `exchange.paradex_client`, `trading.trade_manager`, `trading.regime_state`, `bot.telegram_bot`, `utils.logger`, `utils.errors`

### config/settings.py

- **Responsibility:** Loads environment variables, defines all configuration constants, provides mutable runtime state dict, validates required env vars.
- **Public functions:**
  - `validate()` → None
    - Description: Validates that all required env vars are set (PARADEX_L1_ADDRESS, PARADEX_PRIVATE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Raises ConfigError if missing.
    - Called by: `main()`.
    - Calls: None (imports ConfigError from utils.errors).
- **Private functions:** None.
- **Module-level constants/state:**
  - `PARADEX_L1_ADDRESS` — str (from env var)
  - `PARADEX_PRIVATE_KEY` — str (from env var)
  - `PARADEX_ENV` — str (from env var, default "TESTNET")
  - `TELEGRAM_BOT_TOKEN` — str (from env var)
  - `TELEGRAM_CHAT_ID` — str (from env var)
  - `runtime` — dict with keys: `risk_usd` (float), `coin` (str), `running` (bool)
  - `EMA_FAST = 8` — int
  - `EMA_SLOW = 30` — int
  - `CANDLE_LIMIT = 200` — int
  - `MIN_CANDLES = 50` — int
  - `SCAN_WATCHLIST` — list[str] (9 tokens)
  - `SCAN_SPREAD_THRESHOLD = 0.3` — float
  - `SCAN_SLOPE_LOOKBACK = 5` — int
  - `SCAN_SLOPE_THRESHOLD = 0.05` — float
  - `REGIME_WATCHER_ENABLED = True` — bool
  - `REGIME_WATCHER_INTERVAL_SECONDS = 900` — int
  - `REGIME_CONFIRMATION_CYCLES = 2` — int
  - `REGIME_ALERT_ON_ENTER_CLEAN = True` — bool
  - `REGIME_ALERT_ON_LEAVE_CLEAN = False` — bool
  - `REGIME_ALERT_COOLDOWN_HOURS = 4` — int
  - `REGIME_STATE_FILE = "data/regime_state.json"` — str
- **External dependencies:**
  - Third-party: `os`, `dotenv.load_dotenv`
  - Project modules: None

### exchange/paradex_client.py

- **Responsibility:** Thin wrapper around paradex-py SDK for all order management, position queries, and market data.
- **Class:** `ParadexClient`
  - `__init__()` → None
    - Description: Initializes Paradex SDK client with env credentials.
    - Calls: `Paradex()` constructor.
  - `place_limit_order(coin, is_buy, size, price)` → dict
    - Description: Places a GTC limit order. Returns normalized response with status, oid, raw response.
    - Called by: `trade_manager.execute_trade()`, `trade_manager.update_pending_orders()`.
    - Calls: `_coin_to_symbol()`, `_round_size()`, `_round_price()`, `client.api_client.submit_order()`.
  - `place_trigger_order(coin, is_buy, size, trigger_px, tpsl, reduce_only=True)` → dict
    - Description: Places a trigger order (TP or SL). Returns normalized response with status, oid.
    - Called by: `trade_manager._place_tpsl()`.
    - Calls: `_coin_to_symbol()`, `_round_size()`, `_round_price()`, `client.api_client.submit_order()`.
  - `place_batch_orders(orders_data)` → dict
    - Description: [DEAD CODE] Body is `pass`. Never called.
  - `cancel_all_orders(coin)` → list
    - Description: Cancels all open orders for a specific coin.
    - Called by: `trade_manager.update_pending_orders()`.
    - Calls: `_coin_to_symbol()`, `client.api_client.cancel_all_orders()`.
  - `cancel_all()` → list
    - Description: Cancels ALL open orders across all coins.
    - Called by: `paradex_client.close_all_positions()`.
    - Calls: `client.api_client.cancel_all_orders()`.
  - `cancel_order(oid)` → bool
    - Description: Cancels a single order by OID.
    - Called by: None (dead code — never called).
    - Calls: `client.api_client.cancel_order()`.
  - `place_market_order(coin, is_buy, size)` → dict
    - Description: Places an IOC market order (for pyramid add-on). Never called by production code.
    - Called by: None (dead code).
    - Calls: `_coin_to_symbol()`, `_round_size()`, `client.api_client.submit_order()`.
  - `get_positions()` → list
    - Description: Returns list of open positions with normalized details (coin, symbol, size, entry_px, unrealized_pnl, liquidation_px).
    - Called by: `trade_manager.check_fills()`, `trade_manager.update_pending_orders()`, `trade_manager.get_pnl()`, `trade_manager.get_status()`, `paradex_client.close_position()`, `trade_manager._position_still_open()`.
    - Calls: `client.api_client.fetch_positions()`, `_symbol_to_coin()`.
  - `get_balance()` → dict
    - Description: Returns account_value, total_margin_used, withdrawable.
    - Called by: `telegram_bot.cmd_balance()`.
    - Calls: `client.api_client.fetch_account_summary()`.
  - `get_open_orders()` → list
    - Description: Returns all open/resting orders with normalized format (oid, coin, side, sz, limitPx).
    - Called by: `trade_manager.check_fills()`, `trade_manager.update_pending_orders()`, `trade_manager.get_status()`, `telegram_bot.cmd_status()`.
    - Calls: `client.api_client.fetch_orders()`, `_symbol_to_coin()`.
  - `close_position(coin)` → dict | None
    - Description: Market-closes a specific position (IOC, reduce_only).
    - Called by: `paradex_client.close_all_positions()`.
    - Calls: `_coin_to_symbol()`, `get_positions()`, `client.api_client.submit_order()`.
  - `close_all_positions()` → list
    - Description: Cancels all orders, then market-closes every open position.
    - Called by: `trade_manager.close_all()`.
    - Calls: `cancel_all()`, `get_positions()`, `close_position()`.
  - `get_tick_size(coin)` → float
    - Description: Returns the price tick_size for a coin's market (with fallback estimation).
    - Called by: `trade_manager.update_pending_orders()`.
    - Calls: `_coin_to_symbol()`, `_get_market_info()`, `client.api_client.fetch_bbo()` (fallback).
- **Private methods:**
  - `_get_markets()` → dict [internal]
    - Description: Gets and caches markets metadata.
    - Called by: `_get_market_info()`.
    - Calls: `client.api_client.fetch_markets()`.
  - `_get_market_info(symbol)` → dict | None [internal]
    - Description: Returns market metadata for a symbol.
    - Called by: `_round_size()`, `_round_price()`, `get_tick_size()`.
    - Calls: `_get_markets()`, `_get_field()`.
  - `_coin_to_symbol(coin)` → str [internal]
    - Description: Converts "BTC" to "BTC-USD-PERP".
    - Called by: Most order methods.
  - `_symbol_to_coin(symbol)` → str [internal]
    - Description: Converts "BTC-USD-PERP" to "BTC".
    - Called by: `get_positions()`, `get_open_orders()`.
  - `_get_field(obj, field, default=None)` → any [internal]
    - Description: Safely gets a field from dict or object.
    - Called by: `_get_market_info()`, `_round_size()`, `_round_price()`, `get_tick_size()`.
  - `_round_size(size, symbol)` → Decimal [internal]
    - Description: Rounds size down to market's asset step_size.
    - Called by: `place_limit_order()`, `place_trigger_order()`, `place_market_order()`.
  - `_round_price(price, symbol)` → Decimal [internal]
    - Description: Rounds price to nearest tick_size.
    - Called by: `place_limit_order()`, `place_trigger_order()`.
- **Module-level constants/state:**
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `logging`, `math`, `decimal.Decimal`, `paradex_py.Paradex`, `paradex_py.environment.Environment`, `paradex_py.common.order` (Order, OrderType, OrderSide)
  - Project modules: `config.settings`

### strategy/ema_setup.py

- **Responsibility:** Core strategy engine — fetches candles, computes EMAs, determines trend, validates signals, calculates trade levels (entry/SL/TP).
- **Public functions:**
  - `fetch_candles(coin, resolution="5", paradex_client=None)` → pd.DataFrame
    - Description: Fetches OHLCV candles from Paradex REST API. Drops the live candle, returns only closed candles.
    - Called by: `validate_signal()`, `scan_1h_regime()`, `get_ema30()`, `get_mark_price()`.
    - Calls: `_get_auth_headers()`, `requests.get()`, `pd.DataFrame()`, `pd.to_datetime()`.
  - `compute_emas(df)` → pd.DataFrame
    - Description: Adds ema8, ema15, ema30 columns to a DataFrame with 'close' column.
    - Called by: `validate_signal()`, `scan_1h_regime()`, `get_ema30()`.
    - Calls: `df["close"].ewm().mean()` (pandas).
  - `get_trend(df)` → str
    - Description: Returns "LONG" if ema8 > ema30, else "SHORT".
    - Called by: `validate_signal()`.
  - `calculate_levels(ema8, ema30, direction)` → dict
    - Description: Calculates entry (=ema8), SL (=ema30), TP (1:3 RR). Returns dict with entry, sl, tp, risk_per_unit.
    - Called by: `validate_signal()`.
    - Calls: `_price_precision()`.
  - `validate_signal(direction, coin, exec_tf="5", paradex_client=None)` → dict
    - Description: Full signal validation — fetches 1H candles for trend, exec_tf candles for levels, checks trend alignment, returns levels if valid.
    - Called by: `trade_manager.validate_signal()`, `trade_manager.update_pending_orders()`.
    - Calls: `fetch_candles()` (twice: 1H + exec_tf), `compute_emas()`, `get_trend()`, `calculate_levels()`.
  - `get_ema30(coin, exec_tf="5", paradex_client=None)` → float | None
    - Description: Lightweight fetch of latest EMA30 value. [DEAD CODE — never called by production code].
    - Called by: None.
    - Calls: `fetch_candles()`, `compute_emas()`.
  - `get_mark_price(coin, paradex_client=None)` → float | None
    - Description: Returns current approximate price from latest 5m candle close. [DEAD CODE — never called by production code].
    - Called by: None.
    - Calls: `fetch_candles()`.
- **Private functions:**
  - `_get_auth_headers(paradex_client)` → dict [internal]
    - Description: Extracts JWT Bearer token from ParadexClient for authenticated API requests.
    - Called by: `fetch_candles()`.
  - `_price_precision(price)` → int [internal]
    - Description: Determines decimal places needed for a price level (2 for >=1000, 3 for >=10, 4 for >=1, 5 otherwise).
    - Called by: `calculate_levels()`, `validate_signal()`.
- **Module-level constants/state:**
  - `_is_prod` — bool (computed from PARADEX_ENV)
  - `_PARADEX_API_URL` — str (computed from _is_prod)
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `logging`, `time`, `requests`, `pandas`, `datetime`, `typing.Optional`
  - Project modules: `config.settings`, `exchange.paradex_client`

### strategy/filters.py

- **Responsibility:** 1H EMA regime classification for token watchlist scanning — classifies tokens as CLEAN LONG, CLEAN SHORT, or TANGLED.
- **Public functions:**
  - `scan_1h_regime(paradex_client, coins=None)` → dict
    - Description: Scans watchlist on 1H timeframe, classifies each token based on EMA ordering, slope direction, and spread threshold. Returns dict with long_bias, short_bias, tangled, errors, timestamp, last_candle_close.
    - Called by: `trade_manager.scan_regime()`.
    - Calls: `fetch_candles()`, `compute_emas()`.
- **Private functions:** None.
- **Module-level constants/state:**
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `logging`, `datetime`, `typing` (Optional, List, Dict)
  - Project modules: `config.settings`, `exchange.paradex_client`, `strategy.ema_setup`

### trading/trade_manager.py

- **Responsibility:** Orchestrates trade lifecycle — position sizing, entry execution, TP/SL placement, fill detection, order updates, PnL tracking.
- **Class:** `TradeManager`
  - `__init__(client)` → None
    - Description: Stores ParadexClient, loads active trades from disk.
    - Called by: `main()`.
    - Calls: `load_trades()`.
  - `calculate_size(entry, sl, risk_usd=None)` → float
    - Description: Calculates position size from fixed USD risk: size = risk_usd / |entry - sl|.
    - Called by: `execute_trade()`, `update_pending_orders()`.
  - `execute_trade(coin, direction, levels, exec_tf="5")` → dict
    - Description: Executes a trade — places limit entry order, creates trade record, places TP/SL if filled immediately.
    - Called by: `telegram_bot._handle_trade()`.
    - Calls: `calculate_size()`, `client.place_limit_order()`, `_place_tpsl()` (if filled), `save_trades()`.
  - `check_fills()` → list[dict]
    - Description: Lightweight fill detector — checks pending entries, places TP/SL when filled, cleans up closed trades.
    - Called by: `main.fill_check_loop()`.
    - Calls: `client.get_positions()`, `client.get_open_orders()`, `_place_tpsl()`, `save_trades()`.
  - `update_pending_orders(closed_tfs=None)` → list[dict]
    - Description: Re-fetches EMAs, updates unfilled limit entry + SL + TP orders to latest levels.
    - Called by: `main.order_update_loop()`.
    - Calls: `client.get_open_orders()`, `client.get_positions()`, `validate_signal()`, `client.cancel_all_orders()`, `client.place_limit_order()`, `_place_tpsl()`, `client.get_tick_size()`, `calculate_size()`, `save_trades()`.
  - `get_pnl()` → list
    - Description: Returns unrealized PnL for all open positions.
    - Called by: `telegram_bot.cmd_pnl()`.
    - Calls: `client.get_positions()`.
  - `close_all()` → dict
    - Description: Closes all positions, cancels all orders, clears tracked trades.
    - Called by: `telegram_bot.cmd_close_all()`.
    - Calls: `client.close_all_positions()`, `save_trades()`.
  - `get_status()` → dict
    - Description: Returns positions, open orders, tracked trades count.
    - Called by: `telegram_bot.cmd_status()`.
    - Calls: `client.get_positions()`, `client.get_open_orders()`.
  - `validate_signal(direction, coin, exec_tf="5")` → dict
    - Description: Wrapper around strategy.validate_signal with ParadexClient injection.
    - Called by: `telegram_bot._handle_trade()`, `trade_manager.update_pending_orders()`.
    - Calls: `ema_setup.validate_signal()`.
  - `scan_regime()` → dict
    - Description: Wrapper around strategy.scan_1h_regime with ParadexClient injection.
    - Called by: `main.regime_watcher_loop()`, `telegram_bot.cmd_scan()`.
    - Calls: `filters.scan_1h_regime()`.
- **Private methods:**
  - `_position_still_open(coin)` → bool [internal]
    - Description: Checks if an open position exists for a coin on the exchange. [DEAD CODE — never called].
    - Called by: None.
    - Calls: `client.get_positions()`.
  - `_cleanup_closed_trade(trade)` → None [internal]
    - Description: Removes a trade whose position no longer exists. [DEAD CODE — logic inlined in check_fills()].
    - Called by: None.
    - Calls: `save_trades()`.
  - `_place_tpsl(trade)` → None [internal]
    - Description: Places TP and SL trigger orders for a filled trade. Raises Exception if either fails.
    - Called by: `execute_trade()`, `check_fills()`, `update_pending_orders()`.
    - Calls: `client.place_trigger_order()` (twice: SL + TP).
- **Module-level constants/state:**
  - `TRADES_FILE = "active_trades.json"` (UNUSED — shadows import from trading.state)
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `json`, `logging`, `os`, `datetime`
  - Project modules: `config.settings`, `exchange.paradex_client`, `strategy.ema_setup`, `trading.state`

### trading/regime_state.py

- **Responsibility:** Handles loading, saving, and updating regime state for the background regime watcher.
- **Public functions:**
  - `load_regime_state()` → dict
    - Description: Loads regime state from data/regime_state.json. Returns empty dict if file missing/corrupted.
    - Called by: `main.regime_watcher_loop()`.
  - `save_regime_state(state)` → None
    - Description: Persists regime state to disk atomically (writes to .tmp, then os.replace).
    - Called by: `main.regime_watcher_loop()`.
  - `update_token_state(state, token, new_state, now_utc)` → dict
    - Description: Updates regime state for a single token (mutates in place). Increments consecutive_count if same state, resets if changed.
    - Called by: `main.regime_watcher_loop()`.
  - `should_alert(state, token, confirmation_cycles, cooldown_hours, alert_on_enter, alert_on_leave, now_utc)` → tuple[bool, str | None]
    - Description: Determines if a regime change alert should be sent based on state change, consecutive confirmations, cooldown, and alert type flags.
    - Called by: `main.regime_watcher_loop()`.
  - `mark_alerted(state, token, now_utc)` → dict
    - Description: Marks a token as having been alerted (sets last_alert_state and last_alert_at).
    - Called by: `main.regime_watcher_loop()`.
- **Private functions:** None.
- **Module-level constants/state:**
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `json`, `logging`, `os`, `datetime`
  - Project modules: `config.settings`

### trading/state.py

- **Responsibility:** Handles loading and saving active_trades.json.
- **Public functions:**
  - `load_trades()` → list
    - Description: Loads active trades from active_trades.json. Returns empty list if file missing/corrupted.
    - Called by: `TradeManager.__init__()`.
  - `save_trades(trades)` → None
    - Description: Persists active trades to active_trades.json.
    - Called by: `TradeManager.execute_trade()`, `TradeManager.check_fills()`, `TradeManager.update_pending_orders()`, `TradeManager.close_all()`, `TradeManager._cleanup_closed_trade()` (dead code).
- **Private functions:** None.
- **Module-level constants/state:**
  - `TRADES_FILE = "active_trades.json"` — str
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `json`, `logging`, `os`
  - Project modules: None

### bot/telegram_bot.py

- **Responsibility:** Telegram interface — all command handlers, notification system, auth gate.
- **Decorator:**
  - `auth(func)` → wrapper
    - Description: Restricts commands to authorized chat (TELEGRAM_CHAT_ID). Returns "⛔ Unauthorized." if chat ID doesn't match.
    - Applied to: All command handlers.
- **Class:** `MomathiTelegramBot`
  - `__init__(trade_manager)` → None
    - Description: Stores TradeManager reference.
    - Called by: `main()`.
  - `build()` → Application
    - Description: Builds Telegram Application with all handlers registered.
    - Called by: `main()`.
    - Calls: `Application.builder().token().build()`, `CommandHandler()` for each command, `MessageHandler()` for trade commands, `add_error_handler()`.
  - `set_commands()` → None (async)
    - Description: Registers bot commands in Telegram menu (shows in / command list).
    - Called by: `main.post_init()`.
    - Calls: `app.bot.set_my_commands()`.
  - `notify(text)` → None (async)
    - Description: Sends a notification message to the authorized chat.
    - Called by: `main.fill_check_loop()`, `main.order_update_loop()`, `send_regime_alert()`.
    - Calls: `app.bot.send_message()`.
  - `send_regime_alert(token, token_state, alert_type)` → None (async)
    - Description: Sends a regime change notification (ENTERED_CLEAN or LEFT_CLEAN).
    - Called by: `main.regime_watcher_loop()`.
    - Calls: `notify()`.
  - `cmd_start(update, context)` → None (async)
    - Description: /start command handler.
    - Calls: `update.message.reply_text()`.
  - `cmd_trade(update, context)` → None (async)
    - Description: Generic trade message handler (regex match).
    - Calls: `_handle_trade()`.
  - `cmd_trade_fallback(update, context)` → None (async)
    - Description: Fallback for unknown /commands.
    - Calls: `_handle_trade()`.
  - `cmd_status(update, context)` → None (async)
    - Description: /status command handler.
    - Calls: `trade_mgr.get_status()`, `update.message.reply_text()`.
  - `cmd_balance(update, context)` → None (async)
    - Description: /balance command handler.
    - Calls: `trade_mgr.client.get_balance()`, `update.message.reply_text()`.
  - `cmd_pnl(update, context)` → None (async)
    - Description: /pnl command handler.
    - Calls: `trade_mgr.get_pnl()`, `update.message.reply_text()`.
  - `cmd_set_risk(update, context)` → None (async)
    - Description: /set_risk command handler.
    - Calls: `update.message.reply_text()`.
  - `cmd_get_risk(update, context)` → None (async)
    - Description: /get_risk command handler.
    - Calls: `update.message.reply_text()`.
  - `cmd_close_all(update, context)` → None (async)
    - Description: /close_all command handler.
    - Calls: `trade_mgr.close_all()`, `update.message.reply_text()`.
  - `cmd_stop_bot(update, context)` → None (async)
    - Description: /stop_bot command handler.
    - Calls: `update.message.reply_text()`, sets `runtime["running"] = False`, schedules SIGINT.
  - `cmd_scan(update, context)` → None (async)
    - Description: /scan command handler.
    - Calls: `trade_mgr.scan_regime()`, `msg.edit_text()`.
- **Private methods:**
  - `_parse_trade_command(text)` → tuple[coin, direction, exec_tf] [internal]
    - Description: Parses trade command string. Raises ValueError if invalid.
    - Called by: `_handle_trade()`.
  - `_handle_trade(update, context)` → None (async) [internal]
    - Description: Handles trade command — parses, validates signal, executes trade, sends formatted result.
    - Called by: `cmd_trade()`, `cmd_trade_fallback()`.
    - Calls: `_parse_trade_command()`, `trade_mgr.validate_signal()`, `trade_mgr.execute_trade()`, `update.message.reply_text()`.
  - `_error_handler(update, context)` → None (async) [internal]
    - Description: Logs errors and notifies user of Telegram errors.
    - Called by: Telegram framework (registered as error handler).
    - Calls: `context.bot.send_message()`.
- **Module-level constants/state:**
  - `logger` — logging.Logger instance
- **External dependencies:**
  - Third-party: `logging`, `os`, `signal`, `asyncio`, `functools.wraps`, `telegram` (Update, BotCommand), `telegram.ext` (Application, CommandHandler, MessageHandler, ContextTypes, filters)
  - Project modules: `config.settings`, `trading.trade_manager`

### utils/logger.py

- **Responsibility:** Centralized logging setup for the application.
- **Public functions:**
  - `setup_logger(level=logging.INFO)` → None
    - Description: Configures logging with console (stdout) and file (momathi.log) handlers.
    - Called by: `main.py` (module level, line 29).
- **Private functions:** None.
- **External dependencies:**
  - Third-party: `logging`, `sys`

### utils/errors.py

- **Responsibility:** Custom exception definitions.
- **Classes:**
  - `ParadexAPIError(Exception)` — [DEAD CODE — never raised or caught].
  - `TradeExecutionError(Exception)` — [DEAD CODE — never raised or caught].
  - `ConfigError(Exception)` — Used by `config.settings.validate()` and caught in `main()`.
- **External dependencies:** None.

### scripts/debug_api_format.py

- **Responsibility:** Standalone diagnostic script for debugging Paradex API response format.
- **Not called by any production code.**
- **External dependencies:** `config.settings`, `paradex_py.Paradex`, `time`

### scripts/debug_trigger_orders.py

- **Responsibility:** Standalone diagnostic script for debugging trigger orders and order history.
- **Not called by any production code.**
- **External dependencies:** `os`, `time`, `config.settings`, `paradex_py.Paradex`

### Package __init__.py Files

All `__init__.py` files contain only a package docstring. No code:
- `bot/__init__.py`, `config/__init__.py`, `exchange/__init__.py`, `strategy/__init__.py`, `trading/__init__.py`, `utils/__init__.py`, `scripts/__init__.py`, `tests/__init__.py`

---

## SECTION 4 — DATA FLOW MAP

### Flow A: User Places Manual Trade

**Trigger:** User sends `/btc long 5` in Telegram.

| Step | File:Function | Action | State Changes |
|------|--------------|--------|---------------|
| 1 | `bot/telegram_bot.py:cmd_trade` or `cmd_trade_fallback` | Receives message, routes to `_handle_trade()`. | None |
| 2 | `bot/telegram_bot.py:_handle_trade` | Parses text via `_parse_trade_command()` → extracts coin="BTC", direction="LONG", exec_tf="5". Sends "Validating signal..." message. | None |
| 3 | `bot/telegram_bot.py:_handle_trade` | Calls `trade_mgr.validate_signal("LONG", "BTC", "5")` via `run_in_executor`. | None |
| 4 | `trading/trade_manager.py:validate_signal` | Delegates to `ema_setup.validate_signal()`. | None |
| 5 | `strategy/ema_setup.py:validate_signal` | Fetches 1H candles via `fetch_candles(coin, "60")`. Drops live candle. | None |
| 6 | `strategy/ema_setup.py:compute_emas` | Computes ema8, ema15, ema30 on 1H DataFrame. | None |
| 7 | `strategy/ema_setup.py:get_trend` | Determines trend: "LONG" if ema8 > ema30, else "SHORT". | None |
| 8 | `strategy/ema_setup.py:validate_signal` | Checks if direction matches trend. Rejects if mismatch. | None |
| 9 | `strategy/ema_setup.py:validate_signal` | Fetches exec_tf (5m) candles via `fetch_candles(coin, "5")`. Computes EMAs. | None |
| 10 | `strategy/ema_setup.py:calculate_levels` | Calculates entry=ema8, SL=ema30, TP=entry±3*risk. Returns dict. | None |
| 11 | `strategy/ema_setup.py:validate_signal` | Returns `{"valid": True, "trend": "...", "levels": {...}, ...}`. | None |
| 12 | `bot/telegram_bot.py:_handle_trade` | Receives valid result. Sends "Signal VALID" message with levels. | None |
| 13 | `bot/telegram_bot.py:_handle_trade` | Calls `trade_mgr.execute_trade("BTC", "LONG", levels, "5")` via `run_in_executor`. | None |
| 14 | `trading/trade_manager.py:execute_trade` | Calls `calculate_size(entry, sl)` → returns size = risk_usd / |entry - sl|. | None |
| 15 | `trading/trade_manager.py:execute_trade` | Calls `client.place_limit_order("BTC", is_buy=True, size, entry)`. | None |
| 16 | `exchange/paradex_client.py:place_limit_order` | Rounds size/price, submits GTC limit order to Paradex API. Returns response with OID. | None |
| 17 | `trading/trade_manager.py:execute_trade` | Creates trade dict with coin, direction, entry, sl, tp, size, entry_oid, filled=False, timestamp. Appends to `self.active_trades`. | `self.active_trades` updated in memory |
| 18 | `trading/trade_manager.py:execute_trade` | Calls `save_trades(self.active_trades)`. | `active_trades.json` written to disk |
| 19 | `bot/telegram_bot.py:_handle_trade` | Sends "Trade Placed" message with order statuses. | None |

**If entry fills immediately (rare for limit orders):**
| Step 20 | `trading/trade_manager.py:execute_trade` | Calls `_place_tpsl(trade)`. | None |
| Step 21 | `trading/trade_manager.py:_place_tpsl` | Calls `client.place_trigger_order()` for SL, then TP. Updates trade["sl_oid"], trade["tp_oid"]. | None |
| Step 22 | `trading/trade_manager.py:execute_trade` | Saves updated trade. | `active_trades.json` updated |

### Flow B: Automatic Fill Detection

**Trigger:** `fill_check_loop` runs every 60 seconds.

| Step | File:Function | Action | State Changes |
|------|--------------|--------|---------------|
| 1 | `main.py:fill_check_loop` | Sleeps 60s, checks if `active_trades` is non-empty. | None |
| 2 | `main.py:fill_check_loop` | Calls `trade_mgr.check_fills()` via `run_in_executor`. | None |
| 3 | `trading/trade_manager.py:check_fills` | **Cleanup phase:** Calls `client.get_positions()` to get all open coins. Removes any filled trades whose coin is not in open positions (TP/SL hit on exchange). | `self.active_trades` modified, `active_trades.json` updated |
| 4 | `trading/trade_manager.py:check_fills` | **Fill detection phase:** Calls `client.get_open_orders()`. | None |
| 5 | `trading/trade_manager.py:check_fills` | For each unfilled trade: checks if entry_oid is still in open_orders. | None |
| 6 | `trading/trade_manager.py:check_fills` | If entry_oid NOT in open_orders: calls `client.get_positions()` to check if position exists for that coin. | None |
| 7 | `trading/trade_manager.py:check_fills` | If position exists: marks `trade["filled"] = True`, calls `_place_tpsl(trade)`, adds to `filled_trades` list. | `trade["filled"]` set to True, `trade["sl_oid"]`, `trade["tp_oid"]` set |
| 8 | `trading/trade_manager.py:check_fills` | If position does NOT exist: removes trade from `active_trades` (order was cancelled externally). | `self.active_trades` modified, `active_trades.json` updated |
| 9 | `trading/trade_manager.py:check_fills` | If entry_oid is None: removes trade (untrackable). | `self.active_trades` modified, `active_trades.json` updated |
| 10 | `trading/trade_manager.py:check_fills` | Calls `save_trades()` if any changes made. | `active_trades.json` updated |
| 11 | `trading/trade_manager.py:check_fills` | Returns `filled_trades` list. | None |
| 12 | `main.py:fill_check_loop` | For each filled trade, constructs message and calls `tg_bot.notify()`. | None |
| 13 | `bot/telegram_bot.py:notify` | Sends message to `TELEGRAM_CHAT_ID` via Telegram API. | None |

### Flow C: Regime Watcher Alert

**Trigger:** `regime_watcher_loop` runs every 900 seconds (15 minutes).

| Step | File:Function | Action | State Changes |
|------|--------------|--------|---------------|
| 1 | `main.py:regime_watcher_loop` | Checks `REGIME_WATCHER_ENABLED`. If False, returns immediately. | None |
| 2 | `main.py:regime_watcher_loop` | Calls `load_regime_state()`. | Reads `data/regime_state.json` |
| 3 | `main.py:regime_watcher_loop` | Calls `trade_mgr.scan_regime()`. | None |
| 4 | `trading/trade_manager.py:scan_regime` | Delegates to `filters.scan_1h_regime()`. | None |
| 5 | `strategy/filters.py:scan_1h_regime` | For each coin in SCAN_WATCHLIST: fetches 1H candles, computes EMAs, calculates spread and slopes, classifies as long_bias/short_bias/tangled. | None |
| 6 | `strategy/filters.py:scan_1h_regime` | Returns dict with long_bias, short_bias, tangled, errors lists. | None |
| 7 | `main.py:regime_watcher_loop` | For each token in SCAN_WATCHLIST: calls `_derive_state_from_scan(scan_result, token)`. | None |
| 8 | `main.py:_derive_state_from_scan` | Maps token to one of: CLEAN_LONG, CLEAN_SHORT, TANGLED, INSUFFICIENT_DATA. Returns string. | None |
| 9 | `main.py:regime_watcher_loop` | Calls `update_token_state(state, token, new_state, now)`. Mutates state dict in place. | `state[token]` updated in memory |
| 10 | `main.py:regime_watcher_loop` | Calls `should_alert(state, token, ...)` — checks state change, consecutive confirmations, cooldown, alert type flags. | None |
| 11 | `main.py:regime_watcher_loop` | If `should_alert` returns True: calls `telegram_bot.send_regime_alert(token, state[token], alert_type)`. | None |
| 12 | `bot/telegram_bot.py:send_regime_alert` | Constructs message based on alert_type (ENTERED_CLEAN or LEFT_CLEAN), calls `notify()`. | None |
| 13 | `bot/telegram_bot.py:notify` | Sends message to `TELEGRAM_CHAT_ID`. | None |
| 14 | `main.py:regime_watcher_loop` | Calls `mark_alerted(state, token, now)` — sets last_alert_state and last_alert_at. | `state[token]` updated in memory |
| 15 | `main.py:regime_watcher_loop` | Calls `save_regime_state(state)`. | `data/regime_state.json` written to disk |

### Flow D: Manual /scan Command

**Trigger:** User sends `/scan` in Telegram.

| Step | File:Function | Action | State Changes |
|------|--------------|--------|---------------|
| 1 | `bot/telegram_bot.py:cmd_scan` | Sends initial "Scanning..." message. | None |
| 2 | `bot/telegram_bot.py:cmd_scan` | Calls `trade_mgr.scan_regime()` via `run_in_executor`. | None |
| 3 | `trading/trade_manager.py:scan_regime` | Delegates to `filters.scan_1h_regime()`. | None |
| 4 | `strategy/filters.py:scan_1h_regime` | For each coin in SCAN_WATCHLIST: fetches 1H candles, computes EMAs, classifies. | None |
| 5 | `strategy/filters.py:scan_1h_regime` | Returns result dict with long_bias, short_bias, tangled, errors, timestamp, last_candle_close. | None |
| 6 | `bot/telegram_bot.py:cmd_scan` | Formats message: builds lines for each category (long bias, short bias, tangled, errors). | None |
| 7 | `bot/telegram_bot.py:cmd_scan` | Calls `msg.edit_text()` with formatted message. | None |

---

## SECTION 5 — STATE & PERSISTENCE

### Persisted Files

#### 1. active_trades.json (root directory)

- **Schema:**
```json
[
  {
    "coin": "BTC",                    // str — trading pair
    "direction": "LONG",              // str — "LONG" or "SHORT"
    "entry": 77234.56,                // float — limit entry price
    "sl": 76890.12,                   // float — stop loss price
    "tp": 78273.44,                   // float — take profit price
    "size": 0.012345,                 // float — position size
    "risk_usd": 10.0,                 // float — USD risk for this trade
    "is_buy": true,                   // bool — True for long, False for short
    "exec_tf": "5",                   // str — execution timeframe ("5" or "15")
    "entry_oid": "12345678",          // str | None — Paradex order ID for entry
    "filled": false,                  // bool — True if entry order filled
    "sl_oid": null,                   // str | None — Paradex order ID for SL
    "tp_oid": null,                   // str | None — Paradex order ID for TP
    "timestamp": "2026-05-22T10:30:00", // str — ISO format when trade created
    "last_updated": "2026-05-22T10:35:00" // str | optional — ISO format when last updated
  }
]
```

- **Read by:** `trading.state.load_trades()` → called from `TradeManager.__init__()` at startup.
- **Written by:** `trading.state.save_trades()` → called from:
  - `TradeManager.execute_trade()` — adds new trade
  - `TradeManager.check_fills()` — marks trade as filled, removes cancelled trades
  - `TradeManager.update_pending_orders()` — updates entry/SL/TP/size/OID, removes cancelled trades
  - `TradeManager.close_all()` — clears all trades
  - `TradeManager._cleanup_closed_trade()` — [dead code, never called]
- **Lifecycle:** Created on first trade execution. Updated on every trade state change (fill, update, removal). Cleared (set to `[]`) on `/close_all` command or bot restart with no active trades.
- **If missing:** `load_trades()` returns empty list `[]`. Bot starts with no tracked trades.
- **If corrupted (invalid JSON):** `load_trades()` catches `Exception`, logs error, returns empty list `[]`. Trade history lost.

#### 2. data/regime_state.json

- **Schema:**
```json
{
  "BTC": {
    "current_state": "CLEAN_LONG",      // str — CLEAN_LONG, CLEAN_SHORT, TANGLED, INSUFFICIENT_DATA
    "previous_state": "TANGLED",        // str | null — state before current
    "consecutive_count": 3,             // int — how many consecutive checks with same state
    "last_alert_state": "TANGLED",      // str | null — state when last alert sent
    "last_alert_at": "2026-05-22T08:00:00Z", // str | null — ISO format when last alert sent
    "last_check_utc": "2026-05-22T09:34:11Z" // str — ISO format of last regime check
  }
}
```

- **Read by:** `trading.regime_state.load_regime_state()` → called from `main.regime_watcher_loop()` at start of each cycle.
- **Written by:** `trading.regime_state.save_regime_state()` → called from `main.regime_watcher_loop()` at end of each cycle.
- **Lifecycle:** Created/updated every 15 minutes (or `REGIME_WATCHER_INTERVAL_SECONDS`). Grows to include all tokens in `SCAN_WATCHLIST`. State persists across bot restarts.
- **If missing:** `load_regime_state()` returns empty dict `{}`. Watcher starts fresh, all tokens treated as new.
- **If corrupted (invalid JSON):** `load_regime_state()` catches `Exception`, logs error, returns empty dict `{}`. Alert history lost, may re-alert for tokens already alerted.

#### 3. momathi.log (root directory)

- **Schema:** Plain text log lines. Format: `%(asctime)s | %(name)-24s | %(levelname)-5s | %(message)s`
- **Written by:** Python `logging.FileHandler` configured in `utils.logger.setup_logger()`.
- **Lifecycle:** Appended to on every log message. Grows indefinitely (no rotation configured).
- **If missing:** Created automatically by `FileHandler`.

### In-Memory Mutable State

#### config.settings.runtime (dict)

| Key | Type | Initial Value | Mutated By | Persists Across Restart? |
|-----|------|---------------|------------|-------------------------|
| `risk_usd` | float | From `DEFAULT_RISK_USD` env var (default 10.0) | `/set_risk` command in `telegram_bot.cmd_set_risk()` | No — resets to env var on restart |
| `coin` | str | From `DEFAULT_COIN` env var (default "BTC") | Never mutated after startup | N/A |
| `running` | bool | `True` | `/stop_bot` command in `telegram_bot.cmd_stop_bot()` (sets to `False`) | N/A — process terminates |

---

## SECTION 6 — CONFIGURATION

### All config/settings.py Constants

| Constant | Current Value | Type | Controls | Hot-Reload? | Effect of Change |
|----------|--------------|------|----------|-------------|------------------|
| `PARADEX_L1_ADDRESS` | From `.env` | str | Paradex wallet address for API authentication | No (startup) | Bot cannot authenticate to Paradex |
| `PARADEX_PRIVATE_KEY` | From `.env` | str | Paradex signing key for order submission | No (startup) | Bot cannot sign orders |
| `PARADEX_ENV` | From `.env` (default "TESTNET") | str | Paradex network: "TESTNET", "PROD", or "MAINNET" | No (startup) | Switches between testnet/production API endpoints |
| `TELEGRAM_BOT_TOKEN` | From `.env` | str | Telegram Bot API token | No (startup) | Bot cannot connect to Telegram |
| `TELEGRAM_CHAT_ID` | From `.env` | str | Authorized chat ID for command access | No (startup) | All commands return "⛔ Unauthorized." |
| `runtime["risk_usd"]` | 10.0 (from `DEFAULT_RISK_USD`) | float | USD risk per trade (position sizing) | Yes (`/set_risk`) | Changes position size for all subsequent trades |
| `runtime["coin"]` | "BTC" (from `DEFAULT_COIN`) | str | Default coin when not specified in commands | No | Only used if `coin=None` passed to `fetch_candles()` or `validate_signal()` |
| `runtime["running"]` | `True` | bool | Master running flag for all loops | Yes (`/stop_bot`) | Setting to `False` stops all background loops |
| `EMA_FAST` | 8 | int | Fast EMA period (used for entry level) | No (startup) | Changes entry level calculation |
| `EMA_SLOW` | 30 | int | Slow EMA period (used for SL and trend) | No (startup) | Changes SL and trend determination |
| `CANDLE_LIMIT` | 200 | int | Number of candles to fetch from Paradex API | No (startup) | Affects EMA stability (more candles = more stable EMA) |
| `MIN_CANDLES` | 50 | int | Minimum closed candles required for reliable EMA30 | No (startup) | Below this, signal validation rejects trades |
| `SCAN_WATCHLIST` | [BTC, ETH, BNB, HYPE, SOL, ARB, LINK, XRP, ZEC] | list[str] | Tokens scanned in 1H regime scan | No (startup) | Changes which tokens are monitored for regime changes |
| `SCAN_SPREAD_THRESHOLD` | 0.3 | float | Minimum EMA8-EMA30 spread % to classify as "clean" | No (startup) | Lower = more tokens classified as clean; higher = fewer |
| `SCAN_SLOPE_LOOKBACK` | 5 | int | Number of candles ago to calculate slope | No (startup) | Affects slope sensitivity |
| `SCAN_SLOPE_THRESHOLD` | 0.05 | float | % change threshold to consider slope "up" or "down" | No (startup) | Lower = more sensitive to slope changes |
| `REGIME_WATCHER_ENABLED` | `True` | bool | Enable/disable background regime watcher | No (startup) | If `False`, regime watcher loop is never started |
| `REGIME_WATCHER_INTERVAL_SECONDS` | 900 | int | Seconds between regime checks | No (startup) | Controls how frequently regime is re-scanned |
| `REGIME_CONFIRMATION_CYCLES` | 2 | int | Consecutive checks needed to confirm regime change | No (startup) | Higher = fewer false alerts but slower response |
| `REGIME_ALERT_ON_ENTER_CLEAN` | `True` | bool | Send alert when token enters CLEAN regime | No (startup) | If `False`, no alerts for entering clean regimes |
| `REGIME_ALERT_ON_LEAVE_CLEAN` | `False` | bool | Send alert when token leaves CLEAN regime | No (startup) | If `True`, alerts when regime degrades |
| `REGIME_ALERT_COOLDOWN_HOURS` | 4 | int | Minimum hours between alerts for same token | No (startup) | Prevents alert spam |
| `REGIME_STATE_FILE` | "data/regime_state.json" | str | File path for regime state persistence | No (startup) | Changes where regime state is stored |

### Environment Variables Read

| Env Var | Default | Used By | Required? |
|---------|---------|---------|-----------|
| `PARADEX_L1_ADDRESS` | `""` | `config/settings.py`, `exchange/paradex_client.py` | Yes (validated at startup) |
| `PARADEX_PRIVATE_KEY` | `""` | `config/settings.py`, `exchange/paradex_client.py` | Yes (validated at startup) |
| `PARADEX_ENV` | `"TESTNET"` | `config/settings.py`, `exchange/paradex_client.py`, `strategy/ema_setup.py` | No |
| `TELEGRAM_BOT_TOKEN` | `""` | `config/settings.py`, `bot/telegram_bot.py` | Yes (validated at startup) |
| `TELEGRAM_CHAT_ID` | `""` | `config/settings.py`, `bot/telegram_bot.py` | Yes (validated at startup) |
| `DEFAULT_RISK_USD` | `"10"` | `config/settings.py` (→ `runtime["risk_usd"]`) | No |
| `DEFAULT_COIN` | `"BTC"` | `config/settings.py` (→ `runtime["coin"]`) | No |

---

## SECTION 7 — KNOWN BEHAVIORS, GAPS, AND ASSUMPTIONS

### A) Things That Work But Might Be Fragile

1. **Dual fill detection race condition:** Both `check_fills()` (60s loop) and `update_pending_orders()` (candle boundary loop) independently detect fills. If an order fills between a candle boundary check and the next 60s fill check, both loops could attempt to place TP/SL for the same trade. The code does not use locks or atomic operations to prevent this.

2. **Broad exception swallowing in background loops:** All three background loops (`fill_check_loop`, `order_update_loop`, `regime_watcher_loop`) catch `Exception` broadly and only log errors. The user is NOT notified of background failures. If `check_fills()` fails silently for multiple cycles, TP/SL orders may never be placed for filled entries, leaving positions unprotected.

3. **Brittle JWT token extraction:** `_get_auth_headers()` in `strategy/ema_setup.py` (lines 24-43) navigates deep into the Paradex SDK's internal object structure (`paradex_client.client.account.jwt_token`) to extract a JWT token. This depends on undocumented SDK internals and may break with SDK updates.

4. **No response validation from Paradex API:** Functions like `fetch_candles()` assume the API returns a specific JSON structure (`{"results": [[timestamp, open, high, low, close, volume], ...]}`) without validating field presence or types. If Paradex changes the response format, the bot will crash or produce incorrect EMAs.

5. **`close_all()` clears tracked trades before positions are confirmed closed:** `TradeManager.close_all()` (line 455) calls `self.active_trades.clear()` and `save_trades()` immediately after calling `client.close_all_positions()`, but `close_all_positions()` is synchronous and may not wait for all market closes to confirm on-chain. If a close fails, the trade is removed from tracking but the position may still be open on Paradex.

6. **`TRADES_FILE` shadow constant:** `trading/trade_manager.py` line 17 defines `TRADES_FILE = "active_trades.json"`, which shadows the import from `trading.state` (line 13). This local constant is never used — all save/load calls use `save_trades()` and `load_trades()` from `trading.state`, which use their own `TRADES_FILE` constant.

7. **`ORDER_UPDATE_INTERVAL` is dead code:** Defined in `main.py` line 33 as `ORDER_UPDATE_INTERVAL = 60` but never referenced anywhere. The `order_update_loop` computes its own interval dynamically.

### B) Assumptions Baked Into the Code

1. **Paradex candle response format:** `fetch_candles()` (ema_setup.py line 94) assumes `results` is a list of 6-element arrays matching `[timestamp, open, high, low, close, volume]` column order. No validation of column count or data types.

2. **Paradex position size convention:** `get_positions()` (paradex_client.py line 296) filters out positions with `size == 0`, assuming this means the position is closed. If Paradex returns closed positions with non-zero size, the bot may incorrectly think a position is still open.

3. **Candle timestamp unit:** `fetch_candles()` (ema_setup.py line 104) assumes timestamps are in milliseconds (`unit="ms"`). If Paradex changes to seconds, all timestamps will be interpreted incorrectly.

4. **Deprecated Python API usage:** `filters.py` line 90 and `ema_setup.py` use `datetime.utcfromtimestamp()`, which is deprecated in Python 3.12 (the runtime version specified in `runtime.txt`). This will raise `DeprecationWarning` and may be removed in future Python versions.

5. **System clock matches exchange clock:** `order_update_loop` (main.py line 92) uses `time.time() % tf_sec < 10` to detect candle boundaries. This assumes the system clock is synchronized with Paradex's candle close times. Clock drift could cause updates to fire on the wrong candle.

6. **`active_trades.json` is never manually edited:** The bot assumes this file is only modified by the bot itself. If a user manually edits it (e.g., to remove a trade), the bot may re-track it on the next load or fail to clean up orphaned positions.

7. **Paradex SDK response always has `"id"` field:** `place_limit_order()` (paradex_client.py line 147) and `place_trigger_order()` (line 195) assume `result.get("id")` exists for successful orders. If the SDK returns a different field name, the bot will think the order failed.

8. **Single-account, single-bot operation:** The code assumes only one bot instance is running against one Paradex account. Multiple instances would corrupt `active_trades.json` and `regime_state.json` due to unsynchronized writes.

### C) Missing Features / Gaps

1. **No dry-run / paper trading mode:** With `PARADEX_ENV=MAINNET` in `.env`, all trades execute with real money. There is no configuration flag to simulate trades without placing real orders.

2. **No unit tests:** The `tests/` directory contains only an empty `__init__.py`. No test coverage for any module.

3. **No reconciliation on startup:** When the bot starts, it loads `active_trades.json` but does NOT cross-check with Paradex's actual open positions. If the file is out of sync (e.g., bot crashed while a trade was in progress), the bot may track phantom trades or miss real ones.

4. **No rate limit handling:** If Paradex returns HTTP 429 (Too Many Requests), the bot will raise an exception. The background loops will catch it and continue, but the specific operation (fill check, order update, regime scan) will fail for that cycle.

5. **No health check endpoint:** `Procfile` defines `web: python main.py`, but the bot uses Telegram polling (blocking). A health check endpoint would enable monitoring on platforms like Heroku or Render.

6. **No position size minimum validation:** Before placing orders, the bot does not check if the calculated size meets Paradex's minimum order size. The `_round_size()` function rounds down, which could result in a size of 0 if the risk is too small relative to the entry-SL distance.

7. **No log rotation:** `momathi.log` grows indefinitely. No log rotation or size limit is configured. Over time, this will consume significant disk space.

8. **`data/.gitkeep` suggests data files belong in `data/`, but `active_trades.json` is at root:** `trading/state.py` uses `"active_trades.json"` (root path), while `data/active_trades.json` also exists separately. This is confusing and may lead to the wrong file being read/written.

9. **No SL/TP reconciliation:** The bot assumes TP/SL orders are always placed successfully once a trade is filled. If `_place_tpsl()` fails (e.g., API error), the trade remains tracked with `sl_oid=None` and `tp_oid=None`, but the retry logic in `check_fills()` may not catch this if the position exists.

10. **No partial fill handling:** The bot assumes orders are either fully filled or not filled at all. Paradex supports partial fills, but the bot does not account for this in position sizing or TP/SL placement.

### D) Dead Code / Unused Functions

1. **`paradex_client.place_batch_orders()`** (paradex_client.py line 205) — Body is `pass`. Never called by any code.

2. **`paradex_client.cancel_order(oid)`** (paradex_client.py line 233) — Defined but never called. Only `cancel_all_orders()` and `cancel_all()` are used.

3. **`paradex_client.place_market_order()`** (paradex_client.py line 249) — Defined for "pyramid add-on" but never called by production code.

4. **`utils.errors.ParadexAPIError`** (errors.py line 4) — Defined but never raised or caught.

5. **`utils.errors.TradeExecutionError`** (errors.py line 9) — Defined but never raised or caught.

6. **`trade_manager._cleanup_closed_trade()`** (trade_manager.py line 39) — Defined but never called. Cleanup logic is inlined in `check_fills()`.

7. **`trade_manager._position_still_open()`** (trade_manager.py line 30) — Defined but never called.

8. **`TRADES_FILE` in trade_manager.py** (line 17) — Shadows import from `trading.state`, never used.

9. **`ORDER_UPDATE_INTERVAL` in main.py** (line 33) — Defined as 60, never referenced.

10. **`ema_setup.get_mark_price()`** (ema_setup.py line 164) — Defined but never called by production code.

11. **`ema_setup.get_ema30()`** (ema_setup.py line 140) — Defined but never called by production code.

12. **`scripts/debug_api_format.py`** — Standalone script, not imported by any module.

13. **`scripts/debug_trigger_orders.py`** — Standalone script, not imported by any module.

### E) Inconsistencies

1. **`active_trades.json` path inconsistency:**
   - `trading/state.py` uses `"active_trades.json"` (root directory).
   - `data/active_trades.json` exists as a separate file.
   - `data/.gitkeep` implies data files should be in `data/`.
   - `.gitignore` lists `active_trades.json` (root) but not `data/active_trades.json`.
   - **Impact:** Two separate files may exist with different content, causing confusion.

2. **Duplicate fill detection logic:**
   - `check_fills()` and `update_pending_orders()` both check if entry_oid is still in open_orders, check for position existence, and remove trades if cancelled. This logic is duplicated with minor differences (e.g., `update_pending_orders()` re-validates the signal before updating, `check_fills()` does not).

3. **`_derive_state_from_scan()` duplicates classification logic:**
   - `main.py` defines `_derive_state_from_scan()` which maps scan results to states.
   - `strategy/filters.py` already classifies tokens internally (long_bias, short_bias, tangled).
   - The two functions use different data structures (scan result lists vs. state strings), creating a translation layer that could be eliminated.

4. **Inconsistent response normalization:**
   - `place_limit_order()` (paradex_client.py line 148) fabricates a Hyperliquid-style response structure with nested `response.data.statuses` to maintain compatibility with existing code.
   - `place_trigger_order()` (line 196) returns a simpler structure with just `status` and `oid`.
   - `execute_trade()` expects the Hyperliquid-style structure (line 93: `entry_result.get("response", {}).get("data", {}).get("statuses", [])`), but `_place_tpsl()` does not parse responses in the same way.

5. **Mixed error handling patterns:**
   - ParadexClient methods return error dicts (e.g., `{"status": "error", "msg": "..."}`).
   - `_place_tpsl()` raises exceptions on failure.
   - Background loops catch all exceptions and continue.
   - Trade commands catch exceptions and reply to user.
   - **Impact:** Callers must handle both return codes and exceptions, increasing complexity.

6. **Docstring out of sync with code:**
   - `strategy/filters.py` docstring (line 21-22) says "spread >= 0.4%", but `SCAN_SPREAD_THRESHOLD = 0.3` (config/settings.py line 42).
   - Comment in config/settings.py (line 42) explains the change from 0.4 to 0.3, but the docstring was not updated.

7. **`runtime["coin"]` is set but barely used:**
   - Defined in config and loaded from env var.
   - Only used as a fallback default in `fetch_candles()` and `validate_signal()` when `coin=None`.
   - No command uses this default — all trade commands require an explicit coin.
