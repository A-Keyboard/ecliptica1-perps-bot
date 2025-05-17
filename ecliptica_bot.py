# ecliptica_bot.py — v0.6.14
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, suggestions, and formatted AI responses

v0.6.14
──────
• Setup wizard now uses reply buttons for all profile questions
• Quick-click options reduce typing and focus UX on trading
• Minor version bump
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
db_path: Final[str] = "ecliptica.db"

def init_db() -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")

def save_profile(uid: int, data: dict[str, str]) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("REPLACE INTO profile (uid, data) VALUES (?,?)", (uid, json.dumps(data)))

def load_profile(uid: int) -> dict[str, str]:
    with sqlite3.connect(db_path) as con:
        cur = con.cursor(); cur.execute("SELECT data FROM profile WHERE uid=?", (uid,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}

# Asset list
ASSETS: List[str] = []

def init_assets() -> None:
    global ASSETS
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
        r.raise_for_status()
        ASSETS = sorted(
            [s["symbol"] for s in r.json().get("symbols", []) if s.get("contractType") == "PERPETUAL"]
        )
        logging.info(f"Loaded {len(ASSETS)} assets")
    except Exception:
        ASSETS = []
        logging.exception("Asset load failed")

# Profile questions with button options
QUESTS: Final[list[tuple[str, str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital",    "Capital allocated (USD) (e.g. 1000 / 5000 / 10000)"),
    ("risk",       "Max loss % (e.g. 1 / 2 / 5)"),
    ("quote",      "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe",  "Timeframe (scalp / intraday / swing / position)"),
    ("leverage",   "Leverage multiple (1 / 3 / 5 / 10)"),
    ("funding",    "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]
SETUP, TRADE_ASSET, TRADE_TYPE, TRADE_LEN = range(4)

# Helper to send a setup question with buttons
def build_choices(text: str) -> ReplyKeyboardMarkup:
    # extract parenthesis
    if "(" in text and ")" in text:
        opts = text[text.find("(")+1:text.rfind(")")].split("/")
        rows = [[KeyboardButton(o.strip()) for o in opts[i:i+3]] for i in range(0, len(opts), 3)]
        return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    return ReplyKeyboardRemove()

# REI API call with robust error handling
def rei_call(prompt: str, profile: dict[str, str]) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    messages = []
    if profile:
        profile_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        messages.append({"role": "user", "content": f"Trader profile:\n{profile_txt}"})
    messages.append({"role": "user", "content": prompt})
    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": messages}
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=300,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            time.sleep(2 ** attempt)
    raise RuntimeError("REI API failed after 3 attempts")

# Handlers
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], ["🤖 Ask AI", "❓ FAQ"]],
    resize_keyboard=True,
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Welcome! Choose an option below:", reply_markup=MAIN_MENU)

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/setup | /trade | /ask | /cancel | /faq")

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        textwrap.dedent(
            """
*Perps 101*
• Funding: paid every 8h between longs & shorts.
• Mark price: fair reference to avoid wicks.
• Keep a healthy margin buffer!"""
        ), parse_mode=ParseMode.MARKDOWN
    )
    await update.message.reply_text("/setup | /trade | /ask | /cancel")

# Setup flow with buttons
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    ctx.user_data['i'], ctx.user_data['ans'] = 0, {}
    q = QUESTS[0][1]
    await update.message.reply_text(f"Set up your profile ({len(QUESTS)} questions) — /cancel anytime.")
    kb = build_choices(q)
    await update.message.reply_text(f"[1/{len(QUESTS)}] {q}", reply_markup=kb)
    return SETUP

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key, q = QUESTS[i]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] += 1
    if ctx.user_data['i'] < len(QUESTS):
        n = ctx.user_data['i']; qn = QUESTS[n][1]
        kb = build_choices(qn)
        await update.message.reply_text(f"[{n+1}/{len(QUESTS)}] {qn}", reply_markup=kb)
        return SETUP
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# Other flows unchanged ...

# (Remaining trade and ask handlers as before)

async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # ... existing code ...
    return TRADE_ASSET

# ...

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_env(); init_db(); init_assets()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('faq', faq_cmd))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler('setup', setup_start)],
            states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
            fallbacks=[CommandHandler('cancel', cancel)],
        )
    )
    # ... other handlers ...
    app.run_polling()

if __name__ == '__main__':
    main()
