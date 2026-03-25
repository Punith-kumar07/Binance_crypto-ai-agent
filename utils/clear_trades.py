import os
import sys
from loguru import logger

# Add current directory to path so we can import config/db
sys.path.append(os.getcwd())

import config
from db import client as db

def clear_open_trades():
    """Mark all currently open trades as 'CANCELLED' for a clean start."""
    logger.info("Cleaning up open trades in Supabase...")
    
    try:
        supabase = db.get_client()
        
        # 1. Fetch all open trades
        res = supabase.table("trade_history").select("id, pair, side").is_("closed_at", "null").execute()
        open_trades = res.data or []
        
        if not open_trades:
            logger.info("✅ No open trades found. Database is already clean.")
            return

        logger.info(f"Found {len(open_trades)} open trades. Marking as cancelled...")

        # 2. Update each trade to close it
        for trade in open_trades:
            supabase.table("trade_history").update({
                "closed_at": "now()",
                "outcome": "cancelled",
                "pnl_pct": 0.0
            }).eq("id", trade["id"]).execute()
            
            logger.info(f"  - {trade['pair']} ({trade['side']}) trade marked as cancelled.")

        logger.info("✅ Cleanup complete. The agent will now be able to open new positions.")

    except Exception as e:
        logger.error(f"Failed to clear trades: {e}")

if __name__ == "__main__":
    clear_open_trades()
