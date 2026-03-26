"""
execution/executor.py — Step 9: Action + Exit Order Management.

Full lifecycle:
  1. BUY  → market order (spend exact USDT)
  2. EXIT → immediately place OCO sell (TP + SL) on Binance server-side
             If OCO fails (small balance/precision) → falls back to stop-limit
  3. SELL → market sell of full asset balance

With server-side exit orders, your computer does NOT need to stay on.
Binance executes the exit automatically when price hits TP or SL.
"""
import math
import time
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
from loguru import logger
from datetime import datetime, timezone
import config
from risk.manager import TradeOrder
from db import client as db


class TradeExecutor:
    def __init__(self):
        self.binance = BinanceClient(
            config.BINANCE_API_KEY,
            config.BINANCE_SECRET_KEY,
            requests_params={"timeout": 10},
        )
        if config.BINANCE_TESTNET:
            self.binance.API_URL = "https://testnet.binance.vision/api"
        self._symbol_info_cache = {}

    def execute(self, order: TradeOrder) -> dict:
        logger.info(
            f"[{order.pair}] {'[DRY RUN] ' if config.DRY_RUN else ''}Executing "
            f"{order.side} ${order.usdt_amount:.4f} USDT @ ~${order.entry_price}"
        )

        trade_record = {
            "pair":               order.pair,
            "side":               order.side,
            "entry_price":        order.entry_price,
            "quantity":           round(order.quantity, 6),
            "usdt_value":         order.usdt_amount,
            "stop_loss_price":    order.stop_loss_price,
            "take_profit_price":  order.take_profit_price,
            "confidence":         order.confidence,
            "direction":          order.side,
            "is_dry_run":         config.DRY_RUN,
            "binance_order_id":   None,
            "oco_protected":      False,
            "reasoning_id":       order.reasoning.get("_reasoning_id"),
        }

        if config.DRY_RUN:
            dry_id = f"DRY_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            mode_label = f"FUTURES {config.FUTURES_LEVERAGE}x" if config.TRADE_MODE == "futures" else "SPOT"
            side_label = "LONG" if order.side == "BUY" else "SHORT"
            logger.info(
                f"[{order.pair}] 🧪 DRY RUN [{mode_label}] — would open {side_label} "
                f"{order.quantity:.6f} {order.pair.replace('USDT','')} "
                f"| SL={order.stop_loss_price} TP={order.take_profit_price}"
            )
            trade_record["binance_order_id"] = dry_id
            trade_record["oco_protected"] = True
            db.log_trade(trade_record)
            return trade_record

        try:
            if config.TRADE_MODE == "futures":
                if order.side == "BUY":
                    result = self._execute_futures_buy(order, trade_record)
                else:
                    result = self._execute_futures_short(order, trade_record)
            else:
                if order.side == "BUY":
                    result = self._execute_buy(order, trade_record)
                else:
                    result = self._execute_sell(order, trade_record)

            trade_record["binance_order_id"] = result.get("orderId", "unknown")
            logger.info(
                f"[{order.pair}] ✅ Order placed: "
                f"ID={trade_record['binance_order_id']} "
                f"| OCO protected: {trade_record['oco_protected']}"
            )

        except BinanceAPIException as e:
            logger.error(f"[{order.pair}] ❌ Binance error: {e.code} — {e.message}")
            trade_record["binance_order_id"] = f"FAILED_{e.code}"
            trade_record["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade_record["outcome"] = "failed"
            # -4140 = symbol suspended — flag so caller can apply long cooldown
            if e.code == -4140:
                trade_record["_symbol_suspended"] = True
                logger.warning(f"[{order.pair}] ⚠️ Symbol suspended on Binance (-4140). Will skip for 24h.")
        except Exception as e:
            logger.error(f"[{order.pair}] ❌ Execution error: {e}")
            trade_record["binance_order_id"] = "FAILED_UNKNOWN"
            trade_record["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade_record["outcome"] = "failed"

        db.log_trade(trade_record)
        return trade_record

    def _execute_buy(self, order: TradeOrder, trade_record: dict) -> dict:
        buy_result = self.binance.order_market_buy(
            symbol=order.pair,
            quoteOrderQty=order.usdt_amount,
        )
        logger.info(f"[{order.pair}] ✅ Market BUY filled: {buy_result.get('orderId')}")

        fills        = buy_result.get("fills", [])
        filled_qty   = float(buy_result.get("executedQty", order.quantity))
        filled_price = float(fills[0].get("price", order.entry_price)) if fills else order.entry_price

        # Deduct fee if it was charged in the base asset (e.g. ETH, SOL)
        # to avoid -2010 'insufficient balance' when placing exit sell orders
        base_asset = order.pair.replace("USDT", "")
        if fills and fills[0].get("commissionAsset") == base_asset:
            total_fee = sum(float(f.get("commission", 0)) for f in fills)
            filled_qty = filled_qty - total_fee
            logger.info(f"[{order.pair}] Fee deducted: {total_fee:.8f} {base_asset} → net qty={filled_qty:.8f}")

        trade_record["entry_price"] = filled_price
        trade_record["quantity"]    = filled_qty

        sl_price = round(filled_price * (1 - config.STOP_LOSS_PCT), 8)
        tp_price = round(filled_price * (1 + config.TAKE_PROFIT_PCT), 8)
        trade_record["stop_loss_price"]   = sl_price
        trade_record["take_profit_price"] = tp_price

        logger.info(f"[{order.pair}] Fill: {filled_qty} @ {filled_price} | SL={sl_price} | TP={tp_price}")

        logger.info(f"[{order.pair}] Waiting 3s for Binance to register asset as free...")
        time.sleep(3)

        qty_rounded = self._round_quantity(order.pair, filled_qty)
        self._place_exit_orders(order.pair, qty_rounded, sl_price, tp_price, trade_record)

        return buy_result

    def _format_decimal(self, value: float) -> str:
        """Format float as a plain decimal string — avoids scientific notation (e.g. 7e-05 → '0.00007')."""
        return f"{value:.10f}".rstrip('0').rstrip('.')

    def _place_exit_orders(self, pair, qty, sl_price, tp_price, trade_record):
        """
        Try OCO first — handles both TP and SL in one Binance server-side order.
        Falls back to stop-limit AND limit-tp if OCO fails (common with small balances).
        """
        tp_rounded = self._round_price(pair, tp_price)
        sl_rounded = self._round_price(pair, sl_price)
        sl_limit   = self._round_price(pair, sl_price * 0.999)

        # Pre-check: Binance requires min $5 notional on both legs of OCO.
        # If lot-size rounding caused the SL leg to fall below $5, skip OCO —
        # the feedback loop (every 15s) will handle the exit instead.
        sl_notional = qty * sl_rounded
        if sl_notional < 5.0:
            logger.warning(
                f"[{pair}] ⚠️ Skipping OCO — SL notional ${sl_notional:.2f} below $5 minimum "
                f"(lot-size rounding). Feedback loop will monitor and close this position."
            )
            trade_record["oco_protected"] = False
            return

        # Use plain decimal strings — Binance rejects scientific notation (e.g. "7e-05")
        qty_str = self._format_decimal(qty)
        tp_str  = self._format_decimal(tp_rounded)
        sl_str  = self._format_decimal(sl_rounded)
        sl_lmt  = self._format_decimal(sl_limit)

        # Try new OCO endpoint format (POST /api/v3/orderList/oco — introduced 2024)
        try:
            self.binance._post('orderList/oco', True, data={
                'symbol':             pair,
                'side':               'SELL',
                'quantity':           qty_str,
                'aboveType':          'LIMIT_MAKER',
                'abovePrice':         tp_str,
                'belowType':          'STOP_LOSS_LIMIT',
                'belowStopPrice':     sl_str,
                'belowPrice':         sl_lmt,
                'belowTimeInForce':   'GTC',
            })
            trade_record["oco_protected"] = True
            logger.info(
                f"[{pair}] ✅ OCO exit placed — TP={tp_str} | SL={sl_str} "
                f"| Binance will auto-exit even if agent goes offline"
            )
            return
        except BinanceAPIException as e:
            logger.warning(f"[{pair}] ⚠️ New OCO failed ({e.code}: {e.message}) — trying legacy OCO")

        # Legacy OCO removed — Binance's old endpoint now also requires aboveType/belowType format

        # Fallback 1: Stop-Loss
        try:
            self.binance.create_order(
                symbol=pair,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                quantity=qty_str,
                stopPrice=sl_str,
                price=sl_lmt,
                timeInForce="GTC",
            )
            logger.info(f"[{pair}] ✅ Stop-limit SL={sl_str} placed.")
        except BinanceAPIException as e:
            logger.error(f"[{pair}] ❌ Fallback SL failed ({e.code})")

        # Fallback 2: Take-Profit (Limit order)
        try:
            self.binance.order_limit_sell(
                symbol=pair,
                quantity=qty_str,
                price=tp_str,
            )
            logger.info(f"[{pair}] ✅ Limit TP={tp_str} placed.")
            trade_record["oco_protected"] = False
        except BinanceAPIException as e:
            logger.warning(f"[{pair}] ⚠️ Fallback TP failed ({e.code}) — likely insufficient 'free' balance for two separate orders.")
            trade_record["oco_protected"] = False

    def _execute_sell(self, order: TradeOrder, trade_record: dict) -> dict:
        """
        Executes a SELL order to close an existing position.
        """
        asset = order.pair.replace("USDT", "")

        # 1. Cancel all open orders for this pair first
        try:
            open_orders = self.binance.get_open_orders(symbol=order.pair)
            for o in open_orders:
                self.binance.cancel_order(symbol=order.pair, orderId=o["orderId"])
                logger.info(f"[{order.pair}] Cancelled open order {o['orderId']}")
        except Exception as e:
            logger.warning(f"[{order.pair}] Could not cancel open orders: {e}")

        # 2. Get actual balance to sell (don't rely on database qty only)
        account = self.binance.get_account()
        asset_balance = 0.0
        for b in account["balances"]:
            if b["asset"] == asset:
                asset_balance = float(b["free"])
                break

        if asset_balance <= 0:
            # Check if it's already sold or tiny
            logger.warning(f"[{order.pair}] Asset balance is {asset_balance}, nothing to sell.")
            return {"orderId": "ALREADY_CLOSED", "status": "FILLED"}

        qty = self._round_quantity(order.pair, asset_balance)
        
        # 3. Market sell
        sell_result = self.binance.order_market_sell(symbol=order.pair, quantity=qty)
        logger.info(f"[{order.pair}] ✅ Market SELL filled: {sell_result.get('orderId')}")
        
        trade_record["oco_protected"] = False
        trade_record["quantity"] = qty
        
        return sell_result

    def _get_symbol_info(self, pair):
        if pair not in self._symbol_info_cache:
            self._symbol_info_cache[pair] = self.binance.get_symbol_info(pair) or {}
        return self._symbol_info_cache[pair]

    def _round_price(self, pair, price):
        try:
            for f in self._get_symbol_info(pair).get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                    precision = max(0, -int(math.log10(tick)))
                    return round(math.floor(price / tick) * tick, precision)
        except Exception:
            pass
        return round(price, 2)

    def _round_quantity(self, pair, qty):
        try:
            for f in self._get_symbol_info(pair).get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    precision = max(0, -int(math.log10(step)))
                    return round(math.floor(qty / step) * step, precision)
        except Exception:
            pass
        return round(qty, 5)

    # ── Futures helpers ────────────────────────────────────────────────────

    def _get_futures_symbol_info(self, pair):
        key = f"F_{pair}"
        if key not in self._symbol_info_cache:
            info = self.binance.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == pair:
                    self._symbol_info_cache[key] = s
                    break
        return self._symbol_info_cache.get(f"F_{pair}", {})

    def _round_price_futures(self, pair, price):
        try:
            for f in self._get_futures_symbol_info(pair).get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                    precision = max(0, -int(math.log10(tick)))
                    return round(math.floor(price / tick) * tick, precision)
        except Exception:
            pass
        return round(price, 2)

    def _round_quantity_futures(self, pair, qty):
        try:
            for f in self._get_futures_symbol_info(pair).get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    precision = max(0, -int(math.log10(step)))
                    return round(math.floor(qty / step) * step, precision)
        except Exception:
            pass
        return round(qty, 5)

    def _execute_futures_buy(self, order: TradeOrder, trade_record: dict) -> dict:
        pair = order.pair

        # Set leverage on this pair
        self.binance.futures_change_leverage(symbol=pair, leverage=config.FUTURES_LEVERAGE)
        logger.info(f"[{pair}] Futures leverage set to {config.FUTURES_LEVERAGE}x")

        # Get current futures mark price
        ticker = self.binance.futures_symbol_ticker(symbol=pair)
        raw = ticker.get("price") or ticker.get("markPrice") or ticker.get("lastPrice")
        if not raw:
            mp = self.binance.futures_mark_price(symbol=pair)
            raw = mp.get("markPrice") or mp.get("indexPrice")
        entry_price = float(raw)

        # Notional = margin * leverage; qty = notional / price
        notional = order.usdt_amount * config.FUTURES_LEVERAGE
        qty = self._round_quantity_futures(pair, notional / entry_price)
        qty_str = self._format_decimal(qty)

        if qty <= 0:
            logger.warning(
                f"[{pair}] ⚠️ Futures qty rounds to 0 (notional ${notional:.2f} too small for lot size). "
                f"Increase balance or reduce pairs."
            )
            raise ValueError(f"Futures qty is 0 for {pair} — notional ${notional:.2f} below lot-size minimum")

        logger.info(
            f"[{pair}] Futures BUY: margin=${order.usdt_amount:.2f} x {config.FUTURES_LEVERAGE}x "
            f"= ${notional:.2f} notional | qty={qty_str}"
        )

        buy_result = self.binance.futures_create_order(
            symbol=pair,
            side="BUY",
            type="MARKET",
            quantity=qty_str,
        )

        filled_qty   = float(buy_result.get("executedQty", qty)) or qty  # Binance may return "0" immediately
        filled_price = float(buy_result.get("avgPrice", 0)) or entry_price

        trade_record["entry_price"] = filled_price
        trade_record["quantity"]    = filled_qty

        sl_price = round(filled_price * (1 - config.STOP_LOSS_PCT), 8)
        tp_price = round(filled_price * (1 + config.TAKE_PROFIT_PCT), 8)
        trade_record["stop_loss_price"]   = sl_price
        trade_record["take_profit_price"] = tp_price

        logger.info(f"[{pair}] Futures fill: {filled_qty} @ {filled_price} | SL={sl_price} | TP={tp_price}")

        self._place_futures_exit_orders(pair, sl_price, tp_price, trade_record)
        return buy_result

    def _place_futures_exit_orders(self, pair, sl_price, tp_price, trade_record):
        """Place server-side TP (TAKE_PROFIT_MARKET) and SL (STOP_MARKET) on futures."""
        sl_str = self._format_decimal(self._round_price_futures(pair, sl_price))
        tp_str = self._format_decimal(self._round_price_futures(pair, tp_price))

        tp_ok = sl_ok = False

        try:
            self.binance.futures_create_order(
                symbol=pair,
                side="SELL",
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp_str,
                closePosition="true",
                workingType="MARK_PRICE",
            )
            logger.info(f"[{pair}] ✅ Futures TP placed: {tp_str}")
            tp_ok = True
        except BinanceAPIException as e:
            logger.error(f"[{pair}] ❌ Futures TP failed ({e.code}: {e.message})")

        try:
            self.binance.futures_create_order(
                symbol=pair,
                side="SELL",
                type="STOP_MARKET",
                stopPrice=sl_str,
                closePosition="true",
                workingType="MARK_PRICE",
            )
            logger.info(f"[{pair}] ✅ Futures SL placed: {sl_str}")
            sl_ok = True
        except BinanceAPIException as e:
            logger.error(f"[{pair}] ❌ Futures SL failed ({e.code}: {e.message})")

        trade_record["oco_protected"] = tp_ok and sl_ok
        if trade_record["oco_protected"]:
            logger.info(f"[{pair}] ✅ Futures position fully protected (TP + SL on Binance servers)")

    def _execute_futures_short(self, order: TradeOrder, trade_record: dict) -> dict:
        """Open a new SHORT (SELL) position on futures."""
        pair = order.pair

        self.binance.futures_change_leverage(symbol=pair, leverage=config.FUTURES_LEVERAGE)
        logger.info(f"[{pair}] Futures leverage set to {config.FUTURES_LEVERAGE}x")

        ticker = self.binance.futures_symbol_ticker(symbol=pair)
        raw = ticker.get("price") or ticker.get("markPrice") or ticker.get("lastPrice")
        if not raw:
            mp = self.binance.futures_mark_price(symbol=pair)
            raw = mp.get("markPrice") or mp.get("indexPrice")
        entry_price = float(raw)

        notional = order.usdt_amount * config.FUTURES_LEVERAGE
        qty      = self._round_quantity_futures(pair, notional / entry_price)
        qty_str  = self._format_decimal(qty)

        if qty <= 0:
            raise ValueError(
                f"Futures SHORT qty is 0 for {pair} — notional ${notional:.2f} below lot-size minimum"
            )

        logger.info(
            f"[{pair}] Futures SHORT: margin=${order.usdt_amount:.2f} x {config.FUTURES_LEVERAGE}x "
            f"= ${notional:.2f} notional | qty={qty_str}"
        )

        sell_result = self.binance.futures_create_order(
            symbol=pair,
            side="SELL",
            type="MARKET",
            quantity=qty_str,
        )

        filled_qty   = float(sell_result.get("executedQty", qty)) or qty
        filled_price = float(sell_result.get("avgPrice", 0)) or entry_price

        # SHORT: SL is ABOVE entry, TP is BELOW entry
        sl_price = round(filled_price * (1 + config.STOP_LOSS_PCT), 8)
        tp_price = round(filled_price * (1 - config.TAKE_PROFIT_PCT), 8)

        trade_record["entry_price"]        = filled_price
        trade_record["quantity"]           = filled_qty
        trade_record["stop_loss_price"]    = sl_price
        trade_record["take_profit_price"]  = tp_price

        logger.info(
            f"[{pair}] SHORT fill: {filled_qty} @ {filled_price} "
            f"| SL={sl_price} (above) | TP={tp_price} (below)"
        )

        self._place_futures_short_exit_orders(pair, sl_price, tp_price, trade_record)
        return sell_result

    def _place_futures_short_exit_orders(self, pair, sl_price, tp_price, trade_record):
        """Place server-side TP and SL for a SHORT position (BUY-side close orders)."""
        sl_str = self._format_decimal(self._round_price_futures(pair, sl_price))
        tp_str = self._format_decimal(self._round_price_futures(pair, tp_price))

        tp_ok = sl_ok = False

        try:
            self.binance.futures_create_order(
                symbol=pair,
                side="BUY",
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp_str,
                closePosition="true",
                workingType="MARK_PRICE",
            )
            logger.info(f"[{pair}] ✅ SHORT TP placed: {tp_str} (BUY at price drop)")
            tp_ok = True
        except BinanceAPIException as e:
            logger.error(f"[{pair}] ❌ SHORT TP failed ({e.code}: {e.message})")

        try:
            self.binance.futures_create_order(
                symbol=pair,
                side="BUY",
                type="STOP_MARKET",
                stopPrice=sl_str,
                closePosition="true",
                workingType="MARK_PRICE",
            )
            logger.info(f"[{pair}] ✅ SHORT SL placed: {sl_str} (BUY at price rise)")
            sl_ok = True
        except BinanceAPIException as e:
            logger.error(f"[{pair}] ❌ SHORT SL failed ({e.code}: {e.message})")

        trade_record["oco_protected"] = tp_ok and sl_ok
        if trade_record["oco_protected"]:
            logger.info(f"[{pair}] ✅ SHORT position fully protected (TP + SL on Binance servers)")

    def _execute_futures_close_position(self, pair: str, trade_record: dict) -> dict:
        """Close any open futures position (LONG or SHORT) using reduceOnly."""
        # Cancel all open TP/SL orders first
        try:
            open_orders = self.binance.futures_get_open_orders(symbol=pair)
            for o in open_orders:
                self.binance.futures_cancel_order(symbol=pair, orderId=o["orderId"])
                logger.info(f"[{pair}] Cancelled futures order {o['orderId']}")
        except Exception as e:
            logger.warning(f"[{pair}] Could not cancel futures orders: {e}")

        # Detect position direction from positionAmt sign
        try:
            positions = self.binance.futures_position_information(symbol=pair)
            qty        = 0.0
            close_side = "SELL"
            for pos in positions:
                pos_amt = float(pos.get("positionAmt", 0))
                if pos_amt > 0:          # long
                    qty        = pos_amt
                    close_side = "SELL"
                    break
                elif pos_amt < 0:        # short
                    qty        = abs(pos_amt)
                    close_side = "BUY"
                    break
        except Exception as e:
            logger.error(f"[{pair}] Could not get futures position: {e}")
            return {"orderId": "FAILED_NO_POSITION", "status": "ERROR"}

        if qty <= 0:
            logger.warning(f"[{pair}] No open futures position to close.")
            return {"orderId": "ALREADY_CLOSED", "status": "FILLED"}

        qty_str    = self._format_decimal(self._round_quantity_futures(pair, qty))
        result     = self.binance.futures_create_order(
            symbol=pair,
            side=close_side,
            type="MARKET",
            quantity=qty_str,
            reduceOnly="true",
        )
        logger.info(f"[{pair}] ✅ Futures position closed ({close_side}): {result.get('orderId')}")

        trade_record["oco_protected"] = False
        trade_record["quantity"]      = qty
        return result