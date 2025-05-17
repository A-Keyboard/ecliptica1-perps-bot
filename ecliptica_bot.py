# ecliptica_bot.py — v0.6.15
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, suggestions, and formatted AI responses

v0.6.15
──────
• Setup wizard now uses reply buttons for all profile questions
• Quick-click options reduce typing and focus UX on trading
• Main menu buttons now actionable (Setup, Trade, Ask AI, FAQ)
• Added a Start button to kick off interactions
"""
from __future__ import annotations
import os
import json
import sqlite3
import logging
import textwrap
import time
import functools
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

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load environment
def init_env():
    load_dotenv()
    global BOT_TOKEN, REI_KEY
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    REI_KEY   = os.environ.get("REICORE_API_KEY", "").strip()

# Database
...
# (unchanged helper functions)
...

# Main and menus
INIT_MENU = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=True
)
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], ["🤖 Ask AI", "❓ FAQ"]],
    resize_keyboard=True,
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # On /start or Start button
    await update.message.reply_text("👋 Welcome! Choose an option below:", reply_markup=MAIN_MENU)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/setup | /trade | /ask | /cancel | /faq")

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ...

# Setup flow unchanged
...

# Trade and Ask flows unchanged
...

async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ...
    return TRADE_ASSET

# Set up application and handlers
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_env(); init_db(); init_assets()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    # Core commands
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Regex(r'^▶️ Start$'), start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^❓ FAQ$'), faq_cmd))
    app.add_handler(CommandHandler('faq', faq_cmd))

    # Button-driven menu
    app.add_handler(MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start))
    app.add_handler(CommandHandler('setup', setup_start))
    app.add_handler(MessageHandler(filters.Regex(r'^📊 Trade$'), trade_start))
    app.add_handler(CommandHandler('trade', trade_start))
    app.add_handler(MessageHandler(filters.Regex(r'^🤖 Ask AI$'), ask_cmd))
    app.add_handler(CommandHandler('ask', ask_cmd))

    # Setup Conversation
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler('setup', setup_start), MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start)],
            states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
            fallbacks=[CommandHandler('cancel', cancel)],
        )
    )

    # Other flows registration (trade wizard, fee handlers...)
    ...

    # Launch bot
    app.run_polling()

if __name__ == '__main__':
    main()
