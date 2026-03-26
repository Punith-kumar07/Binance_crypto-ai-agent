"""
constants.py — Single source of truth for all magic numbers.

Previously these were scattered across collector.py, brain.py, risk/manager.py,
and execution/executor.py. Centralising them here makes tuning straightforward.
"""

# ── Indicator periods ──────────────────────────────────────────────────────
RSI_PERIOD        = 14
STOCH_RSI_PERIOD  = 14
STOCH_K           = 3
STOCH_D           = 3
MACD_FAST         = 12
MACD_SLOW         = 26
MACD_SIGNAL       = 9
BB_PERIOD         = 20
BB_STD            = 2.0
EMA_SHORT         = 20
EMA_MID           = 50
EMA_LONG          = 200
ATR_PERIOD        = 14
ADX_PERIOD        = 14
WILLIAMS_R_PERIOD = 14

# Minimum candles needed for ADX + StochRSI to warm up reliably
MIN_CANDLES_FOR_INDICATORS = 60

# ── Binance limits ─────────────────────────────────────────────────────────
BINANCE_MIN_ORDER_USDT  = 5.5   # Binance spot minimum notional (USDT pairs)
BINANCE_TAKER_FEE_RATE  = 0.001 # 0.1% taker fee
BINANCE_FEE_BUFFER_USDT = 0.50  # Extra reserve kept free for fees

# ── Risk / position sizing ─────────────────────────────────────────────────
# ATR% above this threshold is considered high volatility → halve position
HIGH_VOLATILITY_ATR_PCT = 3.0

# Confidence scaling: at MIN_CONFIDENCE → 50% of max size; at 100% → 100% of max
CONFIDENCE_SIZE_FLOOR = 0.5

# ── Multi-timeframe confluence thresholds ──────────────────────────────────
MTF_STRONG_BULL_THRESHOLD = 6   # out of 8 signals
MTF_BULL_THRESHOLD        = 4
MTF_STRONG_BEAR_THRESHOLD = 6
MTF_BEAR_THRESHOLD        = 4

# ── Technical score weights ────────────────────────────────────────────────
# These weights feed the composite technical_score in collector.py
SCORE_WEIGHT_RSI_OVERSOLD      = 25
SCORE_WEIGHT_RSI_MILD_BULL     = 10
SCORE_WEIGHT_RSI_OVERBOUGHT    = -25
SCORE_WEIGHT_RSI_MILD_BEAR     = -10
SCORE_WEIGHT_STOCH_OVERSOLD    = 15
SCORE_WEIGHT_STOCH_OVERBOUGHT  = -15
SCORE_WEIGHT_STOCH_CROSS       = 5
SCORE_WEIGHT_MACD_CROSS        = 15
SCORE_WEIGHT_EMA_TREND         = 15
SCORE_WEIGHT_ADX_DI            = 10
SCORE_WEIGHT_BB_POSITION       = 10
SCORE_WEIGHT_OBV               = 5
SCORE_WEIGHT_VWAP              = 5
SCORE_VOLUME_SPIKE_MULTIPLIER  = 1.1

# ── API / network ──────────────────────────────────────────────────────────
API_TIMEOUT_SECONDS  = 10   # default requests timeout
GROQ_MAX_TOKENS         = 1200
OPENROUTER_MAX_TOKENS   = 2500   # free models are more verbose than Groq — needs more room
GEMINI_MAX_TOKENS       = 8192   # Gemini responses are more verbose — needs higher limit
GROQ_TEMPERATURE        = 0.4

# Retry settings (via tenacity in brain.py)
GROQ_RETRY_ATTEMPTS  = 3
GROQ_RETRY_MIN_WAIT  = 2   # seconds
GROQ_RETRY_MAX_WAIT  = 10  # seconds

# ── Volume analysis window ─────────────────────────────────────────────────
VOLUME_SPIKE_RATIO   = 1.5  # current vol / avg vol > this → spike
VOLUME_AVG_WINDOW    = 20
VOLUME_TREND_SHORT   = 5

# ── BB squeeze threshold ───────────────────────────────────────────────────
BB_SQUEEZE_THRESHOLD = 0.03  # bb_width < this → squeeze (low volatility)

# ── Order book depth ──────────────────────────────────────────────────────
ORDER_BOOK_DEPTH = 20

# ── News fetch limit ──────────────────────────────────────────────────────
NEWS_FETCH_LIMIT = 10
