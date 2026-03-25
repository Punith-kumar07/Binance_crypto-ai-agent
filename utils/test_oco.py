"""
utils/test_oco.py — Validates OCO order parameters without placing any real orders.

Tests:
  1. Connects to Binance and fetches exchange info
  2. Simulates the exact parameters executor.py would send for each pair
  3. Validates against Binance's LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL filters
  4. Reports whether OCO would succeed or fail

Usage:
  python utils/test_oco.py
"""
import os
import sys
import math

sys.path.append(os.getcwd())
import config
from binance.client import Client as BinanceClient
from loguru import logger

PAIRS = config.TRADING_PAIRS


def format_decimal(value: float) -> str:
    return f"{value:.10f}".rstrip('0').rstrip('.')


def round_price(symbol_info, price):
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            precision = max(0, -int(math.log10(tick)))
            return round(math.floor(price / tick) * tick, precision)
    return round(price, 2)


def round_quantity(symbol_info, qty):
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            precision = max(0, -int(math.log10(step)))
            return round(math.floor(qty / step) * step, precision)
    return round(qty, 5)


def get_min_notional(symbol_info):
    for f in symbol_info.get("filters", []):
        if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            return float(f.get("minNotional", f.get("minNotionalValue", 0)))
    return 0.0


def test_oco_params(client, pair, usdt_amount=5.5, stop_loss_pct=0.02, take_profit_pct=0.04):
    print(f"\n{'='*60}")
    print(f"  Testing OCO parameters for {pair}")
    print(f"{'='*60}")

    try:
        # Get current price
        ticker = client.get_symbol_ticker(symbol=pair)
        price = float(ticker["price"])
        print(f"  Current price : ${price:,.4f}")

        # Get symbol info
        symbol_info = client.get_symbol_info(pair) or {}

        # Simulate what executor would do after a BUY fill
        qty_raw = usdt_amount / price
        qty = round_quantity(symbol_info, qty_raw)
        sl_price = round_price(symbol_info, price * (1 - stop_loss_pct))
        tp_price = round_price(symbol_info, price * (1 + take_profit_pct))
        sl_limit = round_price(symbol_info, sl_price * 0.999)

        qty_str = format_decimal(qty)
        tp_str  = format_decimal(tp_price)
        sl_str  = format_decimal(sl_price)
        sl_lmt  = format_decimal(sl_limit)

        print(f"  Order size    : {qty_str} {pair.replace('USDT','')} (~${qty * price:.2f})")
        print(f"  TP price      : {tp_str}")
        print(f"  SL stop price : {sl_str}")
        print(f"  SL limit price: {sl_lmt}")

        # Validate filters
        min_notional = get_min_notional(symbol_info)
        sl_notional  = qty * sl_price
        tp_notional  = qty * tp_price

        issues = []

        # Check notional
        if sl_notional < min_notional:
            issues.append(f"SL notional ${sl_notional:.4f} < min ${min_notional}")
        if tp_notional < min_notional:
            issues.append(f"TP notional ${tp_notional:.4f} < min ${min_notional}")

        # Check OCO price logic: price > current > stopPrice > stopLimitPrice
        if tp_price <= price:
            issues.append(f"TP {tp_price} must be above current price {price}")
        if sl_price >= price:
            issues.append(f"SL {sl_price} must be below current price {price}")
        if sl_limit >= sl_price:
            issues.append(f"sl_limit {sl_limit} must be below stopPrice {sl_price}")

        # Check quantity > 0
        if qty <= 0:
            issues.append(f"Quantity {qty} must be > 0")

        if issues:
            print(f"\n  ❌ WOULD FAIL — Issues found:")
            for issue in issues:
                print(f"     • {issue}")
        else:
            print(f"\n  ✅ PARAMETERS VALID — OCO should succeed")
            print(f"     Min notional: ${min_notional} | SL value: ${sl_notional:.4f} | TP value: ${tp_notional:.4f}")

    except Exception as e:
        print(f"  ❌ Error: {e}")


if __name__ == "__main__":
    logger.remove()  # Suppress logger output for clean test output
    print("\n🧪 OCO Parameter Validator — No orders will be placed\n")

    client = BinanceClient(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY)

    for pair in PAIRS:
        test_oco_params(client, pair)

    print(f"\n{'='*60}")
    print("  Done. If all pairs show ✅, OCO should work on next run.")
    print(f"{'='*60}\n")
