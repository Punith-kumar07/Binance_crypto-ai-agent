"""
agents/pair_selector.py — Smart Pair Selection Engine.

Strategy per cycle:
  1. Draw SCAN_PAIRS_PER_CYCLE candidates from the full TRADING_PAIRS pool
  2. Skip pairs that have an open position (handled upstream)
  3. Skip pairs still on cooldown
  4. Priority: never-scanned first, then least-recently-scanned (FIFO rotation)

Cooldown durations by outcome:
  HOLD         → 20 min  (market not moving, wait for regime change)
  LOW_CONF     → 10 min  (AI found a direction but not confident enough)
  RISK_REJECT  →  5 min  (confidence OK but risk gate blocked it, re-check soon)
  SLOTS_FULL   →  2 min  (was actionable, just no slots — retry next cycle)
  ERROR        →  3 min  (API or AI failure)
  EXECUTED     →  0 min  (pair blocked by open-position check, not cooldown)

State is persisted to logs/pair_selector_state.json so cooldowns survive restarts.
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from loguru import logger
import config

_STATE_FILE = Path("logs/pair_selector_state.json")

# ── Cooldown durations ────────────────────────────────────────────────────
COOLDOWN = {
    "hold":         20,
    "low_conf":     10,
    "risk_reject":   5,
    "slots_full":    2,
    "error":         3,
    "executed":      0,
}


class PairSelector:
    """
    Persistent priority queue for which pairs to scan each cycle.
    Saves state to disk so cooldowns survive agent restarts.
    """

    def __init__(self):
        self._state: dict = self._load()
        self._clean_expired()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            if _STATE_FILE.exists():
                return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            _STATE_FILE.parent.mkdir(exist_ok=True)
            _STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.warning(f"[PairSelector] Could not save state: {e}")

    def _clean_expired(self):
        """Remove expired cooldowns on load to avoid stale state."""
        now = datetime.now(timezone.utc)
        changed = False
        for pair, s in self._state.items():
            cd = s.get("cooldown_until")
            if cd and self._parse(cd) <= now:
                s["cooldown_until"] = None
                changed = True
        if changed:
            self._save()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse(iso: str) -> datetime:
        return datetime.fromisoformat(iso)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _is_on_cooldown(self, pair: str) -> tuple[bool, float]:
        """Returns (on_cooldown, remaining_minutes)."""
        s = self._state.get(pair, {})
        cd = s.get("cooldown_until")
        if not cd:
            return False, 0.0
        cd_dt = self._parse(cd)
        now = self._now()
        if cd_dt > now:
            return True, (cd_dt - now).total_seconds() / 60
        return False, 0.0

    # ── Public API ────────────────────────────────────────────────────────

    def get_next_pairs(self, n: int, exclude_pairs: set = None) -> list:
        """
        Return the next `n` pairs to scan this cycle.

        Eligibility: not in exclude_pairs, not on active cooldown.
        Priority: never-scanned > longest time since last scan (FIFO rotation).

        If ALL pairs are on cooldown (edge case), returns the n pairs with the
        soonest cooldown expiry to avoid an empty cycle.
        """
        exclude = exclude_pairs or set()
        now = self._now()

        eligible = []
        on_cooldown_info = []

        for pair in config.TRADING_PAIRS:
            if pair in exclude:
                continue
            on_cd, remaining = self._is_on_cooldown(pair)
            if on_cd:
                on_cooldown_info.append((pair, remaining))
            else:
                eligible.append(pair)

        if not eligible:
            logger.warning(
                "[PairSelector] ⚠️ All pairs are on cooldown! "
                "Picking soonest-available to avoid empty cycle."
            )
            on_cooldown_info.sort(key=lambda x: x[1])
            return [p for p, _ in on_cooldown_info[:n]]

        # Sort: never-scanned (no last_scanned_at) → oldest last_scanned_at
        def priority(pair):
            lsa = self._state.get(pair, {}).get("last_scanned_at")
            if lsa is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return self._parse(lsa)

        eligible.sort(key=priority)
        selected = eligible[:n]

        # Log pool status
        total = len(config.TRADING_PAIRS) - len(exclude)
        blocked = len(on_cooldown_info)
        never = sum(1 for p in eligible if not self._state.get(p, {}).get("last_scanned_at"))
        logger.info(
            f"[PairSelector] Pool: {len(config.TRADING_PAIRS)} pairs | "
            f"{len(exclude)} with open positions | "
            f"{blocked} on cooldown | "
            f"{never} never scanned | "
            f"→ Scanning: {selected}"
        )
        return selected

    def record_outcome(
        self,
        pair: str,
        reason: str,
        confidence: float = 0.0,
    ):
        """
        Record the outcome of a pair scan and apply appropriate cooldown.

        reason: "executed" | "hold" | "low_conf" | "risk_reject" | "slots_full" | "error"
        """
        now = self._now()
        s = self._state.setdefault(pair, {})
        s["last_scanned_at"] = now.isoformat()
        s["last_reason"]     = reason
        s["last_confidence"] = confidence
        s["scan_count"]      = s.get("scan_count", 0) + 1

        cd_minutes = COOLDOWN.get(reason, COOLDOWN["low_conf"])

        if cd_minutes > 0:
            cd_until = now + timedelta(minutes=cd_minutes)
            s["cooldown_until"] = cd_until.isoformat()
            logger.debug(
                f"[PairSelector] {pair} → {reason.upper()} | "
                f"Cooldown {cd_minutes}min | Next ~{cd_until.strftime('%H:%M')} UTC"
            )
        else:
            s["cooldown_until"] = None

        self._save()

    def status_table(self) -> str:
        """Multi-line status table for logging."""
        now = self._now()
        lines = ["[PairSelector] Status:"]
        for pair in config.TRADING_PAIRS:
            s = self._state.get(pair, {})
            on_cd, rem = self._is_on_cooldown(pair)
            if on_cd:
                status = f"cooldown {rem:.0f}min"
            else:
                lsa = s.get("last_scanned_at")
                if not lsa:
                    status = "never scanned"
                else:
                    mins_ago = (now - self._parse(lsa)).total_seconds() / 60
                    status = f"last {mins_ago:.0f}min ago ({s.get('last_reason','?')} {s.get('last_confidence','?')}%)"
            lines.append(f"  {pair:12} {status}")
        return "\n".join(lines)
