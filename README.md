# 🤖 Crypto AI Trading Agent

A fully autonomous AI-powered crypto trading agent using:
- **Binance** — market data + order execution
- **Groq** (llama-3.3-70b) — research brain / reasoning engine
- **Supabase** — signal storage, trade history, feedback loop
- **pandas-ta** — technical indicators

---

## Architecture (10-Step Research Loop)

```
1. 🌍 Scan    → Binance OHLCV + order book
2. 🧾 Gather  → News (CryptoPanic) + Fear&Greed + sentiment
3. 🧠 Context → Build full market briefing
4. 🔗 Correlate → AI checks signal alignment across timeframes
5. 🧩 Hypothesis → AI forms directional view with reasoning
6. 🎯 Confidence → AI assigns probability score
7. ⚖️  Risk    → Gate: confidence > 65%, no open position, balance OK
8. 🧠 Decision → BUY / SELL / HOLD
9. ⚡ Execute  → Binance market order + OCO stop-loss/take-profit
10. 🔁 Feedback → Outcome vs prediction → improves next cycle
```

---

## Setup

### 1. Clone and install

```bash
cd crypto_agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required:
- `BINANCE_API_KEY` + `BINANCE_SECRET_KEY` — from Binance → API Management
- `GROQ_API_KEY` — from console.groq.com (free)
- `SUPABASE_URL` + `SUPABASE_KEY` — from your Supabase project settings

Optional but recommended:
- `CRYPTOPANIC_API_KEY` — free at cryptopanic.com (improves news quality)

### 3. Set up Supabase database

1. Go to your Supabase project → SQL Editor
2. Paste and run the contents of `db/setup.sql`
3. This creates 3 tables: `signal_snapshots`, `agent_reasoning`, `trade_history`

### 4. Binance API permissions

Your Binance API key needs:
- ✅ Read Info
- ✅ Enable Spot & Margin Trading
- ❌ Do NOT enable withdrawals (never needed)

### 5. Test your config

```bash
python config.py
```

---

## Running the Agent

### ⚠️ Always start in DRY RUN mode first

In `.env`:
```
DRY_RUN=true
```

```bash
# Single test cycle (recommended first run)
python main.py --once

# Test a specific pair
python main.py --pair BTCUSDT --once

# Full autonomous mode (runs every 5 minutes)
python main.py
```

When you're happy with the reasoning quality, set `DRY_RUN=false` in `.env`.

---

## Key Settings (in .env)

| Setting | Default | Description |
|---|---|---|
| `TRADING_PAIRS` | BTCUSDT,ETHUSDT | Pairs to monitor |
| `CYCLE_INTERVAL_SECONDS` | 300 | How often to run (5 min) |
| `MIN_CONFIDENCE` | 65 | AI confidence % needed to trade |
| `MAX_POSITION_PCT` | 20 | Max % of balance per trade (20% of ~$10 = $2) |
| `STOP_LOSS_PCT` | 2.0 | Stop loss % from entry |
| `TAKE_PROFIT_PCT` | 4.0 | Take profit % from entry (2:1 R/R) |
| `DRY_RUN` | true | Set false only when ready for live trading |

---

## What the AI Reasons About

Every cycle, Groq receives a full briefing including:

**Technical signals (1H + 4H):**
- RSI, MACD (cross + histogram), Bollinger Bands position
- EMA20 vs EMA50 trend, EMA gap %
- Volume ratio vs 20-period average
- ATR% (volatility), price change 1h/4h/24h
- Support & resistance levels
- Composite technical score (-100 to +100)

**Market context:**
- Order book bid/ask imbalance
- Fear & Greed Index (0-100)
- Latest news headlines with sentiment labels
- Last 5 predictions + whether they were correct (feedback loop)

**The AI returns:**
- `direction`: BUY / SELL / HOLD
- `confidence`: 0-100%
- `hypothesis`: One sentence explaining its view
- `signal_alignment`: strong / mixed / contradictory
- `risk_level`: LOW / MEDIUM / HIGH
- `reasoning`: Full chain-of-thought

---

## Database Tables (Supabase)

### `signal_snapshots`
Every data collection cycle stored for audit trail.

### `agent_reasoning`
Every AI analysis — full prompt, hypothesis, confidence, and eventually whether prediction was correct.

### `trade_history`
Every trade (real or dry-run) with entry, SL, TP, and outcome.

### `prediction_accuracy` view
```sql
SELECT * FROM prediction_accuracy;
-- Shows win rate per pair over time
```

---

## Risk Management

With ~$10 USDT:
- Max position: 20% = ~$2 USDT per trade
- Position scales with confidence: 65% conf → smaller size, 90% → larger
- High volatility (ATR > 3%) → position halved automatically
- No double-positions: only 1 open trade per pair at a time
- Minimum order: $5.50 USDT (Binance minimum)
- Contradictory signals → automatic REJECT regardless of confidence

---

## Adding More Pairs

Edit `.env`:
```
TRADING_PAIRS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT
```

---

## Logs

Logs are written to `logs/agent_YYYY-MM-DD.log` daily.
Console shows INFO level, file captures DEBUG level for full audit trail.

---

## Extending the Agent

| Want to add | Where |
|---|---|
| New indicator | `data/collector.py` → `compute_indicators()` |
| New data source | `data/collector.py` → new method + add to `collect_all()` |
| Change AI model | `config.py` → `GROQ_MODEL` |
| Change risk rules | `risk/manager.py` → `evaluate()` |
| Different position sizing | `risk/manager.py` → position size block |
| Add Telegram alerts | `execution/executor.py` → after trade logged |

---

## ⚠️ Important Notes

1. **This is experimental software.** Crypto trading involves substantial risk of loss.
2. **Start with DRY_RUN=true** and watch at least 20-30 cycles before going live.
3. **With $10 USDT**, many trades will be near Binance's minimum order size. Check `MIN_ORDER_USDT` in `risk/manager.py`.
4. **Groq free tier** has rate limits. The 5-minute cycle interval is designed to stay within them.
5. **Never enable withdrawal permissions** on your Binance API key.
