# ecliptica_bot.py — v0.6.10
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow and formatted AI responses

v0.6.10
──────
• Simplified asset selection: top-20 quick buttons + manual entry
• Users can type any symbol for full asset list
• Maintained /setup, /ask flows

Dependencies:
    python-telegram-bot==20.7
    requests
    python-dotenv
"""
from __future__ import annotations
import os, json, sqlite3, logging, textwrap, time, functools, asyncio
import requests
from datetime import datetime, timezone
from typing import Final, List
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load env
load_dotenv()
BOT_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
REI_KEY:   Final[str] = os.environ.get("REICORE_API_KEY", "").strip()
DB = "ecliptica.db"

# Global list of perpetual assets from Binance
ASSETS: List[str] = []

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

# ────────── DB Helpers ─────────────────
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")

save_profile = lambda uid, data: sqlite3.connect(DB).execute("REPLACE INTO profile (uid,data) VALUES (?,?)", (uid, json.dumps(data)))
load_profile = lambda uid: json.loads((sqlite3.connect(DB).cursor().execute("SELECT data FROM profile WHERE uid=?", (uid,)).fetchone() or ["{}"])[0])

# ────────── Load assets ────────────────
def init_assets() -> None:
    global ASSETS
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
        r.raise_for_status()
        ASSETS = sorted([s["symbol"] for s in r.json().get("symbols", []) if s.get("contractType")=="PERPETUAL"])
        logging.info(f"Loaded {len(ASSETS)} assets")
    except:
        ASSETS = []
        logging.exception("Asset load failed")

# ────────── REI call ───────────────────
def rei_call(prompt: str, profile: dict[str,str]) -> str:
    headers={"Authorization":f"Bearer {REI_KEY}","Content-Type":"application/json"}
    msgs = []
    if profile:
        profile_txt = "\n".join(f"{k}: {v}" for k,v in profile.items())
        msgs.append({"role":"user","content":f"Trader profile:\n{profile_txt}"})
    msgs.append({"role":"user","content":prompt})
    body={"model":"rei-core-chat-001","temperature":0.2,"messages":msgs}
    for i in range(3):
        try:
            r = requests.post("https://api.reisearch.box/v1/chat/completions",json=body,headers=headers,timeout=300)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            if i<2 and e.response and 500<=e.response.status_code<600:
                time.sleep(2**i)
                continue
            raise
    raise RuntimeError("REI failed")

# ────────── Handlers ───────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Use /setup to configure, /trade for signals, or /ask for ad-hoc queries.", parse_mode=ParseMode.MARKDOWN
    )
async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/setup | /trade | /ask | /cancel")

async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear(); ctx.user_data['i']=0; ctx.user_data['ans']={}
    await update.message.reply_text(f"Set up your profile ({len(QUESTS)} questions) — /cancel anytime.")
    await update.message.reply_text(f"[1/{len(QUESTS)}] {QUESTS[0][1]}")
    return SETUP
async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    i=ctx.user_data['i']; key=QUESTS[i][0]
    ctx.user_data['ans'][key]=update.message.text.strip(); ctx.user_data['i']+=1
    if ctx.user_data['i']<len(QUESTS):
        n=ctx.user_data['i']; await update.message.reply_text(f"[{n+1}/{len(QUESTS)}] {QUESTS[n][1]}")
        return SETUP
    save_profile(update.effective_user.id, ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved. Now /trade to get signals.")
    return ConversationHandler.END
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled."); return ConversationHandler.END

# Guided /trade
async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    prof=load_profile(update.effective_user.id)
    if not prof:
        return await update.message.reply_text("⚠️ Run /setup first."), ConversationHandler.END
    if not ASSETS:
        init_assets()
    top20 = ASSETS[:20]
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(sym) for sym in top20[i:i+4]] for i in range(0,20,4)] + [[KeyboardButton("Type manually")]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text("Select asset or type symbol:", reply_markup=kb)
    return TRADE_ASSET

async def asset_choice_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text=update.message.text.strip().upper()
    if text=="TYPE MANUALLY":
        await update.message.reply_text("Please enter the symbol (e.g. BTCUSDT).")
        return TRADE_ASSET
    if text not in ASSETS:
        await update.message.reply_text("Invalid symbol. Try again.")
        return TRADE_ASSET
    ctx.user_data['asset']=text
    # direction
    kb=[[InlineKeyboardButton("Long", callback_data="type:long"),InlineKeyboardButton("Short", callback_data="type:short")]]
    await update.message.reply_text(f"Asset: {text}\nChoose direction:", reply_markup=InlineKeyboardMarkup(kb))
    return TRADE_TYPE

async def type_choice(query: CallbackQuery, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await query.answer()
    choice=query.data.split(':')[1]
    ctx.user_data['type']=choice
    kb=[[InlineKeyboardButton("Concise", callback_data="len:concise"),InlineKeyboardButton("Detailed", callback_data="len:detailed")]]
    await query.edit_message_text(f"Direction: {choice.upper()}\nSelect length:", reply_markup=InlineKeyboardMarkup(kb))
    return TRADE_LEN

async def len_choice(query: CallbackQuery, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await query.answer()
    length=query.data.split(':')[1]
    asset,trade_type = ctx.user_data['asset'],ctx.user_data['type']
    prof=load_profile(query.from_user.id)
    p_txt="\n".join(f"{k}: {v}" for k,v in prof.items())
    prompt=f"Trader profile:\n{p_txt}\nSignal: {trade_type.upper()} {asset}. Format: ENTRY; STOP; TP; R:R. Length: {length}."
    await query.edit_message_text("🧠 Generating…")
    loop=asyncio.get_running_loop()
    async with token_lock:
        res=await loop.run_in_executor(None, functools.partial(rei_call,prompt,prof))
    prefix = "🟢 LONG" if trade_type=="long" else "🔴 SHORT"
    await query.message.reply_text(f"{prefix} {asset}\n{res}", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prof=load_profile(update.effective_user.id)
    if not prof:
        return await update.message.reply_text("⚠️ Run /setup first.")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await update.message.reply_text("🧠 Analyzing…")
    q=" ".join(ctx.args) or "Give me a market outlook."
    loop=asyncio.get_running_loop()
    async with token_lock:
        ans=await loop.run_in_executor(None,functools.partial(rei_call,q,prof))
    await update.message.reply_text(ans, parse_mode=ParseMode.MARKDOWN)

# Main

def main():
    logging.basicConfig(level=logging.INFO)
    init_db(); init_assets()
    app=Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setup",setup_start)],
        states={SETUP:[MessageHandler(filters.TEXT&~filters.COMMAND,collect)]},
        fallbacks=[CommandHandler("cancel",cancel)]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("trade",trade_start)],
        states={
            TRADE_ASSET:[MessageHandler(filters.TEXT&~filters.COMMAND,asset_choice_msg)],
            TRADE_TYPE:[CallbackQueryHandler(type_choice,pattern="^type:")],
            TRADE_LEN:[CallbackQueryHandler(len_choice,pattern="^len:")],
        },
        fallbacks=[CommandHandler("cancel",cancel)]
    ))
    app.add_handler(CommandHandler("ask",ask_cmd))
    app.run_polling()

if __name__=="__main__":
    main()
