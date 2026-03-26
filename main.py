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
from agents.brain import TradingBrain, AllKeysExhaustedError
from agents.feedback import FeedbackLoop
from agents.pair_selector import PairSelector
from risk.manager import RiskManager
from execution.executor import TradeExecutor
from db import client as db
from notifications import telegram as tg


# ── Logging setup ──────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level=config.LOG_LEVEL, colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add("logs/agent_{time:YYYY-MM-DD}.log", rotation="1 day", level="DEBUG",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")


# ── Components (singleton per run) ───────────────────────────────────────────
collector = DataCollector()
brain     = TradingBrain()
risk      = RiskManager()
executor  = TradeExecutor()
feedback  = FeedbackLoop()
selector  = PairSelector()


def run_cycle(pairs: list = None):
    """
    One full scan-rank-select-execute cycle.

    Flow:
      1.  Feedback loop       (check existing TP/SL hits)
      2.  Balance             → compute available order slots
      3.  PairSelector        → pick SCAN_PAIRS_PER_CYCLE candidates (cooldown-aware, FIFO rotation)
      4.  Collect + AI        (Steps 1-6)
      5.  Rank by confidence, filter by MIN_CONFIDENCE
      6.  Execute top N       (N = remaining slots)
      7.  Record outcomes     → apply cooldowns to all scanned pairs
    """
    # pairs is only non-None when --pair CLI flag is used (single-pair test, bypasses selector)
    override_pairs = pairs
    cycle_start = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"🔄 CYCLE START @ {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    logger.info(f"   Pool: {len(config.TRADING_PAIRS)} pairs | Scan/cycle: {config.SCAN_PAIRS_PER_CYCLE} | DRY_RUN: {config.DRY_RUN}")
    logger.info("=" * 60)

    # ── Step 10: feedback loop ───────────────────────────────────────────────
    logger.info("🔁 [Feedback] Checking open positions...")
    try:
        feedback.check_and_update_open_trades()
    except Exception as e:
        logger.error(f"Feedback loop error: {e}")

    # ── Balance + slot calculation ──────────────────────────────────────────────
    balance = collector.get_usdt_balance()

    # Single DB query for ALL open trades (replaces N per-pair queries)
    all_open = db.get_all_open_trades()
    open_count = len(all_open)
    open_pairs = set(t["pair"] for t in all_open)

    # remaining_slots = how many more MIN_ORDER trades the free balance can fund.
    # balance is already the available margin (Binance deducts open position margin).
    # Do NOT subtract open_count — that double-counts already-deducted margin.
    remaining_slots = int(balance / config.MIN_ORDER_USDT)
    max_slots = open_count + remaining_slots   # for display only

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

    # ── Candidate selection ────────────────────────────────────────────────────
    if override_pairs:
        # --pair CLI flag: bypass selector (manual single-pair test)
        candidates = [p for p in override_pairs if p not in open_pairs]
    else:
        # Smart selection: SCAN_PAIRS_PER_CYCLE pairs, cooldown-aware, FIFO rotation
        candidates = selector.get_next_pairs(config.SCAN_PAIRS_PER_CYCLE, exclude_pairs=open_pairs)

    if not candidates:
        logger.info("⏭ No candidate pairs available (all open or on cooldown).")
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    logger.info(f"🔍 Scanning {len(candidates)} pair(s): {candidates}")

    # ── Guard: skip scan when ALL AI providers are unavailable ───────────────
    if brain.all_providers_exhausted():
        wait = brain._key_mgr.earliest_reset_seconds()
        logger.warning(
            f"⏸ ALL AI PROVIDERS EXHAUSTED (Groq+OpenRouter+Gemini) — "
            f"skipping scan. Groq resets in {wait:.0f}s (~{wait/60:.1f}min)"
        )
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    # ── Steps 1-9: Scan + AI analyse + Execute immediately per pair ──────────────
    # Execute as soon as a pair hits MIN_CONFIDENCE — don't wait for all pairs.
    # This prevents stale prices when using slow AI providers (browser AI ~60-90s/call).
    results       = []
    scan_outcomes = {}   # pair → {direction, confidence, in_selected, executed, error}
    executed_set  = set()
    selected_set  = set()

    for pair in candidates:
        # Stop scanning once all slots are filled
        if remaining_slots <= 0:
            logger.info(f"⏭ All slots filled — skipping remaining pairs")
            break

        try:
            logger.info(f"\n[{pair}] ── Step 1-2: Scanning market...")
            snapshot = collector.collect_all(pair)

            if not snapshot.get("current_price"):
                logger.error(f"[{pair}] No price data — skipping.")
                scan_outcomes[pair] = {"direction": "HOLD", "confidence": 0, "error": True}
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
                "pair":       pair,
                "snapshot":   snapshot,
                "reasoning":  reasoning,
                "direction":  direction,
                "confidence": confidence,
                "horizon":    horizon,
            })
            scan_outcomes[pair] = {
                "direction": direction, "confidence": confidence,
                "in_selected": False, "executed": False, "error": False,
            }

            # ── Steps 7-9: Execute immediately if actionable ──────────────────
            # Price is still fresh — no need to wait for other pairs to finish.
            if direction in ("BUY", "SELL") and confidence >= config.MIN_CONFIDENCE:
                selected_set.add(pair)
                scan_outcomes[pair]["in_selected"] = True

                try:
                    logger.info(f"[{pair}] ── Step 7-8: Risk evaluation...")
                    order = risk.evaluate(direction, confidence, snapshot, reasoning)

                    if order is None:
                        logger.info(f"[{pair}] 🚫 Risk gate rejected.")
                    else:
                        logger.info(f"[{pair}] ── Step 9: Executing trade (price fresh)...")
                        trade_result = executor.execute(order)
                        executed_set.add(pair)
                        remaining_slots -= 1
                        scan_outcomes[pair]["executed"] = True

                        logger.info(
                            f"[{pair}] 🎯 Trade logged: {order.side} ${order.usdt_amount:.4f} USDT "
                            f"| OrderID={trade_result.get('binance_order_id')}"
                        )

                        # Symbol suspended (-4140) — apply 24h cooldown
                        if trade_result.get("_symbol_suspended"):
                            from datetime import timedelta
                            cd_until = datetime.now(timezone.utc) + timedelta(hours=24)
                            selector._state.setdefault(pair, {})["cooldown_until"] = cd_until.isoformat()
                            selector._save()
                            logger.warning(f"[{pair}] 🔒 Suspended symbol — skipping for 24h until {cd_until.strftime('%H:%M UTC')}")

                        tg.notify_trade_open(
                            pair=pair,
                            side=order.side,
                            entry=order.entry_price,
                            sl=order.stop_loss_price,
                            tp=order.take_profit_price,
                            usdt_amount=order.usdt_amount,
                            confidence=confidence,
                            is_dry=config.DRY_RUN,
                        )
                except Exception as e:
                    logger.error(f"[{pair}] Execution failed: {e}", exc_info=True)
            else:
                tag = "HOLD" if direction == "HOLD" else f"LOW CONF ({confidence:.0f}% < {config.MIN_CONFIDENCE:.0f}%)"
                logger.info(f"[{pair}] ⏩ Skipping — {tag}")

        except AllKeysExhaustedError as e:
            wait = brain._key_mgr.earliest_reset_seconds()
            logger.warning(
                f"[{pair}] ⏸ All Groq keys exhausted mid-scan — "
                f"skipping remaining pairs. First reset in {wait:.0f}s (~{wait/60:.1f}min)"
            )
            scan_outcomes[pair] = {"direction": "HOLD", "confidence": 0, "error": True}
            break  # no point scanning remaining pairs
        except Exception as e:
            logger.error(f"[{pair}] Scan failed: {e}", exc_info=True)
            scan_outcomes[pair] = {"direction": "HOLD", "confidence": 0, "error": True}

    if not results:
        logger.info("🚫 No AI results. Skipping execution.")
        if not override_pairs:
            _record_all_outcomes(scan_outcomes, selected_set=set(), executed_set=set())
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(f"✅ CYCLE DONE in {elapsed:.1f}s | Next in {config.CYCLE_INTERVAL}s")
        return

    # ── Print cycle summary table ──────────────────────────────────────────
    logger.info(f"\n{'─'*60}")
    logger.info(
        f"📊 CYCLE SUMMARY  ({len(results)} scanned | "
        f"{len(selected_set)} actionable | {len(executed_set)} executed)"
    )
    logger.info(f"{'─'*60}")
    for r in results:
        pair = r["pair"]
        if pair in executed_set:
            tag = "✅ EXECUTED"
        elif pair in selected_set:
            tag = "🚫 RISK REJECTED"
        elif r["direction"] == "HOLD":
            tag = "⏩ HOLD  (20min cooldown)"
        elif r["confidence"] < config.MIN_CONFIDENCE:
            tag = f"⏩ LOW CONF (<{config.MIN_CONFIDENCE:.0f}%)  (10min cooldown)"
        else:
            tag = "⏩ SLOTS FULL  (2min cooldown)"
        logger.info(
            f"  {r['pair']:12} {r['direction']:4}  {r['confidence']:3.0f}%"
            f"  ~{r['horizon']}min  {tag}"
        )
    logger.info(f"{'─'*60}\n")

    # ── Record outcomes → apply cooldowns to all scanned pairs ─────────────────
    if not override_pairs:   # skip when using --pair (manual test)
        _record_all_outcomes(scan_outcomes, selected_set, executed_set)


def _record_all_outcomes(scan_outcomes: dict, selected_set: set, executed_set: set):
    """
    After every cycle: map each scanned pair to a cooldown reason and record it.
    Reasons (longest to shortest cooldown):
      hold         → 20 min (AI said HOLD)
      low_conf     → 10 min (direction found but confidence too low)
      risk_reject  →  5 min (conf OK, risk gate blocked)
      slots_full   →  2 min (actionable but no slot available this cycle)
      error        →  3 min (scan/AI failure)
      executed     →  0 min (trade placed — pair locked by open-position check)
    """
    for pair, o in scan_outcomes.items():
        if o.get("error"):
            reason = "error"
        elif pair in executed_set:
            reason = "executed"
        elif o["direction"] == "HOLD":
            reason = "hold"
        elif o["confidence"] < config.MIN_CONFIDENCE:
            reason = "low_conf"
        elif pair not in selected_set:
            # confidence >= threshold but wasn't selected → slots were full
            reason = "slots_full"
        else:
            # was selected but risk gate rejected it (executed_set already handled above)
            reason = "risk_reject"
        selector.record_outcome(pair, reason, o.get("confidence", 0))


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crypto AI Trading Agent")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--pair", type=str, help="Override pair (e.g. BTCUSDT)")
    args = parser.parse_args()

    # --pair bypasses selector; otherwise selector manages the full pool
    override = [args.pair] if args.pair else None

    # Validate config
    try:
        config.validate()
    except EnvironmentError as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)

    import os
    os.makedirs("logs", exist_ok=True)

    logger.info("🤖 Crypto AI Trading Agent starting...")
    logger.info(f"   Model:         {config.GROQ_MODEL}")
    logger.info(f"   Pair pool:     {len(config.TRADING_PAIRS)} pairs {config.TRADING_PAIRS}")
    logger.info(f"   Scan/cycle:    {config.SCAN_PAIRS_PER_CYCLE} pairs")
    logger.info(f"   Interval:      {config.CYCLE_INTERVAL}s")
    logger.info(f"   Dry Run:       {config.DRY_RUN}")
    logger.info(f"   Min Conf:      {config.MIN_CONFIDENCE}%")
    logger.info(f"   Max Position:  {config.MAX_POSITION_PCT*100:.0f}% of balance")
    logger.info(f"   Trade Mode:    {config.TRADE_MODE.upper()} ({config.FUTURES_LEVERAGE}x leverage)")

    tg.notify_startup(
        pairs=config.TRADING_PAIRS,
        dry_run=config.DRY_RUN,
        mode=config.TRADE_MODE,
        leverage=config.FUTURES_LEVERAGE,
    )
    tg.start_polling()

    if config.DRY_RUN:
        logger.warning("🧪 DRY RUN MODE — no real orders will be placed")

    # Reconcile any stale open DB trades against live Binance positions
    # (handles case where agent was stopped while positions were open and Binance closed them)
    try:
        feedback.reconcile_stale_trades()
    except Exception as e:
        logger.warning(f"Reconcile on startup failed (non-fatal): {e}")

    if args.once:
        run_cycle(override)
        return

    # Scheduled loop
    run_cycle(override)  # immediate first run
    schedule.every(config.CYCLE_INTERVAL).seconds.do(run_cycle, pairs=override)

    logger.info(f"⏰ Scheduled to run every {config.CYCLE_INTERVAL}s. Press Ctrl+C to stop.")
    try:
        while True:
            if brain.all_providers_exhausted():
                # All AI providers unavailable — sleep until first Groq key resets
                wait = brain._key_mgr.earliest_reset_seconds()
                logger.info(
                    f"💤 All AI providers exhausted (Groq+OpenRouter+Gemini) — "
                    f"sleeping {wait:.0f}s (~{wait/60:.1f}min) until first Groq key resets..."
                )
                time.sleep(max(wait + 10, 60))  # +10s buffer; min 60s so we don't spam
                # Clear stale schedule entries to avoid a burst of back-to-back cycles
                schedule.clear()
                schedule.every(config.CYCLE_INTERVAL).seconds.do(run_cycle, pairs=override)
            else:
                schedule.run_pending()
                time.sleep(5)
    except KeyboardInterrupt:
        logger.info("👋 Agent stopped by user.")


if __name__ == "__main__":
    main()
