"""
dashboard/app.py — Real-time trading dashboard backend.

Run from project root:
    uvicorn dashboard.app:app --host 0.0.0.0 --port 8000

Or:
    python dashboard/app.py
"""
import asyncio
import glob
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path so config/db imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
from db import client as db
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI(title="Crypto Trading Dashboard")


_binance_client: BinanceClient | None = None


def _binance() -> BinanceClient:
    global _binance_client
    if _binance_client is None:
        _binance_client = BinanceClient(
            config.BINANCE_API_KEY,
            config.BINANCE_SECRET_KEY,
            requests_params={"timeout": 10},
        )
    return _binance_client


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/status")
async def get_status():
    c = _binance()
    balance = 0.0
    positions = []

    try:
        if config.TRADE_MODE == "futures":
            for b in c.futures_account_balance():
                if b["asset"] == "USDT":
                    balance = float(b["availableBalance"])
                    break
            for pos in c.futures_position_information():
                qty = float(pos.get("positionAmt", 0))
                if qty != 0:
                    entry = float(pos["entryPrice"])
                    mark  = float(pos["markPrice"])
                    side = "LONG" if qty > 0 else "SHORT"
                    raw_pnl = (
                        (mark - entry) / entry * 100 if qty > 0
                        else (entry - mark) / entry * 100
                    ) if entry else 0
                    pnl_pct_lev = raw_pnl * config.FUTURES_LEVERAGE
                    positions.append({
                        "pair":            pos["symbol"],
                        "qty":             qty,
                        "side":            side,
                        "entry":           entry,
                        "mark":            mark,
                        "pnl_pct":         round(pnl_pct_lev, 2),
                        "unrealized_pnl":  round(float(pos["unRealizedProfit"]), 4),
                        "leverage":        config.FUTURES_LEVERAGE,
                    })
        else:
            account = c.get_account()
            for a in account["balances"]:
                if a["asset"] == "USDT":
                    balance = float(a["free"])
                    break
    except Exception:
        pass

    # Open trades from Supabase
    open_trades = []
    recent_trades = []
    try:
        sup = db.get_client()
        open_trades = (
            sup.table("trade_history")
            .select("*")
            .is_("closed_at", "null")
            .execute()
        ).data or []
        recent_trades = (
            sup.table("trade_history")
            .select("id,pair,side,entry_price,actual_exit_price,stop_loss_price,take_profit_price,pnl_pct,outcome,is_dry_run,created_at")
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        ).data or []
    except Exception:
        pass

    # Fetch current prices for dry-run open positions
    prices = {}
    try:
        dry_pairs = list({t["pair"] for t in open_trades if t.get("is_dry_run")})
        for pair in dry_pairs:
            try:
                if config.TRADE_MODE == "futures":
                    ticker = c.futures_symbol_ticker(symbol=pair)
                else:
                    ticker = c.get_symbol_ticker(symbol=pair)
                prices[pair] = float(ticker["price"])
            except Exception:
                pass
    except Exception:
        pass

    # Stats from closed real trades
    stats = {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "today_pnl": 0.0, "total_pnl": 0.0}
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        closed = (
            db.get_client().table("trade_history")
            .select("pnl_pct,outcome,closed_at,is_dry_run")
            .not_.is_("closed_at", "null")
            .eq("is_dry_run", False)
            .execute()
        ).data or []
        wins   = sum(1 for t in closed if t.get("outcome") == "win")
        losses = sum(1 for t in closed if t.get("outcome") == "loss")
        total  = len(closed)
        today_pnl = sum(float(t.get("pnl_pct") or 0) for t in closed if (t.get("closed_at") or "").startswith(today))
        total_pnl = sum(float(t.get("pnl_pct") or 0) for t in closed)
        stats = {
            "total_trades": total,
            "wins":         wins,
            "losses":       losses,
            "win_rate":     round(wins / total * 100, 1) if total else 0.0,
            "today_pnl":    round(today_pnl, 2),
            "total_pnl":    round(total_pnl, 2),
        }
    except Exception:
        pass

    # Margin in use + total unrealized PnL across open positions
    margin_used      = 0.0
    total_unrealized = 0.0
    for pos in positions:
        total_unrealized += pos.get("unrealized_pnl", 0)
        if config.TRADE_MODE == "futures":
            entry = pos.get("entry", 0)
            qty   = abs(pos.get("qty", 0))
            lev   = config.FUTURES_LEVERAGE or 1
            if entry and qty:
                margin_used += qty * entry / lev

    return {
        "balance":          round(balance, 4),
        "margin_used":      round(margin_used, 4),
        "total_unrealized": round(total_unrealized, 4),
        "positions":        positions,
        "open_trades":      open_trades,
        "recent_trades":    recent_trades,
        "prices":           prices,
        "stats":            stats,
        "cfg": {
            "trade_mode":         config.TRADE_MODE,
            "leverage":           config.FUTURES_LEVERAGE,
            "dry_run":            config.DRY_RUN,
            "pairs":              config.TRADING_PAIRS,
            "stop_loss_pct":      config.STOP_LOSS_PCT * 100,
            "take_profit_pct":    config.TAKE_PROFIT_PCT * 100,
            "min_confidence":     config.MIN_CONFIDENCE,
            "cycle_interval":     config.CYCLE_INTERVAL,
            "max_position_pct":   round(config.MAX_POSITION_PCT * 100),
            "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
        },
    }


@app.get("/api/analytics")
async def get_analytics():
    try:
        # All closed trades
        trades = (
            db.get_client().table("trade_history")
            .select("pair,side,pnl_pct,outcome,confidence,reasoning_id,is_dry_run")
            .not_.is_("closed_at", "null")
            .execute()
        ).data or []

        # Fetch reasoning records for market_regime (batch by IDs)
        regime_map = {}
        r_ids = [t["reasoning_id"] for t in trades if t.get("reasoning_id")]
        if r_ids:
            recs = (
                db.get_client().table("agent_reasoning")
                .select("id,market_regime")
                .in_("id", r_ids)
                .execute()
            ).data or []
            regime_map = {r["id"]: r.get("market_regime", "unknown") for r in recs}

        def _agg(records):
            wins   = sum(1 for r in records if r.get("outcome") == "win")
            losses = sum(1 for r in records if r.get("outcome") == "loss")
            total  = wins + losses
            pnls   = [float(r["pnl_pct"]) for r in records if r.get("pnl_pct") is not None]
            return {
                "wins":      wins,
                "losses":    losses,
                "total":     total,
                "win_rate":  round(wins / total * 100, 1) if total else None,
                "avg_pnl":   round(sum(pnls) / len(pnls), 2) if pnls else None,
                "total_pnl": round(sum(pnls), 2) if pnls else 0,
            }

        # By pair
        pairs = sorted(set(t["pair"] for t in trades))
        by_pair = {p: _agg([t for t in trades if t["pair"] == p]) for p in pairs}

        # By direction (BUY = LONG, SELL = SHORT)
        by_direction = {
            "LONG  (BUY)":  _agg([t for t in trades if t.get("side") == "BUY"]),
            "SHORT (SELL)": _agg([t for t in trades if t.get("side") == "SELL"]),
        }

        # By market regime (from linked reasoning)
        by_regime = {}
        for t in trades:
            regime = regime_map.get(t.get("reasoning_id"), "unknown")
            by_regime.setdefault(regime, []).append(t)
        by_regime = {k: _agg(v) for k, v in by_regime.items()}

        # By dry vs live
        by_mode = {
            "dry_run": _agg([t for t in trades if t.get("is_dry_run")]),
            "live":    _agg([t for t in trades if not t.get("is_dry_run")]),
        }

        return {
            "total_closed": len(trades),
            "by_pair":      by_pair,
            "by_direction": by_direction,
            "by_regime":    by_regime,
            "by_mode":      by_mode,
            "overall":      _agg(trades),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/reconcile")
async def reconcile_stale_trades():
    """
    Compares every open DB trade against live Binance positions.
    Any trade where Binance shows the position closed gets marked closed in the DB
    with correct PnL — works even when main.py is not running.
    """
    c = _binance()
    closed_trades = []
    still_open    = []

    try:
        all_open   = db.get_all_open_trades()
        real_open  = [t for t in all_open if not t.get("is_dry_run")]
        dry_open   = [t for t in all_open if  t.get("is_dry_run")]

        # ── Dry-run trades: check price to see if TP/SL hit ────────────────
        for trade in dry_open:
            pair   = trade["pair"]
            entry  = float(trade.get("entry_price") or 0)
            tp     = float(trade.get("take_profit_price") or 0)
            sl     = float(trade.get("stop_loss_price") or 0)
            side   = trade.get("side", "BUY")
            if not entry or not tp or not sl:
                continue
            try:
                if config.TRADE_MODE == "futures":
                    px = float(c.futures_symbol_ticker(symbol=pair)["price"])
                else:
                    px = float(c.get_symbol_ticker(symbol=pair)["price"])
                hit_tp = (px >= tp) if side == "BUY" else (px <= tp)
                hit_sl = (px <= sl) if side == "BUY" else (px >= sl)
                if hit_tp or hit_sl:
                    lev     = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
                    raw_pnl = ((px - entry) / entry * 100) if side == "BUY" else ((entry - px) / entry * 100)
                    pnl     = round(raw_pnl * lev, 4)
                    outcome = "win" if hit_tp else "loss"
                    db.get_client().table("trade_history").update({
                        "closed_at":         datetime.now(timezone.utc).isoformat(),
                        "actual_exit_price": px,
                        "pnl_pct":           pnl,
                        "outcome":           outcome,
                        "prediction_correct": hit_tp,
                    }).eq("id", trade["id"]).execute()
                    closed_trades.append({"pair": pair, "type": "dry", "outcome": outcome, "pnl_pct": pnl})
            except Exception:
                pass

        if not real_open:
            return {
                "success": True,
                "message": f"Reconciled {len(closed_trades)} dry-run trade(s). No real trades to check.",
                "closed": closed_trades,
                "still_open": still_open,
            }

        # ── Real trades: check Binance position ────────────────────────────
        for trade in real_open:
            pair     = trade["pair"]
            trade_id = trade["id"]
            entry    = float(trade.get("entry_price") or 0)
            side     = trade.get("side", "BUY")
            tp       = float(trade.get("take_profit_price") or 0)

            try:
                if config.TRADE_MODE == "futures":
                    positions = c.futures_position_information(symbol=pair)
                    live_qty  = sum(abs(float(p.get("positionAmt", 0))) for p in positions)

                    if live_qty > 0:
                        still_open.append({"pair": pair, "qty": live_qty})
                        continue

                    # Position closed on Binance — find actual exit price
                    exit_price = None
                    try:
                        recent = c.futures_account_trades(symbol=pair, limit=20)
                        for t in sorted(recent, key=lambda x: x.get("time", 0), reverse=True):
                            if float(t.get("realizedPnl", 0)) != 0:
                                exit_price = float(t["price"])
                                break
                    except Exception:
                        pass
                    if not exit_price:
                        exit_price = float(c.futures_symbol_ticker(symbol=pair)["price"])

                else:  # spot
                    asset = pair.replace("USDT", "")
                    acct  = c.get_account()
                    bal   = next((float(b["free"]) + float(b["locked"])
                                  for b in acct["balances"] if b["asset"] == asset), 0.0)
                    if bal * float(c.get_symbol_ticker(symbol=pair)["price"]) >= config.MIN_ORDER_USDT:
                        still_open.append({"pair": pair, "qty": bal})
                        continue
                    recent = c.get_my_trades(symbol=pair, limit=1)
                    exit_price = float(recent[0]["price"]) if recent else float(c.get_symbol_ticker(symbol=pair)["price"])

                # Compute PnL and outcome
                if entry:
                    raw_pnl = ((exit_price - entry) / entry * 100) if side == "BUY" else ((entry - exit_price) / entry * 100)
                    lev     = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
                    pnl     = round(raw_pnl * lev, 4)
                else:
                    pnl = 0.0

                if pnl > 0.05:
                    outcome = "win"
                elif pnl < -0.05:
                    outcome = "loss"
                else:
                    outcome = "win" if (side == "BUY" and exit_price >= tp) or (side == "SELL" and exit_price <= tp) else "loss"

                db.get_client().table("trade_history").update({
                    "closed_at":          datetime.now(timezone.utc).isoformat(),
                    "actual_exit_price":  exit_price,
                    "pnl_pct":            pnl,
                    "outcome":            outcome,
                    "prediction_correct": outcome == "win",
                }).eq("id", trade_id).execute()

                closed_trades.append({
                    "pair":    pair,
                    "type":    "live",
                    "outcome": outcome,
                    "pnl_pct": pnl,
                    "exit":    exit_price,
                })

            except Exception as e:
                still_open.append({"pair": pair, "error": str(e)})

        n = len(closed_trades)
        return {
            "success":    True,
            "message":    f"Closed {n} stale trade(s)" if n else "No stale trades found — all positions match Binance",
            "closed":     closed_trades,
            "still_open": still_open,
        }

    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/cancel-dry/{trade_id}")
async def cancel_dry_trade(trade_id: str):
    try:
        db.get_client().table("trade_history").update({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "outcome":   "cancelled",
            "pnl_pct":   0.0,
        }).eq("id", trade_id).execute()
        return {"success": True}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/close/{pair}")
async def close_position(pair: str):
    c = _binance()
    try:
        if config.TRADE_MODE == "futures":
            for o in c.futures_get_open_orders(symbol=pair):
                c.futures_cancel_order(symbol=pair, orderId=o["orderId"])
            qty        = 0.0
            close_side = "SELL"
            for pos in c.futures_position_information(symbol=pair):
                pos_amt = float(pos.get("positionAmt", 0))
                if pos_amt > 0:      # LONG — close with SELL
                    qty        = pos_amt
                    close_side = "SELL"
                    break
                elif pos_amt < 0:   # SHORT — close with BUY
                    qty        = abs(pos_amt)
                    close_side = "BUY"
                    break
            if qty <= 0:
                return {"success": False, "message": "No open futures position found"}
            result = c.futures_create_order(
                symbol=pair, side=close_side, type="MARKET",
                quantity=qty, reduceOnly="true",
            )
        else:
            for o in c.get_open_orders(symbol=pair):
                c.cancel_order(symbol=pair, orderId=o["orderId"])
            asset = pair.replace("USDT", "")
            qty = 0.0
            for b in c.get_account()["balances"]:
                if b["asset"] == asset:
                    qty = float(b["free"])
                    break
            if qty <= 0:
                return {"success": False, "message": "No spot balance found"}
            result = c.order_market_sell(symbol=pair, quantity=qty)

        # Fetch exit price from order fills or mark price fallback
        exit_price = None
        try:
            fills = result.get("fills", [])
            if fills:
                exit_price = float(fills[0]["price"])
            elif config.TRADE_MODE == "futures":
                exit_price = float(c.futures_symbol_ticker(symbol=pair)["price"])
            else:
                exit_price = float(c.get_symbol_ticker(symbol=pair)["price"])
        except Exception:
            pass

        # Mark closed in Supabase with exit price + PnL
        try:
            open_trades = (
                db.get_client().table("trade_history")
                .select("id,entry_price,side")
                .eq("pair", pair)
                .is_("closed_at", "null")
                .execute()
            ).data or []
            for trade in open_trades:
                entry    = float(trade.get("entry_price") or 0)
                t_side   = trade.get("side", "BUY")
                pnl_pct  = None
                if exit_price and entry:
                    raw = (
                        (exit_price - entry) / entry * 100 if t_side == "BUY"
                        else (entry - exit_price) / entry * 100
                    )
                    lev = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
                    pnl_pct = round(raw * lev, 4)
                update = {"closed_at": datetime.now(timezone.utc).isoformat(), "outcome": "manual_close"}
                if exit_price:
                    update["actual_exit_price"] = exit_price
                if pnl_pct is not None:
                    update["pnl_pct"] = pnl_pct
                db.get_client().table("trade_history").update(update).eq("id", trade["id"]).execute()
        except Exception:
            pass

        return {"success": True, "orderId": str(result.get("orderId", ""))}

    except BinanceAPIException as e:
        return {"success": False, "message": f"Binance {e.code}: {e.message}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        log_dir = Path(__file__).parent.parent / "logs"
        log_files = sorted(log_dir.glob("agent_*.log"), reverse=True)
        if not log_files:
            await websocket.send_text("[No log file yet — start python main.py first]")
            await asyncio.sleep(2)
            return

        log_file = str(log_files[0])
        with open(log_file, "r") as f:
            # Send last 100 lines on connect
            lines = f.readlines()
            for line in lines[-100:]:
                if line.strip():
                    await websocket.send_text(line.rstrip())
            # Tail new lines
            while True:
                line = f.readline()
                if line:
                    if line.strip():
                        await websocket.send_text(line.rstrip())
                else:
                    await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8000, reload=False)
