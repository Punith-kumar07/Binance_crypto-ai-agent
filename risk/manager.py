"""
risk/manager.py — Steps 7 & 8: Risk Awareness + Decision Gate.

Before any trade is executed, this module checks:
  - Is confidence above the minimum threshold?
  - Is there already an open position in this pair?
  - Is the proposed position size within limits?
  - Is volatility (ATR) acceptable?
  - Will we blow our tiny $10 balance on fees?

Returns an approved TradeOrder or a REJECT with reason.
"""
from loguru import logger
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
import config
import constants as C
from db import client as db
from notifications import telegram as tg

# Tracks "pair-YYYY-MM-DD" keys so the daily limit alert fires only once per pair per day
_daily_limit_alerted: set = set()


@dataclass
class TradeOrder:
    pair: str
    side: str               # "BUY" or "SELL"
    usdt_amount: float      # how much USDT to spend
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    confidence: float
    reasoning: dict
    approved: bool = True
    reject_reason: str = ""
    close_position_qty: float = 0.0  # qty to sell when closing a position

    @property
    def quantity(self) -> float:
        """Estimated quantity of the asset to buy/sell."""
        return self.usdt_amount / self.entry_price if self.entry_price > 0 else 0.0


class RiskManager:

    # Binance minimum order: $5 USDT equivalent (with our tiny balance we must be careful)
    MIN_ORDER_USDT = 5.5

    def evaluate(
        self,
        direction: str,
        confidence: float,
        snapshot: dict,
        reasoning: dict,
    ) -> Optional[TradeOrder]:
        """
        Main entry point. Returns a TradeOrder if approved, None if rejected.
        """
        pair        = snapshot["pair"]
        price       = snapshot["current_price"]
        balance     = snapshot["usdt_balance"]
        ind1h       = snapshot.get("indicators_1h", {})
        atr_pct     = ind1h.get("atr_pct", 1.0)
        atr_val     = ind1h.get("atr", price * 0.01)   # absolute ATR in price units

        # ── Gate 1: Daily Loss Limit ────────────────────────────────────────
        daily_pnl = db.get_daily_pnl_pct(pair)
        if daily_pnl <= config.MAX_DAILY_LOSS_PCT:
            logger.warning(
                f"[{pair}] ❌ REJECT: Daily loss limit reached ({daily_pnl:.2f}% <= {config.MAX_DAILY_LOSS_PCT}%)"
            )
            alert_key = f"{pair}-{datetime.now(timezone.utc).date().isoformat()}"
            if alert_key not in _daily_limit_alerted:
                _daily_limit_alerted.add(alert_key)
                tg.notify_daily_limit_hit(pair, daily_pnl)
            return None

        # ── Gate 1b: Funding rate warning — skip if fighting the funding ───
        funding = snapshot.get("funding_rate", {})
        fr_warning = funding.get("warning")
        if fr_warning:
            if "HIGH_LONG_FUNDING" in fr_warning and direction == "BUY":
                logger.warning(
                    f"[{pair}] ❌ REJECT: Funding rate too high ({funding.get('rate_pct',0):+.4f}%) "
                    f"— risky to go LONG when longs are paying heavily."
                )
                return None
            if "HIGH_SHORT_FUNDING" in fr_warning and direction == "SELL":
                logger.warning(
                    f"[{pair}] ❌ REJECT: Funding rate too negative ({funding.get('rate_pct',0):+.4f}%) "
                    f"— risky to go SHORT when shorts are paying heavily."
                )
                return None

        # ── Gate 1c: Liquidity filter — skip low-volume pairs ───────────────
        quote_vol_24h = snapshot.get("stats_24h", {}).get("quote_volume_24h", 0)
        if quote_vol_24h and quote_vol_24h < C.MIN_QUOTE_VOLUME_24H:
            logger.warning(
                f"[{pair}] ❌ REJECT: 24h volume ${quote_vol_24h:,.0f} below "
                f"liquidity minimum ${C.MIN_QUOTE_VOLUME_24H:,.0f}"
            )
            return None

        # ── Gate 2: Direction must be actionable ────────────────────────────
        if direction == "HOLD":
            logger.info(f"[{pair}] ⏸ HOLD — no action.")
            return None

        # ── Gate 3: Confidence threshold ────────────────────────────────────
        if confidence < config.MIN_CONFIDENCE:
            logger.info(
                f"[{pair}] ❌ REJECT: Confidence {confidence:.0f}% < "
                f"minimum {config.MIN_CONFIDENCE:.0f}%"
            )
            return None

        # ── Gate 4: No double-position on same pair ─────────────────────────
        open_trades = db.get_open_trades(pair)
        if open_trades:
            logger.info(
                f"[{pair}] ❌ REJECT: Already have {len(open_trades)} open position(s). "
                f"Skipping {direction}."
            )
            return None
        # SELL now means open a SHORT (futures), not close an existing LONG.

        # ── Gate 5: Signal alignment check ─────────────────────────────────
        alignment = reasoning.get("signal_alignment", "mixed")
        if alignment == "contradictory":
            logger.info(f"[{pair}] ❌ REJECT: Signals are contradictory — too risky.")
            return None

        # ── Gate 6: Extreme volatility guard ────────────────────────────────
        if atr_pct and atr_pct > 3.0:
            logger.warning(f"[{pair}] ⚠️  High volatility: ATR={atr_pct:.2f}%. Halving position size.")
            # We don't reject, just reduce size

        # ── Gate 7: Sufficient balance ──────────────────────────────────────
        if balance < self.MIN_ORDER_USDT:
            logger.error(f"[{pair}] ❌ REJECT: Balance ${balance:.4f} too low to trade.")
            return None

        # ── Compute position size ────────────────────────────────────────────
        base_size = balance * config.MAX_POSITION_PCT

        # Scale by confidence: 65% conf → 60% of max, 90% conf → ~90% of max
        confidence_factor = (confidence - config.MIN_CONFIDENCE) / (100 - config.MIN_CONFIDENCE)
        adjusted_size = base_size * (0.5 + 0.5 * confidence_factor)

        # Halve in high volatility
        if atr_pct and atr_pct > 3.0:
            adjusted_size *= 0.5

        # Clamp to min/max
        adjusted_size = max(self.MIN_ORDER_USDT, min(adjusted_size, balance * config.MAX_POSITION_PCT))
        adjusted_size = min(adjusted_size, balance - 0.5)  # keep $0.50 buffer for fees

        # ── Compute ATR-based stop loss + take profit ────────────────────────
        # SL = ATR × 1.5  |  TP = ATR × 3.5  →  R:R ≈ 2.3:1
        # Falls back to config percentages if ATR is missing/zero.
        if atr_val and atr_val > 0:
            sl_distance = atr_val * C.ATR_SL_MULTIPLIER
            tp_distance = atr_val * C.ATR_TP_MULTIPLIER
            # Enforce minimum R:R of 2:1
            if tp_distance < sl_distance * C.ATR_MIN_RR_RATIO:
                tp_distance = sl_distance * C.ATR_MIN_RR_RATIO
            sl_pct_actual = sl_distance / price * 100
            tp_pct_actual = tp_distance / price * 100
            sl_label = f"ATR×{C.ATR_SL_MULTIPLIER} ({sl_pct_actual:.2f}%)"
        else:
            # Fallback to config fixed percentages
            sl_distance = price * config.STOP_LOSS_PCT
            tp_distance = price * config.TAKE_PROFIT_PCT
            sl_pct_actual = config.STOP_LOSS_PCT * 100
            tp_pct_actual = config.TAKE_PROFIT_PCT * 100
            sl_label = f"fixed ({sl_pct_actual:.2f}%)"

        if direction == "BUY":
            stop_loss_price   = round(price - sl_distance, 6)
            take_profit_price = round(price + tp_distance, 6)
        else:  # SELL / SHORT
            stop_loss_price   = round(price + sl_distance, 6)
            take_profit_price = round(price - tp_distance, 6)

        rr_actual = round(tp_distance / sl_distance, 2) if sl_distance > 0 else 0

        order = TradeOrder(
            pair=pair,
            side=direction,
            usdt_amount=round(adjusted_size, 4),
            entry_price=price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            confidence=confidence,
            reasoning=reasoning,
        )

        logger.info(
            f"[{pair}] ✅ APPROVED: {direction} ${adjusted_size:.4f} USDT "
            f"| Entry: {price} | SL: {stop_loss_price} ({sl_label}) "
            f"| TP: {take_profit_price} ({tp_pct_actual:.2f}%) | R:R={rr_actual} "
            f"| Confidence: {confidence:.0f}% | Alignment: {alignment}"
        )

        return order