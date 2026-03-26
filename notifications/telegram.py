"""
notifications/telegram.py — Telegram trade alert bot with interactive controls.

Features:
  - Startup / trade-open / trade-close alerts
  - Live 20-second PnL updates on the trade-open message (edits in-place)
  - Inline "Close Position" button — works for both live futures and dry-run
  - Background callback poller (long-polling, no webhook needed)

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Start a chat with your new bot (send any message)
  3. Open: https://api.telegram.org/bot<TOKEN>/getUpdates
     Find "chat":{"id": 123456789} — that is your CHAT_ID
  4. Add to .env:
       TELEGRAM_BOT_TOKEN=123456789:ABCdef...
       TELEGRAM_CHAT_ID=123456789

If token/chat_id are blank the module silently does nothing.
"""
import threading
import time
import requests
from datetime import datetime, timezone
from loguru import logger
import config

_BASE    = "https://api.telegram.org/bot{token}/{method}"
_ENABLED = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

# ── Internal state ──────────────────────────────────────────────────────────
_monitors: dict  = {}   # pair → {message_id, stop_event, side, entry, ...}
_poll_offset: int = 0
_poll_thread: threading.Thread | None = None
_bc = None              # cached Binance client


# ── Low-level API helpers ───────────────────────────────────────────────────

def _api(method: str, **kwargs) -> dict | None:
    """POST to Telegram Bot API. Returns parsed JSON or None on failure."""
    if not _ENABLED:
        return None
    try:
        resp = requests.post(
            _BASE.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
            json=kwargs,
            timeout=8,
        )
        if resp.ok:
            return resp.json()
        err = resp.text
        if "message is not modified" in err:
            return None  # silent — content unchanged between edits
        logger.warning(f"[Telegram] {method} failed: {resp.status_code} {err[:120]}")
    except Exception as e:
        logger.warning(f"[Telegram] {method} error: {e}")
    return None


def _get_updates(offset: int) -> list:
    """Long-poll getUpdates (30 s timeout). Returns list of update dicts."""
    if not _ENABLED:
        return []
    try:
        resp = requests.get(
            _BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates"),
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        if resp.ok:
            return resp.json().get("result", [])
    except Exception as e:
        logger.debug(f"[Telegram] getUpdates error: {e}")
    return []


def _send(text: str) -> dict | None:
    return _api("sendMessage",
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML")


# ── Keyboard & message builder ──────────────────────────────────────────────

def _close_keyboard(pair: str) -> dict:
    return {"inline_keyboard": [[
        {"text": f"🔴 Close {pair}", "callback_data": f"close:{pair}"}
    ]]}


def _build_trade_text(
    pair: str, side: str, entry: float, sl: float, tp: float,
    usdt_amount: float, confidence: float, is_dry: bool,
    current_price: float | None = None,
    pnl_pct: float | None = None,
    update_n: int = 0,
) -> str:
    lev       = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
    mode      = config.TRADE_MODE.upper()
    dry_tag   = " 🧪" if is_dry else " 🔴"
    dir_icon  = "📈" if side == "BUY" else "📉"
    dir_label = "LONG" if side == "BUY" else "SHORT"
    sl_pct    = abs(sl - entry) / entry * 100
    tp_pct    = abs(tp - entry) / entry * 100

    text = (
        f"{dir_icon} <b>{dir_label} {pair}</b>{dry_tag}\n"
        f"Mode:   <b>{mode} {lev}x</b>\n"
        f"Entry:  <code>${entry:,.4f}</code>\n"
        f"SL:     <code>${sl:,.4f}</code>  (-{sl_pct:.2f}% / -{sl_pct*lev:.1f}% real)\n"
        f"TP:     <code>${tp:,.4f}</code>  (+{tp_pct:.2f}% / +{tp_pct*lev:.1f}% real)\n"
        f"Margin: <b>${usdt_amount:.2f}</b>  |  Conf: <b>{confidence:.0f}%</b>"
    )

    if current_price is not None and pnl_pct is not None:
        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text += (
            f"\n\n{pnl_icon} <b>Mark:</b>  <code>${current_price:,.4f}</code>"
            f"   PnL: <b>{pnl_pct:+.2f}%</b>\n"
            f"<i>⟳ {ts}  (update #{update_n})</i>"
        )
    return text


# ── Price fetch (reuses cached Binance client) ──────────────────────────────

def _binance():
    global _bc
    if _bc is None:
        from binance.client import Client as Bc
        _bc = Bc(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY,
                 requests_params={"timeout": 10})
    return _bc


def _get_price(pair: str) -> float | None:
    try:
        c = _binance()
        if config.TRADE_MODE == "futures":
            return float(c.futures_symbol_ticker(symbol=pair)["price"])
        return float(c.get_symbol_ticker(symbol=pair)["price"])
    except Exception as e:
        logger.debug(f"[Telegram] price fetch {pair}: {e}")
        return None


# ── Position monitor (background thread per open trade) ─────────────────────

def _monitor_loop(pair: str, stop_event: threading.Event):
    n = 0
    while not stop_event.wait(20):      # fires every 20 s
        m = _monitors.get(pair)
        if not m:
            break
        price = _get_price(pair)
        if price is None:
            continue
        n += 1
        lev  = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
        pnl  = (
            (price - m["entry"]) / m["entry"] * 100 * lev if m["side"] == "BUY"
            else (m["entry"] - price) / m["entry"] * 100 * lev
        )
        text = _build_trade_text(
            pair, m["side"], m["entry"], m["sl"], m["tp"],
            m["usdt_amount"], m["confidence"], m["is_dry"],
            current_price=price, pnl_pct=pnl, update_n=n,
        )
        _api("editMessageText",
             chat_id=config.TELEGRAM_CHAT_ID,
             message_id=m["message_id"],
             text=text,
             parse_mode="HTML",
             reply_markup=_close_keyboard(pair))


def stop_monitor(pair: str):
    """Stop the live-update thread for a pair (called on trade close)."""
    m = _monitors.pop(pair, None)
    if m:
        m["stop_event"].set()


# ── Callback poller (background thread, started once on agent startup) ───────

def _poll_loop():
    global _poll_offset
    logger.info("[Telegram] Callback poller started.")
    while True:
        try:
            updates = _get_updates(_poll_offset)
            for upd in updates:
                _poll_offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if cb:
                    _handle_callback(cb)
        except Exception as e:
            logger.warning(f"[Telegram] Poll loop error: {e}")
            time.sleep(5)


def _handle_callback(cb: dict):
    cb_id = cb["id"]
    data  = cb.get("data", "")
    if not data.startswith("close:"):
        return
    pair = data.split(":", 1)[1]
    _api("answerCallbackQuery",
         callback_query_id=cb_id,
         text=f"⏳ Closing {pair}…")
    _close_via_telegram(pair)


def _close_via_telegram(pair: str):
    m      = _monitors.get(pair)
    is_dry = m.get("is_dry", False) if m else False
    try:
        if is_dry:
            from db import client as db_client
            open_rows = (
                db_client.get_client().table("trade_history")
                .select("id,entry_price,side")
                .eq("pair", pair)
                .is_("closed_at", "null")
                .execute()
            ).data or []
            current_price = _get_price(pair)
            lev = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
            for row in open_rows:
                entry = float(row.get("entry_price") or 0)
                side  = row.get("side", "BUY")
                pnl   = None
                if current_price and entry:
                    raw = (
                        (current_price - entry) / entry * 100 if side == "BUY"
                        else (entry - current_price) / entry * 100
                    )
                    pnl = round(raw * lev, 4)
                update = {
                    "closed_at":          datetime.now(timezone.utc).isoformat(),
                    "outcome":            "manual_close",
                    "actual_exit_price":  current_price,
                }
                if pnl is not None:
                    update["pnl_pct"] = pnl
                db_client.get_client().table("trade_history").update(update).eq("id", row["id"]).execute()
            _send(f"✅ <b>{pair}</b> dry-run trade closed via Telegram")
        else:
            c = _binance()
            qty, close_side = 0.0, "SELL"
            for pos in c.futures_position_information(symbol=pair):
                amt = float(pos.get("positionAmt", 0))
                if amt > 0:
                    qty, close_side = amt, "SELL"
                    break
                elif amt < 0:
                    qty, close_side = abs(amt), "BUY"
                    break
            if qty > 0:
                c.futures_create_order(
                    symbol=pair, side=close_side, type="MARKET",
                    quantity=qty, reduceOnly="true",
                )
                _send(f"✅ <b>{pair}</b> position closed via Telegram")
            else:
                _send(f"⚠️ No open position found for <b>{pair}</b>")
    except Exception as e:
        logger.error(f"[Telegram] Close {pair} via Telegram failed: {e}")
        _send(f"❌ Failed to close <b>{pair}</b>: <code>{e}</code>")
    finally:
        stop_monitor(pair)


def start_polling():
    """Start the background callback-polling thread. Safe to call multiple times."""
    global _poll_thread
    if not _ENABLED:
        return
    if _poll_thread and _poll_thread.is_alive():
        return
    _poll_thread = threading.Thread(
        target=_poll_loop, daemon=True, name="tg-poll"
    )
    _poll_thread.start()


# ── Public notification helpers ─────────────────────────────────────────────

def notify_startup(pairs: list, dry_run: bool, mode: str, leverage: int):
    dry_label = "🧪 DRY RUN" if dry_run else "🔴 LIVE"
    _send(
        f"🤖 <b>Crypto AI Agent Started</b>\n"
        f"Mode: <b>{mode.upper()} {leverage}x</b>  {dry_label}\n"
        f"Scanning: <code>{', '.join(pairs)}</code>"
    )


def notify_trade_open(
    pair: str,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    usdt_amount: float,
    confidence: float,
    is_dry: bool,
):
    if not _ENABLED:
        return

    text   = _build_trade_text(pair, side, entry, sl, tp, usdt_amount, confidence, is_dry)
    result = _api("sendMessage",
                  chat_id=config.TELEGRAM_CHAT_ID,
                  text=text,
                  parse_mode="HTML",
                  reply_markup=_close_keyboard(pair))
    if not result:
        return

    message_id = result["result"]["message_id"]
    stop_event = threading.Event()
    _monitors[pair] = {
        "message_id":  message_id,
        "stop_event":  stop_event,
        "side":        side,
        "entry":       entry,
        "sl":          sl,
        "tp":          tp,
        "usdt_amount": usdt_amount,
        "confidence":  confidence,
        "is_dry":      is_dry,
    }
    threading.Thread(
        target=_monitor_loop,
        args=(pair, stop_event),
        daemon=True,
        name=f"tg-monitor-{pair}",
    ).start()
    logger.debug(f"[Telegram] Live monitor started for {pair}")


def notify_trade_close(
    pair: str,
    side: str,
    entry: float,
    exit_price: float,
    pnl_pct: float,
    outcome: str,
    is_dry: bool,
):
    stop_monitor(pair)   # kill the live-update thread

    dir_label = "LONG" if side == "BUY" else "SHORT"
    if outcome == "win":
        icon, label = "✅", "TAKE PROFIT HIT"
    elif outcome == "loss":
        icon, label = "🔴", "STOP LOSS HIT"
    else:
        icon, label = "🟡", "MANUAL CLOSE"

    dry_tag = " 🧪" if is_dry else ""
    _send(
        f"{icon} <b>{pair} {dir_label} CLOSED</b>{dry_tag}\n"
        f"{label}\n"
        f"Entry:  <code>${entry:,.4f}</code>\n"
        f"Exit:   <code>${exit_price:,.4f}</code>\n"
        f"PnL:    <b>{pnl_pct:+.2f}%</b>"
    )
