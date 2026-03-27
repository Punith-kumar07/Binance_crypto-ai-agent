"""
notifications/telegram.py — Telegram trade alert bot with interactive controls.

Features:
  - Startup / trade-open / trade-close alerts
  - Live 20-second PnL updates on the trade-open message (edits in-place)
  - Inline "Close Position" button — works for both live futures and dry-run
  - Background callback poller (long-polling, no webhook needed)
  - Commands: /status, /balance, /pause, /resume
  - Daily loss limit halt alert
  - USDT PnL shown on trade close

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
import subprocess
import os
import signal
import requests
from datetime import datetime, timezone
from collections import deque
from loguru import logger
import config

_BASE    = "https://api.telegram.org/bot{token}/{method}"
_ENABLED = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)

# ── Internal state ──────────────────────────────────────────────────────────
_monitors: dict  = {}   # pair → {message_id, stop_event, side, entry, ...}
_poll_offset: int = 0
_poll_thread: threading.Thread | None = None
_bc = None              # cached Binance client

_paused      = False
_paused_lock = threading.Lock()

# ── Agent subprocess management ──────────────────────────────────────────
_agent_proc: subprocess.Popen | None = None
_agent_lock = threading.Lock()
_agent_log  = deque(maxlen=50)          # last 50 lines of agent stdout/stderr
_log_thread: threading.Thread | None = None
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Agent subprocess helpers ─────────────────────────────────────────────────

def _is_agent_running() -> bool:
    with _agent_lock:
        return _agent_proc is not None and _agent_proc.poll() is None


def _agent_log_reader(proc: subprocess.Popen):
    """Background thread that reads agent stdout/stderr into _agent_log, prints to terminal, and detects crashes."""
    import sys as _sys
    try:
        for line in iter(proc.stdout.readline, ""):
            if line:
                stripped = line.rstrip()
                _agent_log.append(stripped)
                print(stripped, file=_sys.stdout, flush=True)
    except Exception:
        pass
    # Stream ended — check if process crashed
    if proc.poll() is not None and proc.returncode != 0:
        _send(
            f"🚨 <b>Agent CRASHED</b>\n"
            f"Exit code: <code>{proc.returncode}</code>\n\n"
            f"Last log lines:\n{_get_recent_logs(5)}\n\n"
            f"Send /run to restart or /logs for more details."
        )
    elif proc.poll() is not None and proc.returncode == 0:
        _send("ℹ️ <b>Agent stopped</b> (exit code 0).")


def _start_agent() -> str:
    """Launch main.py as a subprocess. Returns status message."""
    global _agent_proc, _log_thread
    with _agent_lock:
        if _agent_proc is not None and _agent_proc.poll() is None:
            return "⚠️ Agent is already running (PID {}).".format(_agent_proc.pid)
        import sys
        _agent_log.clear()
        _agent_proc = subprocess.Popen(
            [sys.executable, os.path.join(_PROJECT_DIR, "main.py")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=_PROJECT_DIR,
        )
        _log_thread = threading.Thread(
            target=_agent_log_reader, args=(_agent_proc,),
            daemon=True, name="agent-log-reader",
        )
        _log_thread.start()
        return "✅ Agent started (PID {}).".format(_agent_proc.pid)


def _stop_agent() -> str:
    """Terminate the running agent subprocess. Returns status message."""
    global _agent_proc
    with _agent_lock:
        if _agent_proc is None or _agent_proc.poll() is not None:
            _agent_proc = None
            return "⚠️ Agent is not running."
        pid = _agent_proc.pid
        _agent_proc.terminate()
        try:
            _agent_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _agent_proc.kill()
            _agent_proc.wait(timeout=5)
        _agent_proc = None
        return "🛑 Agent terminated (PID {}).".format(pid)


def _restart_agent() -> str:
    """Terminate (if running) then start the agent. Returns status message."""
    stop_msg = ""
    if _is_agent_running():
        stop_msg = _stop_agent() + "\n"
    start_msg = _start_agent()
    return stop_msg + "🔄 " + start_msg


def _build_positions_text() -> tuple[str, dict | None]:
    """Build a lightweight positions-only message with close buttons.
    Returns (html_text, reply_markup_dict_or_None)."""
    lev = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
    close_buttons = []
    lines = ["📂 <b>Open Positions</b>", ""]

    try:
        c = _binance()
        if config.TRADE_MODE == "futures":
            all_pos = [p for p in c.futures_position_information()
                       if float(p.get("positionAmt", 0)) != 0]
        else:
            all_pos = []

        if all_pos:
            for p in all_pos:
                sym       = p["symbol"]
                amt       = float(p["positionAmt"])
                entry     = float(p["entryPrice"])
                mark      = float(p["markPrice"])
                upnl      = float(p["unRealizedProfit"])
                dir_label = "LONG" if amt > 0 else "SHORT"
                pnl_pct   = (mark - entry) / entry * 100 * lev if amt > 0 \
                             else (entry - mark) / entry * 100 * lev
                pnl_icon  = "🟢" if upnl >= 0 else "🔴"
                lines.append(
                    f"{pnl_icon} <b>{sym}</b>  {dir_label}  {lev}x\n"
                    f"   Entry: <code>${entry:,.4f}</code>\n"
                    f"   Mark:  <code>${mark:,.4f}</code>\n"
                    f"   PnL:   <b>{pnl_pct:+.2f}%</b>  (<b>{upnl:+.3f} USDT</b>)"
                )
                lines.append("")
                close_buttons.append(
                    [{"text": f"🔴 Close {sym} ({pnl_pct:+.1f}%)", "callback_data": f"close:{sym}"}]
                )
        else:
            lines.append("<i>No open positions.</i>")
    except Exception as e:
        lines.append(f"<i>Could not fetch positions: {e}</i>")

    keyboard = {"inline_keyboard": close_buttons} if close_buttons else None
    return "\n".join(lines), keyboard


def _get_recent_logs(n: int = 20) -> str:
    """Return the last N lines of agent output."""
    lines = list(_agent_log)[-n:]
    if not lines:
        return "<i>No log output yet.</i>"
    # Escape HTML entities and truncate long lines
    escaped = []
    for ln in lines:
        ln = ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if len(ln) > 200:
            ln = ln[:200] + "…"
        escaped.append(ln)
    return "<pre>" + "\n".join(escaped) + "</pre>"


# ── Pause / resume (checked by main.py each cycle) ─────────────────────────

def is_paused() -> bool:
    with _paused_lock:
        return _paused


def _set_paused(state: bool):
    global _paused
    with _paused_lock:
        _paused = state


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
        pnl_usdt = pnl_pct / 100 * usdt_amount
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text += (
            f"\n\n{pnl_icon} <b>Mark:</b>  <code>${current_price:,.4f}</code>"
            f"   PnL: <b>{pnl_pct:+.2f}%</b>  (<b>{pnl_usdt:+.3f} USDT</b>)\n"
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


def _fetch_balance() -> float | None:
    """Fetch available USDT balance from Binance futures or spot wallet."""
    try:
        c = _binance()
        if config.TRADE_MODE == "futures":
            for asset in c.futures_account_balance():
                if asset["asset"] == "USDT":
                    return float(asset["availableBalance"])
        else:
            info = c.get_asset_balance(asset="USDT")
            return float(info["free"]) if info else None
    except Exception as e:
        logger.debug(f"[Telegram] balance fetch error: {e}")
    return None


# ── Status builder ──────────────────────────────────────────────────────────

def _build_status_text() -> tuple[str, dict | None]:
    """Build status message text and an inline keyboard with Close buttons.
    Returns (html_text, reply_markup_dict_or_None)."""
    lev      = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
    mode     = config.TRADE_MODE.upper()
    dry_tag  = "🧪 DRY RUN" if config.DRY_RUN else "🔴 LIVE"
    paused   = is_paused()

    lines = [
        f"📊 <b>Agent Status</b>",
        f"Mode:    <b>{mode} {lev}x</b>  {dry_tag}",
        f"State:   <b>{'⏸ PAUSED' if paused else '▶️ RUNNING'}</b>",
        "",
    ]

    # Balance
    bal = _fetch_balance()
    if bal is not None:
        lines.append(f"💰 <b>Balance:</b>  <code>${bal:.2f} USDT</code>")

    # Daily PnL across all pairs
    try:
        from db import client as db
        total_pnl = sum(db.get_daily_pnl_pct(p) for p in config.TRADING_PAIRS)
        pnl_icon  = "🟢" if total_pnl >= 0 else "🔴"
        lines.append(f"{pnl_icon} <b>Daily PnL:</b>  <b>{total_pnl:+.2f}%</b>  (limit: {config.MAX_DAILY_LOSS_PCT}%)")
    except Exception:
        pass

    lines.append("")

    # Open positions — pulled directly from Binance so restarts don't miss trades
    close_buttons = []
    try:
        c = _binance()
        if config.TRADE_MODE == "futures":
            all_pos = [p for p in c.futures_position_information()
                       if float(p.get("positionAmt", 0)) != 0]
        else:
            all_pos = []

        if all_pos:
            lines.append(f"📂 <b>Open positions ({len(all_pos)}):</b>")
            for p in all_pos:
                sym       = p["symbol"]
                amt       = float(p["positionAmt"])
                entry     = float(p["entryPrice"])
                mark      = float(p["markPrice"])
                upnl      = float(p["unRealizedProfit"])
                dir_label = "LONG" if amt > 0 else "SHORT"
                lev_n     = config.FUTURES_LEVERAGE if config.TRADE_MODE == "futures" else 1
                pnl_pct   = (mark - entry) / entry * 100 * lev_n if amt > 0 \
                             else (entry - mark) / entry * 100 * lev_n
                pnl_icon  = "🟢" if upnl >= 0 else "🔴"
                lines.append("")
                lines.append(
                    f"  {pnl_icon} <b>{sym}</b>  {dir_label}\n"
                    f"     Entry: <code>${entry:,.4f}</code>\n"
                    f"     Mark:  <code>${mark:,.4f}</code>\n"
                    f"     PnL:   <b>{pnl_pct:+.2f}%</b>  (<b>{upnl:+.3f} USDT</b>)"
                )
                close_buttons.append(
                    [{"text": f"🔴 Close {sym} ({pnl_pct:+.1f}%)", "callback_data": f"close:{sym}"}]
                )
        else:
            lines.append("📂 <b>No open positions</b>")
    except Exception as e:
        lines.append(f"📂 <i>Could not fetch positions: {e}</i>")

    lines.append("")
    agent_state = "🟢 RUNNING" if _is_agent_running() else "⚪ STOPPED"
    lines.append(f"🤖 <b>Agent Process:</b>  {agent_state}")
    lines.append("")
    lines.append("<i>Commands: /run · /terminate · /logs · /pause · /resume · /balance · /status</i>")

    keyboard = {"inline_keyboard": close_buttons} if close_buttons else None
    return "\n".join(lines), keyboard


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


def stop_monitor(pair: str) -> int | None:
    """Stop the live-update thread for a pair. Returns message_id if available."""
    m = _monitors.pop(pair, None)
    if m:
        m["stop_event"].set()
        return m.get("message_id")
    return None


# ── Callback & message poller ────────────────────────────────────────────────

def _poll_loop():
    global _poll_offset
    logger.info("[Telegram] Callback poller started.")
    while True:
        try:
            updates = _get_updates(_poll_offset)
            for upd in updates:
                _poll_offset = upd["update_id"] + 1
                cb  = upd.get("callback_query")
                msg = upd.get("message")
                if cb:
                    _handle_callback(cb)
                elif msg:
                    _handle_message(msg)
        except Exception as e:
            logger.warning(f"[Telegram] Poll loop error: {e}")
            time.sleep(5)


def _handle_message(msg: dict):
    """Handle text commands sent directly to the bot."""
    text    = (msg.get("text") or "").strip().lower().split("@")[0]  # strip @botname suffix
    chat_id = str(msg.get("chat", {}).get("id", ""))

    # Only respond to our configured chat
    if chat_id != str(config.TELEGRAM_CHAT_ID):
        return

    if text == "/run":
        result = _start_agent()
        _send(f"🤖 {result}")
    elif text == "/terminate":
        result = _stop_agent()
        _send(f"🤖 {result}")
    elif text == "/restart":
        result = _restart_agent()
        _send(f"🤖 {result}")
    elif text == "/positions":
        pos_text, keyboard = _build_positions_text()
        kwargs = dict(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=pos_text,
            parse_mode="HTML",
        )
        if keyboard:
            kwargs["reply_markup"] = keyboard
        _api("sendMessage", **kwargs)
    elif text == "/logs":
        _send(f"📋 <b>Recent Agent Logs</b>\n\n{_get_recent_logs(20)}")
    elif text == "/pause":
        _set_paused(True)
        _send("⏸ <b>Agent paused.</b> No new trades will be opened.\nSend /resume to restart.")
    elif text == "/resume":
        _set_paused(False)
        _send("▶️ <b>Agent resumed.</b> Trading is active again.")
    elif text == "/balance":
        bal = _fetch_balance()
        if bal is not None:
            _send(f"💰 <b>Balance:</b>  <code>${bal:.2f} USDT</code>")
        else:
            _send("❌ Could not fetch balance — check Binance API connection.")
    elif text == "/status":
        status_text, keyboard = _build_status_text()
        kwargs = dict(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=status_text,
            parse_mode="HTML",
        )
        if keyboard:
            kwargs["reply_markup"] = keyboard
        _api("sendMessage", **kwargs)
    elif text == "/help" or text == "/start":
        _send(
            "🤖 <b>Crypto AI Agent — Commands</b>\n\n"
            "<b>Agent Control:</b>\n"
            "/run       — start the trading agent\n"
            "/terminate — stop the trading agent\n"
            "/restart   — terminate + start in one go\n"
            "/logs      — show recent agent output\n\n"
            "<b>Trading:</b>\n"
            "/status    — full status + close buttons\n"
            "/positions — open positions + close buttons\n"
            "/balance   — wallet balance\n"
            "/pause     — stop opening new trades\n"
            "/resume    — resume trading\n\n"
            "<i>Auto-alerts: You'll be notified if the agent crashes.</i>"
        )


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
    msg_id = m.get("message_id") if m else None
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
        if msg_id:
            _api("editMessageReplyMarkup",
                 chat_id=config.TELEGRAM_CHAT_ID,
                 message_id=msg_id,
                 reply_markup={"inline_keyboard": []})


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
        f"Scanning: <code>{', '.join(pairs)}</code>\n\n"
        f"<i>/status · /balance · /pause · /resume</i>"
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
    usdt_amount: float = 0.0,
):
    msg_id = stop_monitor(pair)   # kill live-update thread, get original message_id

    dir_label = "LONG" if side == "BUY" else "SHORT"
    if outcome == "win":
        icon, label = "✅", "TAKE PROFIT HIT"
    elif outcome == "loss":
        icon, label = "🔴", "STOP LOSS HIT"
    else:
        icon, label = "🟡", "MANUAL CLOSE"

    dry_tag  = " 🧪" if is_dry else ""
    pnl_usdt = pnl_pct / 100 * usdt_amount if usdt_amount else None

    pnl_line = f"PnL:    <b>{pnl_pct:+.2f}%</b>"
    if pnl_usdt is not None:
        pnl_line += f"  (<b>{pnl_usdt:+.3f} USDT</b>)"

    close_text = (
        f"{icon} <b>{pair} {dir_label} CLOSED</b>{dry_tag}\n"
        f"{label}\n"
        f"Entry:  <code>${entry:,.4f}</code>\n"
        f"Exit:   <code>${exit_price:,.4f}</code>\n"
        f"{pnl_line}"
    )

    # Remove the Close button from the original open message
    if msg_id:
        _api("editMessageReplyMarkup",
             chat_id=config.TELEGRAM_CHAT_ID,
             message_id=msg_id,
             reply_markup={"inline_keyboard": []})

    _send(close_text)


def notify_daily_limit_hit(pair: str, daily_pnl: float):
    """Alert when a pair's daily loss limit is reached. Fires once per pair per day."""
    _send(
        f"🚨 <b>Daily Loss Limit Hit — {pair}</b>\n"
        f"Daily PnL: <b>{daily_pnl:+.2f}%</b>  (limit: {config.MAX_DAILY_LOSS_PCT}%)\n"
        f"<b>{pair}</b> will not open new trades until tomorrow.\n"
        f"<i>Other pairs continue trading normally.</i>\n\n"
        f"<i>Agent resumes {pair} automatically at midnight UTC.</i>"
    )
