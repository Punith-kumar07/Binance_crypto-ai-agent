"""
utils/check_groq_keys.py — Check remaining tokens/requests for every Groq API key.

Groq returns rate-limit headers on each response:
  x-ratelimit-limit-tokens       — daily token cap
  x-ratelimit-remaining-tokens   — tokens left today
  x-ratelimit-limit-requests     — requests per minute cap
  x-ratelimit-remaining-requests — requests left in current window
  x-ratelimit-reset-tokens       — when token limit resets

Usage:
    python utils/check_groq_keys.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import config

MODEL = config.GROQ_MODEL
KEYS  = config.GROQ_API_KEYS

SEP = "=" * 68

def check_key(index: int, key: str) -> dict:
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization":  f"Bearer {key}",
                "Content-Type":   "application/json",
            },
            json={
                "model":      MODEL,
                "messages":   [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=15,
        )

        h = resp.headers

        if resp.status_code == 401:
            return {"status": "❌ INVALID KEY", "key_tail": key[-6:]}
        if resp.status_code == 429:
            reset = h.get("retry-after") or h.get("x-ratelimit-reset-tokens", "?")
            return {
                "status":   "🔴 RATE LIMITED",
                "key_tail": key[-6:],
                "reset_in": reset,
            }
        if resp.status_code != 200:
            return {"status": f"⚠️  HTTP {resp.status_code}", "key_tail": key[-6:]}

        def fmt(val):
            if val is None:
                return "?"
            try:
                n = int(val)
                return f"{n:,}"
            except ValueError:
                return val

        return {
            "status":             "✅ OK",
            "key_tail":           key[-6:],
            "tokens_remaining":   fmt(h.get("x-ratelimit-remaining-tokens")),
            "tokens_limit":       fmt(h.get("x-ratelimit-limit-tokens")),
            "requests_remaining": fmt(h.get("x-ratelimit-remaining-requests")),
            "requests_limit":     fmt(h.get("x-ratelimit-limit-requests")),
            "tokens_reset":       h.get("x-ratelimit-reset-tokens", "?"),
            "requests_reset":     h.get("x-ratelimit-reset-requests", "?"),
        }

    except requests.exceptions.Timeout:
        return {"status": "⏱ TIMEOUT", "key_tail": key[-6:]}
    except Exception as e:
        return {"status": f"⚠️  ERROR: {e}", "key_tail": key[-6:]}


print(f"\n{SEP}")
print(f"  Groq API Key Status Check  —  Model: {MODEL}")
print(f"  {len(KEYS)} key(s) found")
print(SEP)

for i, key in enumerate(KEYS, 1):
    print(f"\n  Key #{i}  (...{key[-6:]})")
    info = check_key(i, key)

    status = info.get("status", "?")
    print(f"    Status            : {status}")

    if "tokens_remaining" in info:
        pct = ""
        try:
            rem = int(info["tokens_remaining"].replace(",", ""))
            lim = int(info["tokens_limit"].replace(",", ""))
            pct = f"  ({rem/lim*100:.0f}% remaining)" if lim else ""
        except Exception:
            pass
        print(f"    Tokens remaining  : {info['tokens_remaining']} / {info['tokens_limit']}{pct}")
        print(f"    Requests remaining: {info['requests_remaining']} / {info['requests_limit']}")
        print(f"    Token reset in    : {info['tokens_reset']}")
        print(f"    Request reset in  : {info['requests_reset']}")
    elif "reset_in" in info:
        print(f"    Retry after       : {info['reset_in']}s")

print(f"\n{SEP}\n")
