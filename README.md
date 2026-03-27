# Crypto AI Trading Agent

A fully autonomous AI-powered crypto futures trading agent.

- **Binance Futures** — market data + order execution (LONG & SHORT)
- **Groq** (llama-3.3-70b) — primary reasoning engine
- **Browser AI** — fallback to Gemini or ChatGPT via Playwright (no API key needed)
- **Supabase** — signal storage, trade history, feedback loop
- **Telegram** — real-time alerts + manual control

---

## Architecture

```
1. Scan      → Binance OHLCV + order book (15 liquid pairs)
2. Gather    → Funding rate + open interest + Fear&Greed + news
3. Indicators → RSI, MACD, Bollinger, EMA, ATR (1H + 4H)
4. AI Brain  → Groq (primary) → Browser AI (fallback)
5. Risk Gate → Confidence > threshold, funding rate check, liquidity check
6. Execute   → Binance futures market order (LONG or SHORT)
7. Monitor   → ATR-based dynamic SL/TP, Telegram live updates
8. Feedback  → Outcome vs prediction → improves next cycle
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

**Required:**
- `BINANCE_API_KEY` + `BINANCE_SECRET_KEY` — Binance → API Management (Futures enabled)
- `GROQ_API_KEY` — console.groq.com (free)
- `SUPABASE_URL` + `SUPABASE_KEY` — Supabase project settings
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — @BotFather on Telegram

**Optional:**
- `CRYPTOPANIC_API_KEY` — free at cryptopanic.com (better news)

### 3. Set up Supabase

1. Supabase project → SQL Editor
2. Paste and run `db/setup.sql`
3. Creates: `signal_snapshots`, `agent_reasoning`, `trade_history`

### 4. Binance API permissions

- ✅ Read Info
- ✅ Enable Futures Trading
- ❌ No withdrawals

### 5. Test config

```bash
python config.py
```

---

## Running

```bash
# Dry run first (always)
DRY_RUN=true python main.py

# Single test cycle
python main.py --once

# Test specific pair
python main.py --pair ETHUSDT --once

# Live trading
DRY_RUN=false python main.py
```

---

## Key Settings (.env)

| Setting | Default | Description |
|---|---|---|
| `TRADING_PAIRS` | 15 liquid pairs | Pairs to monitor |
| `CYCLE_INTERVAL_SECONDS` | 30 | How often to scan |
| `MIN_CONFIDENCE` | 76 | AI confidence % to trade |
| `FUTURES_LEVERAGE` | 5 | Futures leverage (keep low) |
| `MAX_POSITION_PCT` | 30 | Max % of balance per trade |
| `STOP_LOSS_PCT` | 1.5 | Fallback SL % (ATR-based preferred) |
| `TAKE_PROFIT_PCT` | 3.5 | Fallback TP % (ATR-based preferred) |
| `MAX_DAILY_LOSS_PCT` | -6.0 | Daily loss circuit breaker |
| `DRY_RUN` | true | Set false only when ready |

---

## Risk Management

- **ATR-based SL/TP** — SL = ATR × 1.5, TP = ATR × 3.5 (R:R ≈ 2.3:1). Falls back to fixed % if ATR unavailable.
- **Funding rate gate** — Rejects LONGs when funding rate is heavily positive (overleveraged longs = squeeze risk). Rejects SHORTs on negative funding.
- **Liquidity gate** — Rejects pairs with 24h volume < $30M.
- **Daily loss limit** — Stops trading for the day at `MAX_DAILY_LOSS_PCT`. Telegram alert fires once.
- **Confidence threshold** — Only trades at ≥ 76% AI confidence.
- **No double positions** — One open trade per pair at a time.
- **LONG & SHORT** — Agent can trade both directions on futures.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/status` | Open positions (pulled live from Binance) |
| `/balance` | Wallet balance (USDT) |
| `/pause` | Pause trading (current trades stay open) |
| `/resume` | Resume trading |
| `/help` | Command list |

Inline buttons on trade alerts: **Close** (market close) — button auto-removes after trade closes.

---

## Browser AI (Gemini / ChatGPT fallback)

When Groq is rate-limited or unavailable, the agent falls back to Gemini or ChatGPT in a real browser via Playwright — no API key needed, uses your logged-in session.

### Setup (one-time)

```bash
pip install playwright
playwright install chromium

# Log in to your Google account in the browser
python agents/browser_ai.py --login gemini

# Or for ChatGPT
python agents/browser_ai.py --login chatgpt
```

### Enable in .env

```
BROWSER_AI_PROVIDER=gemini    # or chatgpt
BROWSER_AI_HEADED=false       # true to see the browser window
```

### How it works

- Browser stays open in the background (no per-call startup cost)
- Each call: paste prompt → send → extract JSON result → reset page for next call
- 15-second cooldown between calls to avoid rate limiting
- Prompts > 7000 chars are trimmed (head + tail kept, middle data trimmed)
- Auto-detects Gemini error pages and retries

### Switch Google account

Delete the session folder and re-login:

```bash
rm -rf browser_session/
python agents/browser_ai.py --login gemini
```

### Test browser AI manually

```bash
python agents/browser_ai.py --message "Say hello in JSON: {\"hello\": \"world\"}" --headed
```

---

## What the AI Analyses

**Technical (1H + 4H):**
RSI, MACD, Bollinger Bands, EMA20/50, ATR, volume ratio, price change, support/resistance, composite score

**Futures market intelligence:**
- Funding rate — positive = overleveraged longs (SHORT risk), negative = overleveraged shorts (LONG risk)
- Open interest trend — rising OI confirms move, falling OI = weak conviction

**Market context:**
Order book imbalance, Fear & Greed Index, news headlines, last 5 predictions + outcomes

**AI returns:**
`direction` (LONG/SHORT/HOLD), `confidence`, `hypothesis`, `market_regime`, `risk_reward_ratio`, `reasoning`

---

## Database Tables

| Table | Contents |
|---|---|
| `signal_snapshots` | Every data collection cycle |
| `agent_reasoning` | Full AI analysis per cycle (prompt, hypothesis, confidence, outcome) |
| `trade_history` | Every trade with entry, SL, TP, PnL |

```sql
-- Win rate per pair
SELECT * FROM prediction_accuracy;
```

---

## File Structure

```
main.py                  — Main loop
config.py                — Env var loading
constants.py             — ATR multipliers, thresholds
agents/
  brain.py               — AI prompt builder + decision logic
  browser_ai.py          — Playwright browser AI fallback
  feedback.py            — Outcome tracking
data/
  collector.py           — Binance OHLCV, funding rate, open interest, news
db/
  client.py              — Supabase client (auto-reconnect)
  setup.sql              — DB schema
risk/
  manager.py             — Risk gates, ATR SL/TP, position sizing
notifications/
  telegram.py            — Alerts, commands, trade monitor
dashboard/
  index.html             — Local web dashboard
browser_session/         — Playwright Chrome session (gitignored)
```

---

## Extending

| Want to add | Where |
|---|---|
| New indicator | `data/collector.py` → `compute_indicators()` |
| New data source | `data/collector.py` → new method + `collect_all()` |
| Change AI model | `config.py` → `GROQ_MODEL` |
| Change risk rules | `risk/manager.py` → `evaluate()` |
| New Telegram command | `notifications/telegram.py` → `_handle_message()` |

---

## Important Notes

1. **Experimental software.** Crypto trading involves substantial risk of loss.
2. **Start with DRY_RUN=true** — watch 20-30 cycles before going live.
3. **Keep leverage low** — default is 5x. High leverage + tight stops = guaranteed losses.
4. **Groq free tier** has rate limits. Browser AI fallback handles this automatically.
5. **Never enable withdrawal permissions** on your Binance API key.
