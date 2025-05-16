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
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load environment
load_dotenv()
BOT_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN",""
).strip()
REI_KEY:   Final[str] = os.environ.get("REICORE_API_KEY",""
).strip()
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

def load_profile(uid:int)->dict[str,str]:
    with sqlite3.connect(DB) as con:
        cur=con.cursor(); cur.execute("SELECT data FROM profile WHERE uid=?",(uid,))
        row=cur.fetchone()
    return json.loads(row[0]) if row else {}

# ───────────────────────────── REI API Call ───────────────────────────────── #
def rei_call(prompt:str,profile:dict[str,str])->str:
    headers={"Authorization":f"Bearer {REI_KEY}","Content-Type":"application/json"}
    msgs=[]
    if profile:
        p_txt="\n".join(f"{k}: {v}" for k,v in profile.items())
        msgs.append({"role":"user","content":f"Trader profile:\n{p_txt}"})
    msgs.append({"role":"user","content":prompt})
    body={"model":"rei-core-chat-001","temperature":0.2,"messages":msgs}
    # retry on 5xx
    for i in range(3):
        start=time.time()
        try:
            r=requests.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,json=body,timeout=300
            )
            r.raise_for_status()
            logging.info(f"REI call success {time.time()-start:.1f}s")
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.HTTPError as e:
            code=e.response.status_code if e.response else None
            if code and 500<=code<600 and i<2:
                backoff=2**i; time.sleep(backoff)
                continue
            raise
    raise RuntimeError("REI retry failed")

# ───────────────────────────── Telegram Handlers ────────────────────────── #
async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->None:
    await update.message.reply_text(
        "👋 Welcome to *Ecliptica Perps Assistant*!\n"+
        "Use /setup, or /trade for guided signals, or /ask for free query.",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->None:
    await update.message.reply_text(
        "/setup – profile wizard\n"+
        "/trade – step-by-step signal\n"+
        "/ask – free-form query\n"+
        "/cancel – abort"
    )

# Setup wizard
async def setup_start(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->int:
    ctx.user_data.update({'i':0,'ans':{}})
    await update.message.reply_text("Let's set up your trading profile ("+
                                 f"{len(QUESTS)} questions) – /cancel anytime.")
    _,q=QUESTS[0]
    await update.message.reply_text(f"[1/{len(QUESTS)}] {q}")
    return SETUP

async def collect(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->int:
    i=ctx.user_data['i']; key,_=QUESTS[i]
    ctx.user_data['ans'][key]=update.message.text.strip(); ctx.user_data['i']=i+1
    if ctx.user_data['i']<len(QUESTS):
        _,q=QUESTS[ctx.user_data['i']]
        await update.message.reply_text(f"[{ctx.user_data['i']+1}/{len(QUESTS)}] {q}")
        return SETUP
    save_profile(update.effective_user.id,ctx.user_data['ans'])
    await update.message.reply_text("✅ Profile saved! Now /trade or /ask.")
    return ConversationHandler.END

async def cancel(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->int:
    await update.message.reply_text("Setup cancelled.")
    return ConversationHandler.END

# Guided /trade flow
async def trade_start(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->int:
    prof=load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Please run /setup first.")
        return ConversationHandler.END
    assets=["BTC","ETH","SOL","ADA","DOT"]
    kb=[[InlineKeyboardButton(a,callback_data=f"asset:{a}") for a in assets]]
    await update.message.reply_text("Select asset:",reply_markup=InlineKeyboardMarkup(kb))
    return TRADE_ASSET

async def asset_choice(query:CallbackQuery,ctx:ContextTypes.DEFAULT_TYPE)->int:
    asset=query.data.split(':',1)[1]; ctx.user_data['trade_asset']=asset
    kb=[[InlineKeyboardButton("Long",callback_data="type:long"),
         InlineKeyboardButton("Short",callback_data="type:short")]]
    await query.edit_message_text(f"Asset: {asset}\nChoose direction:",
                                  reply_markup=InlineKeyboardMarkup(kb))
    return TRADE_TYPE

async def type_choice(query:CallbackQuery,ctx:ContextTypes.DEFAULT_TYPE)->int:
    typ=query.data.split(':',1)[1]; ctx.user_data['trade_type']=typ
    kb=[[InlineKeyboardButton("Concise", callback_data="diff:concise"), InlineKeyboardButton("Detailed", callback_data="diff:detailed")]]
    await query.edit_message_text(f"Direction: {typ.upper()}\nSelect difficulty:",
                                  reply_markup=InlineKeyboardMarkup(kb))
    return TRADE_DIFF

async def diff_choice(query:CallbackQuery,ctx:ContextTypes.DEFAULT_TYPE)->int:
    diff=query.data.split(':',1)[1]
    asset=ctx.user_data['trade_asset']; typ=ctx.user_data['trade_type']
    prof=load_profile(query.from_user.id)
    # build formatted prompt
    p_txt="\n".join(f"{k}: {v}" for k,v in prof.items())
    prompt=(
        f"Trader profile:\n{p_txt}\n"+
        f"Request: {typ.upper()} {asset} signal."
        f" Format: RECOMMENDATION; ENTRY; STOP-LOSS; TAKE-PROFIT; RISK-REWARD."
        f" Response length: {'Concise' if diff=='concise' else 'Detailed'}."
    )
    await query.edit_message_text("🧠 Generating formatted signal…")
    loop=asyncio.get_running_loop()
    async with token_lock:
        ans=await loop.run_in_executor(None,functools.partial(rei_call,prompt,prof))
    prefix="🟢 LONG" if typ=='long' else "🔴 SHORT"
    await query.message.reply_text(f"{prefix} {asset}\n{ans}",parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# Free-form /ask
async def ask_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE)->None:
    prof=load_profile(update.effective_user.id)
    if not prof:
        await update.message.reply_text("⚠️ Please run /setup first."); return
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id,action=ChatAction.TYPING)
    await update.message.reply_text("🧠 Analyzing market trends…")
    q=" ".join(ctx.args) or "Give me a market outlook."
    loop=asyncio.get_running_loop()
    async with token_lock:
        ans=await loop.run_in_executor(None,functools.partial(rei_call,q,prof))
    await update.message.reply_text(ans,parse_mode=ParseMode.MARKDOWN)

# Entrypoint
def main()->None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    app=Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(CommandHandler("setup",setup_start))
    app.add_handler(CommandHandler("ask",ask_cmd))
    # setup wizard
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("setup",setup_start)],
        states={SETUP:[MessageHandler(filters.TEXT&~filters.COMMAND,collect)]},
        fallbacks=[CommandHandler("cancel",cancel)]
    ))
    # trade wizard
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("trade",trade_start)],
        states={
            TRADE_ASSET:[CallbackQueryHandler(asset_choice,pattern="^asset:")],
            TRADE_TYPE:[CallbackQueryHandler(type_choice,pattern="^type:")],
            TRADE_DIFF:[CallbackQueryHandler(diff_choice,pattern="^diff:")],
        },
        fallbacks=[CommandHandler("cancel",cancel)]
    ))
    app.run_polling()

if __name__=="__main__": main()
