"""
db/client.py — Supabase client wrapper.

Fixes:
  - Singleton client (one connection, not one per call)
  - log_agent_reasoning returns the inserted row ID
  - log_trade accepts reasoning_id to link AI decision → trade
"""
from supabase import create_client, Client
import config
from loguru import logger
from datetime import datetime, timezone

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


def _reset_client():
    """Force a fresh connection on next get_client() call."""
    global _client
    _client = None


def _db_error(msg: str, e: Exception):
    """Log DB error and reset client if the connection was dropped."""
    logger.error(f"{msg}: {e}")
    if "disconnect" in str(e).lower() or "server disconnected" in str(e).lower():
        _reset_client()


# ── Write helpers ──────────────────────────────────────────────────────────

def log_signal_snapshot(pair: str, signals: dict, raw_data: dict):
    """Save every data collection cycle's raw signals for audit trail."""
    try:
        get_client().table("signal_snapshots").insert({
            "pair": pair,
            "signals": signals,
            "raw_data": raw_data,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        _db_error("DB signal_snapshot write failed", e)


def log_agent_reasoning(pair: str, context_sent: str, reasoning: dict) -> str | None:
    """
    Save the Groq AI's full chain-of-thought.
    Returns the inserted record's ID so it can be linked to the trade.
    """
    try:
        res = get_client().table("agent_reasoning").insert({
            "pair": pair,
            "context_sent": context_sent,
            "direction": reasoning.get("direction"),
            "confidence": reasoning.get("confidence"),
            "hypothesis": reasoning.get("hypothesis"),
            "signal_alignment": reasoning.get("signal_alignment"),
            "risk_level": reasoning.get("risk_level"),
            "market_regime": reasoning.get("market_regime"),
            "reasoning_text": reasoning.get("reasoning"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        rows = res.data
        if rows:
            return rows[0].get("id")
    except Exception as e:
        _db_error("DB agent_reasoning write failed", e)
    return None


def log_trade(trade: dict):
    """Record every executed (or dry-run) trade."""
    try:
        get_client().table("trade_history").insert({
            **trade,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        _db_error("DB trade_history write failed", e)


def update_trade_outcome(trade_id: str, outcome: dict):
    """Called after a trade closes — records actual PnL vs predicted."""
    try:
        get_client().table("trade_history").update({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "actual_exit_price": outcome.get("exit_price"),
            "pnl_pct": outcome.get("pnl_pct"),
            "outcome": outcome.get("result"),
            "prediction_correct": outcome.get("prediction_correct"),
        }).eq("id", trade_id).execute()
    except Exception as e:
        _db_error("DB trade outcome update failed", e)


def update_reasoning_accuracy(reasoning_id: str, was_correct: bool):
    """Link trade outcome back to the reasoning record that triggered it."""
    try:
        get_client().table("agent_reasoning") \
            .update({"prediction_correct": was_correct}) \
            .eq("id", reasoning_id) \
            .execute()
    except Exception as e:
        _db_error("DB reasoning accuracy update failed", e)


# ── Read helpers ───────────────────────────────────────────────────────────

def get_recent_reasoning(pair: str, limit: int = 5) -> list:
    """Fetch last N reasoning cycles to feed back into next Groq prompt."""
    try:
        res = get_client().table("agent_reasoning") \
            .select("direction,confidence,hypothesis,prediction_correct,created_at") \
            .eq("pair", pair) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        return res.data or []
    except Exception as e:
        _db_error("DB get_recent_reasoning failed", e)
        return []


def get_open_trades(pair: str) -> list:
    """Returns currently open positions for a pair (not yet closed)."""
    try:
        res = get_client().table("trade_history") \
            .select("*") \
            .eq("pair", pair) \
            .is_("closed_at", "null") \
            .execute()
        return res.data or []
    except Exception as e:
        _db_error("DB get_open_trades failed", e)
        return []


def get_all_open_trades() -> list:
    """
    Returns ALL currently open trades across every pair in one single query.
    Use this in the main cycle instead of calling get_open_trades() per pair.
    """
    try:
        res = get_client().table("trade_history") \
            .select("*") \
            .is_("closed_at", "null") \
            .execute()
        return res.data or []
    except Exception as e:
        _db_error("DB get_all_open_trades failed", e)
        return []


def get_daily_pnl_pct(pair: str) -> float:
    """Sum of pnl_pct for all trades closed today. Used for daily loss limit."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        res = get_client().table("trade_history") \
            .select("pnl_pct") \
            .eq("pair", pair) \
            .gte("closed_at", today) \
            .not_.is_("pnl_pct", "null") \
            .execute()
        return sum(r["pnl_pct"] for r in (res.data or []))
    except Exception as e:
        _db_error("DB get_daily_pnl failed", e)
        return 0.0
