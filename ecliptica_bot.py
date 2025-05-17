# ecliptica_bot.py — v0.6.16
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, suggestions, and formatted AI responses

v0.6.16
──────
• Added missing database and asset initialization functions
• Improved startup logic to invoke init_db and init_assets cleanly
• Minor housekeeping and docstring updates
"""
from __future__ import annotations
import os
import json
import sqlite3
import logging
import textwrap
import asyncio
import requests
from datetime import datetime, timezone
from typing import Final, List
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ───────────────────────────── configuration ────────────────────────────────── #
DB: Final[str] = "ecliptica.db"
ASSETS: List[str] = []

# Serialize REI calls across users
token_lock = asyncio.Lock()

# ───────────────────────────── environment ─────────────────────────────────── #
def init_env() -> None:
    load_dotenv()
    global BOT_TOKEN, REI_KEY
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    REI_KEY   = os.environ.get("REICORE_API_KEY", "").strip()

# ───────────────────────────── database ────────────────────────────────────── #
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS sub (uid INTEGER PRIMARY KEY, exp TEXT)")
    logging.info("Initialized database tables")

# ───────────────────────────── assets ─────────────────────────────────────── #
def init_assets() -> None:
    global ASSETS
    try:
        with open("assets.json", "r") as f:
            ASSETS = json.load(f)
    except FileNotFoundError:
        ASSETS = []
    logging.info(f"Loaded {len(ASSETS)} assets")

# ───────────────────────────── rei request ────────────────────────────────── #
async def rei_call(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
    async with token_lock:
        resp = requests.post(
            "https://api.reisearch.box/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=300
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

# ───────────────────────────── telegram callbacks ────────────────────────── #
INIT_MENU = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=True
)
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], ["🤖 Ask AI", "❓ FAQ"]],
    resize_keyboard=True,
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome! Choose an option below:",
        reply_markup=MAIN_MENU
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/setup | /trade | /ask | /cancel | /faq")

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(textwrap.dedent("""
    *Perps 101*
    • Funding: paid every 8h between longs & shorts.
    • Mark price: fair reference to avoid wicks.
    • Keep a healthy margin buffer!"""), parse_mode=ParseMode.MARKDOWN)

# (Setup, Trade, Ask flows unchanged)

# ───────────────────────────── main ─────────────────────────────────────── #
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_env()
    init_db()
    init_assets()

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    # Core menu
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Regex(r'^▶️ Start$'), start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^❓ FAQ$'), faq_cmd))
    app.add_handler(CommandHandler('faq', faq_cmd))

    # Button shortcuts
    app.add_handler(MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start))
    app.add_handler(CommandHandler('setup', setup_start))
    app.add_handler(MessageHandler(filters.Regex(r'^📊 Trade$'), trade_start))
    app.add_handler(CommandHandler('trade', trade_start))
    app.add_handler(MessageHandler(filters.Regex(r'^🤖 Ask AI$'), ask_cmd))
    app.add_handler(CommandHandler('ask', ask_cmd))

    # Conversations
    app.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler('setup', setup_start),
                MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start)
            ],
            states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
            fallbacks=[CommandHandler('cancel', cancel)]
        )
    )

    # (Other flow handlers go here)

    app.run_polling()

if __name__ == '__main__':
    main()
