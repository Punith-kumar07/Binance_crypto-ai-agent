"""
agents/brain.py — Steps 3-6: Context Builder + Correlator + Hypothesis + Confidence.

Improvements:
  - Richer market context (ADX regime, StochRSI, OBV, VWAP, Williams %R)
  - Explicit multi-timeframe confluence scoring
  - News impact weighting (high-impact headlines flagged separately)
  - reasoning_id returned so the feedback loop can link outcomes back
  - Structured 6-step analysis chain fed to Groq
"""
import json
import re
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from groq import Groq
from loguru import logger
import config
from db import client as db

_KEY_STATUS_FILE = Path("logs/groq_key_status.json")


class GroqKeyManager:
    """
    Manages Groq API keys with persistent exhaustion tracking.

    - Saves exhaustion state to logs/groq_key_status.json so restarts
      don't retry keys that are known to be rate-limited.
    - Starts on a random available key to spread load across keys.
    - Parses retry-after from 429 errors to know exactly when a key resets.
    """

    def __init__(self, keys: list):
        self.keys   = keys
        self._state = self._load()
        self._clean_expired()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if _KEY_STATUS_FILE.exists():
                return json.loads(_KEY_STATUS_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            _KEY_STATUS_FILE.parent.mkdir(exist_ok=True)
            _KEY_STATUS_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.warning(f"[KeyManager] Could not save key state: {e}")

    def _clean_expired(self):
        """Remove keys whose reset time has already passed."""
        now = datetime.now(timezone.utc)
        expired = [
            tail for tail, info in self._state.items()
            if datetime.fromisoformat(info["reset_at"]) <= now
        ]
        for tail in expired:
            del self._state[tail]
            logger.info(f"[KeyManager] Key ...{tail} reset — marking available.")
        if expired:
            self._save()

    # ── Public API ────────────────────────────────────────────────────────

    def available_keys(self) -> list:
        """Return all keys that are not currently exhausted."""
        self._clean_expired()
        return [k for k in self.keys if k[-8:] not in self._state]

    def pick_start_key(self) -> str:
        """Return a random available key to spread load on startup."""
        avail = self.available_keys()
        if not avail:
            raise RuntimeError(
                "All Groq API keys are exhausted! "
                f"Run: python utils/check_groq_keys.py to see reset times."
            )
        chosen = random.choice(avail)
        logger.info(
            f"[KeyManager] Starting with key ...{chosen[-4:]} "
            f"({len(avail)}/{len(self.keys)} available)"
        )
        return chosen

    def next_available(self, current_key: str) -> str | None:
        """Return next available key after current, or None if all exhausted."""
        avail = self.available_keys()
        avail = [k for k in avail if k != current_key]
        if not avail:
            return None
        return avail[0]

    def mark_rate_limited(self, key: str, error_msg: str = ""):
        """
        Mark a key as exhausted. Parses retry-after seconds from error message.
        Falls back to 60s (per-minute limit) or 86400s (daily limit) if unknown.
        """
        # Try to parse "Please try again in X.XXXs" from Groq error message
        seconds = None
        m = re.search(r"try again in ([\d.]+)s", error_msg, re.IGNORECASE)
        if m:
            seconds = float(m.group(1))
        else:
            # Heuristic: if error mentions 'day' or 'daily' → daily reset
            if "day" in error_msg.lower() or "daily" in error_msg.lower():
                seconds = 86400
            else:
                seconds = 65   # per-minute limit — wait 65s to be safe

        reset_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        tail = key[-8:]
        self._state[tail] = {"reset_at": reset_at.isoformat()}
        self._save()
        logger.warning(
            f"[KeyManager] Key ...{key[-4:]} rate-limited. "
            f"Available again in {seconds:.0f}s "
            f"(~{reset_at.strftime('%H:%M:%S UTC')})"
        )

    def status_summary(self) -> str:
        self._clean_expired()
        avail = len(self.available_keys())
        total = len(self.keys)
        exhausted = [
            f"...{tail[-4:]} until {info['reset_at'][11:19]} UTC"
            for tail, info in self._state.items()
        ]
        line = f"{avail}/{total} keys available"
        if exhausted:
            line += " | Exhausted: " + ", ".join(exhausted)
        return line


class TradingBrain:
    def __init__(self):
        self._key_mgr     = GroqKeyManager(config.GROQ_API_KEYS)
        self._current_key = self._key_mgr.pick_start_key()
        self._init_client()

    def _init_client(self):
        self.groq = Groq(api_key=self._current_key)

    def _rotate_key(self, error_msg: str = "") -> bool:
        """Mark current key as rate-limited and switch to next available."""
        self._key_mgr.mark_rate_limited(self._current_key, error_msg)
        nxt = self._key_mgr.next_available(self._current_key)
        if nxt:
            self._current_key = nxt
            self._init_client()
            logger.info(f"[KeyManager] Switched to key ...{nxt[-4:]} | {self._key_mgr.status_summary()}")
            return True
        logger.error(f"[KeyManager] All keys exhausted! {self._key_mgr.status_summary()}")
        return False

    # ── Prompt Builder ────────────────────────────────────────────────────

    def _build_prompt(self, snapshot: dict, recent_reasoning: list) -> str:
        pair   = snapshot["pair"]
        price  = snapshot["current_price"]
        ind1h  = snapshot.get("indicators_1h", {})
        ind4h  = snapshot.get("indicators_4h", {})
        news   = snapshot.get("news", [])
        ns     = snapshot.get("news_summary", {})
        fng    = snapshot.get("fear_greed", {})
        ob     = snapshot.get("order_book_imbalance", 0)
        bal    = snapshot.get("usdt_balance", 0)
        s24    = snapshot.get("stats_24h", {})

        # ── News section: high-impact first ──────────────────────────────
        high_impact = [n for n in news if n.get("high_impact")]
        normal_news = [n for n in news if not n.get("high_impact")]
        news_lines = ""
        if high_impact:
            news_lines += "  ⚡ HIGH-IMPACT HEADLINES:\n"
            for n in high_impact[:4]:
                news_lines += f"    [{n.get('sentiment','?').upper()}] {n.get('title')} ({n.get('source')})\n"
        if normal_news:
            news_lines += "  📰 Other news:\n"
            for n in normal_news[:4]:
                news_lines += f"    [{n.get('sentiment','?').upper()}] {n.get('title')}\n"
        if not news_lines:
            news_lines = "  No recent news available.\n"

        # ── Feedback loop: past predictions ──────────────────────────────
        feedback_lines = ""
        if recent_reasoning:
            feedback_lines = "\n📊 YOUR RECENT PREDICTION PERFORMANCE:\n"
            wins = sum(1 for r in recent_reasoning if r.get("prediction_correct") is True)
            total_resolved = sum(1 for r in recent_reasoning if r.get("prediction_correct") is not None)
            if total_resolved:
                feedback_lines += f"  Accuracy: {wins}/{total_resolved} resolved predictions correct\n"
            for r in recent_reasoning:
                icon = "✅" if r.get("prediction_correct") else ("❌" if r.get("prediction_correct") is False else "⏳")
                feedback_lines += f"  {icon} {r.get('direction')} @ {r.get('confidence')}% — {str(r.get('hypothesis',''))[:90]}\n"

        # ── Multi-timeframe confluence summary ────────────────────────────
        mtf_signals = {
            "1h_bias":     ind1h.get("technical_bias", "?"),
            "4h_bias":     ind4h.get("technical_bias", "?"),
            "1h_ema":      ind1h.get("ema_trend", "?"),
            "4h_ema":      ind4h.get("ema_trend", "?"),
            "1h_macd":     ind1h.get("macd_cross", "?"),
            "4h_macd":     ind4h.get("macd_cross", "?"),
            "1h_regime":   ind1h.get("market_regime", "?"),
            "4h_regime":   ind4h.get("market_regime", "?"),
        }
        # Count bull vs bear signals across both timeframes
        bullish_count = sum(1 for v in mtf_signals.values() if "bull" in str(v))
        bearish_count = sum(1 for v in mtf_signals.values() if "bear" in str(v))
        mtf_confluence = (
            "STRONG BULL" if bullish_count >= 6 else
            "BULL"        if bullish_count >= 4 else
            "STRONG BEAR" if bearish_count >= 6 else
            "BEAR"        if bearish_count >= 4 else
            "MIXED"
        )

        lev      = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
        sl_pct   = config.STOP_LOSS_PCT * 100
        tp_pct   = config.TAKE_PROFIT_PCT * 100
        trade_usdt = max(bal * config.MAX_POSITION_PCT, config.MIN_ORDER_USDT) if bal else config.MIN_ORDER_USDT
        mode_line = (
            f"FUTURES {lev}x | Deploying ~${trade_usdt:.2f} USDT | "
            f"Auto-exit: SL=-{sl_pct:.1f}% (real -{sl_pct*lev:.0f}%) / TP=+{tp_pct:.1f}% (real +{tp_pct*lev:.0f}%)"
            if config.TRADE_MODE == "futures"
            else f"SPOT | Deploying ~${trade_usdt:.2f} USDT | SL=-{sl_pct:.1f}% / TP=+{tp_pct:.1f}%"
        )

        prompt = f"""You are an elite cryptocurrency FUTURES trader managing a live account.
Your job: conduct rigorous multi-factor research and deliver a precise short-term directional call.
BUY = open a LONG (profit if price rises). SELL = open a SHORT (profit if price falls).
Both directions are equally valid — choose based on evidence, not bias.

══════════════════════════════════════════════════════════════
MARKET SNAPSHOT — {pair} @ ${price:,.4f}
Time: {snapshot.get('timestamp')}  |  USDT Balance: ${bal:.4f}
24h: High=${s24.get('high_24h','?')} | Low=${s24.get('low_24h','?')} | Change={s24.get('price_change_pct_24h','?')}%
Trade context: {mode_line}
══════════════════════════════════════════════════════════════

▶ MARKET REGIME & TREND STRENGTH:
  1H ADX: {ind1h.get('adx','?')} ({ind1h.get('market_regime','?')}) | +DI: {ind1h.get('adx_pos_di','?')} | -DI: {ind1h.get('adx_neg_di','?')}
  4H ADX: {ind4h.get('adx','?')} ({ind4h.get('market_regime','?')}) | +DI: {ind4h.get('adx_pos_di','?')} | -DI: {ind4h.get('adx_neg_di','?')}
  → REGIME: Use trend-following signals when ADX>25, mean-reversion when ADX<20.

▶ MULTI-TIMEFRAME CONFLUENCE: {mtf_confluence} ({bullish_count} bull / {bearish_count} bear signals)
  1H Score: {ind1h.get('technical_score',0)}/100 ({ind1h.get('technical_bias','?')})
  4H Score: {ind4h.get('technical_score',0)}/100 ({ind4h.get('technical_bias','?')})

▶ MOMENTUM INDICATORS:
  RSI(14) 1H: {ind1h.get('rsi','?')} [{ind1h.get('rsi_zone','?')}]  |  4H: {ind4h.get('rsi','?')} [{ind4h.get('rsi_zone','?')}]
  StochRSI K/D 1H: {ind1h.get('stoch_rsi_k','?')} / {ind1h.get('stoch_rsi_d','?')} [{ind1h.get('stoch_rsi_signal','?')}] cross={ind1h.get('stoch_cross','?')}
  StochRSI K/D 4H: {ind4h.get('stoch_rsi_k','?')} / {ind4h.get('stoch_rsi_d','?')} [{ind4h.get('stoch_rsi_signal','?')}] cross={ind4h.get('stoch_cross','?')}
  Williams %R 1H: {ind1h.get('williams_r','?')} [{ind1h.get('williams_r_zone','?')}]

▶ TREND INDICATORS:
  MACD 1H: {ind1h.get('macd','?')} | Signal: {ind1h.get('macd_signal','?')} | Hist: {ind1h.get('macd_hist','?')} | Cross: {ind1h.get('macd_cross','?')} | Momentum: {ind1h.get('macd_momentum','?')}
  MACD 4H: {ind4h.get('macd','?')} | Cross: {ind4h.get('macd_cross','?')} | Momentum: {ind4h.get('macd_momentum','?')}
  EMA 1H: EMA20={ind1h.get('ema20','?')} EMA50={ind1h.get('ema50','?')} EMA200={ind1h.get('ema200','?')} | Trend: {ind1h.get('ema_trend','?')} | vs EMA200: {ind1h.get('price_vs_ema200','?')}
  EMA 4H: Trend: {ind4h.get('ema_trend','?')} | vs EMA200: {ind4h.get('price_vs_ema200','?')}

▶ VOLUME & PRICE STRUCTURE:
  OBV Trend 1H: {ind1h.get('obv_trend','?')}  |  4H: {ind4h.get('obv_trend','?')}
  VWAP 1H: ${ind1h.get('vwap','?')} | Price {ind1h.get('price_vs_vwap','?')} VWAP by {ind1h.get('vwap_gap_pct','?')}%
  Volume Ratio: {ind1h.get('volume_ratio','?')}x avg {'⚡ VOLUME SPIKE' if ind1h.get('volume_spike') else ''} | Trend: {ind1h.get('volume_trend','?')}
  Price changes: 1h={ind1h.get('change_1h',0):.2f}% | 4h={ind1h.get('change_4h',0):.2f}% | 24h={ind1h.get('change_24h',0):.2f}%

▶ VOLATILITY & LEVELS:
  ATR% 1H: {ind1h.get('atr_pct','?')}%  |  BB Width: {ind1h.get('bb_width','?')} {'[SQUEEZE]' if ind1h.get('bb_squeeze') else ''}
  BB Position 1H: {ind1h.get('bb_position','?')} (0=lower/oversold, 1=upper/overbought)
  Support (20): ${ind1h.get('support_20','?')} ({ind1h.get('dist_to_support_pct','?')}% below)
  Resistance (20): ${ind1h.get('resistance_20','?')} ({ind1h.get('dist_to_resistance_pct','?')}% above)

▶ ORDER BOOK IMBALANCE: {ob:+.3f}
  (>+0.2 = strong buy pressure | <-0.2 = strong sell pressure)

▶ MARKET SENTIMENT — Fear & Greed: {fng.get('value',50)}/100 ({fng.get('label','?')})
  Daily trend: {fng.get('trend','?')} | Weekly trend: {fng.get('weekly_trend','?')}
  → {fng.get('interpretation','')}

▶ NEWS INTELLIGENCE ({ns.get('total',0)} articles):
  Sentiment: {ns.get('positive',0)} bullish | {ns.get('negative',0)} bearish | {ns.get('neutral',0)} neutral
  High-impact articles: {ns.get('high_impact',0)} | News bias: {ns.get('bias','neutral')}
{news_lines}{feedback_lines}

══════════════════════════════════════════════════════════════
YOUR 6-STEP ANALYSIS TASK  (SHORT-TERM FUTURES — 10 to 40 min horizon)
══════════════════════════════════════════════════════════════

STEP 1 — REGIME CHECK:
  Is the market trending (ADX>25) or ranging (ADX<20)?
  Trending: trust EMA/MACD/DI signals for continuation.
  Ranging: trust RSI/StochRSI/BB mean-reversion (buy oversold, SELL overbought).

STEP 2 — DIRECTION ASSESSMENT (BUY or SELL equally valid):
  Bullish signals  → BUY/LONG:  rising EMA, bullish MACD cross, RSI<50 recovering, OBV up, +DI>-DI.
  Bearish signals  → SELL/SHORT: falling EMA, bearish MACD cross, RSI>70 rolling over, OBV down, -DI>+DI.
  Do 1H and 4H agree on direction? If both bearish → SELL. If both bullish → BUY. Mixed → HOLD.

STEP 3 — VOLUME CONFIRMATION:
  Does volume confirm the move? OBV trend matches price direction?
  Volume spike in direction of trade = higher confidence. Divergence = reduce confidence.

STEP 4 — NEWS CATALYST ASSESSMENT:
  High-impact bearish news = short opportunity. High-impact bullish news = long opportunity.
  No news = technically driven move = more predictable short-term.

STEP 5 — HYPOTHESIS & TRADE HORIZON:
  Form ONE clear hypothesis for the next 10-40 MINUTES.
  This is a short-term futures trade with automatic TP/SL exits.
  Estimate how many minutes you expect before price hits TP or SL.
  Only trade if R:R > 1.5:1 and you have conviction.

STEP 6 — CONFIDENCE CALIBRATION:
  Start at 50%. Adjust:
  +15 if 1H and 4H fully agree on direction (both bullish = BUY, both bearish = SELL)
  +10 if volume confirms (OBV matches + volume spike)
  +10 if strong news catalyst aligns with direction
  +10 if market regime matches strategy (trending→follow trend, ranging→fade extremes)
  -15 if timeframes contradict
  -10 if high ATR% (>2%) = unpredictable short-term swings
  -10 if signals are mixed with no dominant direction
  Final confidence must reflect genuine conviction. Bearish markets deserve SELL — do not force BUY.

Respond ONLY with a JSON object (no markdown, no text outside JSON):
{{
  "direction": "BUY" or "SELL" or "HOLD",
  "confidence": <integer 0-100>,
  "hypothesis": "<one sentence: what price will do, why, and the trigger>",
  "signal_alignment": "strong" or "mixed" or "contradictory",
  "key_signals": ["<signal 1>", "<signal 2>", "<signal 3>", "<signal 4>"],
  "market_regime": "trending" or "ranging" or "strong_trend",
  "risk_level": "LOW" or "MEDIUM" or "HIGH",
  "invalidation": "<exactly what would prove you wrong>",
  "risk_reward_ratio": <float, e.g. 2.0>,
  "market_context": "bullish" or "bearish" or "neutral",
  "trade_horizon_minutes": <integer 10-40, your expected time to TP or SL>,
  "reasoning": "<your full step-by-step reasoning from all 6 steps, 5-8 sentences>"
}}"""

        return prompt

    # ── Main Analysis Entry ────────────────────────────────────────────────

    def analyze(self, snapshot: dict) -> dict:
        """
        Send market context to Groq and get back a structured trading decision.
        Returns the reasoning dict with `_reasoning_id` attached so the
        executor can link the trade record back to this analysis.
        """
        from tenacity import retry, stop_after_attempt, wait_exponential
        import constants as C

        pair = snapshot["pair"]
        recent_reasoning = db.get_recent_reasoning(pair, limit=5)
        prompt = self._build_prompt(snapshot, recent_reasoning)

        logger.info(f"[{pair}] Sending research context to Groq ({config.GROQ_MODEL})...")

        @retry(
            stop=stop_after_attempt(C.GROQ_RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=C.GROQ_RETRY_MIN_WAIT, max=C.GROQ_RETRY_MAX_WAIT),
            reraise=True
        )
        def _get_completion():
            try:
                return self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a decisive professional crypto FUTURES trader. "
                                "Always respond with valid JSON only — no markdown, no preamble. "
                                "BUY = open a LONG (profit if price rises). "
                                "SELL = open a SHORT (profit if price falls). "
                                "Both directions are equally valid — choose based on evidence, not habit. "
                                "Bearish technicals (falling EMA, bearish MACD, RSI>70 rolling over, -DI>+DI) = SELL/SHORT. "
                                "Bullish technicals (rising EMA, bullish MACD, RSI recovering, +DI>-DI) = BUY/LONG. "
                                "HOLD only when signals genuinely conflict with no clear dominant direction. "
                                "Never default to BUY — if the market is falling, SELL is the correct call."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=C.GROQ_TEMPERATURE,
                    max_tokens=C.GROQ_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                # If we hit a rate limit (429), try rotating the key
                if "rate_limit_exceeded" in str(e).lower() or "429" in str(e):
                    if self._rotate_key(str(e)):
                        return _get_completion()
                raise e

        try:
            response = _get_completion()
            raw = response.choices[0].message.content
            reasoning = json.loads(raw)

            # Log to Supabase and capture the row ID for feedback loop linkage
            reasoning_id = db.log_agent_reasoning(pair, prompt, reasoning)
            reasoning["_reasoning_id"] = reasoning_id

            logger.info(
                f"[{pair}] Decision: {reasoning.get('direction')} @ {reasoning.get('confidence')}% "
                f"| Regime: {reasoning.get('market_regime')} "
                f"| Alignment: {reasoning.get('signal_alignment')} "
                f"| Risk: {reasoning.get('risk_level')} "
                f"| R:R={reasoning.get('risk_reward_ratio','?')} "
                f"| Horizon: ~{reasoning.get('trade_horizon_minutes','?')}min"
            )
            logger.info(f"[{pair}] Hypothesis: {reasoning.get('hypothesis')}")

            return reasoning

        except json.JSONDecodeError as e:
            logger.error(f"[{pair}] Groq returned invalid JSON: {e}")
            return {
                "direction": "HOLD",
                "confidence": 0,
                "reasoning": "JSON parse error",
                "_reasoning_id": None,
                "error": "JSON_PARSE_ERROR"
            }
        except Exception as e:
            logger.error(f"[{pair}] Groq API call failed after retries: {e}")
            return {
                "direction": "HOLD",
                "confidence": 0,
                "reasoning": str(e),
                "_reasoning_id": None,
                "error": "API_FAILURE"
            }
