"""
agents/feedback.py — Step 10: Feedback Loop.

Fixes:
  - Testnet URL applied (was missing — feedback was hitting live Binance even in testnet mode)
  - Uses db.update_reasoning_accuracy (centralized, not inline Supabase call)
  - Skips dry-run trades in TP/SL evaluation (they have no real Binance order)

After a trade closes (TP hit, SL hit, or manual SELL):
  1. Calculate actual PnL %
  2. Mark prediction_correct on the trade record
  3. Update the agent_reasoning record → AI sees its own track record in next prompt
"""
from binance.client import Client as BinanceClient
from loguru import logger
import config
from db import client as db


class FeedbackLoop:
    def __init__(self):
        self.binance = BinanceClient(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY, requests_params={"timeout": 10})
        if config.BINANCE_TESTNET:
            self.binance.API_URL = "https://testnet.binance.vision/api"   # was missing before

    def check_and_update_open_trades(self):
        """
        Check all open (non-closed) trades in Supabase.
        For each, fetch current price or Binance order status and mark TP/SL if hit.
        """
        for pair in config.TRADING_PAIRS:
            open_trades = db.get_open_trades(pair)
            if not open_trades:
                continue

            for trade in open_trades:
                if trade.get("is_dry_run"):
                    self._check_dry_run_trade(trade)
                elif config.TRADE_MODE == "futures":
                    self._check_futures_trade(trade)
                else:
                    self._check_real_trade(trade)

    def _check_dry_run_trade(self, trade: dict):
        """Simulate TP/SL check for dry-run trades using current market price."""
        pair = trade.get("pair")
        try:
            ticker = self.binance.get_symbol_ticker(symbol=pair)
            current_price = float(ticker["price"])
            
            # Log progress for dry-runs to provide visibility
            entry = float(trade.get("entry_price", 0))
            tp = float(trade.get("take_profit_price", 0))
            sl = float(trade.get("stop_loss_price", 0))
            pnl = (current_price - entry) / entry * 100 if trade.get("side") == "BUY" else (entry - current_price) / entry * 100
            
            logger.info(f"[{pair}] Dry-run monitoring: Price=${current_price:,.2f} | PnL={pnl:+.2f}% (TP=${tp:,.2f} / SL=${sl:,.2f})")
            
            self._evaluate_trade(trade, current_price)
        except Exception as e:
            logger.error(f"[{pair}] Dry-run feedback price fetch failed: {e}")

    def _check_real_trade(self, trade: dict):
        """Check actual Binance order status for real trades (OCO or Stop-Loss)."""
        pair = trade.get("pair")
        order_id = trade.get("binance_order_id")
        
        if not order_id or "FAILED" in str(order_id):
            # If no order ID, fallback to manual price check
            return self._check_dry_run_trade(trade)

        try:
            # For OCO, we need to check the list of orders
            # Simplified: check if the asset balance is gone or if any orders filled
            open_orders = self.binance.get_open_orders(symbol=pair)
            
            # If no open orders, it means OCO or SL/TP was filled or cancelled
            if not open_orders:
                # Get last trades to find exit price
                trades = self.binance.get_my_trades(symbol=pair, limit=1)
                if trades:
                    last_trade = trades[0]
                    exit_price = float(last_trade["price"])
                    self._evaluate_trade(trade, exit_price)
                else:
                    # Fallback to current price if no trades found
                    self._check_dry_run_trade(trade)
            else:
                logger.debug(f"[{pair}] Trade {trade.get('id')} still has open orders on Binance.")
        except Exception as e:
            logger.error(f"[{pair}] Real trade feedback check failed: {e}")
            self._check_dry_run_trade(trade)

    def _evaluate_trade(self, trade: dict, current_price: float):
        """Check if TP or SL has been hit; update Supabase if so."""
        side     = trade.get("side")
        entry    = float(trade.get("entry_price", 0))
        sl       = float(trade.get("stop_loss_price", 0))
        tp       = float(trade.get("take_profit_price", 0))
        trade_id = trade.get("id")
        pair     = trade.get("pair")

        if not entry or not sl or not tp:
            return

        hit_tp = hit_sl = False

        if side == "BUY":
            hit_tp = current_price >= tp
            hit_sl = current_price <= sl
        elif side == "SELL":
            # A SELL trade that's still open means we're tracking a short (rare on spot)
            hit_tp = current_price <= tp
            hit_sl = current_price >= sl

        if not hit_tp and not hit_sl:
            logger.debug(f"[{pair}] Trade {trade_id} open. Price={current_price} | TP={tp} | SL={sl}")
            return

        raw_pnl = (
            (current_price - entry) / entry * 100 if side == "BUY"
            else (entry - current_price) / entry * 100
        )
        lev = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
        pnl_pct = raw_pnl * lev

        outcome = {
            "exit_price":         current_price,
            "pnl_pct":            round(pnl_pct, 4),
            "result":             "win" if hit_tp else "loss",
            "prediction_correct": hit_tp,
        }

        db.update_trade_outcome(trade_id, outcome)

        label = "TAKE PROFIT HIT" if hit_tp else "STOP LOSS HIT"
        logger.info(
            f"[{pair}] {label} | Entry={entry} → Exit={current_price} "
            f"| PnL={pnl_pct:+.2f}% | {'WIN' if hit_tp else 'LOSS'}"
        )

        # Update the AI reasoning record so the agent learns from this outcome
        reasoning_id = trade.get("reasoning_id")
        if reasoning_id:
            db.update_reasoning_accuracy(reasoning_id, hit_tp)
        else:
            logger.debug(f"[{pair}] Trade {trade_id} has no reasoning_id — feedback accuracy not linked.")

    def _check_futures_trade(self, trade: dict):
        """Check futures position status every cycle; mark closed if TP/SL was hit."""
        pair = trade.get("pair")
        try:
            positions = self.binance.futures_position_information(symbol=pair)
            for pos in positions:
                pos_amt = float(pos.get("positionAmt", 0))
                mark_price = float(pos.get("markPrice", 0))
                entry = float(trade.get("entry_price", 0))

                if pos_amt == 0:
                    # Position closed — find actual exit price from recent trades
                    exit_price = None
                    try:
                        recent = self.binance.futures_account_trades(symbol=pair, limit=10)
                        for t in sorted(recent, key=lambda x: x.get("time", 0), reverse=True):
                            if t.get("side") == "SELL" and float(t.get("realizedPnl", 0)) != 0:
                                exit_price = float(t["price"])
                                break
                    except Exception:
                        pass
                    if not exit_price:
                        ticker = self.binance.futures_symbol_ticker(symbol=pair)
                        exit_price = float(ticker["price"])
                    self._evaluate_trade(trade, exit_price)
                else:
                    pnl_pct = (mark_price - entry) / entry * 100 if entry else 0
                    unrl = float(pos.get("unRealizedProfit", 0))
                    tp = float(trade.get("take_profit_price", 0))
                    sl = float(trade.get("stop_loss_price", 0))
                    logger.info(
                        f"[{pair}] Futures open | Mark=${mark_price:,.2f} | PnL={pnl_pct:+.2f}% "
                        f"(unrealized ${unrl:+.4f}) | TP=${tp:,.2f} | SL=${sl:,.2f}"
                    )
        except Exception as e:
            logger.error(f"[{pair}] Futures feedback check failed: {e}")
            self._check_dry_run_trade(trade)
