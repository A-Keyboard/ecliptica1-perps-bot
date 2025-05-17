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
from datetime import datetime, timezone, timedelta
from typing import Final, List, Dict
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    Message,
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
import aiohttp

# ───────────────────────────── configuration ────────────────────────────────── #
DB: Final[str] = "ecliptica.db"
ASSETS: List[str] = []

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Conversation states
SETUP, TRADE_SELECT, TRADE_ASSET, TRADE_DIRECTION = range(4)
TRADE_SUGGEST, TRADE_CUSTOM = range(5, 7)  # New conversation states
TRADE_SETUP, TRADE_ANALYSIS = range(7, 9)

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

# Add new constants
TOP_ASSETS_COUNT = 5  # Number of top volume assets to show

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

# Add function to fetch top volume assets
async def fetch_top_volume_assets() -> List[str]:
    """Fetch top volume perpetual trading pairs from a reliable API"""
    try:
        async with aiohttp.ClientSession() as session:
            # Using Binance API as an example - you can change to your preferred data source
            async with session.get('https://fapi.binance.com/fapi/v1/ticker/24hr') as resp:
                data = await resp.json()
                # Sort by volume and get top pairs
                sorted_pairs = sorted(data, key=lambda x: float(x['volume']), reverse=True)
                return [f"{p['symbol']}-PERP" for p in sorted_pairs[:TOP_ASSETS_COUNT]]
    except Exception as e:
        logging.error(f"Error fetching top assets: {e}")
        return ASSETS[:TOP_ASSETS_COUNT]  # Fallback to default assets

async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    top_assets = await fetch_top_volume_assets()
    
    # Create keyboard with top volume assets and additional options
    buttons = [
        [InlineKeyboardButton(f"📈 {asset} (Top Volume)", callback_data=f"trade:asset:{asset}")]
        for asset in top_assets
    ]
    buttons.extend([
        [InlineKeyboardButton("🎯 Get Trade Suggestion", callback_data="trade:suggest")],
        [InlineKeyboardButton("🔍 Custom Asset", callback_data="trade:custom")]
    ])
    
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        "Choose from top volume assets or get a suggestion:",
        reply_markup=markup
    )
    return TRADE_ASSET

async def handle_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        # Handle direct text input for custom asset
        asset = update.message.text.strip().upper()
        if not asset.endswith('-PERP'):
            asset = f"{asset}-PERP"
        ctx.user_data['asset'] = asset
        return await provide_trade_options(update.message, ctx)
        
    await query.answer()
    
    if ":" not in query.data:
        await query.message.reply_text("Invalid selection. Please try again.")
        return ConversationHandler.END
        
    parts = query.data.split(":")
    action = parts[1]
    
    if action == "suggest":
        return await provide_trade_suggestion(query.message, ctx)
    elif action == "custom":
        await query.message.reply_text(
            "Enter asset symbol (e.g. BTC, ETH):\nI'll add -PERP automatically."
        )
        return TRADE_CUSTOM
    elif action == "asset":
        ctx.user_data['asset'] = parts[2]
        return await provide_trade_options(query.message, ctx)
    elif action == "setup":
        return await generate_trade_setup(query.message, ctx)
    elif action == "analysis":
        return await provide_market_analysis(query.message, ctx)

async def provide_trade_options(message: Message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    asset = ctx.user_data.get('asset')
    buttons = [
        [InlineKeyboardButton("📊 Get Trade Setup", callback_data=f"trade:setup")],
        [InlineKeyboardButton("📈 Market Analysis", callback_data=f"trade:analysis")],
    ]
    markup = InlineKeyboardMarkup(buttons)
    await message.reply_text(
        f"What would you like to know about {asset}?",
        reply_markup=markup
    )
    return TRADE_SETUP

async def generate_trade_setup(message: Message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    asset = ctx.user_data.get('asset')
    
    # Get user profile for personalized setup
    with sqlite3.connect(DB) as con:
        data = con.execute("SELECT data FROM profile WHERE uid=?", 
                          (message.chat.id,)).fetchone()
    
    if not data:
        await message.reply_text(
            "Please /setup your profile first for personalized trade setups."
        )
        return ConversationHandler.END
        
    profile = json.loads(data[0])
    prompt = f"""Given user profile:
    - Experience: {profile.get('experience')}
    - Risk: {profile.get('risk')}
    - Timeframe: {profile.get('timeframe')}
    - Leverage: {profile.get('leverage')}
    
    Provide a detailed trade setup for {asset} with:
    1. Current market context
    2. Entry zones with reasoning
    3. Stop loss placement
    4. Take profit targets
    5. Key levels to watch
    6. Risk management considerations
    """
    
    await message.reply_text("🧠 Analyzing market conditions...")
    setup = await rei_call(prompt)
    await message.reply_text(setup, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def provide_market_analysis(message: Message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    asset = ctx.user_data.get('asset')
    prompt = f"""Provide a comprehensive market analysis for {asset} including:
    1. Current trend and market structure
    2. Key support and resistance levels
    3. Important technical indicators
    4. Recent price action analysis
    5. Potential catalysts to watch
    """
    
    await message.reply_text("🧠 Analyzing market conditions...")
    analysis = await rei_call(prompt)
    await message.reply_text(analysis, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def provide_trade_suggestion(message: Message, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Get user profile for personalized suggestion
    with sqlite3.connect(DB) as con:
        data = con.execute("SELECT data FROM profile WHERE uid=?", 
                          (message.chat.id,)).fetchone()
    
    if not data:
        await message.reply_text(
            "Please /setup your profile first for personalized suggestions."
        )
        return ConversationHandler.END
        
    profile = json.loads(data[0])
    prompt = f"""Given user profile:
    - Experience: {profile.get('experience')}
    - Risk: {profile.get('risk')}
    - Timeframe: {profile.get('timeframe')}
    - Leverage: {profile.get('leverage')}
    
    Suggest the best trading opportunity right now with:
    1. Asset selection with detailed reasoning
    2. Entry zones with market context
    3. Stop loss placement
    4. Take profit targets
    5. Key levels to watch
    6. Risk management advice
    """
    
    await message.reply_text("🧠 Analyzing market conditions...")
    suggestion = await rei_call(prompt)
    await message.reply_text(suggestion, parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

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

    # Add to conversation handler states
    app.add_handler(
        ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex(r'^📊 Trade$'), trade_start),
                CommandHandler('trade', trade_start)
            ],
            states={
                TRADE_ASSET: [
                    CallbackQueryHandler(handle_trade, pattern=r'^trade:'),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, trade_start)
                ],
                TRADE_CUSTOM: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_trade)
                ],
                TRADE_SETUP: [
                    CallbackQueryHandler(handle_trade, pattern=r'^trade:'),
                ],
                TRADE_ANALYSIS: [
                    CallbackQueryHandler(handle_trade, pattern=r'^trade:'),
                ]
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
    )

    app.run_polling()

if __name__=='__main__':
    main()
