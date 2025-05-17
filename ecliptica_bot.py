from __future__ import annotations

import json
import logging
import os
import sqlite3
import textwrap
from datetime import datetime, timezone
from typing import Final, Optional

from dotenv import load_dotenv
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# ───────────────────────────── configuration ────────────────────────────── #
BOT_TOKEN: Final[str] = os.environ["TELEGRAM_BOT_TOKEN"]
REI_KEY: Final[str] = os.environ["REICORE_API_KEY"]
STRIPE_TOKEN: Optional[str] = os.getenv("TELEGRAM_PROVIDER_TOKEN")
COINBASE_KEY: Optional[str] = os.getenv("COINBASE_COMMERCE_API_KEY")

DB = "ecliptica.db"
EXPIRY_DAYS = 30

QUESTS: Final[list[tuple[str, str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital", "Capital allocated (USD)"),
    ("risk", "Max loss % (e.g. 2)"),
    ("quote", "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe", "Timeframe (scalp / intraday / swing / position)"),
    ("leverage", "Leverage multiple (1 if none)"),
    ("funding", "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]

QUESTION_OPTIONS: Final[dict[str, list[str]]] = {
    "experience": ["0-3m", "3-12m", ">12m"],
    "capital": ["1k", "5k", "10k", "Custom"],
    "risk": ["1%", "2%", "3%", "5%", "Custom"],
    "quote": ["USDT", "USD-C", "BTC"],
    "timeframe": ["scalp", "intraday", "swing", "position"],
    "leverage": ["1x", "3x", "5x", "Custom"],
    "funding": ["yes", "unsure", "prefer spot"],
}

SETUP, = range(1)

# ───────────────────────────── db helpers ────────────────────────────────── #

def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS sub (uid INTEGER PRIMARY KEY, exp TEXT)"""
        )


def save_profile(uid: int, data: dict[str, str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute("REPLACE INTO profile VALUES (?,?)", (uid, json.dumps(data)))


def load_profile(uid: int) -> dict[str, str]:
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute("SELECT data FROM profile WHERE uid=?", (uid,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}


def sub_active(uid: int) -> bool:
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute("SELECT exp FROM sub WHERE uid=?", (uid,))
        row = cur.fetchone()
    return bool(row and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc))

# ───────────────────────────── rei request ───────────────────────────────── #

def rei_call(prompt: str, profile: dict[str, str]) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    msgs: list[dict[str, str]] = []
    if profile:
        p_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        msgs.append({"role": "user", "content": f"Trader profile:\n{p_txt}"})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": msgs}
    r = requests.post(
        "https://api.reisearch.box/v1/chat/completions", headers=headers, json=body, timeout=300
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

# ───────────────────────────── telegram callbacks ────────────────────────── #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica Perps Assistant*!\nUse /setup then /ask <question>.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/setup – profile wizard\n/ask BTC outlook? – personalised answer\n/faq – quick perps primer"
    )

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        textwrap.dedent(
            """
    *Perps 101*\n• Funding: paid every 8h between longs & shorts.\n• Mark price: fair reference.\n• Keep a healthy margin buffer!"""
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

# setup wizard
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["i"] = 0
    ctx.user_data["ans"] = {}
    await update.message.reply_text("Let's set up your trading profile (7 questions) – /cancel anytime.")
    return await ask_next(update, ctx)

async def ask_next(update_or_q, ctx):
    i = ctx.user_data["i"]
    if i >= len(QUESTS):
        save_profile(update_or_q.effective_user.id, ctx.user_data["ans"])
        await update_or_q.message.reply_text(
            "✅ Profile saved! Now /ask your first question.", reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    key, question = QUESTS[i]
    opts = QUESTION_OPTIONS.get(key)
    if opts:
        keyboard = [[opt] for opt in opts]
        markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update_or_q.message.reply_text(f"[{i+1}/{len(QUESTS)}] {question}", reply_markup=markup)
    else:
        await update_or_q.message.reply_text(
            f"[{i+1}/{len(QUESTS)}] {question}", reply_markup=ReplyKeyboardRemove()
        )
    return SETUP

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = QUESTS[ctx.user_data["i"]][0]
    ctx.user_data["ans"][key] = update.message.text.strip()
    ctx.user_data["i"] += 1
    return await ask_next(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# /ask handler
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args) or "Give me a market outlook."
    await update.message.reply_text("🧠 Analyzing market trends…")
    prof = load_profile(update.effective_user.id)
    answer = await ctx.application.run_in_executor(None, functools.partial(rei_call, q, prof))
    await update.message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)

# main

def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("faq", faq_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))

    wizard = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_start)],
        states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(wizard)

    app.run_polling()

if __name__ == "__main__":
    main()
