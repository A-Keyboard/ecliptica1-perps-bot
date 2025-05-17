# ecliptica_bot.py — v0.6.18
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, interactive setup via buttons, suggestions, and formatted AI responses

v0.6.18
──────
• Restored interactive buttons for setup questions
• Added InlineKeyboardMarkup in ask_next/handle_setup
"""
from __future__ import annotations
import os, json, sqlite3, logging, textwrap, asyncio, requests
from datetime import datetime, timezone
from typing import Final, List, Dict
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

# Conversation states
SETUP, TRADE_SELECT, TRADE_ASSET, TRADE_DIRECTION = range(4)

# Setup questions + options
QUESTS: Final[List[tuple[str, str]]] = [
    ("experience", "Your perps experience?"),
    ("capital",    "Capital allocated (USD)"),
    ("risk",       "Max loss % (e.g. 2)"),
    ("quote",      "Quote currency"),
    ("timeframe",  "Timeframe"),
    ("leverage",   "Leverage multiple"),
    ("funding",    "Comfort paying funding 8h?"),
]
OPTIONS: Final[Dict[str, List[str]]] = {
    "experience": ["0-3m", "3-12m", ">12m"],
    "capital":    ["1k", "5k", "10k+"],
    "risk":       ["1%", "2%", "5%"],
    "quote":      ["USDT", "USD-C", "BTC"],
    "timeframe":  ["scalp", "intraday", "swing", "position"],
    "leverage":   ["1x", "3x", "5x", "10x"],
    "funding":    ["yes", "unsure", "prefer spot"],
}

# ───────────────────────────── environment ─────────────────────────────────── #
def init_env() -> None:
    load_dotenv()
    global BOT_TOKEN, REI_KEY
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    REI_KEY   = os.environ.get("REICORE_API_KEY",     "").strip()

# ───────────────────────────── database ────────────────────────────────────── #
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS sub     (uid INTEGER PRIMARY KEY, exp  TEXT)")
    logging.info("Initialized database tables")

# ───────────────────────────── assets ─────────────────────────────────────── #
def init_assets() -> None:
    global ASSETS
    try:
        with open("assets.json","r") as f:
            ASSETS = json.load(f)
    except FileNotFoundError:
        ASSETS = []
    logging.info(f"Loaded {len(ASSETS)} assets")

# ───────────────────────────── rei request ────────────────────────────────── #
async def rei_call(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    body = {"model":"rei-core-chat-001","temperature":0.2,
            "messages":[{"role":"user","content":prompt}]}
    async with token_lock:
        resp = requests.post(
            "https://api.reisearch.box/v1/chat/completions",
            headers=headers, json=body, timeout=300
        )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

# ───────────────────────────── telegram callbacks ────────────────────────── #
INIT_MENU = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=True
)
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile","📊 Trade"], ["🤖 Ask AI","❓ FAQ"]],
    resize_keyboard=True
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome! Press ▶️ Start to begin.",
        reply_markup=INIT_MENU
    )

async def main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome! Choose an option below:",
        reply_markup=MAIN_MENU
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/setup | /trade | /ask | /cancel | /faq")

async def faq_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(textwrap.dedent(
        """
        *Perps 101*
        • Funding: paid every 8h.
        • Mark price: fair reference.
        • Keep a healthy margin buffer!"""), parse_mode=ParseMode.MARKDOWN)

# ─── Setup wizard with buttons ───────────────────────────────────────────── #
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear(); ctx.user_data["i"]=0; ctx.user_data["ans"]={}
    await update.message.reply_text(
        "Let's set up your profile — /cancel anytime.",
        reply_markup=ReplyKeyboardRemove()
    )
    return await ask_next(update, ctx)

async def ask_next(update_or_query, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data["i"]
    if i >= len(QUESTS):
        data = json.dumps(ctx.user_data["ans"])
        uid = update_or_query.effective_chat.id if hasattr(update_or_query, 'effective_chat') else update_or_query.message.chat.id
        with sqlite3.connect(DB) as con:
            con.execute("REPLACE INTO profile VALUES (?,?)", (uid, data))
        await update_or_query.message.reply_text("✅ Profile saved!", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    key, question = QUESTS[i]
    buttons = [[InlineKeyboardButton(opt, callback_data=f"setup:{key}:{opt}")]
               for opt in OPTIONS[key]]
    markup = InlineKeyboardMarkup(buttons)
    await update_or_query.message.reply_text(f"[{i+1}/{len(QUESTS)}] {question}", reply_markup=markup)
    return SETUP

async def handle_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        if not query:
            return SETUP
            
        logging.info(f"Received callback data: {query.data}")
        
        if ":" not in query.data:
            await query.answer("Invalid callback data")
            return SETUP
            
        data = query.data.split(":")
        if len(data) != 3:
            await query.answer("Invalid callback format")
            return SETUP
            
        _, key, value = data
        ctx.user_data["ans"][key] = value
        ctx.user_data["i"] += 1
        
        await query.answer(f"Selected: {value}")
        
        # Check if this was the last question
        if ctx.user_data["i"] >= len(QUESTS):
            data = json.dumps(ctx.user_data["ans"])
            with sqlite3.connect(DB) as con:
                con.execute("REPLACE INTO profile VALUES (?,?)", (query.from_user.id, data))
            await query.message.reply_text("✅ Profile saved!", reply_markup=MAIN_MENU)
            return ConversationHandler.END
            
        return await ask_next(query, ctx)
        
    except Exception as e:
        logging.error(f"Error in handle_setup: {str(e)}")
        if update.callback_query:
            await update.callback_query.answer("An error occurred")
        return SETUP

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# ─── Ask AI / Trade flows omitted ─────────────────────────────────────────── #
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = update.message.text.replace("/ask","",1).strip() or "Give me a market outlook."
    await update.message.reply_text("🧠 Analyzing market trends…")
    ans = await rei_call(prompt)
    await update.message.reply_text(ans, parse_mode=ParseMode.MARKDOWN)

async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Select asset or type symbol, or ask suggestion:",
        reply_markup=ReplyKeyboardRemove()
    )
    return TRADE_ASSET

# ───────────────────────────── main ─────────────────────────────────────── #
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_env(); init_db(); init_assets()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    # Start & main menu
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.Regex(r'^▶️ Start$'), main_menu))

    # Main menu buttons
    app.add_handler(MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start))
    app.add_handler(MessageHandler(filters.Regex(r'^📊 Trade$'), trade_start))
    app.add_handler(MessageHandler(filters.Regex(r'^🤖 Ask AI$'), ask_cmd))
    app.add_handler(MessageHandler(filters.Regex(r'^❓ FAQ$'), faq_cmd))
    app.add_handler(CommandHandler('setup', setup_start))
    app.add_handler(CommandHandler('trade', trade_start))
    app.add_handler(CommandHandler('ask', ask_cmd))
    app.add_handler(CommandHandler('faq', faq_cmd))
    app.add_handler(CommandHandler('help', help_cmd))

    # Setup conversation
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(r'^🔧 Setup Profile$'), setup_start),
                         CommandHandler('setup', setup_start)],
            states={
                SETUP: [
                    CallbackQueryHandler(handle_setup),  # Remove pattern restriction to catch all callbacks
                    MessageHandler(filters.TEXT & ~filters.COMMAND, setup_start)
                ]
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
    )

    # Add a general callback query handler for debugging
    app.add_handler(CallbackQueryHandler(handle_setup))

    app.run_polling()

if __name__=='__main__':
    main()
