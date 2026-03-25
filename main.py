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
    One full scan-rank-select-execute cycle.

    Flow:
      1. Feedback loop (check existing TP/SL hits)
      2. Balance → compute available order slots
      3. Scan all candidate pairs with AI
      4. Rank by confidence, filter by MIN_CONFIDENCE
      5. Execute top N (N = remaining slots)
    """
    pairs = pairs or config.TRADING_PAIRS
    cycle_start = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"🔄 CYCLE START @ {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    logger.info(f"   Scan pool: {pairs} | DRY_RUN: {config.DRY_RUN}")
    logger.info("=" * 60)

    # ── Step 10: feedback loop ─────────────────────────────────────────────
    logger.info("🔁 [Feedback] Checking open positions...")
    try:
        feedback.check_and_update_open_trades()
    except Exception as e:
        logger.error(f"Feedback loop error: {e}")

    # ── Balance + slot calculation ─────────────────────────────────────────
    balance = collector.get_usdt_balance()
    max_slots = int(balance / config.MIN_ORDER_USDT)

    open_count = sum(len(db.get_open_trades(p)) for p in pairs)
    remaining_slots = max(0, max_slots - open_count)

    logger.info(
        f"💰 Balance: ${balance:.4f} | Slots: {max_slots} total / "
        f"{open_count} open / {remaining_slots} available"
    )

    if balance < config.MIN_ORDER_USDT:
        logger.info(f"⏭ Balance ${balance:.2f} below ${config.MIN_ORDER_USDT:.2f} minimum. Skipping scan.")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    if remaining_slots <= 0:
        logger.info(f"⏭ All {max_slots} slot(s) occupied. Waiting for TP/SL exits.")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    # ── Candidate pairs: skip any with an existing open position ──────────
    candidates = [p for p in pairs if not db.get_open_trades(p)]
    if not candidates:
        logger.info("⏭ No candidate pairs — all have open positions.")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    logger.info(f"🔍 Scanning {len(candidates)} pair(s): {candidates}")

    # ── Steps 1-6: Collect + AI analyse all candidates ─────────────────────
    results = []
    for pair in candidates:
        try:
            logger.info(f"\n[{pair}] ── Step 1-2: Scanning market...")
            snapshot = collector.collect_all(pair)

            if not snapshot.get("current_price"):
                logger.error(f"[{pair}] No price data — skipping.")
                continue

            db.log_signal_snapshot(pair, snapshot.get("indicators_1h", {}), {
                "price":        snapshot["current_price"],
                "fear_greed":   snapshot.get("fear_greed"),
                "news_count":   len(snapshot.get("news", [])),
                "ob_imbalance": snapshot.get("order_book_imbalance"),
            })

            logger.info(f"[{pair}] ── Step 3-6: Running AI analysis...")
            reasoning  = brain.analyze(snapshot)
            direction  = reasoning.get("direction", "HOLD")
            confidence = float(reasoning.get("confidence", 0))
            horizon    = reasoning.get("trade_horizon_minutes", "?")

            results.append({
                "pair":      pair,
                "snapshot":  snapshot,
                "reasoning": reasoning,
                "direction": direction,
                "confidence": confidence,
                "horizon":   horizon,
            })
        except Exception as e:
            logger.error(f"[{pair}] Scan failed: {e}", exc_info=True)

    if not results:
        logger.info("🚫 No AI results. Skipping execution.")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    # ── Rank: filter actionable, sort by confidence DESC ──────────────────
    actionable = [
        r for r in results
        if r["direction"] in ("BUY", "SELL") and r["confidence"] >= config.MIN_CONFIDENCE
    ]
    actionable.sort(key=lambda x: x["confidence"], reverse=True)
    selected = actionable[:remaining_slots]

    # ── Print ranking table ────────────────────────────────────────────────
    logger.info(f"\n{'─'*60}")
    logger.info(
        f"📊 PAIR RANKING  ({len(results)} scanned | "
        f"{len(actionable)} actionable | {len(selected)} selected)"
    )
    logger.info(f"{'─'*60}")
    for r in results:
        if r in selected:
            tag = "✅ SELECTED"
        elif r["direction"] == "HOLD":
            tag = "⏩ HOLD"
        elif r["confidence"] < config.MIN_CONFIDENCE:
            tag = f"⏩ LOW CONF (<{config.MIN_CONFIDENCE:.0f}%)"
        else:
            tag = "⏩ SLOTS FULL"
        logger.info(
            f"  {r['pair']:12} {r['direction']:4}  {r['confidence']:3.0f}%"
            f"  ~{r['horizon']}min  {tag}"
        )
    logger.info(f"{'─'*60}\n")

    if not selected:
        logger.info("🚫 No pairs met confidence threshold. No trades this cycle.")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    # ── Steps 7-9: Risk check + Execute for each selected pair ────────────
    for r in selected:
        pair      = r["pair"]
        snapshot  = r["snapshot"]
        reasoning = r["reasoning"]
        direction = r["direction"]
        confidence = r["confidence"]

        try:
            logger.info(f"\n[{pair}] ── Step 7-8: Risk evaluation...")
            order = risk.evaluate(direction, confidence, snapshot, reasoning)

            if order is None:
                logger.info(f"[{pair}] 🚫 Risk gate rejected.")
                continue

            logger.info(f"[{pair}] ── Step 9: Executing trade...")
            trade_result = executor.execute(order)

            logger.info(
                f"[{pair}] 🎯 Trade logged: {order.side} ${order.usdt_amount:.4f} USDT "
                f"| OrderID={trade_result.get('binance_order_id')}"
            )
        except Exception as e:
            logger.error(f"[{pair}] Execution failed: {e}", exc_info=True)


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

    # Reconcile any stale open DB trades against live Binance positions
    # (handles case where agent was stopped while positions were open and Binance closed them)
    try:
        feedback.reconcile_stale_trades()
    except Exception as e:
        logger.warning(f"Reconcile on startup failed (non-fatal): {e}")

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
