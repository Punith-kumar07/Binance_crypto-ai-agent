"""
utils/test_brain.py — Run AI analysis for each trading pair and print results.
No orders placed. No risk checks. Just AI output.

Usage:
    python utils/test_brain.py
    python utils/test_brain.py SOLUSDT          # single pair
    python utils/test_brain.py ETHUSDT SOLUSDT  # multiple pairs
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from agents.brain import TradingBrain
from data.collector import DataCollector

PAIRS = sys.argv[1:] if len(sys.argv) > 1 else config.TRADING_PAIRS

collector = DataCollector()
brain     = TradingBrain()

SEP = "=" * 70

for pair in PAIRS:
    print(f"\n{SEP}")
    print(f"  🤖  AI ANALYSIS — {pair}")
    print(SEP)

    try:
        print(f"  Collecting market data...")
        snapshot = collector.collect(pair)

        print(f"  Sending to Groq ({config.GROQ_MODEL})...\n")
        result = brain.analyze(snapshot)

        direction  = result.get("direction", "?")
        confidence = result.get("confidence", 0)
        hypothesis = result.get("hypothesis", "")
        regime     = result.get("market_regime", "")
        risk_lvl   = result.get("risk_level", "")
        alignment  = result.get("signal_alignment", "")
        context    = result.get("market_context", "")
        rr         = result.get("risk_reward_ratio", 0)
        invalidate = result.get("invalidation", "")
        reasoning  = result.get("reasoning", "")
        signals    = result.get("key_signals", [])

        dir_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(direction, "⚪")
        conf_bar  = "█" * (confidence // 10) + "░" * (10 - confidence // 10)

        print(f"  Decision     : {dir_emoji} {direction}")
        print(f"  Confidence   : [{conf_bar}] {confidence}%")
        print(f"  Hypothesis   : {hypothesis}")
        print(f"  Regime       : {regime}  |  Context: {context}  |  Risk: {risk_lvl}")
        print(f"  Alignment    : {alignment}  |  R:R: {rr}")
        print(f"  Invalidation : {invalidate}")
        print()
        print("  Key Signals:")
        for s in signals:
            print(f"    • {s}")
        print()
        print("  Reasoning:")
        for line in reasoning.split(". "):
            if line.strip():
                print(f"    {line.strip()}.")
        print()

    except Exception as e:
        print(f"  ❌ Error: {e}")

print(f"\n{SEP}")
print("  Done. No orders were placed.")
print(SEP)
