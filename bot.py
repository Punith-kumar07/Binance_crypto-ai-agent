"""
bot.py — Standalone Telegram bot controller.

Run this instead of main.py directly. It starts the Telegram polling loop
and lets you control the trading agent via Telegram commands:

  /run       — start main.py as a subprocess
  /terminate — stop the running agent
  /logs      — view recent agent output
  /status    — positions, balance, PnL
  /balance   — wallet balance
  /pause     — pause trading (no new trades)
  /resume    — resume trading
  /help      — list all commands

Usage:
  python bot.py
"""
import sys
import time
from loguru import logger

import config
from notifications import telegram as tg

logger.remove()
logger.add(sys.stdout, level="INFO", colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")


def main():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    logger.info("🤖 Telegram Bot Controller starting...")
    logger.info("   Send /help to your bot for available commands.")
    logger.info("   Send /run  to start the trading agent.")

    tg._send(
        "🤖 <b>Bot Controller Online</b>\n\n"
        "Send /run to start the trading agent.\n"
        "Send /help for all commands."
    )
    tg.start_polling()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("👋 Bot controller stopped.")
        # Clean up agent subprocess if running
        if tg._is_agent_running():
            logger.info("🛑 Terminating agent subprocess...")
            tg._stop_agent()


if __name__ == "__main__":
    main()
