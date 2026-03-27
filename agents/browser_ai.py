"""
agents/browser_ai.py — Browser AI Agent (last-resort fallback)

Uses Playwright to send prompts to ChatGPT or Gemini in your browser and
extract JSON trading decisions — identical shape to Groq/OpenRouter/Gemini API.

SETUP (one-time):
  pip install playwright
  playwright install chromium

  Then log into the provider in headed mode:
    python agents/browser_ai.py --login chatgpt
    python agents/browser_ai.py --login gemini

  Your session is saved to ./browser_session/ so future runs stay logged in.

SWITCH in .env:
  BROWSER_AI_PROVIDER=chatgpt        # use ChatGPT browser
  BROWSER_AI_PROVIDER=gemini         # use Gemini browser
  BROWSER_AI_PROVIDER=off            # disabled (default)

NOTES:
  - The browser window is kept running in the background after first use.
  - Each call opens a NEW conversation to avoid context bleed between pairs.
  - The AI is asked to respond ONLY with JSON — same prompt as Groq.
  - If JSON parsing fails we retry up to 2 times before returning HOLD.
"""
import asyncio
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

# Allow running as:  python agents/browser_ai.py  (from project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger
import config

USER_DATA_DIR = Path("browser_session")

# ── Chrome performance flags ──────────────────────────────────────────────────
# Base flags used for both headed (login) and headless (agent) modes.
_CHROME_ARGS_BASE = [
    "--no-sandbox",                                  # required on Linux/Docker
    "--disable-blink-features=AutomationControlled", # hide automation signal so Google doesn't restrict session
    "--disable-dev-shm-usage",                       # prevent crashes on limited shared memory (Linux)
]
# Headless-only: disable GPU rendering pipeline (no display available)
_CHROME_ARGS_HEADLESS = _CHROME_ARGS_BASE + ["--disable-gpu"]

# ── Cache directories to clean periodically ───────────────────────────────────
# IMPORTANT: Only clean GPU/shader caches — NOT the HTTP cache (Default/Cache)
# or Code Cache. Those contain Gemini's JS bundles. Deleting them forces Chrome
# to re-download 5-10MB of JS every cycle, causing 15-30s load delays.
_CACHE_DIRS = [
    USER_DATA_DIR / "Default" / "GPUCache",
    USER_DATA_DIR / "Default" / "DawnWebGPUCache",
    USER_DATA_DIR / "Default" / "DawnGraphiteCache",
    USER_DATA_DIR / "ShaderCache",
    USER_DATA_DIR / "GrShaderCache",
    USER_DATA_DIR / "GraphiteDawnCache",
]
_CACHE_CLEAN_INTERVAL = 45 * 60   # 45 minutes


def _clean_browser_cache():
    """Delete Chrome cache dirs. Chrome recreates them on demand."""
    cleaned, freed = 0, 0
    for d in _CACHE_DIRS:
        if d.exists():
            try:
                freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d)
                cleaned += 1
            except Exception as e:
                logger.debug(f"[BrowserAI] Cache clean skip {d.name}: {e}")
    if cleaned:
        logger.info(f"[BrowserAI] Cache cleaned — {cleaned} dirs, {freed / 1024 / 1024:.1f} MB freed")


def _cache_cleaner_loop():
    """Background thread: clean cache every 45 minutes."""
    while True:
        time.sleep(_CACHE_CLEAN_INTERVAL)
        _clean_browser_cache()


# ── Selectors (fallback lists — most reliable first) ─────────────────────────
_CHATGPT_INPUT_SELECTORS = [
    "#prompt-textarea",
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true']",
]
_CHATGPT_RESPONSE_SELECTORS = [
    "[data-message-author-role='assistant'] .markdown",
    "[data-message-author-role='assistant']",
    "article[data-testid*='conversation-turn'] .markdown",
    "article[data-testid*='conversation-turn']",
]
_CHATGPT_STOP_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label='Stop streaming']",
    "[aria-label='Stop generating']",
]
_CHATGPT_NEW_CHAT_SELECTORS = [
    "a[href='/']:not([aria-label])",            # sidebar "New chat" link
    "button[aria-label='New chat']",
    "a[aria-label='New chat']",
]

_GEMINI_INPUT_SELECTORS = [
    "rich-textarea .ql-editor",
    "div.ql-editor[contenteditable='true']",
    "div[contenteditable='true'][aria-label]",
    "textarea[placeholder]",
]
_GEMINI_RESPONSE_SELECTORS = [
    "model-response .markdown",
    "model-response",
    ".response-content",
    "message-content",
]
_GEMINI_NEW_CHAT_SELECTORS = [
    "a[href='/app']",
    "button[aria-label='New chat']",
    "[data-test-id='new-chat-button']",
]

RESPONSE_TIMEOUT_MS = 90_000   # 90 seconds max wait


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _find_element(page, selectors: list, timeout_ms: int = 12000):
    """Try each selector in order; return the first visible locator found."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    raise RuntimeError(f"None of the selectors found: {selectors}")


def _extract_json(text: str) -> dict:
    """
    Extract the first valid JSON object from AI response text.
    AI often wraps JSON in ```json ... ``` markdown blocks.
    """
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "").strip()

    # Find the outermost { ... } block
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Unbalanced braces in JSON response")


# ─── ChatGPT ─────────────────────────────────────────────────────────────────

async def _ask_chatgpt(page, message: str, attempt: int = 0) -> str:
    """
    Send a message on ChatGPT and return the full text response.
    The page is pre-warmed (already on chatgpt.com) — no navigation at start.
    We navigate back to a fresh chat at the END so the next call is instant.
    """
    logger.info(f"[BrowserAI/ChatGPT] Sending prompt (attempt {attempt + 1})...")

    # Only navigate if not already on ChatGPT (error recovery)
    if "chatgpt.com" not in page.url:
        logger.debug(f"[BrowserAI/ChatGPT] Not on ChatGPT — navigating now")
        await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1500)

    # Type message
    logger.debug(f"[BrowserAI/ChatGPT] Typing prompt ({len(message)} chars)...")
    textarea = await _find_element(page, _CHATGPT_INPUT_SELECTORS)
    await textarea.click()
    await textarea.fill(message)
    await page.wait_for_timeout(400)

    # Send
    await page.keyboard.press("Enter")
    logger.info("[BrowserAI/ChatGPT] Prompt sent — waiting for response...")
    await page.wait_for_timeout(2000)

    # Wait for streaming to start (Stop button appears) then finish (disappears)
    try:
        stop_btn = page.locator(", ".join(_CHATGPT_STOP_SELECTORS))
        await stop_btn.wait_for(state="visible", timeout=15_000)
        await stop_btn.wait_for(state="hidden", timeout=RESPONSE_TIMEOUT_MS)
    except Exception:
        # Stop button never appeared (fast response) — just wait a moment
        await page.wait_for_timeout(4000)

    await page.wait_for_timeout(800)

    # Extract last assistant message
    result = None
    for sel in _CHATGPT_RESPONSE_SELECTORS:
        try:
            msgs = await page.locator(sel).all()
            if msgs:
                result = (await msgs[-1].inner_text()).strip()
                break
        except Exception:
            continue

    if result is None:
        raise RuntimeError("Could not extract ChatGPT response")

    # Pre-warm: navigate to fresh chat so next call is instant
    await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
    logger.debug("[BrowserAI/ChatGPT] Page reset — ready for next call")
    return result


# ─── Gemini ──────────────────────────────────────────────────────────────────

async def _ask_gemini(page, message: str, attempt: int = 0) -> str:
    """Send a message on Gemini and return the full text response."""
    logger.info(f"[BrowserAI/Gemini] Opening new conversation (attempt {attempt + 1})...")

    # ── Step 1: Verify we're on a blank Gemini page ──────────────────────────
    # The page is pre-warmed at init and reloaded at the END of each call,
    # so we should already be on a fresh /app page — no navigation needed.
    # Only navigate if something went wrong (crash, wrong URL, etc.).
    current_url = page.url
    if "gemini.google.com/app" not in current_url:
        logger.debug(f"[BrowserAI/Gemini] Not on Gemini (url={current_url!r}) — navigating now")
        await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)

    # ── Step 2: Wait for input + paste prompt ────────────────────────────────
    # Poll via JS — handles Angular shadow DOM and slow hydration.
    # No fixed sleep — we wait for the element itself to be ready.
    logger.debug(f"[BrowserAI/Gemini] Waiting for input + pasting prompt ({len(message)} chars)...")
    pasted = False
    for _ in range(20):   # max 20 × 1s = 20s
        pasted = await page.evaluate(
            """(text) => {
                const el = document.querySelector('div[role="textbox"]')
                         || document.querySelector('.ql-editor')
                         || document.querySelector('rich-textarea');
                if (!el) return false;
                el.focus();
                el.click();
                document.execCommand('selectAll');
                document.execCommand('insertText', false, text);
                return el.textContent.length > 0;
            }""",
            message,
        )
        if pasted:
            break
        await page.wait_for_timeout(1000)   # 1s between checks (was 2s)

    if not pasted:
        raise RuntimeError("Gemini input never appeared — is the session still logged in?")

    # ── Step 3: Send ─────────────────────────────────────────────────────────
    await page.keyboard.press("Enter")
    logger.info("[BrowserAI/Gemini] Prompt sent — waiting for response...")

    # After sending, Gemini navigates from /app → /app/<conversation_id>
    try:
        await page.wait_for_url(re.compile(r".*/app/.+"), timeout=10_000)
        logger.debug("[BrowserAI/Gemini] URL changed — conversation created")
    except Exception:
        pass  # some Gemini versions stay on /app — that's fine

    # ── Step 4: Wait for response element ────────────────────────────────────
    found_sel = None
    for sel in _GEMINI_RESPONSE_SELECTORS:
        try:
            await page.locator(sel).first.wait_for(state="attached", timeout=60_000)
            found_sel = sel
            logger.debug(f"[BrowserAI/Gemini] Response element found: {sel!r}")
            break
        except Exception:
            logger.debug(f"[BrowserAI/Gemini] Selector {sel!r} not found — trying next")
            continue

    if not found_sel:
        raise RuntimeError(
            "Gemini never started responding — none of the response selectors matched. "
            "Check that the session is still valid (run: python agents/browser_ai.py --login gemini)"
        )

    # ── Step 5: Poll for stable response text ────────────────────────────────
    # Streaming is done when text stops changing for 2 consecutive 1s checks (= 2s stable).
    logger.info("[BrowserAI/Gemini] Polling for stable response text...")
    prev_text = ""
    stable_count = 0
    raw = ""
    for _ in range(100):         # max 100 × 1s = 100s
        await page.wait_for_timeout(1000)   # 1s poll (was 2s)
        try:
            count = await page.locator(found_sel).count()
            if count == 0:
                continue
            candidate = (await page.locator(found_sel).last.inner_text()).strip()
            if candidate and candidate == prev_text:
                stable_count += 1
                if stable_count >= 2:   # unchanged for 2s = streaming done
                    raw = candidate
                    break
            else:
                stable_count = 0
            prev_text = candidate
        except Exception:
            continue
    else:
        raw = prev_text
        if not raw:
            raise RuntimeError("Gemini response never produced stable text after 100s")

    # Strip "Gemini said\n\n" accessibility prefix added for screen readers
    if raw.lower().startswith("gemini said"):
        raw = raw.split("\n", 2)[-1].strip()

    # Detect Gemini error pages — raise so the caller retries
    _ERROR_PHRASES = [
        "something went wrong",
        "couldn't process",
        "an error occurred",
        "try again",
        "i'm not able to",
        "unable to respond",
    ]
    if any(p in raw.lower() for p in _ERROR_PHRASES) and len(raw) < 400:
        raise RuntimeError(f"Gemini returned an error page: {raw[:120]}")

    # ── Step 6: Pre-warm for next call ───────────────────────────────────────
    # Navigate back to /app NOW (while we process the result) so the next call
    # finds a blank page ready to go — zero navigation wait on the next request.
    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
    logger.debug("[BrowserAI/Gemini] Page reset to /app — ready for next call")

    return raw


# ─── BrowserAIAgent ──────────────────────────────────────────────────────────

class BrowserAIAgent:
    """
    Singleton browser AI agent. Keeps one Playwright context alive for the
    entire agent run to avoid per-call browser startup overhead (~3s).

    Thread-safe: uses an asyncio event loop running in a background thread.
    """

    # Minimum seconds between consecutive Gemini/ChatGPT calls — avoids rate limiting
    _INTER_CALL_COOLDOWN = 15

    def __init__(self, headed: bool | None = None):
        self._provider   = config.BROWSER_AI_PROVIDER.lower()   # "chatgpt" | "gemini" | "off"
        # headed: CLI flag overrides .env; .env overrides default (False)
        self._headed     = headed if headed is not None else config.BROWSER_AI_HEADED
        self._loop       = None
        self._context    = None
        self._page       = None
        self._playwright = None
        self._lock       = threading.Lock()
        self._ready      = False
        self._last_call  = 0.0   # epoch time of last completed call

        if self._provider == "off":
            logger.info("[BrowserAI] Disabled (BROWSER_AI_PROVIDER=off)")
        else:
            logger.info(f"[BrowserAI] Provider: {self._provider.upper()} | "
                        f"Session dir: {USER_DATA_DIR}")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Launch the background event loop and open the browser (non-blocking)."""
        if self._provider == "off" or self._ready:
            return
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()
        # Wait up to 30s for browser to be ready
        import time
        for _ in range(60):
            if self._ready:
                break
            time.sleep(0.5)
        if not self._ready:
            logger.warning("[BrowserAI] Browser did not become ready in 30s — "
                           "make sure you are logged in (run: python agents/browser_ai.py --login)")

    def _run_loop(self):
        """Background thread: owns the asyncio event loop and browser context."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init_browser())
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"[BrowserAI] Background loop error: {e}")
        finally:
            if self._context:
                self._loop.run_until_complete(self._context.close())

    async def _init_browser(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        USER_DATA_DIR.mkdir(exist_ok=True)
        # Clean stale cache before launch so startup is faster
        _clean_browser_cache()
        # channel="chrome" uses your real system Chrome — Google/OpenAI accept it.
        # Bundled Chromium gets "Couldn't sign you in / browser may not be secure".
        chrome_args = _CHROME_ARGS_HEADLESS if not self._headed else _CHROME_ARGS_BASE
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=not self._headed,
            args=chrome_args,
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        # Patch navigator.webdriver = undefined on every page load.
        # --disable-blink-features=AutomationControlled removes the Chrome flag,
        # but JS code can still read navigator.webdriver = true. Google checks both.
        await self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        # Pre-warm: navigate to Gemini/ChatGPT now so the first call has zero nav wait.
        # Subsequent calls also skip navigation — we reload at the END of each call instead.
        await self._prewarm_page()
        self._ready = True
        logger.info(f"[BrowserAI] Browser ready ({self._provider.upper()} mode)")
        # Start periodic cache cleaner (runs every 45 min in background)
        threading.Thread(
            target=_cache_cleaner_loop, daemon=True, name="browser-cache-cleaner"
        ).start()

    async def _prewarm_page(self):
        """Navigate to the provider's home so the page is ready for the next call."""
        try:
            if self._provider == "chatgpt":
                await self._page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
            else:
                await self._page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
            logger.debug(f"[BrowserAI] Page pre-warmed for next {self._provider.upper()} call")
        except Exception as e:
            logger.warning(f"[BrowserAI] Pre-warm navigation failed (non-fatal): {e}")

    def stop(self):
        """Gracefully shut down the browser."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._context.close(), self._loop)

    # ── Public API ────────────────────────────────────────────────────────

    def ask(self, message: str, pair: str) -> dict:
        """
        Send prompt to the configured browser AI and return a parsed reasoning dict.
        This is the method called by brain.py — runs synchronously from the caller's
        thread, executes async work on the background event loop.
        """
        if self._provider == "off":
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "Browser AI disabled", "_reasoning_id": None,
                    "error": "BROWSER_AI_DISABLED"}

        if not self._ready:
            logger.warning(f"[{pair}] [BrowserAI] Browser not ready — skipping")
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": "Browser not ready", "_reasoning_id": None,
                    "error": "BROWSER_NOT_READY"}

        # Enforce inter-call cooldown — avoid rate limiting / "something went wrong"
        elapsed = time.time() - self._last_call
        if elapsed < self._INTER_CALL_COOLDOWN:
            wait = self._INTER_CALL_COOLDOWN - elapsed
            logger.info(f"[{pair}] [BrowserAI] Cooldown: waiting {wait:.1f}s before next call")
            time.sleep(wait)

        # Trim prompt if too long — preserve HEAD (instructions) + TAIL (JSON schema),
        # trim only the MIDDLE (raw market data numbers). This ensures Gemini always
        # sees the format requirements and can return valid JSON.
        MAX_PROMPT_CHARS = 7000
        if len(message) > MAX_PROMPT_CHARS:
            keep_head = 3000   # instructions + context
            keep_tail = 2000   # JSON schema + output format
            trimmed = len(message) - keep_head - keep_tail
            logger.debug(
                f"[{pair}] [BrowserAI] Trimming prompt {len(message)} → "
                f"{keep_head + keep_tail} chars (removed {trimmed} chars of market data)"
            )
            message = (
                message[:keep_head]
                + f"\n\n[... {trimmed} chars of market data omitted for length ...]\n\n"
                + message[-keep_tail:]
            )

        with self._lock:   # one request at a time
            future = asyncio.run_coroutine_threadsafe(
                self._ask_async(message, pair), self._loop
            )
            try:
                result = future.result(timeout=300)   # 5 min: 60s nav + 10s hydrate + 60s element + 100s poll + buffer
                self._last_call = time.time()
                return result
            except Exception as e:
                self._last_call = time.time()   # still count failed calls against cooldown
                logger.error(f"[{pair}] [BrowserAI] Request timed out or failed: {e}")
                return {"direction": "HOLD", "confidence": 0,
                        "reasoning": str(e), "_reasoning_id": None,
                        "error": "BROWSER_AI_TIMEOUT"}

    async def _ask_async(self, message: str, pair: str, attempt: int = 0) -> dict:
        """Internal async implementation."""
        try:
            if self._provider == "chatgpt":
                raw = await _ask_chatgpt(self._page, message, attempt)
            else:
                raw = await _ask_gemini(self._page, message, attempt)

            logger.debug(f"[{pair}] [BrowserAI] Raw response ({len(raw)} chars): {raw[:200]}...")

            # Extract JSON from response
            reasoning = _extract_json(raw)

            # Validate required fields
            if "direction" not in reasoning or "confidence" not in reasoning:
                raise ValueError(f"Missing required fields in response: {list(reasoning.keys())}")

            reasoning["_reasoning_id"] = None   # no DB logging for browser AI
            reasoning["_source"] = f"browser_{self._provider}"

            logger.info(
                f"[{pair}] [BrowserAI/{self._provider.upper()}] "
                f"Decision: {reasoning.get('direction')} @ {reasoning.get('confidence')}% "
                f"| Regime: {reasoning.get('market_regime', '?')} "
                f"| R:R={reasoning.get('risk_reward_ratio', '?')}"
            )
            logger.info(f"[{pair}] Hypothesis: {reasoning.get('hypothesis', '?')}")
            return reasoning

        except (ValueError, json.JSONDecodeError) as e:
            if attempt < 2:
                logger.warning(
                    f"[{pair}] [BrowserAI] JSON parse failed (attempt {attempt + 1}): {e} — retrying"
                )
                return await self._ask_async(message, pair, attempt + 1)
            logger.error(f"[{pair}] [BrowserAI] JSON parse failed after 3 attempts: {e}")
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": f"JSON parse error: {e}", "_reasoning_id": None,
                    "error": "JSON_PARSE_ERROR"}

        except Exception as e:
            logger.error(f"[{pair}] [BrowserAI] Failed: {e}")
            # Reinitialise page on navigation/crash errors
            try:
                self._page = await self._context.new_page()
                await self._page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
            except Exception:
                pass
            return {"direction": "HOLD", "confidence": 0,
                    "reasoning": str(e), "_reasoning_id": None,
                    "error": "BROWSER_AI_ERROR"}

    @property
    def enabled(self) -> bool:
        return self._provider != "off"


# ── Singleton ─────────────────────────────────────────────────────────────────
# Imported and reused by brain.py — created once at module load.
_instance: BrowserAIAgent | None = None


def get_agent() -> BrowserAIAgent:
    global _instance
    if _instance is None:
        _instance = BrowserAIAgent()
    return _instance


# ─── Login helper (run once to set up session) ───────────────────────────────

async def _login_flow(provider: str):
    """Open browser headed so you can log in. Session saved to browser_session/."""
    from playwright.async_api import async_playwright
    print(f"\n{'='*60}")
    print(f"  LOGIN HELPER — {provider.upper()}")
    print(f"{'='*60}")
    print(f"  Browser will open. Log in to {provider.upper()}, then come back here.")
    print(f"  Session saved to: {USER_DATA_DIR.resolve()}")
    print(f"  Press Ctrl+C here when done.\n")

    USER_DATA_DIR.mkdir(exist_ok=True)
    url = "https://chatgpt.com" if provider == "chatgpt" else "https://gemini.google.com/app"

    async with async_playwright() as p:
        # Must use channel="chrome" — Google blocks sign-in from bundled Chromium
        # Use base args only (no --disable-gpu) — headed login needs GPU rendering
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=_CHROME_ARGS_BASE,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        # Use domcontentloaded — faster than waiting for all resources (images/fonts)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        print(f"  Opened {url}")
        print("  Waiting — press Ctrl+C when you are logged in and see the chat UI...")
        try:
            await asyncio.sleep(300)   # 5 minutes
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await context.close()
            except Exception:
                pass  # connection already torn down by Ctrl+C — session is already saved
    print("\n✅ Session saved. You can now run the agent.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Browser AI Agent helper")
    parser.add_argument(
        "--login",
        choices=["chatgpt", "gemini"],
        help="Open browser headed to log in and save session",
    )
    parser.add_argument("--provider", "-p", choices=["chatgpt", "gemini"], default="chatgpt")
    parser.add_argument("--message",  "-m", type=str, help="Send a test message")
    parser.add_argument("--headed",   action="store_true", help="Show the browser window (debug mode)")
    args = parser.parse_args()

    if args.login:
        asyncio.run(_login_flow(args.login))
    elif args.message:
        agent = BrowserAIAgent(headed=args.headed)
        agent.start()   # blocks until browser is ready (up to 30s)
        result = agent.ask(args.message, "TEST")
        print("\n" + "─" * 50)
        print("RESULT:")
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
