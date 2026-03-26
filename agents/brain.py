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
from agents import browser_ai as _browser_ai_module

_KEY_STATUS_FILE = Path("logs/groq_key_status.json")
_OR_STATUS_FILE  = Path("logs/openrouter_key_status.json")
_GEM_STATUS_FILE = Path("logs/gemini_key_status.json")

# Free models tried in order when upstream rate-limits the primary model.
# Update config.OPENROUTER_MODEL to use any of these as primary.
_OPENROUTER_MODEL_FALLBACKS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-3-27b-it:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "openrouter/free",   # last resort — auto-routes to whatever is available
]

_AI_SYSTEM_PROMPT = (
    "You are a decisive professional crypto FUTURES trader. "
    "Always respond with valid JSON only — no markdown, no preamble. "
    "BUY = open a LONG (profit if price rises). "
    "SELL = open a SHORT (profit if price falls). "
    "Both directions are equally valid — choose based on evidence, not habit. "
    "Bearish technicals (falling EMA, bearish MACD, RSI>70 rolling over, -DI>+DI) = SELL/SHORT. "
    "Bullish technicals (rising EMA, bullish MACD, RSI recovering, +DI>-DI) = BUY/LONG. "
    "HOLD only when signals genuinely conflict with no clear dominant direction. "
    "Never default to BUY — if the market is falling, SELL is the correct call."
)


class AllKeysExhaustedError(Exception):
    """Raised when every Groq API key is currently rate-limited."""

class OpenRouterExhaustedError(Exception):
    """Raised when every OpenRouter free model failed or was rate-limited."""


class ApiKeyPool:
    """
    Generic rate-limited API key pool with persistent exhaustion tracking.

    Works for Groq, OpenRouter, and Gemini — just pass a different status_file.
    - Saves exhaustion state to disk so restarts don't retry known-bad keys.
    - Starts on a random available key to spread load.
    - Parses retry-after from error messages when available.
    """

    def __init__(self, keys: list, name: str, status_file: Path,
                 default_wait_seconds: float = 65.0):
        self.keys   = keys
        self.name   = name
        self._status_file        = status_file
        self._default_wait       = default_wait_seconds
        self._state              = self._load()
        self._clean_expired()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if self._status_file.exists():
                return json.loads(self._status_file.read_text())
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            self._status_file.parent.mkdir(exist_ok=True)
            self._status_file.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.warning(f"[{self.name}KeyPool] Could not save key state: {e}")

    def _clean_expired(self):
        """Remove keys whose reset time has already passed."""
        now = datetime.now(timezone.utc)
        expired = [
            tail for tail, info in self._state.items()
            if datetime.fromisoformat(info["reset_at"]) <= now
        ]
        for tail in expired:
            del self._state[tail]
            logger.info(f"[{self.name}KeyPool] Key ...{tail} reset — marking available.")
        if expired:
            self._save()

    # ── Public API ────────────────────────────────────────────────────────

    def available_keys(self) -> list:
        """Return all keys not currently exhausted."""
        self._clean_expired()
        return [k for k in self.keys if k[-8:] not in self._state]

    def all_exhausted(self) -> bool:
        return len(self.available_keys()) == 0

    def earliest_reset_seconds(self) -> float:
        """Seconds until the soonest exhausted key becomes available."""
        self._clean_expired()
        if not self._state:
            return 0.0
        now = datetime.now(timezone.utc)
        deltas = [
            (datetime.fromisoformat(info["reset_at"]) - now).total_seconds()
            for info in self._state.values()
        ]
        return max(0.0, min(deltas))

    def pick_start_key(self) -> str:
        """Return a random available key to spread load."""
        avail = self.available_keys()
        if not avail:
            raise RuntimeError(f"All {self.name} API keys are exhausted!")
        chosen = random.choice(avail)
        logger.info(
            f"[{self.name}KeyPool] Using key ...{chosen[-4:]} "
            f"({len(avail)}/{len(self.keys)} available)"
        )
        return chosen

    def next_available(self, current_key: str) -> str | None:
        """Return next available key after current, or None if all exhausted."""
        avail = [k for k in self.available_keys() if k != current_key]
        return avail[0] if avail else None

    def mark_rate_limited(self, key: str, error_msg: str = ""):
        """
        Mark a key as exhausted. Parses retry-after from error message.
        Falls back to daily (86400s) or per-minute (65s) heuristic.
        """
        m = re.search(r"try again in ([\d.]+)s", error_msg, re.IGNORECASE)
        if m:
            seconds = float(m.group(1))
        elif "day" in error_msg.lower() or "daily" in error_msg.lower():
            seconds = 86400
        else:
            seconds = self._default_wait

        reset_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        tail = key[-8:]
        self._state[tail] = {"reset_at": reset_at.isoformat()}
        self._save()
        logger.warning(
            f"[{self.name}KeyPool] Key ...{key[-4:]} rate-limited. "
            f"Available again in {seconds:.0f}s (~{reset_at.strftime('%H:%M:%S UTC')})"
        )

    def status_summary(self) -> str:
        self._clean_expired()
        avail  = len(self.available_keys())
        total  = len(self.keys)
        exhausted = [
            f"...{tail[-4:]} until {info['reset_at'][11:19]} UTC"
            for tail, info in self._state.items()
        ]
        line = f"{avail}/{total} keys available"
        if exhausted:
            line += " | Exhausted: " + ", ".join(exhausted)
        return line


class GroqKeyManager(ApiKeyPool):
    """Groq-specific key pool — adds startup log with check_groq_keys.py hint."""

    def __init__(self, keys: list):
        super().__init__(keys, "Groq", _KEY_STATUS_FILE, default_wait_seconds=65.0)

    def pick_start_key(self) -> str:
        avail = self.available_keys()
        if not avail:
            raise RuntimeError(
                "All Groq API keys are exhausted! "
                "Run: python utils/check_groq_keys.py to see reset times."
            )
        chosen = random.choice(avail)
        logger.info(
            f"[KeyManager] Starting with key ...{chosen[-4:]} "
            f"({len(avail)}/{len(self.keys)} available)"
        )
        return chosen

    # Keep old mark_rate_limited signature (no breaking change)
    def mark_rate_limited(self, key: str, error_msg: str = ""):
        super().mark_rate_limited(key, error_msg)


class TradingBrain:
    def __init__(self):
        # ── Key pools ─────────────────────────────────────────────────────
        self._key_mgr    = GroqKeyManager(config.GROQ_API_KEYS)
        self._or_key_mgr = ApiKeyPool(
            config.OPENROUTER_API_KEYS, "OpenRouter", _OR_STATUS_FILE,
            default_wait_seconds=3600.0,   # OpenRouter free tier resets hourly
        )
        self._gem_key_mgr = ApiKeyPool(
            config.GEMINI_API_KEYS, "Gemini", _GEM_STATUS_FILE,
            default_wait_seconds=3600.0,
        )

        # ── Active provider state ─────────────────────────────────────────
        self._current_key        = None
        self.groq                = None
        self._gemini_client      = None
        self._gemini_current_key = None
        self._using_fallback     = False   # True when NOT on Groq
        self._active_provider    = "groq"  # "groq" | "openrouter" | "gemini"

        # Per-call timestamps for hourly limits
        self._or_call_times:     list = []
        self._gemini_call_times: list = []

        # Spending-cap / permanent-disable flags
        self._or_spending_capped     = False
        self._gemini_spending_capped = False  # True when Google spending cap is hit

        # ── Browser AI (last-resort fallback) ─────────────────────────────
        self._browser_ai = _browser_ai_module.get_agent()
        if self._browser_ai.enabled:
            self._browser_ai.start()
            logger.info(
                f"[Brain] 🌐 Browser AI fallback enabled — "
                f"provider: {config.BROWSER_AI_PROVIDER.upper()}"
            )

        # ── Startup: pick first available provider (Groq → OR → Browser → Gemini) ─
        if not self._key_mgr.all_exhausted():
            self._current_key = self._key_mgr.pick_start_key()
            self._init_client()
        elif config.OPENROUTER_API_KEYS and not self._or_key_mgr.all_exhausted():
            logger.warning("[Brain] All Groq keys exhausted at startup — using OpenRouter fallback")
            self._active_provider = "openrouter"
            self._using_fallback  = True
        elif self._browser_ai.enabled:
            logger.warning("[Brain] Groq+OpenRouter exhausted at startup — Browser AI will be used")
            self._active_provider = "browser"
            self._using_fallback  = True
        elif config.GEMINI_API_KEYS and not self._gem_key_mgr.all_exhausted():
            logger.warning("[Brain] Groq+OpenRouter+Browser exhausted at startup — using Gemini API (paid)")
            self._init_gemini()
        else:
            logger.warning(
                "[Brain] All AI providers exhausted at startup. "
                "Cycles will skip until a provider resets."
            )

    def _init_client(self):
        self.groq = Groq(api_key=self._current_key)

    def all_providers_exhausted(self) -> bool:
        """
        True only when every tier is unavailable.
        Order: Groq → OpenRouter → Browser AI → Gemini API (paid).
        Gemini is considered available unless its spending cap is hit AND keys exhausted.
        """
        groq_dead    = self._key_mgr.all_exhausted()
        or_dead      = (not config.OPENROUTER_API_KEYS
                        or self._or_key_mgr.all_exhausted()
                        or self._or_spending_capped)
        browser_dead = not self._browser_ai.enabled or not self._browser_ai._ready
        gem_dead     = (not config.GEMINI_API_KEYS
                        or self._gemini_spending_capped
                        or self._gem_key_mgr.all_exhausted())
        return groq_dead and or_dead and browser_dead and gem_dead

    # ── Provider init ─────────────────────────────────────────────────────

    def _init_gemini(self):
        """Initialise Gemini with the next available key."""
        avail = self._gem_key_mgr.available_keys()
        if not avail:
            raise RuntimeError(
                "All Gemini API keys are exhausted. "
                "Add more keys as GEMINI_API_KEYS=key1,key2 in .env."
            )
        from google import genai
        self._gemini_current_key = avail[0]
        self._gemini_client = genai.Client(api_key=self._gemini_current_key)
        self._active_provider = "gemini"
        self._using_fallback  = True
        logger.info(
            f"[Brain] 🔀 Gemini fallback active — model: {config.GEMINI_MODEL} "
            f"| key ...{self._gemini_current_key[-4:]} "
            f"({len(avail)}/{len(config.GEMINI_API_KEYS)} available)"
        )

    def _try_recover_providers(self):
        """
        On each cycle: step back to the highest-priority available provider.
        Priority: Groq > OpenRouter > Browser AI > Gemini API
        """
        if not self._using_fallback:
            return  # already on Groq

        # Can we go back to Groq?
        if not self._key_mgr.all_exhausted():
            self._current_key = self._key_mgr.pick_start_key()
            self._init_client()
            self._active_provider = "groq"
            self._using_fallback  = False
            logger.info("[Brain] ✅ Groq keys available again — switched back to Groq")
            return

        # Can we step up from browser/gemini to OpenRouter?
        if (self._active_provider in ("browser", "gemini")
                and config.OPENROUTER_API_KEYS
                and not self._or_key_mgr.all_exhausted()
                and not self._or_spending_capped):
            self._active_provider = "openrouter"
            logger.info("[Brain] ↩️ OpenRouter available — stepping up from browser/Gemini")
            return

        # Can we step up from Gemini to Browser AI?
        if (self._active_provider == "gemini"
                and self._browser_ai.enabled
                and self._browser_ai._ready):
            self._active_provider = "browser"
            logger.info("[Brain] ↩️ Browser AI available — stepping up from Gemini API")

    # kept for backward compat
    def _try_switch_back_to_groq(self) -> bool:
        self._try_recover_providers()
        return not self._using_fallback

    def _calls_this_hour(self, timestamps: list) -> tuple[int, list]:
        """Sliding-window call count. Returns (count, pruned_list)."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        pruned = [t for t in timestamps if t > cutoff]
        return len(pruned), pruned

    def _gemini_calls_this_hour(self) -> int:
        """Count Gemini calls made in the last 60 minutes (sliding window)."""
        count, self._gemini_call_times = self._calls_this_hour(self._gemini_call_times)
        return count

    # ── OpenRouter ────────────────────────────────────────────────────────

    def _call_openrouter(self, prompt: str, pair: str, _model_idx: int = 0) -> dict:
        """
        Call OpenRouter (OpenAI-compatible API) and return a parsed reasoning dict.
        _model_idx: index into _OPENROUTER_MODEL_FALLBACKS used when upstream 429s the primary.
        """
        import requests as _req
        import constants as C

        # ── Hourly rate limit guard ────────────────────────────────────────
        used, self._or_call_times = self._calls_this_hour(self._or_call_times)
        if used >= config.OPENROUTER_HOURLY_LIMIT:
            oldest = self._or_call_times[0]
            resets_in = int((oldest + timedelta(hours=1) - datetime.now(timezone.utc)).total_seconds())
            logger.warning(
                f"[{pair}] ⏸ OpenRouter hourly limit reached "
                f"({used}/{config.OPENROUTER_HOURLY_LIMIT} calls). "
                f"Resets in {resets_in//60}m{resets_in%60:02d}s — returning HOLD."
            )
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "OpenRouter hourly limit reached", "_reasoning_id": None,
                    "error": "OPENROUTER_RATE_LIMITED"}

        # ── Pick key ───────────────────────────────────────────────────────
        if self._or_key_mgr.all_exhausted():
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "All OpenRouter keys exhausted", "_reasoning_id": None,
                    "error": "OPENROUTER_EXHAUSTED"}
        key = self._or_key_mgr.pick_start_key()

        # ── Pick model (primary config, then fallback list on upstream 429) ─
        # Build model order: [config model] + fallbacks (deduped)
        model_order = [config.OPENROUTER_MODEL] + [
            m for m in _OPENROUTER_MODEL_FALLBACKS if m != config.OPENROUTER_MODEL
        ]
        if _model_idx >= len(model_order):
            logger.error(f"[{pair}] OpenRouter: all free models failed — falling through to next provider.")
            raise OpenRouterExhaustedError("All OpenRouter free models rate-limited or failed.")
        model = model_order[_model_idx]

        logger.info(f"[{pair}] Sending research context to OpenRouter ({model})...")
        try:
            resp = _req.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization":  f"Bearer {key}",
                    "Content-Type":   "application/json",
                    "HTTP-Referer":   "https://github.com/crypto-agent",
                    "X-Title":        "Crypto Trading Agent",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _AI_SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    "temperature":     C.GROQ_TEMPERATURE,
                    "max_tokens":      C.OPENROUTER_MAX_TOKENS,
                    "response_format": {"type": "json_object"},
                },
                timeout=45,
            )

            if resp.status_code == 429:
                err_text = resp.text
                # Upstream model rate-limited → try next model in fallback list
                if "upstream" in err_text.lower() or "provider returned error" in err_text.lower():
                    logger.warning(
                        f"[{pair}] OpenRouter model {model!r} upstream rate-limited — "
                        f"trying next free model ({_model_idx + 1}/{len(model_order) - 1})"
                    )
                    return self._call_openrouter(prompt, pair, _model_idx + 1)
                # Our key rate-limited → rotate key
                if "spending cap" in err_text.lower() or "credits" in err_text.lower():
                    self._or_spending_capped = True
                    logger.error(
                        f"[{pair}] 💳 OpenRouter spending cap/credits exhausted — disabling. "
                        f"Add credits at openrouter.ai"
                    )
                    return {"direction": "HOLD", "confidence": 0,
                            "reasoning": "OpenRouter spending cap", "_reasoning_id": None,
                            "error": "OPENROUTER_SPENDING_CAP"}
                self._or_key_mgr.mark_rate_limited(key, err_text)
                nxt = self._or_key_mgr.next_available(key)
                if nxt:
                    logger.info(f"[{pair}] OpenRouter key exhausted — rotating to ...{nxt[-4:]}")
                    return self._call_openrouter(prompt, pair, _model_idx)
                logger.error(f"[{pair}] All OpenRouter keys exhausted — falling through to next provider.")
                raise OpenRouterExhaustedError("All OpenRouter keys exhausted.")

            resp.raise_for_status()
            data = resp.json()

            # Some reasoning models put content in `reasoning` field or return None content
            msg = data["choices"][0]["message"]
            raw = msg.get("content") or msg.get("reasoning") or ""
            if not raw or not raw.strip():
                logger.warning(f"[{pair}] OpenRouter model {model!r} returned empty content — trying next model")
                return self._call_openrouter(prompt, pair, _model_idx + 1)
            reasoning = json.loads(raw)
            self._or_call_times.append(datetime.now(timezone.utc))
            reasoning_id = db.log_agent_reasoning(pair, prompt, reasoning)
            reasoning["_reasoning_id"] = reasoning_id

            used_now = len(self._or_call_times)
            model_used = data.get("model", model)
            logger.info(
                f"[{pair}] Decision: {reasoning.get('direction')} @ {reasoning.get('confidence')}% "
                f"| Regime: {reasoning.get('market_regime')} "
                f"| Alignment: {reasoning.get('signal_alignment')} "
                f"| Risk: {reasoning.get('risk_level')} "
                f"| R:R={reasoning.get('risk_reward_ratio','?')} "
                f"| Horizon: ~{reasoning.get('trade_horizon_minutes','?')}min "
                f"| OpenRouter ({model_used}): {used_now}/{config.OPENROUTER_HOURLY_LIMIT} calls/hr"
            )
            logger.info(f"[{pair}] Hypothesis: {reasoning.get('hypothesis')}")
            return reasoning

        except json.JSONDecodeError as e:
            logger.error(f"[{pair}] OpenRouter returned invalid JSON: {e}")
            # Try next model on JSON error too (some free models misbehave)
            return self._call_openrouter(prompt, pair, _model_idx + 1)
        except Exception as e:
            err = str(e)
            if "spending cap" in err.lower() or "credits" in err.lower():
                self._or_spending_capped = True
                logger.error(f"[{pair}] 💳 OpenRouter spending cap/credits exhausted — disabling.")
                return {"direction": "HOLD", "confidence": 0,
                        "reasoning": "OpenRouter spending cap", "_reasoning_id": None,
                        "error": "OPENROUTER_SPENDING_CAP"}
            logger.error(f"[{pair}] OpenRouter API call failed: {e}")
            return {"direction": "HOLD", "confidence": 0, "reasoning": err,
                    "_reasoning_id": None, "error": "API_FAILURE"}

    def _call_gemini(self, prompt: str, pair: str) -> dict:
        """Call Gemini and return a parsed reasoning dict (same shape as Groq)."""
        from google import genai
        import constants as C

        # ── Hourly rate limit guard (per key × number of keys) ───────────
        total_hourly_cap = config.GEMINI_HOURLY_LIMIT * max(len(config.GEMINI_API_KEYS), 1)
        used = self._gemini_calls_this_hour()
        if used >= total_hourly_cap:
            oldest = self._gemini_call_times[0]
            resets_in = int((oldest + timedelta(hours=1) - datetime.now(timezone.utc)).total_seconds())
            logger.warning(
                f"[{pair}] ⏸ Gemini hourly limit reached "
                f"({used}/{total_hourly_cap} calls across {len(config.GEMINI_API_KEYS)} key(s)). "
                f"Resets in {resets_in//60}m{resets_in%60:02d}s — returning HOLD."
            )
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "Gemini hourly limit reached", "_reasoning_id": None,
                    "error": "GEMINI_RATE_LIMITED"}

        logger.info(
            f"[{pair}] Sending research context to Gemini ({config.GEMINI_MODEL}) "
            f"| key ...{(self._gemini_current_key or '')[-4:]}..."
        )
        try:
            response = self._gemini_client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=_AI_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=C.GROQ_TEMPERATURE,
                    max_output_tokens=C.GEMINI_MAX_TOKENS,
                ),
            )
            self._gemini_call_times.append(datetime.now(timezone.utc))  # record usage
            reasoning = json.loads(response.text)
            reasoning_id = db.log_agent_reasoning(pair, prompt, reasoning)
            reasoning["_reasoning_id"] = reasoning_id
            used_now = len(self._gemini_call_times)
            logger.info(
                f"[{pair}] Decision: {reasoning.get('direction')} @ {reasoning.get('confidence')}% "
                f"| Regime: {reasoning.get('market_regime')} "
                f"| Alignment: {reasoning.get('signal_alignment')} "
                f"| Risk: {reasoning.get('risk_level')} "
                f"| R:R={reasoning.get('risk_reward_ratio','?')} "
                f"| Horizon: ~{reasoning.get('trade_horizon_minutes','?')}min "
                f"| Gemini: {used_now}/{config.GEMINI_HOURLY_LIMIT} calls/hr"
            )
            logger.info(f"[{pair}] Hypothesis: {reasoning.get('hypothesis')}")
            return reasoning
        except json.JSONDecodeError as e:
            logger.error(f"[{pair}] Gemini returned invalid JSON: {e}")
            return {"direction": "HOLD", "confidence": 0, "reasoning": "JSON parse error",
                    "_reasoning_id": None, "error": "JSON_PARSE_ERROR"}
        except Exception as e:
            err = str(e)
            # Spending cap — all keys under same project share it
            if "spending cap" in err.lower() or (
                "resource_exhausted" in err.lower() and "spending" in err.lower()
            ):
                self._gemini_spending_capped = True
                logger.error(
                    f"[{pair}] 💳 Gemini spending cap hit — disabling Gemini fallback. "
                    f"Raise cap at: console.cloud.google.com → Billing → Budgets & alerts"
                )
                return {"direction": "HOLD", "confidence": 0,
                        "reasoning": "Gemini spending cap hit",
                        "_reasoning_id": None, "error": "GEMINI_SPENDING_CAP"}
            # Rate limit on this key → try next Gemini key
            if "resource_exhausted" in err.lower() or "429" in err or "quota" in err.lower():
                cur = self._gemini_current_key
                if cur:
                    self._gem_key_mgr.mark_rate_limited(cur, err)
                nxt = self._gem_key_mgr.next_available(cur or "")
                if nxt:
                    logger.warning(f"[{pair}] Gemini key ...{(cur or '')[-4:]} rate-limited — rotating to ...{nxt[-4:]}")
                    self._init_gemini()   # re-init with next available key
                    return self._call_gemini(prompt, pair)
                logger.error(f"[{pair}] All Gemini keys exhausted.")
                return {"direction": "HOLD", "confidence": 0,
                        "reasoning": "All Gemini keys exhausted", "_reasoning_id": None,
                        "error": "GEMINI_EXHAUSTED"}
            logger.error(f"[{pair}] Gemini API call failed: {e}")
            return {"direction": "HOLD", "confidence": 0, "reasoning": err,
                    "_reasoning_id": None, "error": "API_FAILURE"}

    def _rotate_key(self, error_msg: str = ""):
        """Mark current key as rate-limited and switch to next available.
        Raises AllKeysExhaustedError if no key is available after rotation."""
        self._key_mgr.mark_rate_limited(self._current_key, error_msg)
        nxt = self._key_mgr.next_available(self._current_key)
        if nxt:
            self._current_key = nxt
            self._init_client()
            logger.info(f"[KeyManager] Switched to key ...{nxt[-4:]} | {self._key_mgr.status_summary()}")
            return
        wait = self._key_mgr.earliest_reset_seconds()
        logger.error(
            f"[KeyManager] All keys exhausted! {self._key_mgr.status_summary()} "
            f"| First reset in {wait:.0f}s"
        )
        raise AllKeysExhaustedError(
            f"All Groq API keys are rate-limited. First reset in {wait:.0f}s."
        )

    # ── Prompt Builder ────────────────────────────────────────────────────

    def _build_prompt(self, snapshot: dict, recent_reasoning: list) -> str:
        pair   = snapshot["pair"]
        price  = snapshot["current_price"]
        ind1h  = snapshot.get("indicators_1h", {})
        ind4h  = snapshot.get("indicators_4h", {})
        news   = snapshot.get("news", [])
        ns     = snapshot.get("news_summary", {})
        fng    = snapshot.get("fear_greed", {})
        ob     = snapshot.get("order_book_imbalance") or 0
        bal    = snapshot.get("usdt_balance") or 0
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
  Price changes: 1h={(ind1h.get('change_1h') or 0):.2f}% | 4h={(ind1h.get('change_4h') or 0):.2f}% | 24h={(ind1h.get('change_24h') or 0):.2f}%

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

        # ── Try to recover to a higher-priority provider ──────────────────────
        if self._using_fallback:
            self._try_recover_providers()

        # ── Short-circuit to active fallback provider ─────────────────────────
        if self._using_fallback:
            if self._active_provider == "openrouter":
                try:
                    return self._call_openrouter(prompt, pair)
                except OpenRouterExhaustedError:
                    # All free models failed — fall through to next tier
                    pass
            if self._active_provider in ("openrouter", "browser"):
                if self._browser_ai.enabled and self._browser_ai._ready:
                    logger.warning(f"[{pair}] OpenRouter exhausted — switching to Browser AI")
                    self._active_provider = "browser"
                    return self._browser_ai.ask(prompt, pair)
                # No browser AI — jump straight to Gemini
                if config.GEMINI_API_KEYS and not self._gem_key_mgr.all_exhausted() and not self._gemini_spending_capped:
                    logger.warning(f"[{pair}] 💳 OpenRouter+Browser exhausted — switching to Gemini API (paid)")
                    self._init_gemini()
                    self._active_provider = "gemini"
            if self._active_provider == "gemini":
                return self._call_gemini(prompt, pair)
            # All providers truly dead
            wait = self._key_mgr.earliest_reset_seconds()
            logger.error(f"[{pair}] ⛔ ALL PROVIDERS EXHAUSTED. Groq resets in {wait:.0f}s.")
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "All AI providers exhausted", "_reasoning_id": None,
                    "error": "ALL_AI_EXHAUSTED"}

        # ── Groq path ─────────────────────────────────────────────────────────
        logger.info(f"[{pair}] Sending research context to Groq ({config.GROQ_MODEL})...")

        from tenacity import retry_if_not_exception_type

        @retry(
            stop=stop_after_attempt(C.GROQ_RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=1, min=C.GROQ_RETRY_MIN_WAIT, max=C.GROQ_RETRY_MAX_WAIT),
            retry=retry_if_not_exception_type(AllKeysExhaustedError),
            reraise=True
        )
        def _get_completion():
            # Bail immediately — no point hitting the API when all keys are dead
            if self._key_mgr.all_exhausted():
                raise AllKeysExhaustedError(
                    f"All Groq API keys are rate-limited. "
                    f"First reset in {self._key_mgr.earliest_reset_seconds():.0f}s."
                )
            try:
                return self.groq.chat.completions.create(
                    model=config.GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": _AI_SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    temperature=C.GROQ_TEMPERATURE,
                    max_tokens=C.GROQ_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
            except AllKeysExhaustedError:
                raise
            except Exception as e:
                err = str(e)
                # 401 invalid key — remove it permanently and try next key
                if "401" in err or "invalid_api_key" in err.lower():
                    logger.error(
                        f"[KeyManager] Key ...{self._current_key[-4:]} is INVALID (401) — "
                        f"removing permanently. Check your .env GROQ_API_KEYS."
                    )
                    bad_key = self._current_key
                    self._key_mgr.keys = [k for k in self._key_mgr.keys if k != bad_key]
                    nxt = self._key_mgr.next_available(bad_key)
                    if nxt:
                        self._current_key = nxt
                        self._init_client()
                        return _get_completion()
                    raise AllKeysExhaustedError("All remaining Groq keys are invalid or exhausted.")
                # If we hit a rate limit (429), rotate to next key and retry
                if "rate_limit_exceeded" in err.lower() or "429" in err:
                    self._rotate_key(err)   # raises AllKeysExhaustedError if none left
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

        except AllKeysExhaustedError:
            # ── Tier 2: OpenRouter (free) ──────────────────────────────────
            if (config.OPENROUTER_API_KEYS
                    and not self._or_key_mgr.all_exhausted()
                    and not self._or_spending_capped):
                logger.warning(f"[{pair}] Groq exhausted — switching to OpenRouter (free)")
                self._active_provider = "openrouter"
                self._using_fallback  = True
                try:
                    return self._call_openrouter(prompt, pair)
                except OpenRouterExhaustedError:
                    pass  # fall through to next tier

            # ── Tier 3: Browser AI (free — ChatGPT/Gemini web) ────────────
            if self._browser_ai.enabled and self._browser_ai._ready:
                logger.warning(
                    f"[{pair}] 🌐 Groq+OpenRouter exhausted — falling back to "
                    f"Browser AI ({config.BROWSER_AI_PROVIDER.upper()})"
                )
                self._active_provider = "browser"
                self._using_fallback  = True
                return self._browser_ai.ask(prompt, pair)

            # ── Tier 4: Gemini API (paid — last resort) ────────────────────
            if (config.GEMINI_API_KEYS
                    and not self._gem_key_mgr.all_exhausted()
                    and not self._gemini_spending_capped):
                logger.warning(
                    f"[{pair}] 💳 Groq+OpenRouter+Browser exhausted — "
                    f"switching to Gemini API (paid, last resort)"
                )
                self._init_gemini()
                return self._call_gemini(prompt, pair)

            # ── All dead ───────────────────────────────────────────────────
            wait = self._key_mgr.earliest_reset_seconds()
            logger.error(
                f"[{pair}] ⛔ ALL AI PROVIDERS EXHAUSTED — "
                f"Groq rate-limited | OpenRouter {'spending cap' if self._or_spending_capped else 'exhausted'} | "
                f"Browser AI {'not configured' if not self._browser_ai.enabled else 'not ready'} | "
                f"Gemini {'spending cap' if self._gemini_spending_capped else 'exhausted/not configured'}. "
                f"Groq resets in {wait:.0f}s (~{wait/60:.1f}min)."
            )
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "All AI providers exhausted",
                    "_reasoning_id": None, "error": "ALL_AI_EXHAUSTED"}

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
