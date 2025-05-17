# ecliptica_bot.py — v0.6.13
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, suggestions, and formatted AI responses

v0.6.13
──────
• Improved REI call error handling (catch timeouts & network failures)
• User sees clear error on REI failures, no more silent hangs
• Wrapped REI executor calls in try/except to recover gracefully
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
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
        con.execute(
            "CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)"
        )

def save_profile(uid: int, data: dict[str, str]) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute(
            "REPLACE INTO profile (uid, data) VALUES (?,?)",
            (uid, json.dumps(data)),
        )

def load_profile(uid: int) -> dict[str, str]:
    with sqlite3.connect(db_path) as con:
        cur = con.cursor()
        cur.execute("SELECT data FROM profile WHERE uid=?", (uid,))
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

# Profile questions
QUESTS: Final[list[tuple[str, str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital",    "Capital allocated (USD)"),
    ("risk",       "Max loss % (e.g. 2)"),
    ("quote",      "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe",  "Timeframe (scalp / intraday / swing / position)"),
    ("leverage",   "Leverage multiple (1 if none)"),
    ("funding",    "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]
SETUP, TRADE_ASSET, TRADE_TYPE, TRADE_LEN = range(4)

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
        except requests.Timeout:
            logging.warning(f"REI timeout on attempt {attempt+1}")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response else None
            if code and 500 <= code < 600 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            logging.error(f"REI HTTPError: {e}")
        except requests.RequestException as e:
            logging.error(f"REI network error: {e}")
        time.sleep(2 ** attempt)
    raise RuntimeError("REI API failed after 3 attempts")

# Handlers

# Main menu keyboard
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], ["🤖 Ask AI", "❓ FAQ"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # send main menu
    await update.message.reply_text(
        "👋 Welcome! Choose an option below:",
        reply_markup=MAIN_MENU
    )

    await update.message.reply_text(
        "👋 Welcome! Use /setup to configure, /trade for signals, /ask for ad-hoc queries.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/setup | /trade | /ask | /cancel")

# Setup flow
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    ctx.user_data['i'] = 0
    ctx.user_data['ans'] = {}
    await update.message.reply_text(f"Set up your profile ({len(QUESTS)} questions) — /cancel anytime.")
    await update.message.reply_text(f"[1/{len(QUESTS)}] {QUESTS[0][1]}")
    return SETUP

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key = QUESTS[i][0]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] += 1
    if ctx.user_data['i'] < len(QUESTS):
        n = ctx.user_data['i']
        await update.message.reply_text(f"[{n+1}/{len(QUESTS)}] {QUESTS[n][1]}")
        return SETUP
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved. Now /trade to get signals.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# Trade flow
async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    prof = load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Run /setup first.")
        return ConversationHandler.END
    if not ASSETS:
        init_assets()
    top20 = ASSETS[:20]
    rows = [top20[i:i+4] for i in range(0, len(top20), 4)]
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(sym) for sym in row] for row in rows]
        + [[KeyboardButton("Type manually"), KeyboardButton("Suggest signal")]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text("Select asset, type symbol, or ask suggestion:", reply_markup=kb)
    return TRADE_ASSET

async def asset_choice_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    prof = load_profile(update.effective_user.id)
    if text == "SUGGEST SIGNAL":
        prompt = "Generate one high-confidence perpetual futures trade signal for me: asset, direction, entry, stop, take-profit, R:R."
        await update.message.reply_text("🧠 Suggesting signal…")
        try:
            loop = asyncio.get_running_loop()
            async with token_lock:
                res = await loop.run_in_executor(None, functools.partial(rei_call, prompt, prof))
            await update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logging.exception("REI call failed")
            await update.message.reply_text("⚠️ REI CORE error — please try again later.")
        return ConversationHandler.END
    if text == "TYPE MANUALLY":
        await update.message.reply_text("Enter symbol (e.g. BTCUSDT):")
        return TRADE_ASSET
    if text not in ASSETS:
        await update.message.reply_text("Invalid symbol. Try again.")
        return TRADE_ASSET
    ctx.user_data['asset'] = text
    kb2 = InlineKeyboardMarkup([
        [InlineKeyboardButton("Long", callback_data="type:long"), InlineKeyboardButton("Short", callback_data="type:short")]
    ])
    await update.message.reply_text(f"Asset: {text}\nChoose direction:", reply_markup=kb2)
    return TRADE_TYPE

async def type_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(':')[1]
    ctx.user_data['type'] = choice
    kb3 = InlineKeyboardMarkup([
        [InlineKeyboardButton("Concise", callback_data="len:concise"), InlineKeyboardButton("Detailed", callback_data="len:detailed")]
    ])
    await query.edit_message_text(f"Direction: {choice.upper()}\nSelect length:", reply_markup=kb3)
    return TRADE_LEN

async def len_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    length = query.data.split(':')[1]
    asset = ctx.user_data.get('asset')
    trade_type = ctx.user_data.get('type')
    prof = load_profile(query.from_user.id)
    profile_txt = "\n".join(f"{k}: {v}" for k, v in prof.items())
    prompt = (
        f"Trader profile:\n{profile_txt}\n"
        f"Signal: {trade_type.upper()} {asset}. Format: ENTRY; STOP; TP; R:R. Length: {length}."
    )
    await query.edit_message_text("🧠 Generating…")
    try:
        loop = asyncio.get_running_loop()
        async with token_lock:
            res = await loop.run_in_executor(None, functools.partial(rei_call, prompt, prof))
        prefix = "🟢 LONG" if trade_type == "long" else "🔴 SHORT"
        await query.message.reply_text(f"{prefix} {asset}\n{res}", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logging.exception("REI call failed")
        await query.message.reply_text("⚠️ REI CORE error — please try again later.")
    return ConversationHandler.END

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prof = load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Run /setup first.")
        return
    await update.message.reply_text("🧠 Analyzing…")
    q = " ".join(ctx.args) or "Give me a market outlook."
    try:
        loop = asyncio.get_running_loop()
        async with token_lock:
            ans = await loop.run_in_executor(None, functools.partial(rei_call, q, prof))
        await update.message.reply_text(ans, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logging.exception("REI call failed")
        await update.message.reply_text("⚠️ REI CORE error — please try again later.")

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_env()
    init_db()
    init_assets()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("setup", setup_start)],
            states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("trade", trade_start)],
            states={
                TRADE_ASSET: [MessageHandler(filters.TEXT & ~filters.COMMAND, asset_choice_msg)],
                TRADE_TYPE: [CallbackQueryHandler(type_choice, pattern="^type:")],
                TRADE_LEN: [CallbackQueryHandler(len_choice, pattern="^len:")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    app.add_handler(CommandHandler("ask", ask_cmd))
    # menu button mappings
    app.add_handler(MessageHandler(filters.Regex("^🔧 Setup Profile$"), setup_start))
    app.add_handler(MessageHandler(filters.Regex("^📊 Trade$"), trade_start))
    app.add_handler(MessageHandler(filters.Regex("^🤖 Ask AI$"), ask_cmd))
    app.add_handler(MessageHandler(filters.Regex("^❓ FAQ$"), faq_cmd))
    app.run_polling()

if __name__ == "__main__":
    main()
