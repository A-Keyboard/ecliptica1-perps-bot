# ecliptica_bot.py — v0.6.8
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow and formatted AI responses

v0.6.8
──────
• Added structured /trade wizard: select asset, direction, response length options
• Prompt enforces formatted trade output from REI
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load environment
load_dotenv()
BOT_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
REI_KEY:   Final[str] = os.environ.get("REICORE_API_KEY", "").strip()
DB = "ecliptica.db"

# Asset universe will be initialized at startup
ASSETS: list[str] = []

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
SETUP = 0
TRADE_ASSET, TRADE_TYPE, TRADE_LEN = 1, 2, 3

# ───────────────────────────── Database Helpers ───────────────────────────── #
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)"
        )

 def save_profile(uid: int, data: dict[str, str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute(
            "REPLACE INTO profile (uid, data) VALUES (?,?)",
            (uid, json.dumps(data)),
        )

 def load_profile(uid: int) -> dict[str, str]:
    with sqlite3.connect(DB) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT data FROM profile WHERE uid=?",
            (uid,)
        )
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}

# ───────────────────────────── REI API Call ───────────────────────────────── #
def rei_call(prompt: str, profile: dict[str, str]) -> str:
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    messages = []
    if profile:
        p_txt = "\n".join(f"{k}: {v}" for k, v in profile.items())
        messages.append({
            "role": "user",
            "content": f"Trader profile:\n{p_txt}"
        })
    messages.append({"role": "user", "content": prompt})
    body = {"model": "rei-core-chat-001", "temperature": 0.2, "messages": messages}

    for attempt in range(3):
        start = time.time()
        try:
            resp = requests.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=300,
            )
            resp.raise_for_status()
            elapsed = time.time() - start
            logging.info(f"REI API call succeeded in {elapsed:.1f}s")
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            code = e.response.status_code if e.response else None
            if code and 500 <= code < 600 and attempt < 2:
                backoff = 2 ** attempt
                logging.warning(
                    f"REI HTTPError {code}, retrying in {backoff}s"
                )
                time.sleep(backoff)
                continue
            raise
        except Exception:
            logging.exception("Unexpected REI error on attempt %d", attempt)
            raise
    raise RuntimeError("REI API retry failed")

# ───────────────────────────── Telegram Handlers ────────────────────────── #
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica Perps Assistant*!\n"
        "Use /setup to configure your profile, or /trade for guided signals.\n"
        "You can also use /ask for free-form queries.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/setup – profile wizard\n"
        "/trade – step-by-step signal\n"
        "/ask – free-form query\n"
        "/cancel – abort"
    )

# Setup wizard
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data['i'] = 0
    ctx.user_data['ans'] = {}
    await update.message.reply_text(
        f"Let's set up your trading profile ({len(QUESTS)} questions) – /cancel anytime."
    )
    _, question = QUESTS[0]
    await update.message.reply_text(f"[1/{len(QUESTS)}] {question}")
    return SETUP

async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i = ctx.user_data['i']
    key, _ = QUESTS[i]
    ctx.user_data['ans'][key] = update.message.text.strip()
    ctx.user_data['i'] += 1
    if ctx.user_data['i'] < len(QUESTS):
        _, q = QUESTS[ctx.user_data['i']]
        await update.message.reply_text(
            f"[{ctx.user_data['i']+1}/{len(QUESTS)}] {q}"
        )
        return SETUP
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved! Now /trade to get a signal.")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END

# Guided /trade flow
async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    prof = load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Please run /setup first.")
        return ConversationHandler.END
    # Use pre-initialized ASSETS list
    keyboard = []
    for symbol in ASSETS:
        keyboard.append([InlineKeyboardButton(symbol, callback_data=f"asset:{symbol}")])
    await update.message.reply_text("Select asset:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TRADE_ASSET

async def asset_choice(query: CallbackQuery, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    asset = query.data.split(':', 1)[1]
    ctx.user_data['trade_asset'] = asset
    kb = [[
        InlineKeyboardButton("Long", callback_data="type:long"),
        InlineKeyboardButton("Short", callback_data="type:short")
    ]]
    await query.edit_message_text(
        f"Asset: {asset}\nChoose direction:",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return TRADE_TYPE

async def type_choice(query: CallbackQuery, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    trade_type = query.data.split(':', 1)[1]
    ctx.user_data['trade_type'] = trade_type
    kb = [[
        InlineKeyboardButton("Concise", callback_data="len:concise"),
        InlineKeyboardButton("Detailed", callback_data="len:detailed")
    ]]
    await query.edit_message_text(
        f"Direction: {trade_type.upper()}\nSelect response length:",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return TRADE_LEN

async def len_choice(query: CallbackQuery, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    length = query.data.split(':', 1)[1]
    asset = ctx.user_data['trade_asset']
    trade_type = ctx.user_data['trade_type']
    prof = load_profile(query.from_user.id)
    p_txt = "\n".join(f"{k}: {v}" for k, v in prof.items())
    prompt = (
        f"Trader profile:\n{p_txt}\n"
        f"Signal: {trade_type.upper()} {asset}. "
        f"Format: RECOMMENDATION; ENTRY; STOP-LOSS; TAKE-PROFIT; RISK-REWARD. "
        f"Length: {'concise' if length=='concise' else 'detailed'}."
    )
    await query.edit_message_text("🧠 Generating signal…")
    loop = asyncio.get_running_loop()
    async with token_lock:
        result = await loop.run_in_executor(None, functools.partial(rei_call, prompt, prof))
    prefix = "🟢 LONG" if trade_type=='long' else "🔴 SHORT"
    await query.message.reply_text(f"{prefix} {asset}\n{result}", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# Free-form /ask
async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prof = load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Please run /setup first."); return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await update.message.reply_text("🧠 Analyzing market trends…")
    q = " ".join(ctx.args) or "Give me a market outlook."
    loop = asyncio.get_running_loop()
    async with token_lock:
        answer = await loop.run_in_executor(None, functools.partial(rei_call, q, prof))
    await update.message.reply_text(answer, parse_mode=ParseMode.MARKDOWN)

# Entrypoint
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    # optionally await init_assets() here if dynamic
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    # setup wizard
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("setup", setup_start)],
            states={SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect)]},
            fallbacks=[CommandHandler("cancel", cancel)]
        )
    )
    # trade wizard
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("trade", trade_start)],
            states={
                TRADE_ASSET: [CallbackQueryHandler(asset_choice, pattern="^asset:")],
                TRADE_TYPE:  [CallbackQueryHandler(type_choice, pattern="^type:")],
                TRADE_LEN:   [CallbackQueryHandler(len_choice, pattern="^len:")],
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )
    )
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.run_polling()

if __name__ == "__main__":
    main()
