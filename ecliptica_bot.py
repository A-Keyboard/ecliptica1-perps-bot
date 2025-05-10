# ecliptica_bot.py — v0.6.5
"""
Ecliptica Perps Assistant — minimal Telegram trading bot with 300 s timeout

v0.6.5
──────
• Increased REI API timeout to 300 s.
• Full REI response returned (no concise split).

Dependencies
    python-telegram-bot==20.7
    requests
    python-dotenv
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
import textwrap
import requests
import asyncio
import functools
from datetime import datetime, timezone
from typing import Final, Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Load environment variables
load_dotenv()
BOT_TOKEN: Final[str] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
REI_KEY: Final[str] = os.environ["REICORE_API_KEY"].strip()

DB = "ecliptica.db"
QUESTS: Final[list[tuple[str, str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital", "Capital allocated (USD)"),
    ("risk", "Max loss % (e.g. 2)"),
    ("quote", "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe", "Timeframe (scalp / intraday / swing / position)"),
    ("leverage", "Leverage multiple (1 if none)"),
    ("funding", "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]
SETUP, = range(1)

# ───────────────────────────── Database Helpers ───────────────────────────── #

def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS sub (uid INTEGER PRIMARY KEY, exp TEXT)"
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
    return bool(row) and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc)

# ───────────────────────────── REI API Call ───────────────────────────────── #

def rei_call(prompt: str, profile: dict[str, str]) -> str:
    headers = {
        "Authorization": f"Bearer {REI_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if profile:
        profile_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        messages.append({"role": "user", "content": f"Trader profile:\n{profile_txt}"})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": "rei-core-chat-001",
        "temperature": 0.2,
        "messages": messages,
    }
    response = requests.post(
        "https://api.reisearch.box/v1/chat/completions",
        headers=headers,
        json=body,
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()

# ───────────────────────────── Telegram Handlers ────────────────────────── #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica Perps Assistant*!\nUse /setup then /ask <question>.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/setup – profile wizard\n/ask BTC outlook? – quick answer\n/faq – perps primer"
    )

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        textwrap.dedent(
            """*Perps 101*\n• Funding every 8h\n• Mark price avoids wicks\n• Keep margin buffer"""
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

# Setup wizard
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data['i'] = 0
    ctx.user_data['ans'] = {}
    await update.message.reply_text("Let's set up your profile – /cancel anytime.")
    return await ask_next(update, ctx)

async def ask_next(update_or_q, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    idx = ctx.user_data['i']
    if idx >= len(QUESTS):
        save_profile(update_or_q.effective_user.id, ctx.user_data['ans'])
        await update_or_q.message.reply_text("✅ Saved! Now /ask your first question.")
        return ConversationHandler.END
    q_text = QUESTS[idx][1]
    await update_or_q.message.reply_text(f"[{idx+1}/{len(QUESTS)}] {q_text}")
    return SETUP

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key = QUESTS[i][0]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] = i + 1
    return await ask_next(update, ctx)

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# /ask handler
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:

    query = " ".join(ctx.args) or "Give me a market outlook."
    await update.message.reply_text("🧠 Analyzing market trends…")
    profile = load_profile(update.effective_user.id)

    try:
        answer = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(rei_call, query, profile),
        )
    except Exception:
        logging.exception("REI error")
        await update.message.reply_text("⚠️ REI CORE did not respond – try later.")
        return
    await update.message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)

# ───────────────────────────── Entrypoint ───────────────────────────────── #

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
