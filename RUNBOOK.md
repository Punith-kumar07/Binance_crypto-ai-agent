# Crypto AI Trading Agent — Runbook

## Overview

This is an AI-powered futures trading agent for Binance. It uses Groq (LLaMA) for market analysis, Supabase for trade history, and includes a real-time local dashboard.

**Stack:**
- **Trading agent** — `main.py` (Python)
- **Dashboard backend** — `dashboard/app.py` (FastAPI)
- **Dashboard frontend** — `dashboard/index.html` (served at `http://localhost:8000`)
- **Database** — Supabase (cloud Postgres)
- **Exchange** — Binance Futures

---

## Prerequisites

- Python 3.10+
- Binance account with **Futures enabled** and API key with Futures permission
- USDT transferred to your **Futures wallet** on Binance
- Supabase project with `trade_history` table
- Groq API key(s) from [console.groq.com](https://console.groq.com)

---

## 1. Installation

```bash
pip install -r requirements.txt
```

---

## 2. Configuration (`.env`)

Copy the template and fill in your values:

```env
# === BINANCE ===
BINANCE_API_KEY=your_key
BINANCE_SECRET_KEY=your_secret
BINANCE_TESTNET=false

# === GROQ ===
GROQ_API_KEYS=key1,key2,key3     # comma-separated, rotates on rate limit

# === SUPABASE ===
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=your_anon_key

# === AGENT SETTINGS ===
TRADING_PAIRS=ETHUSDT,SOLUSDT    # comma-separated (avoid BTCUSDT with small balance)
CYCLE_INTERVAL_SECONDS=60        # seconds between cycles
MIN_CONFIDENCE=80                # minimum AI confidence % to place a trade
MAX_POSITION_PCT=40              # % of balance used per trade
STOP_LOSS_PCT=1.0                # price move % for SL (×leverage = real loss)
TAKE_PROFIT_PCT=2.0              # price move % for TP (×leverage = real gain)
MAX_DAILY_LOSS_PCT=-6.0          # halt trading if daily PnL drops below this
DRY_RUN=true                     # true = simulate only, false = real trades
LOG_LEVEL=INFO

# === TRADE MODE ===
TRADE_MODE=futures               # "spot" or "futures"
FUTURES_LEVERAGE=4               # 1–20x (set on Binance per symbol automatically)
```

> **Never commit `.env` to Git.** It is already in `.gitignore`.

---

## 3. Running the Trading Agent

### Single cycle (test run)
```bash
python main.py --once
```

### Continuous (production)
```bash
python main.py
```

Logs are written to `logs/agent_YYYYMMDD.log`.

---

## 4. Running the Dashboard

The dashboard shows live positions, PnL, recent trades, and live logs. Run it in a **separate terminal**:

```bash
python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
```

Then open: **[http://localhost:8000](http://localhost:8000)**

> The dashboard auto-refreshes every 5 seconds. Live logs stream via WebSocket.

---

## 5. Running Both Together

Open **two terminals** in the project directory:

**Terminal 1 — Trading Agent:**
```bash
python main.py
```

**Terminal 2 — Dashboard:**
```bash
python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
```

---

## 6. Utility Scripts

### View trade results
```bash
python utils/view_results.py
```
Prints a table of all trades with entry, exit, PnL, and outcome.

### Clear all open trades (clean start)
```bash
python utils/clear_trades.py
```
Marks all open trades as `cancelled` in Supabase. Use this before a fresh run.

---

## 7. Dry Run → Live Checklist

Before switching to live trading:

- [ ] Run `python main.py --once` with `DRY_RUN=true` and verify `🧪 DRY RUN [FUTURES 4x]` appears
- [ ] Open dashboard and confirm positions/PnL are showing
- [ ] Check futures wallet has USDT on Binance
- [ ] Confirm API key has **Futures** permission enabled
- [ ] Remove BTCUSDT from `TRADING_PAIRS` if balance < $100 (lot size too large)
- [ ] Run `python utils/clear_trades.py` to remove any dry-run trades from DB
- [ ] Set `DRY_RUN=false` in `.env`
- [ ] Run `python main.py`

---

## 8. Key Risk Settings Explained

| Setting | Effect |
|---|---|
| `STOP_LOSS_PCT=1.0` | Price moves -1% → -4% real loss at 4x leverage |
| `TAKE_PROFIT_PCT=2.0` | Price moves +2% → +8% real gain at 4x leverage (2:1 R:R) |
| `MAX_POSITION_PCT=40` | Each trade uses 40% of available balance |
| `MAX_DAILY_LOSS_PCT=-6.0` | Agent stops trading for the day if cumulative loss hits -6% |
| `FUTURES_LEVERAGE=4` | Set per symbol automatically via Binance API |

**With $13 balance at 4x leverage:**
- Only 2 trades can be open simultaneously (~$5.50 margin each)
- Max loss per trade ≈ $0.22 (1% of $22 notional)
- Max gain per trade ≈ $0.44 (2% of $22 notional)

---

## 9. Manually Closing a Position

**From the dashboard:** Click **✕ Close Position** on the position card — cancels TP/SL orders and market-closes immediately.

**From terminal (emergency):**
```python
python - <<'EOF'
import sys; sys.path.insert(0, '.')
import config
from binance.client import Client
c = Client(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY)
pair = "ETHUSDT"
for o in c.futures_get_open_orders(symbol=pair):
    c.futures_cancel_order(symbol=pair, orderId=o["orderId"])
pos = c.futures_position_information(symbol=pair)
qty = float(pos[0]["positionAmt"])
if qty > 0:
    c.futures_create_order(symbol=pair, side="SELL", type="MARKET", quantity=qty, reduceOnly="true")
    print(f"Closed {qty} {pair}")
EOF
```

---

## 10. File Structure

```
crypto_agent/
├── main.py                  # Trading agent entry point
├── config.py                # Loads .env settings
├── agents/
│   ├── brain.py             # AI analysis (Groq/LLaMA)
│   └── feedback.py          # TP/SL feedback loop
├── data/
│   └── collector.py         # Market data + indicators
├── execution/
│   └── executor.py          # Order placement (spot + futures)
├── risk/
│   └── manager.py           # Risk gates + position sizing
├── db/
│   └── client.py            # Supabase helpers
├── dashboard/
│   ├── app.py               # FastAPI dashboard backend
│   └── index.html           # Dashboard frontend
├── utils/
│   ├── view_results.py      # Print trade history
│   └── clear_trades.py      # Mark open trades as cancelled
├── logs/                    # Agent log files (gitignored)
├── requirements.txt
└── .env                     # Secrets (gitignored)
```
