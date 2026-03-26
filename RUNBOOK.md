# Crypto AI Trading Agent — Runbook

## Overview

This is an AI-powered futures trading agent for Binance. It uses Groq (LLaMA) for market analysis, Supabase for trade history, and includes a real-time local dashboard.

**Stack:**
- **Trading agent** — `main.py` (Python)
- **Dashboard backend** — `dashboard/app.py` (FastAPI)
- **Dashboard frontend** — `dashboard/index.html` (served at `http://localhost:8000`)
- **AI brain** — `agents/brain.py` (Groq/LLaMA → OpenRouter → Browser AI fallback chain)
- **Browser AI** — `agents/browser_ai.py` (Playwright — ChatGPT or Gemini fallback)
- **Database** — Supabase (cloud Postgres)
- **Exchange** — Binance Futures

---

## Prerequisites

- Python 3.10+
- Binance account with **Futures enabled** and API key with Futures permission
- USDT transferred to your **Futures wallet** on Binance
- Supabase project with `trade_history` table
- Groq API key(s) from [console.groq.com](https://console.groq.com)
- *(Optional)* Google Chrome installed — required only for Browser AI feature

---

## 1. Installation

```bash
pip install -r requirements.txt
```

If you plan to use the **Browser AI** feature (ChatGPT or Gemini fallback):

```bash
pip install playwright
playwright install chromium
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

# === BROWSER AI (optional fallback) ===
BROWSER_AI_PROVIDER=off          # "chatgpt", "gemini", or "off" (default)
BROWSER_AI_HEADED=false          # true = show browser window (useful for debugging)
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

To kill any existing process on port 8000 before starting:
```bash
lsof -ti :8000 | xargs kill -9 2>/dev/null; sleep 1; python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
```

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

## 6. Browser AI Feature

The agent has a three-tier AI fallback chain:

```
Tier 1 → Groq (LLaMA) — fast, free, rate-limited
Tier 2 → OpenRouter   — secondary API fallback
Tier 3 → Browser AI   — uses ChatGPT or Gemini via your browser session
```

Browser AI kicks in automatically when all API-based providers fail or are rate-limited. It uses **Playwright** to control your real Chrome browser and send prompts to ChatGPT or Gemini — no API key required, just a logged-in browser session.

### One-time setup

**Step 1 — Install dependencies:**
```bash
pip install playwright
playwright install chromium
```

**Step 2 — Log in and save your session:**

For ChatGPT:
```bash
python agents/browser_ai.py --login chatgpt
```

For Gemini:
```bash
python agents/browser_ai.py --login gemini
```

A Chrome window will open. Log in to the site normally, then press **Ctrl+C** in the terminal when done. Your session is saved to `./browser_session/` and reused on all future runs.

> **Note:** Uses your real system Chrome (`channel="chrome"`). Bundled Chromium is blocked by Google/OpenAI sign-in screens.

### Enable in `.env`

```env
BROWSER_AI_PROVIDER=gemini    # or "chatgpt"
BROWSER_AI_HEADED=false       # set true to watch the browser (debug)
```

Set `BROWSER_AI_PROVIDER=off` to disable (default).

### Testing the browser AI manually

Send a test prompt and see the raw JSON decision:
```bash
python agents/browser_ai.py --provider gemini --message "Test prompt" --headed
```

Without `--headed` it runs in headless (invisible) mode — same as during normal agent operation.

### How it works

- Each trading cycle that needs a browser AI decision opens a **new conversation** to avoid context bleed between pairs.
- The AI is prompted with the same market data JSON as Groq/OpenRouter.
- Response is expected as JSON (`{"direction": "LONG"|"SHORT"|"HOLD", "confidence": 0-100, ...}`).
- If JSON parsing fails, it retries up to 2 times before returning HOLD.
- The browser window stays open in the background after first use — subsequent calls are fast (~5–10s).

### Troubleshooting

| Problem | Fix |
|---|---|
| `Browser did not become ready in 30s` | Session expired — re-run `--login` |
| `Gemini input never appeared` | Session invalid — re-run `--login gemini` |
| `Couldn't sign you in` (Chromium error) | Expected — always use `channel="chrome"` (system Chrome) |
| Browser not opening on macOS | Ensure Chrome is installed at the default path |
| `JSON parse error` after retries | Provider returned non-JSON (CAPTCHA/login wall) — re-run `--login` |

---

## 7. Utility Scripts

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

## 8. Dry Run → Live Checklist

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

## 9. Key Risk Settings Explained

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

## 10. Manually Closing a Position

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

## 11. File Structure

```
crypto_agent/
├── main.py                  # Trading agent entry point
├── config.py                # Loads .env settings
├── agents/
│   ├── brain.py             # AI analysis (Groq → OpenRouter → Browser AI)
│   ├── browser_ai.py        # Browser AI agent (ChatGPT / Gemini via Playwright)
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
├── browser_session/         # Playwright Chrome session (gitignored)
├── logs/                    # Agent log files (gitignored)
├── requirements.txt
└── .env                     # Secrets (gitignored)
```
