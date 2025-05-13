# ecliptica_bot.py — v0.6.7
"""
Ecliptica Perps Assistant — minimal Telegram trading bot with 5 min timeout and retry logic

v0.6.7
──────
• Ensured proper newline handling in REI request payload
• Retry on 5xx errors, log latency
• Timeout set to 300 s (5 min)

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
import time
from datetime import datetime, timezone
from typing import Final
from dotenv import load_dotenv
from telegram import Update, ChatAction
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Ensure only one REI call at a time across all users
token_lock = asyncio.Lock()

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
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS sub (uid INTEGER PRIMARY KEY, exp TEXT)")


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
    """Call REI CORE with retry on 5xx errors and log latency."""
    headers = {
        "Authorization": f"Bearer {REI_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if profile:
        # Proper newline escape
        profile_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        messages.append({
            "role": "user",
            "content": f"Trader profile:\n{profile_txt}",
        })
    messages.append({"role": "user", "content": prompt})

    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": messages}

    # Retry logic: 2 attempts on server errors
    for attempt in range(2):
        start_ts = time.time()
        try:
            resp = requests.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=300,
            )
            resp.raise_for_status()
            elapsed = time.time() - start_ts
            logging.info(f"REI API call succeeded in {elapsed:.1f}s")
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else None
            logging.error(f"REI API HTTPError {status} attempt {attempt+1}: {e}")
            if status and 500 <= status < 600 and attempt == 0:
                time.sleep(2)
                continue
            raise
        except Exception:
            logging.exception("REI API unexpected error on attempt %d", attempt+1)
            raise
    raise RuntimeError("REI API retry failed")

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

# Setup wizard handlers omitted for brevity...

# /ask handler
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prof = load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text(
            "⚠️ Please run /setup to provide your trading profile before asking for signals."
        )
        return

    # Show typing
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    await update.message.reply_text("🧠 Analyzing market trends…")
    query = " ".join(ctx.args) or "Give me a market outlook."

    try:
            # serialize REI calls across users
            async with token_lock:
                answer = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(rei_call, query, prof)
                )
            None, functools.partial(rei_call, query, prof)
        )
    except Exception:
        logging.exception("REI error")
        await update.message.reply_text("⚠️ REI CORE did not respond – try later.")
        return

    await update.message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)

# Entrypoint omitted for brevity...
