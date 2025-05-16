# ecliptica_bot.py — v0.6.8
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow and formatted AI responses

v0.6.8
──────
• Added structured /trade wizard: select asset, direction, difficulty
• Prompt enforces concise trade-format output from REI
• Unstructured /ask remains for advanced queries
• 300 s timeout, retry logic, serialized REI calls

Dependencies:
    python-telegram-bot==20.7
    requests
    python-dotenv
"""
from __future__ import annotations
import os, json, sqlite3, logging, textwrap, time, functools, asyncio
import requests
from datetime import datetime, timezone
from typing import Final
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load environment
load_dotenv()
BOT_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
REI_KEY:   Final[str] = os.environ.get("REICORE_API_KEY","").strip()
DB = "ecliptica.db"

# Profile questions
QUESTS: Final[list[tuple[str,str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital",    "Capital allocated (USD)"),
    ("risk",       "Max loss % (e.g. 2)"),
    ("quote",      "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe",  "Timeframe (scalp / intraday / swing / position)"),
    ("leverage",   "Leverage multiple (1 if none)"),
    ("funding",    "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]
SETUP = 0
TRADE_ASSET, TRADE_TYPE, TRADE_DIFF = 1, 2, 3

# ───────────────────────────── Database Helpers ───────────────────────────── #
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")

def save_profile(uid:int,data:dict[str,str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute("REPLACE INTO profile VALUES(?,?)",(uid,json.dumps(data)))

...")
}]}
