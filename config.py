"""
config.py — Central configuration loader.
All settings come from .env — never hardcode secrets.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Binance ────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
BINANCE_TESTNET    = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# ── Groq ───────────────────────────────────────────────────────────────────
GROQ_API_KEYS      = [k.strip() for k in os.getenv("GROQ_API_KEYS", "").split(",") if k.strip()]
# Fallback for old single key config
if not GROQ_API_KEYS and os.getenv("GROQ_API_KEY"):
    GROQ_API_KEYS = [os.getenv("GROQ_API_KEY").strip()]

GROQ_MODEL         = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── OpenRouter (2nd fallback: Groq exhausted → OpenRouter → Gemini) ────────
# Free models: https://openrouter.ai/models?q=free
OPENROUTER_API_KEYS   = [k.strip() for k in os.getenv("OPENROUTER_API_KEYS", "").split(",") if k.strip()]
OPENROUTER_MODEL      = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_HOURLY_LIMIT = int(os.getenv("OPENROUTER_HOURLY_LIMIT", "40"))

# ── Gemini (3rd fallback when all Groq + OpenRouter keys are exhausted) ─────
# Supports multiple keys: GEMINI_API_KEYS=key1,key2,key3
GEMINI_API_KEYS    = [k.strip() for k in os.getenv("GEMINI_API_KEYS", os.getenv("GEMINI_API_KEY", "")).split(",") if k.strip()]
GEMINI_API_KEY     = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""   # backward compat
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_HOURLY_LIMIT = int(os.getenv("GEMINI_HOURLY_LIMIT", "5"))  # max calls per hour per key

# ── Browser AI (last-resort fallback — uses ChatGPT/Gemini web UI) ─────────
# Set to "chatgpt" or "gemini" to enable. "off" to disable.
# One-time setup: python agents/browser_ai.py --login chatgpt
BROWSER_AI_PROVIDER = os.getenv("BROWSER_AI_PROVIDER", "off").lower()
BROWSER_AI_HEADED   = os.getenv("BROWSER_AI_HEADED", "false").lower() == "true"

# ── Supabase ───────────────────────────────────────────────────────────────
SUPABASE_URL       = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY       = os.getenv("SUPABASE_KEY", "")

# ── External APIs ─────────────────────────────────────────────────────────
CRYPTOPANIC_KEY    = os.getenv("CRYPTOPANIC_API_KEY", "")

# ── Telegram Alerts (optional) ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ── Agent behaviour ───────────────────────────────────────────────────────
_DEFAULT_PAIRS = (
    # ── Blue chips ──────────────────────────────────────────
    "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,"
    # ── L1 / Infrastructure ─────────────────────────────────
    "DOTUSDT,LINKUSDT,LTCUSDT,NEARUSDT,ATOMUSDT,INJUSDT,APTUSDT,SUIUSDT,TONUSDT,SEIUSDT,"
    # ── L2 / Scaling ────────────────────────────────────────
    "ARBUSDT,OPUSDT,POLUSDT,STXUSDT,"
    # ── DeFi ────────────────────────────────────────────────
    "LDOUSDT,AAVEUSDT,UNIUSDT,MKRUSDT,ENAUSDT,PENDLEUSDT,"
    # ── AI / Data ───────────────────────────────────────────
    "FETUSDT,GRTUSDT,WLDUSDT,PYTHUSDT,"
    # ── Storage / Infra ─────────────────────────────────────
    "FILUSDT,RUNEUSDT,TIAUSDT,ONDOUSDT,"
    # ── Meme (high vol, predictable patterns) ───────────────
    "1000PEPEUSDT,WIFUSDT,1000FLOKIUSDT,"
    # ── Trending / Newer ────────────────────────────────────
    "JUPUSDT,ORDIUSDT,JTOUSDT,IMXUSDT"
)
TRADING_PAIRS      = [p.strip() for p in os.getenv("TRADING_PAIRS", _DEFAULT_PAIRS).split(",") if p.strip()]
SCAN_PAIRS_PER_CYCLE = int(os.getenv("SCAN_PAIRS_PER_CYCLE", "5"))   # pairs to analyse per cycle
MIN_ORDER_USDT     = float(os.getenv("MIN_ORDER_USDT", "5.5"))   # Binance min futures order
CYCLE_INTERVAL     = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))
MIN_CONFIDENCE     = float(os.getenv("MIN_CONFIDENCE", "65"))
MAX_POSITION_PCT   = float(os.getenv("MAX_POSITION_PCT", "20")) / 100
STOP_LOSS_PCT      = float(os.getenv("STOP_LOSS_PCT", "2.0")) / 100
TAKE_PROFIT_PCT    = float(os.getenv("TAKE_PROFIT_PCT", "4.0")) / 100
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "-6.0"))
DRY_RUN            = os.getenv("DRY_RUN", "true").lower() == "true"
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO")

# ── Trade mode ─────────────────────────────────────────────────────────────
TRADE_MODE       = os.getenv("TRADE_MODE", "spot").lower()   # "spot" | "futures"
FUTURES_LEVERAGE = int(os.getenv("FUTURES_LEVERAGE", "1"))    # 1-20x, only used in futures mode


# ── Validation ─────────────────────────────────────────────────────────────
def validate():
    """Raise EnvironmentError if any required variable is missing or obviously wrong."""
    missing = []
    for name, val in [
        ("BINANCE_API_KEY",    BINANCE_API_KEY),
        ("BINANCE_SECRET_KEY", BINANCE_SECRET_KEY),
        ("GROQ_API_KEYS",      GROQ_API_KEYS),
        ("SUPABASE_URL",       SUPABASE_URL),
        ("SUPABASE_KEY",       SUPABASE_KEY),
    ]:
        if not val:
            missing.append(name)

    if missing:
        raise EnvironmentError(
            f"Missing or unset env vars: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in real values."
        )

    if not TRADING_PAIRS:
        raise EnvironmentError("TRADING_PAIRS is empty — provide at least one pair (e.g. BTCUSDT)")

    if MIN_CONFIDENCE < 50 or MIN_CONFIDENCE > 100:
        raise EnvironmentError(f"MIN_CONFIDENCE must be between 50 and 100, got {MIN_CONFIDENCE}")

    if MAX_POSITION_PCT <= 0 or MAX_POSITION_PCT > 1:
        raise EnvironmentError(f"MAX_POSITION_PCT must be between 1 and 100 (percent), got {MAX_POSITION_PCT * 100}")

    if CYCLE_INTERVAL < 5:
        raise EnvironmentError(f"CYCLE_INTERVAL_SECONDS must be >= 5 to avoid excessive resource usage, got {CYCLE_INTERVAL}")

    # Warn (not fatal) on aggressive settings
    import warnings
    if MAX_POSITION_PCT > 0.30:
        warnings.warn(
            f"MAX_POSITION_PCT={MAX_POSITION_PCT*100:.0f}% is very aggressive. "
            "Consider 10-20% to limit per-trade risk.",
            stacklevel=2,
        )
    if TRADE_MODE not in ("spot", "futures"):
        raise EnvironmentError(f"TRADE_MODE must be 'spot' or 'futures', got {TRADE_MODE!r}")
    if TRADE_MODE == "futures" and (FUTURES_LEVERAGE < 1 or FUTURES_LEVERAGE > 20):
        raise EnvironmentError(f"FUTURES_LEVERAGE must be between 1 and 20, got {FUTURES_LEVERAGE}")

    if not DRY_RUN:
        warnings.warn(
            "DRY_RUN=false — REAL orders will be placed on Binance!",
            stacklevel=2,
        )
    if TRADE_MODE == "futures" and FUTURES_LEVERAGE > 10:
        warnings.warn(
            f"FUTURES_LEVERAGE={FUTURES_LEVERAGE}x is aggressive. Consider 2-4x to limit liquidation risk.",
            stacklevel=2,
        )


if __name__ == "__main__":
    validate()
    print("✅ Config OK")
    print(f"  Scan pairs:  {TRADING_PAIRS} ({len(TRADING_PAIRS)} pairs)")
    print(f"  Dry run:     {DRY_RUN}")
    print(f"  Interval:    {CYCLE_INTERVAL}s")
    print(f"  Max pos:     {MAX_POSITION_PCT*100:.0f}%")
    print(f"  Daily loss:  {MAX_DAILY_LOSS_PCT}%")
    print(f"  Trade mode:  {TRADE_MODE} ({FUTURES_LEVERAGE}x leverage)")
