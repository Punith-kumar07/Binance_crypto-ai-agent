"""
main.py — The Orchestrator.

This is what you run. It ties all 10 steps together in one loop:

  Step 1-2:  DataCollector.collect_all()    — scan + gather
  Step 3-6:  TradingBrain.analyze()         — context + correlate + hypothesis + confidence
  Step 7-8:  RiskManager.evaluate()         — risk check + decision gate
  Step 9:    TradeExecutor.execute()         — action
  Step 10:   FeedbackLoop.check_and_update() — learning

Usage:
  python main.py              # runs live agent
  python main.py --once       # single cycle (for testing)
  python main.py --pair BTCUSDT --once  # test single pair
"""
import schedule
import time
import argparse
import sys
from loguru import logger
from datetime import datetime, timezone

import config
from data.collector import DataCollector
from agents.brain import TradingBrain
from agents.feedback import FeedbackLoop
from risk.manager import RiskManager
from execution.executor import TradeExecutor
from db import client as db


# ── Logging setup ──────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level=config.LOG_LEVEL, colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add("logs/agent_{time:YYYY-MM-DD}.log", rotation="1 day", level="DEBUG",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")


# ── Components (singleton per run) ─────────────────────────────────────────
collector = DataCollector()
brain     = TradingBrain()
risk      = RiskManager()
executor  = TradeExecutor()
feedback  = FeedbackLoop()


def run_cycle(pairs: list = None):
    """
    One full research + decision cycle for all configured pairs.
    This runs every CYCLE_INTERVAL seconds.
    """
    pairs = pairs or config.TRADING_PAIRS
    cycle_start = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"🔄 CYCLE START @ {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    logger.info(f"   Pairs: {pairs} | DRY_RUN: {config.DRY_RUN}")
    logger.info("=" * 60)

    # Step 10 first: check if any existing positions hit TP/SL
    logger.info("🔁 [Feedback] Checking open positions...")
    try:
        feedback.check_and_update_open_trades()
    except Exception as e:
        logger.error(f"Feedback loop error: {e}")

    # Main research cycle per pair
    for pair in pairs:
        try:
            run_pair_cycle(pair)
        except Exception as e:
            logger.error(f"[{pair}] Cycle failed: {e}", exc_info=True)

    elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
    logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")


def run_pair_cycle(pair: str):
    """Full research + trade cycle for one pair."""

    # ── Pre-check 1: Skip AI if we already have an open position ───────────
    open_trades = db.get_open_trades(pair)
    if open_trades:
        logger.info(f"\n[{pair}] ⏭ Skipping AI — {len(open_trades)} open position(s). Waiting for TP/SL exit.")
        return

    # ── Pre-check 2: Skip AI if balance is too low for the minimum order ──────
    # Note: the risk manager clamps order size up to MIN_ORDER_USDT ($5.50) when
    # balance * MAX_POSITION_PCT < $5.50 but balance >= $5.50. This allows a
    # second order of $5.50 to be placed from the ~$6.50 remaining after the first.
    balance = collector.get_usdt_balance()
    if balance < 5.5:  # BINANCE_MIN_ORDER_USDT is 5.5
        logger.info(f"\n[{pair}] ⏭ Skipping AI — Balance ${balance:.2f} below $5.50 minimum order size.")
        return

    # ── Step 1-2: Scan environment + gather signals ────────────────────────
    logger.info(f"\n[{pair}] ── Step 1-2: Scanning market...")
    snapshot = collector.collect_all(pair)

    if not snapshot.get("current_price"):
        logger.error(f"[{pair}] No price data — skipping cycle.")
        return

    # Save raw signals to Supabase
    db.log_signal_snapshot(pair, snapshot.get("indicators_1h", {}), {
        "price": snapshot["current_price"],
        "fear_greed": snapshot.get("fear_greed"),
        "news_count": len(snapshot.get("news", [])),
        "ob_imbalance": snapshot.get("order_book_imbalance"),
    })

    # ── Step 3-6: AI Research Brain ───────────────────────────────────────
    logger.info(f"[{pair}] ── Step 3-6: Running AI research analysis...")
    reasoning = brain.analyze(snapshot)

    direction  = reasoning.get("direction", "HOLD")
    confidence = float(reasoning.get("confidence", 0))

    logger.info(f"[{pair}] ── Step 3-6 Result: {direction} @ {confidence:.0f}% | {reasoning.get('signal_alignment')} signals")
    logger.info(f"[{pair}] 💡 Hypothesis: {reasoning.get('hypothesis', 'N/A')}")

    # ── Step 7-8: Risk check + Decision gate ──────────────────────────────
    logger.info(f"[{pair}] ── Step 7-8: Risk evaluation...")
    order = risk.evaluate(direction, confidence, snapshot, reasoning)

    if order is None:
        logger.info(f"[{pair}] 🚫 No trade this cycle.")
        return

    # ── Step 9: Execute ────────────────────────────────────────────────────
    logger.info(f"[{pair}] ── Step 9: Executing trade...")
    trade_result = executor.execute(order)

    logger.info(
        f"[{pair}] 🎯 Trade logged: {order.side} ${order.usdt_amount:.4f} USDT "
        f"| OrderID={trade_result.get('binance_order_id')}"
    )


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crypto AI Trading Agent")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--pair", type=str, help="Override pair (e.g. BTCUSDT)")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else config.TRADING_PAIRS

    # Validate config
    try:
        config.validate()
    except EnvironmentError as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)

    import os
    os.makedirs("logs", exist_ok=True)

    logger.info("🤖 Crypto AI Trading Agent starting...")
    logger.info(f"   Model: {config.GROQ_MODEL}")
    logger.info(f"   Pairs: {pairs}")
    logger.info(f"   Interval: {config.CYCLE_INTERVAL}s")
    logger.info(f"   Dry Run: {config.DRY_RUN}")
    logger.info(f"   Min Confidence: {config.MIN_CONFIDENCE}%")
    logger.info(f"   Max Position: {config.MAX_POSITION_PCT*100:.0f}% of balance")
    logger.info(f"   Trade Mode: {config.TRADE_MODE.upper()} ({config.FUTURES_LEVERAGE}x leverage)")

    if config.DRY_RUN:
        logger.warning("🧪 DRY RUN MODE — no real orders will be placed")

    if args.once:
        run_cycle(pairs)
        return

    # Scheduled loop
    run_cycle(pairs)  # immediate first run
    schedule.every(config.CYCLE_INTERVAL).seconds.do(run_cycle, pairs=pairs)

    logger.info(f"⏰ Scheduled to run every {config.CYCLE_INTERVAL}s. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("👋 Agent stopped by user.")


if __name__ == "__main__":
    main()
