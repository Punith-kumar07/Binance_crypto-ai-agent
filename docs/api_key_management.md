# API Key Management

How the agent manages AI provider keys to ensure uninterrupted analysis across a 4-tier fallback chain.

---

## Overview

The agent uses **4 AI providers in priority order**. It always tries the highest available tier and automatically falls back when keys are exhausted. The moment a higher-priority provider recovers, the agent switches back on the next cycle — no restart needed.

---

## Provider Priority

```
┌─────────────────────────────────────────────────────────────┐
│                     AI PROVIDER PRIORITY                    │
│                                                             │
│   Tier 1  ██████████████████  GROQ                          │
│                                llama-3.3-70b-versatile      │
│                                9 keys, ~100k TPD each       │
│                                                             │
│   Tier 2  ████████████░░░░░░  OPENROUTER (free models)      │
│                                llama-3.3-70b + 4 fallbacks  │
│                                40 calls/hour per key        │
│                                                             │
│   Tier 3  ██████░░░░░░░░░░░░  BROWSER AI                    │
│                                Gemini web UI (headless)     │
│                                Completely free, unlimited   │
│                                                             │
│   Tier 4  ░░░░░░░░░░░░░░░░░░  GEMINI API (paid, last resort)│
│                                Disabled by default          │
└─────────────────────────────────────────────────────────────┘

Each tier is only used when all tiers above it are exhausted.
The agent always attempts to step back up to a higher tier each cycle.
```

---

## Full Decision Flow

```
                       ┌─────────────────┐
                       │   analyze()     │
                       │   called for    │
                       │   a pair        │
                       └────────┬────────┘
                                │
                   ┌────────────▼────────────┐
                   │  On a fallback tier?    │
                   └────────────┬────────────┘
                         │               │
                        YES              NO
                         │               │
          ┌──────────────▼──┐            │
          │ Try to step up  │            │
          │ to higher tier  │            │
          └──────┬──────────┘            │
                 │                       │
    ┌────────────▼────────────┐          │
    │  Stepped up? (Groq      │          │
    │  or OpenRouter back?)   │          │
    └────────────┬────────────┘          │
          │              │               │
         YES             NO              │
          │              │               │
    Use recovered    Use current         │
    higher tier      fallback tier       │
          │              │               │
          └──────┬────────┘              │
                 │                       │
                 └───────────────────────┘
                             │
                 ┌───────────▼───────────┐
                 │   GROQ path           │
                 │   Call Groq API       │
                 └───────────┬───────────┘
                          │       │
                        OK       429
                          │       │
                          │  Rotate key → retry
                          │       │
                          │  All keys dead?
                          │       │
                          │      YES
                          │       │
                          │  ┌────▼──────────────────────────┐
                          │  │  Tier 2: OpenRouter (free)    │
                          │  │  Try primary model            │
                          │  │  Auto-fallback through 5      │
                          │  │  free models on upstream 429  │
                          │  └────┬──────────────────────────┘
                          │    │       │
                          │   OK   All models exhausted
                          │    │       │
                          │    │  ┌────▼──────────────────────┐
                          │    │  │  Tier 3: Browser AI       │
                          │    │  │  Headless Chrome → Gemini │
                          │    │  │  web UI                   │
                          │    │  └────┬──────────────────────┘
                          │    │    │       │
                          │    │   OK   Not ready / failed
                          │    │    │       │
                          │    │    │  ┌────▼──────────────────┐
                          │    │    │  │  Tier 4: Gemini API   │
                          │    │    │  │  (paid, if keys set)  │
                          │    │    │  └────┬──────────────────┘
                          │    │    │    │       │
                          │    │    │   OK   All dead
                          │    │    │    │       │
                          │    │    │    │  Return HOLD
                          │    │    │    │  (log reset time)
                          └────┴────┴────┘
                                   │
                          ┌────────▼────────┐
                          │  Return result  │
                          │  to run_cycle   │
                          └─────────────────┘
```

---

## Tier 1 — Groq Key Rotation

The agent holds **9 Groq keys** and rotates through them as each hits its rate limit.

```
   Key Pool (9 keys)
   ┌──────────────────────────────────────────────────────┐
   │                                                      │
   │  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
   │  │  Key 1   │  │  Key 2   │  │  Key 3   │  ...      │
   │  │ ...4xTr  │  │ ...2Q2r  │  │ ...vNJY  │           │
   │  └────┬─────┘  └────┬─────┘  └────┬─────┘           │
   │       │             │             │                  │
   └───────┼─────────────┼─────────────┼──────────────────┘
           │             │             │
           ▼             ▼             ▼
        Active      On standby    On standby
        (current)
           │
           │  Hit 429?
           ▼
      Mark exhausted
      with reset time
      parsed from error
           │
           ▼
      Try next available key
           │
       ┌───┴──────────────┐
       │                  │
    Found one         All exhausted
       │                  │
    Switch +          Switch to
    continue          OpenRouter (Tier 2)
```

**Rate limit types handled:**

| Limit type | Reset time | How detected |
|---|---|---|
| Per-minute (TPM) | ~60s | `"try again in Xs"` parsed from error |
| Per-day (TPD) | ~86400s | `"day"` / `"daily"` in error message |
| Unknown | 65s (safe default) | Fallback heuristic |

Reset times are **persisted to `logs/groq_key_status.json`** — so if the agent restarts, it won't retry keys that are still cooling down.

---

## Tier 2 — OpenRouter Free Models

OpenRouter provides access to multiple free LLMs under a single API key. When the primary model is upstream rate-limited, the agent auto-rotates through a fallback list.

```
   Model fallback order (on upstream 429):

   1. meta-llama/llama-3.3-70b-instruct:free   ← primary (config.OPENROUTER_MODEL)
   2. nvidia/nemotron-3-super-120b-a12b:free
   3. google/gemma-3-27b-it:free
   4. mistralai/mistral-small-3.1-24b-instruct:free
   5. openrouter/free                            ← auto-routes to any available model

   All models exhausted → raise OpenRouterExhaustedError → fall through to Tier 3
```

**Hourly limit guard:**
- Sliding window counter (`OPENROUTER_HOURLY_LIMIT=40` calls/hour default)
- Returns HOLD if limit reached, logs reset time
- Key exhaustion state persisted to `logs/openrouter_key_status.json`

---

## Tier 3 — Browser AI (Headless Chrome)

When all API keys are exhausted, the agent uses a **headless Chrome browser** to send prompts to Gemini's web UI — completely free and unlimited.

```
   Agent startup
        │
        ├── BROWSER_AI_PROVIDER=gemini?
        │        │
        │       YES ──► Launch Chrome in background thread
        │               Load session from browser_session/
        │               Keep browser alive for entire run
        │
        └── BROWSER_AI_PROVIDER=off ──► Skip (disabled)


   Per-call flow:
        │
        ├── Navigate to gemini.google.com/app
        ├── Wait for input to appear (JS poll, handles Shadow DOM)
        ├── Paste prompt via execCommand (instant, no char-by-char typing)
        ├── Press Enter → wait for URL change (/app/<id>)
        ├── Wait for model-response element to appear
        ├── Poll text every 2s until stable for 4s (streaming done)
        └── Extract JSON from response
```

**One-time login setup** (saves session to `browser_session/`):
```bash
python agents/browser_ai.py --login gemini
```

**Headed mode** (shows browser window for debugging):
```bash
# Via .env:
BROWSER_AI_HEADED=true

# Or one-off CLI flag:
python agents/browser_ai.py --headed -p gemini -m "test"
```

---

## Tier 4 — Gemini API (Paid, Last Resort)

Kept as a final safety net. Disabled by default — leave `GEMINI_API_KEYS=` blank to skip it entirely (Browser AI covers Gemini for free anyway).

```
   Sliding window — last 60 minutes
   ────────────────────────────────────────────────────────▶ time
        │                              │             now
        │◄──────── 60 minutes ────────►│
        │                              │
        │  call  call  call  call  call│
        │   1     2     3     4     5  │
        │   ●     ●     ●     ●     ●  │
        │                              │
        │           window             │  next call?
        └──────────────────────────────┘
                                          used=5/5 → HOLD
                                          resets when call 1
                                          falls out of window
```

Multiple keys supported: `GEMINI_API_KEYS=key1,key2,key3` — the hourly limit is multiplied by the number of keys.

---

## Startup Behaviour

```
  Agent starts
       │
       ├── Groq keys available? ──YES──► Start on Groq (Tier 1)
       │
       ├── All Groq exhausted?
       │       └── OpenRouter keys set? ──YES──► Start on OpenRouter (Tier 2)
       │
       ├── OpenRouter also exhausted?
       │       └── BROWSER_AI_PROVIDER != off? ──YES──► Start on Browser AI (Tier 3)
       │
       ├── Browser AI unavailable?
       │       └── GEMINI_API_KEYS set? ──YES──► Start on Gemini API (Tier 4)
       │
       └── All providers dead ──► Start anyway, log warning
                                   Cycles skip until a provider resets
```

---

## Auto Step-Up to Higher Tier

Every `analyze()` call checks if a higher-priority provider has recovered:

```
  On fallback tier, every call:

  analyze() called
       │
       ▼
  Groq keys available again?
       │
      YES ──► Switch back to Groq immediately ✅
       │      Log: "✅ Groq keys available again — switched back to Groq"
       │
      NO ──► OpenRouter available?
                   │
                  YES ──► Step up to OpenRouter ✅
                   │
                  NO ──► Stay on current tier
```

No restart needed. The agent heals itself automatically.

---

## Daily Capacity

```
  TIER 1 — GROQ (primary)
  ┌─────────────────────────────────────────────────────┐
  │  9 keys  ×  ~100,000 TPD  =  ~900,000 tokens/day    │
  │                                                      │
  │  ~2,500 tokens per AI call                           │
  │  → ~360 AI analyses per day on Groq alone            │
  └─────────────────────────────────────────────────────┘

  TIER 2 — OPENROUTER (free)
  ┌─────────────────────────────────────────────────────┐
  │  40 calls/hr  ×  24 hr  =  960 calls/day MAX         │
  │  (per key — add more keys to multiply)               │
  │  5 free models to rotate through on rate-limits      │
  └─────────────────────────────────────────────────────┘

  TIER 3 — BROWSER AI (free)
  ┌─────────────────────────────────────────────────────┐
  │  Unlimited (Gemini free tier via web UI)             │
  │  ~60-90s per call (browser round-trip)               │
  │  Best for low-frequency fallback use                 │
  └─────────────────────────────────────────────────────┘

  TIER 4 — GEMINI API (paid)
  ┌─────────────────────────────────────────────────────┐
  │  Configurable: GEMINI_HOURLY_LIMIT × keys × 24      │
  │  Only consumed when Tiers 1-3 all unavailable        │
  │  Disabled by default (GEMINI_API_KEYS= empty)        │
  └─────────────────────────────────────────────────────┘
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEYS` | — | Comma-separated Groq keys |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model |
| `OPENROUTER_API_KEYS` | — | Comma-separated OpenRouter keys |
| `OPENROUTER_MODEL` | `meta-llama/llama-3.3-70b-instruct:free` | Primary OpenRouter model |
| `OPENROUTER_HOURLY_LIMIT` | `40` | Max OpenRouter calls per hour (sliding window) |
| `BROWSER_AI_PROVIDER` | `off` | `gemini`, `chatgpt`, or `off` |
| `BROWSER_AI_HEADED` | `false` | `true` to show browser window (debug) |
| `GEMINI_API_KEYS` | — | Comma-separated Gemini API keys (optional) |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model |
| `GEMINI_HOURLY_LIMIT` | `20` | Max Gemini API calls per hour per key |

---

## Key State Files

Exhaustion state is persisted so the agent never retries cooling-down keys across restarts:

| File | Provider |
|---|---|
| `logs/groq_key_status.json` | Groq |
| `logs/openrouter_key_status.json` | OpenRouter |
| `logs/gemini_key_status.json` | Gemini API |

```json
{
  "last8charsOfKey": {
    "reset_at": "2026-03-26T18:52:06+00:00"
  }
}
```
