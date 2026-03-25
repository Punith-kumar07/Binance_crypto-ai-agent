import os
import sys
from datetime import datetime, timezone
from tabulate import tabulate
from loguru import logger

# Add current directory to path so we can import config/db
sys.path.append(os.getcwd())

import config
from db import client as db

def view_results():
    """Fetch and display dry-run results from Supabase."""
    logger.info("Fetching results from Supabase...")
    
    try:
        supabase = db.get_client()
        
        # 1. Overall Stats
        res = supabase.table("trade_history").select("id, pair, side, entry_price, actual_exit_price, stop_loss_price, take_profit_price, pnl_pct, is_dry_run, outcome, created_at").execute()
        trades = res.data or []
        
        if not trades:
            print("\n❌ No trades found in database.")
            return

        dry_trades = [t for t in trades if t.get("is_dry_run")]
        real_trades = [t for t in trades if not t.get("is_dry_run")]

        print("\n" + "="*80)
        print(f"📊 TRADING SUMMARY (Total: {len(trades)})")
        print("="*80)
        print(f"  Dry Run Trades: {len(dry_trades)}")
        print(f"  Real Trades:    {len(real_trades)}")
        
        # 2. Real trades table
        if real_trades:
            print("\n" + "-"*80)
            print("💰 REAL TRADES")
            print("-"*80)
            table_data = []
            for t in real_trades[-10:]:
                pnl = t.get("pnl_pct")
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "🟡 OPEN"
                outcome = t.get("outcome") or "PENDING"
                table_data.append([
                    t.get("created_at")[:16].replace("T", " "),
                    t.get("pair"),
                    t.get("side"),
                    f"${float(t.get('entry_price', 0)):,.4f}",
                    f"${float(t.get('stop_loss_price', 0)):,.4f}" if t.get("stop_loss_price") else "N/A",
                    f"${float(t.get('take_profit_price', 0)):,.4f}" if t.get("take_profit_price") else "N/A",
                    pnl_str,
                    outcome
                ])
            headers = ["Time (UTC)", "Pair", "Side", "Entry", "SL", "TP", "PnL%", "Outcome"]
            print(tabulate(table_data, headers=headers, tablefmt="simple"))

            # Real trade performance
            closed_real = [t for t in real_trades if t.get("pnl_pct") is not None]
            if closed_real:
                total_pnl = sum(t["pnl_pct"] for t in closed_real)
                wins = len([t for t in closed_real if t["pnl_pct"] > 0])
                losses = len([t for t in closed_real if t["pnl_pct"] <= 0])
                print(f"\n  Closed: {len(closed_real)} | Wins: {wins} | Losses: {losses} | Total PnL: {total_pnl:+.2f}%")

        # 3. Detailed Trade Table
        if dry_trades:
            print("\n" + "-"*80)
            print("🚀 LATEST DRY-RUN TRADES")
            print("-"*80)
            
            table_data = []
            for t in dry_trades[-10:]: # Show last 10
                pnl = t.get("pnl_pct")
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "OPEN"
                
                table_data.append([
                    t.get("created_at")[:16].replace("T", " "),
                    t.get("pair"),
                    t.get("side"),
                    f"${float(t.get('entry_price', 0)):,.4f}",
                    f"${float(t.get('actual_exit_price', 0)):,.4f}" if t.get("actual_exit_price") else "N/A",
                    pnl_str,
                    t.get("outcome") or "PENDING"
                ])
            
            headers = ["Time (UTC)", "Pair", "Side", "Entry", "Exit", "PnL%", "Outcome"]
            print(tabulate(table_data, headers=headers, tablefmt="simple"))

        # 3. PnL Calculation
        closed_dry = [t for t in dry_trades if t.get("pnl_pct") is not None]
        if closed_dry:
            total_pnl = sum(t["pnl_pct"] for t in closed_dry)
            wins = len([t for t in closed_dry if t["pnl_pct"] > 0])
            losses = len([t for t in closed_dry if t["pnl_pct"] < 0])
            win_rate = (wins / len(closed_dry)) * 100 if closed_dry else 0
            
            print("\n" + "-"*80)
            print("📈 PERFORMANCE METRICS (Dry Run)")
            print("-"*80)
            print(f"  Closed Trades:  {len(closed_dry)}")
            print(f"  Win Rate:       {win_rate:.1f}% ({wins}W / {losses}L)")
            print(f"  Total PnL:      {total_pnl:+.2f}%")
        else:
            print("\nℹ️ No closed dry-run trades to calculate performance yet.")

        # 4. Agent Reasoning Accuracy
        res_reasoning = supabase.table("agent_reasoning").select("pair, direction, confidence, prediction_correct").order("created_at", desc=True).limit(5).execute()
        reasoning = res_reasoning.data or []
        
        if reasoning:
            print("\n" + "-"*80)
            print("🧠 LATEST AI REASONING ACCURACY")
            print("-"*80)
            reasoning_table = []
            for r in reasoning:
                correct = r.get("prediction_correct")
                status = "✅ Correct" if correct is True else ("❌ Wrong" if correct is False else "⏳ Pending")
                reasoning_table.append([
                    r.get("pair"),
                    r.get("direction"),
                    f"{r.get('confidence')}%",
                    status
                ])
            print(tabulate(reasoning_table, headers=["Pair", "Call", "Conf", "Accuracy"], tablefmt="simple"))

        print("\n" + "="*80 + "\n")

    except Exception as e:
        logger.error(f"Failed to fetch results: {e}")

if __name__ == "__main__":
    view_results()
