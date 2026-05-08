# Momathi Protocol

Paradex DEMA trading bot — automated EMA crossover strategy with Telegram control, fixed-risk sizing, and pyramid add-ons.

## Quick Start

```bash
# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your Paradex and Telegram credentials

# Run
python main.py
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PARADEX_L1_ADDRESS` | Yes | Paradex wallet address |
| `PARADEX_PRIVATE_KEY` | Yes | Paradex private key |
| `PARADEX_ENV` | No | `PROD` or `TESTNET` (default) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | Authorized chat ID |
| `DEFAULT_RISK_USD` | No | USD risk per trade (default: 10) |
| `DEFAULT_COIN` | No | Default trading pair (default: BTC) |

## Project Structure

| File | Purpose |
|------|---------|
| `main.py` | Entry point, starts bot and background loops |
| `config.py` | Configuration and runtime settings |
| `strategy.py` | EMA computation, trend detection, signal validation |
| `trade_manager.py` | Trade execution, fill detection, pyramid, trailing SL |
| `paradex_client.py` | Paradex API wrapper (orders, positions, balance) |
| `telegram_bot.py` | Telegram command interface |

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome + command list |
| `/<coin> <direction> <tf>` | Trade: `/btc long 5` |
| `/status` | Open positions & orders |
| `/balance` | Account balance |
| `/pnl` | Unrealized PnL |
| `/set_risk <amount>` | Set risk per trade |
| `/get_risk` | Show current risk |
| `/close_all` | Close everything |
| `/stop_bot` | Shutdown bot |

## Strategy

- **EMAs:** 8 (entry) / 30 (SL) on 5m or 15m execution timeframe
- **Trend filter:** 1H EMA8 vs EMA30 — trades must align
- **Risk:** Fixed USD per trade (configurable via `/set_risk`)
- **TP:** 1:3 risk-reward ratio
- **Pyramid:** Adds 15% position size when trade hits 1:1 RR and EMA30 returns to entry, then trails SL at EMA30

## Deploy on Railway

1. Deploy from GitHub repo
2. Add environment variables in Railway dashboard
3. The `Procfile` tells Railway to run `python main.py`
