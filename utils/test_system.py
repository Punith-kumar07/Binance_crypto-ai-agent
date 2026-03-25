"""
test_system.py — Full pre-flight system check.

Tests every component WITHOUT calling the Groq AI API.
Run this before switching DRY_RUN=false or going live.

Usage (from project root):
    python utils/test_system.py
    python utils/test_system.py --telegram   # also sends a Telegram test message
"""
import sys
import os
import argparse
import traceback
from datetime import datetime, timezone

# ── Ensure project root is on path (file lives in utils/, root is one up) ───────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Colour helpers (no deps) ────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
B  = "\033[94m"   # blue
DIM = "\033[2m"
RST = "\033[0m"

PASS  = f"{G}✅ PASS{RST}"
FAIL  = f"{R}❌ FAIL{RST}"
SKIP  = f"{Y}⏭  SKIP{RST}"
WARN  = f"{Y}⚠️  WARN{RST}"

_failures: list = []
_warnings: list = []


def section(title: str):
    print(f"\n{B}{'─'*60}{RST}")
    print(f"{B}  {title}{RST}")
    print(f"{B}{'─'*60}{RST}")


def ok(label: str, detail: str = ""):
    print(f"  {PASS}  {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))


def fail(label: str, detail: str = ""):
    _failures.append(f"{label}: {detail}")
    print(f"  {FAIL}  {label}" + (f"\n         {R}{detail}{RST}" if detail else ""))


def warn(label: str, detail: str = ""):
    _warnings.append(f"{label}: {detail}")
    print(f"  {WARN}  {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))


def skip(label: str, reason: str = ""):
    print(f"  {SKIP}  {label}" + (f"  {DIM}{reason}{RST}" if reason else ""))


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONFIG
# ═══════════════════════════════════════════════════════════════════════════
section("1 · Config & Environment")

try:
    import config
    config.validate()
    ok("config.validate()", "all required env vars present")
except EnvironmentError as e:
    fail("config.validate()", str(e))
    print(f"\n{R}Cannot continue — fix your .env first.{RST}\n")
    sys.exit(1)
except Exception as e:
    fail("config import", str(e))
    sys.exit(1)

print(f"       Trade mode : {config.TRADE_MODE.upper()} {config.FUTURES_LEVERAGE}x")
print(f"       Dry run    : {config.DRY_RUN}")
print(f"       Pair pool  : {len(config.TRADING_PAIRS)} pairs")
print(f"       Scan/cycle : {config.SCAN_PAIRS_PER_CYCLE}")
print(f"       Min conf   : {config.MIN_CONFIDENCE}%")
print(f"       SL / TP    : {config.STOP_LOSS_PCT*100:.2f}% / {config.TAKE_PROFIT_PCT*100:.2f}%")
print(f"       Interval   : {config.CYCLE_INTERVAL}s")

if not config.DRY_RUN:
    warn("DRY_RUN=false", "REAL orders will be placed when agent runs!")


# ═══════════════════════════════════════════════════════════════════════════
# 2. BINANCE CONNECTIVITY
# ═══════════════════════════════════════════════════════════════════════════
section("2 · Binance API")

try:
    from binance.client import Client as BinanceClient
    _bc = BinanceClient(
        config.BINANCE_API_KEY,
        config.BINANCE_SECRET_KEY,
        requests_params={"timeout": 10},
    )
    if config.BINANCE_TESTNET:
        _bc.API_URL = "https://testnet.binance.vision/api"
        print(f"       {Y}Using Testnet{RST}")

    # Server time & ping
    _bc.ping()
    server_ts = _bc.get_server_time()["serverTime"]
    local_ts  = int(datetime.now(timezone.utc).timestamp() * 1000)
    drift_ms  = abs(server_ts - local_ts)
    if drift_ms > 5000:
        warn("Clock drift", f"{drift_ms}ms — Binance may reject orders if >1000ms")
    else:
        ok("Ping + server time", f"clock drift {drift_ms}ms")

    # Balance
    if config.TRADE_MODE == "futures":
        balances = _bc.futures_account_balance()
        usdt_bal = next((float(b["availableBalance"]) for b in balances if b["asset"] == "USDT"), 0.0)
    else:
        account  = _bc.get_account()
        usdt_bal = next((float(b["free"]) for b in account["balances"] if b["asset"] == "USDT"), 0.0)

    if usdt_bal < config.MIN_ORDER_USDT:
        warn("USDT balance", f"${usdt_bal:.4f} — below MIN_ORDER_USDT (${config.MIN_ORDER_USDT}), no trades will fire")
    else:
        ok("USDT balance", f"${usdt_bal:.4f} available")

    # Spot price
    TEST_PAIR = "BTCUSDT"
    if config.TRADE_MODE == "futures":
        ticker = _bc.futures_symbol_ticker(symbol=TEST_PAIR)
    else:
        ticker = _bc.get_symbol_ticker(symbol=TEST_PAIR)
    price = float(ticker["price"])
    ok(f"Price fetch ({TEST_PAIR})", f"${price:,.2f}")

    # Order book
    book = _bc.get_order_book(symbol=TEST_PAIR, limit=5)
    best_bid = float(book["bids"][0][0])
    best_ask = float(book["asks"][0][0])
    spread_pct = (best_ask - best_bid) / best_bid * 100
    ok("Order book", f"bid={best_bid} ask={best_ask} spread={spread_pct:.4f}%")

    # Symbol info (lot-size + tick-size validation)
    if config.TRADE_MODE == "futures":
        info = _bc.futures_exchange_info()
        sym = next((s for s in info["symbols"] if s["symbol"] == TEST_PAIR), None)
    else:
        sym = _bc.get_symbol_info(TEST_PAIR)
    if sym:
        ok("Symbol info", f"{TEST_PAIR} lot-size/tick-size available")
    else:
        warn("Symbol info", f"{TEST_PAIR} not found in exchange info")

    # Verify all configured pairs exist on exchange
    if config.TRADE_MODE == "futures":
        valid_symbols = {s["symbol"] for s in info["symbols"]}
    else:
        ex_info = _bc.get_exchange_info()
        valid_symbols = {s["symbol"] for s in ex_info["symbols"]}

    missing_pairs = [p for p in config.TRADING_PAIRS if p not in valid_symbols]
    if missing_pairs:
        warn("Pair validation", f"These pairs NOT found on Binance: {missing_pairs}")
    else:
        ok("Pair validation", f"All {len(config.TRADING_PAIRS)} configured pairs exist on Binance")

    # Futures-specific: leverage check
    if config.TRADE_MODE == "futures":
        try:
            _bc.futures_change_leverage(symbol=TEST_PAIR, leverage=config.FUTURES_LEVERAGE)
            ok(f"Futures leverage", f"{config.FUTURES_LEVERAGE}x set on {TEST_PAIR}")
        except Exception as e:
            warn("Futures leverage", str(e))

except Exception as e:
    fail("Binance", traceback.format_exc().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 3. DATA COLLECTOR (no AI)
# ═══════════════════════════════════════════════════════════════════════════
section("3 · DataCollector — OHLCV + Indicators")

try:
    from data.collector import DataCollector
    dc = DataCollector()

    TEST_PAIR = config.TRADING_PAIRS[0]

    # OHLCV 1h
    df_1h = dc.get_ohlcv(TEST_PAIR, "1h", 150)
    if df_1h.empty or len(df_1h) < 100:
        fail("OHLCV 1h", f"only {len(df_1h)} candles returned")
    else:
        ok("OHLCV 1h", f"{len(df_1h)} candles, latest close={float(df_1h['close'].iloc[-1]):,.4f}")

    # OHLCV 4h
    df_4h = dc.get_ohlcv(TEST_PAIR, "4h", 100)
    if df_4h.empty or len(df_4h) < 50:
        fail("OHLCV 4h", f"only {len(df_4h)} candles returned")
    else:
        ok("OHLCV 4h", f"{len(df_4h)} candles")

    # Indicators
    ind = dc.compute_indicators(df_1h)
    required_keys = ["rsi", "macd", "ema20", "ema50", "adx", "atr_pct", "bb_position",
                     "stoch_rsi_k", "obv_trend", "vwap", "technical_score"]
    missing = [k for k in required_keys if k not in ind]
    if missing:
        fail("Indicators", f"missing keys: {missing}")
    else:
        ok("Indicators", (
            f"RSI={ind['rsi']:.1f} | ADX={ind['adx']:.1f} | "
            f"ATR%={ind['atr_pct']:.2f} | Score={ind['technical_score']} ({ind['technical_bias']})"
        ))

    # NaN check
    nan_keys = [k for k, v in ind.items() if v != v]   # NaN != NaN
    if nan_keys:
        warn("Indicator NaN values", f"{nan_keys}")
    else:
        ok("NaN check", "no NaN values in indicators")

    # Fear & Greed
    fng = dc.get_fear_greed_index()
    if fng.get("value", 50) == 50 and fng.get("label") == "Unknown":
        warn("Fear & Greed", "data unavailable (non-critical)")
    else:
        ok("Fear & Greed Index", f"{fng['value']}/100 — {fng['label']} ({fng.get('trend')})")

    # Order book imbalance
    ob = dc.get_order_book_imbalance(TEST_PAIR)
    if ob is None:
        warn("Order book", "imbalance unavailable (non-critical)")
    else:
        ok("Order book imbalance", f"{ob:+.4f} ({'buy' if ob > 0 else 'sell'} pressure)")

    # Balance fetch
    bal = dc.get_usdt_balance()
    ok("Balance via DataCollector", f"${bal:.4f}")

    # Full snapshot
    snapshot = dc.collect_all(TEST_PAIR)
    if not snapshot.get("current_price"):
        fail("Full snapshot", "current_price missing")
    else:
        ok("Full snapshot", (
            f"{TEST_PAIR} @ ${snapshot['current_price']:,.4f} | "
            f"RSI={snapshot['indicators_1h'].get('rsi','?')} | "
            f"News={len(snapshot.get('news',[]))}"
        ))

except Exception as e:
    fail("DataCollector", traceback.format_exc().splitlines()[-1])
    snapshot = None


# ═══════════════════════════════════════════════════════════════════════════
# 4. RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════
section("4 · Risk Manager — gate logic & position sizing")

try:
    from risk.manager import RiskManager, TradeOrder
    rm = RiskManager()

    # Build a minimal mock snapshot
    mock_snap = {
        "pair":          config.TRADING_PAIRS[0],
        "current_price": float(snapshot["current_price"]) if snapshot else 50000.0,
        "usdt_balance":  usdt_bal if "usdt_bal" in dir() else 20.0,
        "indicators_1h": {"atr_pct": 1.0},
    }
    mock_reasoning = {
        "direction":        "BUY",
        "confidence":       85,
        "signal_alignment": "mixed",
        "hypothesis":       "test hypothesis",
        "_reasoning_id":    None,
    }

    # Test: HOLD is rejected
    result_hold = rm.evaluate("HOLD", 85, mock_snap, mock_reasoning)
    if result_hold is not None:
        fail("HOLD gate", "should have returned None for HOLD direction")
    else:
        ok("HOLD gate", "correctly rejected")

    # Test: Low confidence is rejected
    low_conf = float(config.MIN_CONFIDENCE) - 1
    result_low = rm.evaluate("BUY", low_conf, mock_snap, mock_reasoning)
    if result_low is not None:
        fail("Low confidence gate", f"should have rejected conf={low_conf}")
    else:
        ok("Low confidence gate", f"correctly rejected at {low_conf}%")

    # Test: Contradictory signals are rejected
    mock_reasoning["signal_alignment"] = "contradictory"
    result_contr = rm.evaluate("BUY", 85, mock_snap, mock_reasoning)
    if result_contr is not None:
        fail("Contradictory signal gate", "should have rejected contradictory alignment")
    else:
        ok("Contradictory signal gate", "correctly rejected")
    mock_reasoning["signal_alignment"] = "mixed"

    # Test: Valid BUY is approved (if sufficient balance)
    if mock_snap["usdt_balance"] >= config.MIN_ORDER_USDT:
        order = rm.evaluate("BUY", 85, mock_snap, mock_reasoning)
        if order is None:
            warn("BUY approval", "rejected — may be due to low balance or existing open position")
        else:
            sl_ok = order.stop_loss_price < order.entry_price
            tp_ok = order.take_profit_price > order.entry_price
            if not sl_ok:
                fail("SL price (BUY)", f"SL={order.stop_loss_price} should be < entry={order.entry_price}")
            else:
                ok("SL price (BUY)", f"{order.stop_loss_price} < {order.entry_price} ✓")
            if not tp_ok:
                fail("TP price (BUY)", f"TP={order.take_profit_price} should be > entry={order.entry_price}")
            else:
                ok("TP price (BUY)", f"{order.take_profit_price} > {order.entry_price} ✓")
            ok("Position size", (
                f"${order.usdt_amount:.4f} USDT | qty={order.quantity:.6f} | "
                f"conf_factor applied: {order.confidence}%"
            ))

    # Test: Valid SELL (SHORT) is approved
    if mock_snap["usdt_balance"] >= config.MIN_ORDER_USDT:
        order_s = rm.evaluate("SELL", 85, mock_snap, mock_reasoning)
        if order_s is not None:
            sl_ok_s = order_s.stop_loss_price > order_s.entry_price
            tp_ok_s = order_s.take_profit_price < order_s.entry_price
            if not sl_ok_s:
                fail("SL price (SHORT)", f"SL={order_s.stop_loss_price} should be > entry={order_s.entry_price}")
            else:
                ok("SL price (SHORT)", f"{order_s.stop_loss_price} > {order_s.entry_price} ✓")
            if not tp_ok_s:
                fail("TP price (SHORT)", f"TP={order_s.take_profit_price} should be < entry={order_s.entry_price}")
            else:
                ok("TP price (SHORT)", f"{order_s.take_profit_price} < {order_s.entry_price} ✓")

except Exception as e:
    fail("RiskManager", traceback.format_exc().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 5. EXECUTOR — dry-run only, no real order placed
# ═══════════════════════════════════════════════════════════════════════════
section("5 · TradeExecutor — dry-run path only")

try:
    import config as _cfg
    if not _cfg.DRY_RUN:
        warn("Executor test", "DRY_RUN=false — skipping executor test to avoid real order placement")
    else:
        from execution.executor import TradeExecutor
        from risk.manager import TradeOrder
        ex = TradeExecutor()

        # Price rounding helpers
        test_pair = config.TRADING_PAIRS[0]
        test_price = float(snapshot["current_price"]) if snapshot else 50000.0
        rounded_price = ex._round_price(test_pair, test_price) if config.TRADE_MODE == "spot" else ex._round_price_futures(test_pair, test_price)
        ok("Price rounding", f"${test_price} → ${rounded_price}")

        # Quantity rounding
        test_qty = 0.00157
        rounded_qty = ex._round_quantity(test_pair, test_qty) if config.TRADE_MODE == "spot" else ex._round_quantity_futures(test_pair, test_qty)
        ok("Quantity rounding", f"{test_qty} → {rounded_qty}")

        # Format decimal (scientific notation protection)
        fmt = ex._format_decimal(7e-05)
        if "e" in fmt.lower():
            fail("_format_decimal", f"still scientific: {fmt}")
        else:
            ok("_format_decimal", f"7e-05 → '{fmt}' (no sci notation)")

        # Dry-run trade record — no real order placed
        mock_order = TradeOrder(
            pair              = test_pair,
            side              = "BUY",
            usdt_amount       = config.MIN_ORDER_USDT + 1,
            entry_price       = test_price,
            stop_loss_price   = round(test_price * (1 - config.STOP_LOSS_PCT), 6),
            take_profit_price = round(test_price * (1 + config.TAKE_PROFIT_PCT), 6),
            confidence        = 85,
            reasoning         = {"_reasoning_id": None},
        )
        result = ex.execute(mock_order)   # DRY_RUN=true → no real order
        if result.get("binance_order_id", "").startswith("DRY_"):
            ok("Dry-run execute", f"OrderID={result['binance_order_id']} | DB write attempted")
        else:
            fail("Dry-run execute", f"Unexpected order ID: {result.get('binance_order_id')}")

except Exception as e:
    fail("TradeExecutor", traceback.format_exc().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 6. SUPABASE / DATABASE
# ═══════════════════════════════════════════════════════════════════════════
section("6 · Supabase Database")

try:
    from db import client as db

    # Read open trades (all)
    all_open = db.get_all_open_trades()
    ok("get_all_open_trades()", f"{len(all_open)} open trade(s)")

    # Read open trades for one pair
    pair_open = db.get_open_trades(config.TRADING_PAIRS[0])
    ok(f"get_open_trades({config.TRADING_PAIRS[0]})", f"{len(pair_open)} open")

    # Read recent reasoning
    rec = db.get_recent_reasoning(config.TRADING_PAIRS[0], limit=3)
    ok("get_recent_reasoning()", f"{len(rec)} record(s)")

    # Daily PnL
    pnl = db.get_daily_pnl_pct(config.TRADING_PAIRS[0])
    ok("get_daily_pnl_pct()", f"{pnl:.2f}% today")

    # Write test: signal snapshot
    db.log_signal_snapshot("TESTUSDT", {"test_key": 1}, {"price": 0.0, "test": True})
    ok("log_signal_snapshot()", "write succeeded")

except Exception as e:
    fail("Supabase DB", traceback.format_exc().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 7. PAIR SELECTOR
# ═══════════════════════════════════════════════════════════════════════════
section("7 · PairSelector — rotation & cooldown logic")

try:
    from agents.pair_selector import PairSelector
    ps = PairSelector()

    # get_next_pairs: should return at most SCAN_PAIRS_PER_CYCLE
    selected = ps.get_next_pairs(config.SCAN_PAIRS_PER_CYCLE)
    if len(selected) == 0:
        fail("get_next_pairs", "returned empty list")
    elif len(selected) > config.SCAN_PAIRS_PER_CYCLE:
        fail("get_next_pairs", f"returned {len(selected)} > {config.SCAN_PAIRS_PER_CYCLE}")
    else:
        ok("get_next_pairs", f"selected {len(selected)}: {selected}")

    # All selected pairs must be in the configured pool
    invalid = [p for p in selected if p not in config.TRADING_PAIRS]
    if invalid:
        fail("Selected pairs in pool", f"unknown pairs: {invalid}")
    else:
        ok("Selected pairs in pool", "all valid")

    # Exclusion works
    excl = set(config.TRADING_PAIRS[:2])
    selected_excl = ps.get_next_pairs(config.SCAN_PAIRS_PER_CYCLE, exclude_pairs=excl)
    if any(p in excl for p in selected_excl):
        fail("Exclusion", f"excluded pairs appeared in selection: {excl & set(selected_excl)}")
    else:
        ok("Exclusion", f"excluded {excl}, got {selected_excl}")

    # record_outcome + cooldown applied
    test_pair_sel = selected[0]
    ps.record_outcome(test_pair_sel, "low_conf", 70.0)
    selected_after = ps.get_next_pairs(config.SCAN_PAIRS_PER_CYCLE)
    if test_pair_sel in selected_after and len(config.TRADING_PAIRS) > config.SCAN_PAIRS_PER_CYCLE:
        warn("Cooldown", f"{test_pair_sel} still selected after low_conf cooldown (may be only available pair)")
    else:
        ok("Cooldown applied", f"{test_pair_sel} moved to cooldown, not re-selected immediately")

    # Restore: clear cooldown
    ps.record_outcome(test_pair_sel, "executed", 85.0)
    ok("Cooldown clear (executed)", f"{test_pair_sel} cooldown cleared")

except Exception as e:
    fail("PairSelector", traceback.format_exc().splitlines()[-1])


# ═══════════════════════════════════════════════════════════════════════════
# 8. TELEGRAM (optional)
# ═══════════════════════════════════════════════════════════════════════════
section("8 · Telegram Notifications")

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--telegram", action="store_true")
args, _ = parser.parse_known_args()

if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
    skip("Telegram", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")
elif args.telegram:
    try:
        from notifications.telegram import _send
        resp = _send("🔧 <b>Pre-flight test</b> — Telegram delivery confirmed ✅")
        if resp and resp.get("ok"):
            ok("Telegram send", "message delivered")
        else:
            fail("Telegram send", f"API response: {resp}")
    except Exception as e:
        fail("Telegram send", traceback.format_exc().splitlines()[-1])
else:
    skip("Telegram", "pass --telegram to send a test message")


# ═══════════════════════════════════════════════════════════════════════════
# 9. REAL-MONEY SAFETY CHECKLIST
# ═══════════════════════════════════════════════════════════════════════════
section("9 · Real-Money Safety Checklist")

checks = {
    "DRY_RUN should be false for live trading":
        (not config.DRY_RUN, "currently " + ("false ✓" if not config.DRY_RUN else "true — set to false when ready")),
    "BINANCE_TESTNET should be false for real money":
        (not config.BINANCE_TESTNET, "currently " + ("false ✓" if not config.BINANCE_TESTNET else "true — testnet keys won't work on live")),
    "FUTURES_LEVERAGE ≤ 5 recommended for safety":
        (config.FUTURES_LEVERAGE <= 5, f"currently {config.FUTURES_LEVERAGE}x"),
    f"MIN_CONFIDENCE ≥ 75 for real money (current: {config.MIN_CONFIDENCE}%)":
        (config.MIN_CONFIDENCE >= 75, f"currently {config.MIN_CONFIDENCE}%"),
    f"MAX_POSITION_PCT ≤ 30% (current: {config.MAX_POSITION_PCT*100:.0f}%)":
        (config.MAX_POSITION_PCT <= 0.30, f"currently {config.MAX_POSITION_PCT*100:.0f}%"),
    f"STOP_LOSS_PCT set (current: {config.STOP_LOSS_PCT*100:.2f}%)":
        (config.STOP_LOSS_PCT > 0, "ok" if config.STOP_LOSS_PCT > 0 else "STOP LOSS IS ZERO!"),
    "GROQ_API_KEYS has at least 1 key":
        (len(config.GROQ_API_KEYS) >= 1, f"{len(config.GROQ_API_KEYS)} key(s)"),
    "SUPABASE configured":
        (bool(config.SUPABASE_URL and config.SUPABASE_KEY), "ok"),
}

for label, (condition, detail) in checks.items():
    if condition:
        ok(label, detail)
    else:
        warn(label, detail)


# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════
print(f"\n{'═'*60}")
print(f"  SUMMARY")
print(f"{'═'*60}")

if _failures:
    print(f"\n  {R}❌ {len(_failures)} test(s) FAILED:{RST}")
    for f in _failures:
        print(f"     {R}• {f}{RST}")
else:
    print(f"\n  {G}✅ All tests PASSED{RST}")

if _warnings:
    print(f"\n  {Y}⚠️  {len(_warnings)} warning(s):{RST}")
    for w in _warnings:
        print(f"     {Y}• {w}{RST}")

print()
if _failures:
    print(f"  {R}⛔  DO NOT go live until all failures are fixed.{RST}")
    sys.exit(1)
elif _warnings:
    print(f"  {Y}Review warnings before going live with real money.{RST}")
    sys.exit(0)
else:
    print(f"  {G}🚀 System is ready. Review warnings above before enabling real money.{RST}")
    sys.exit(0)
