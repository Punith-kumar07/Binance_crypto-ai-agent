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
import sys
import threading
from pathlib import Path

# Allow running as:  python agents/browser_ai.py  (from project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger
import config

USER_DATA_DIR = Path("browser_session")

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
    Opens a new chat each time to avoid context contamination.
    """
    logger.info(f"[BrowserAI/ChatGPT] Opening new conversation (attempt {attempt + 1})...")

    # Navigate to a fresh chat
    await page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2500)

    # Try clicking "New chat" button if we landed on an existing conversation
    try:
        new_chat = await _find_element(page, _CHATGPT_NEW_CHAT_SELECTORS, timeout_ms=3000)
        await new_chat.click()
        await page.wait_for_timeout(1500)
    except Exception:
        pass  # Already on a blank new chat

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
    for sel in _CHATGPT_RESPONSE_SELECTORS:
        try:
            msgs = await page.locator(sel).all()
            if msgs:
                return (await msgs[-1].inner_text()).strip()
        except Exception:
            continue

    raise RuntimeError("Could not extract ChatGPT response")


# ─── Gemini ──────────────────────────────────────────────────────────────────

async def _ask_gemini(page, message: str, attempt: int = 0) -> str:
    """Send a message on Gemini and return the full text response."""
    logger.info(f"[BrowserAI/Gemini] Opening new conversation (attempt {attempt + 1})...")

    # Navigate to a fresh conversation
    await page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(1500)

    # Poll for the input via JS — handles Angular shadow DOM and slow hydration.
    # wait_for_selector() doesn't pierce shadow roots; JS querySelector does.
    logger.debug(f"[BrowserAI/Gemini] Waiting for input + pasting prompt ({len(message)} chars)...")
    pasted = False
    for _ in range(20):   # max 20 × 2s = 40s
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
        await page.wait_for_timeout(2000)

    if not pasted:
        raise RuntimeError("Gemini input never appeared — is the session still logged in?")

    # Press Enter to send
    await page.keyboard.press("Enter")
    logger.info("[BrowserAI/Gemini] Prompt sent — waiting for URL change...")

    # After sending, Gemini navigates from /app → /app/<conversation_id>
    try:
        await page.wait_for_url(re.compile(r".*/app/.+"), timeout=15_000)
        logger.debug("[BrowserAI/Gemini] URL changed — conversation created")
    except Exception:
        pass  # some Gemini versions stay on /app — that's fine

    logger.info("[BrowserAI/Gemini] Waiting for response element...")

    # Try selectors in order — most reliable first for headless Chrome
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

    # Poll for text content to stabilize — more reliable than spinner in headless mode.
    # Streaming is done when the last element's text stops changing for 2 consecutive checks.
    logger.info("[BrowserAI/Gemini] Polling for stable response text...")
    prev_text = ""
    stable_count = 0
    raw = ""
    for _ in range(50):          # max 50 × 2s = 100s
        await page.wait_for_timeout(2000)
        try:
            count = await page.locator(found_sel).count()
            if count == 0:
                continue
            candidate = (await page.locator(found_sel).last.inner_text()).strip()
            if candidate and candidate == prev_text:
                stable_count += 1
                if stable_count >= 2:   # text unchanged for 4s = streaming done
                    raw = candidate
                    break
            else:
                stable_count = 0
            prev_text = candidate
        except Exception:
            continue
    else:
        # Loop exhausted without stability — return whatever we accumulated
        raw = prev_text
        if not raw:
            raise RuntimeError("Gemini response never produced stable text after 100s")

    # Strip "Gemini said\n\n" accessibility prefix added for screen readers
    if raw.lower().startswith("gemini said"):
        raw = raw.split("\n", 2)[-1].strip()
    return raw


# ─── BrowserAIAgent ──────────────────────────────────────────────────────────

class BrowserAIAgent:
    """
    Singleton browser AI agent. Keeps one Playwright context alive for the
    entire agent run to avoid per-call browser startup overhead (~3s).

    Thread-safe: uses an asyncio event loop running in a background thread.
    """

    def __init__(self, headed: bool | None = None):
        self._provider  = config.BROWSER_AI_PROVIDER.lower()   # "chatgpt" | "gemini" | "off"
        # headed: CLI flag overrides .env; .env overrides default (False)
        self._headed    = headed if headed is not None else config.BROWSER_AI_HEADED
        self._loop      = None
        self._context   = None
        self._page      = None
        self._playwright = None
        self._lock      = threading.Lock()
        self._ready     = False

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
        # channel="chrome" uses your real system Chrome — Google/OpenAI accept it.
        # Bundled Chromium gets "Couldn't sign you in / browser may not be secure".
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=not self._headed,
            args=[
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        self._ready = True
        logger.info(f"[BrowserAI] Browser ready ({self._provider.upper()} mode)")

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

        with self._lock:   # one request at a time
            future = asyncio.run_coroutine_threadsafe(
                self._ask_async(message, pair), self._loop
            )
            try:
                return future.result(timeout=300)   # 5 min: 60s nav + 10s hydrate + 60s element + 100s poll + buffer
            except Exception as e:
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
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=[
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
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
