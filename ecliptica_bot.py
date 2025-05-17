# ecliptica_bot.py — v0.6.19
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow, interactive setup via buttons, suggestions, and formatted AI responses

v0.6.19
──────
• Switched from SQLite to PostgreSQL for Railway deployment
• Added async database operations
• Improved error handling for database operations
"""
from __future__ import annotations
import os, json, logging, textwrap, asyncio, requests
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
import asyncpg

# Set up logging first
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ───────────────────────────── configuration ────────────────────────────────── #
ASSETS: List[str] = []

# Database pool
db_pool = None

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Conversation states
(SETUP, SELECTING_ASSET, ANALYZING_ASSET, TRADING) = range(4)

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
    REI_KEY = os.environ.get("REICORE_API_KEY", "").strip()
    
    # Log environment status (without exposing sensitive data)
    logger.info("Environment initialization:")
    logger.info(f"BOT_TOKEN present: {bool(BOT_TOKEN)}")
    logger.info(f"REI_KEY present: {bool(REI_KEY)}")
    
    if not BOT_TOKEN:
        raise Exception("TELEGRAM_BOT_TOKEN not set")
    if not REI_KEY:
        raise Exception("REICORE_API_KEY not set")

# ───────────────────────────── database ────────────────────────────────────── #
async def init_db() -> None:
    """Initialize PostgreSQL database connection pool"""
    global db_pool
    try:
        # Try different possible URL environment variables
        database_url = (
            os.environ.get('POSTGRES_URL') or 
            os.environ.get('DATABASE_URL') or
            os.environ.get('POSTGRESQL_URL')
        )
        logger.info("Attempting database connection...")
        
        if not database_url:
            logger.error("No database URL found in environment variables!")
            logger.error("Available environment variables: " + ", ".join(os.environ.keys()))
            raise Exception("Database URL not set")
            
        # Create connection pool
        logger.info("Creating database pool...")
        try:
            db_pool = await asyncpg.create_pool(database_url)
            logger.info("Database pool created successfully")
        except Exception as e:
            logger.error(f"Failed to create pool with error: {str(e)}")
            # Try with ssl mode disable if first attempt failed
            if 'ssl' not in database_url.lower():
                logger.info("Retrying with sslmode=disable...")
                if '?' in database_url:
                    database_url += '&sslmode=disable'
                else:
                    database_url += '?sslmode=disable'
                db_pool = await asyncpg.create_pool(database_url)
                logger.info("Database pool created successfully with sslmode=disable")
        
        # Create tables if they don't exist
        async with db_pool.acquire() as conn:
            logger.info("Creating tables if they don't exist...")
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS profile (
                    uid BIGINT PRIMARY KEY,
                    data JSONB
                )
            ''')
            # Verify table was created
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'profile')"
            )
            if table_exists:
                logger.info("Profile table exists and is ready")
                # Count existing profiles
                count = await conn.fetchval('SELECT COUNT(*) FROM profile')
                logger.info(f"Current number of profiles in database: {count}")
            else:
                logger.error("Failed to create profile table!")
                
        logger.info("Database initialization completed successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        logger.error("Database URL format (censored): " + database_url[:10] + "..." if database_url else "None")
        raise

async def get_user_profile(user_id: int) -> dict:
    """Get user profile from database."""
    try:
        if not db_pool:
            logging.error("Database pool not initialized")
            return None
            
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT data FROM profile WHERE uid = $1',
                user_id
            )
            return json.loads(row['data']) if row else None
    except Exception as e:
        logging.error(f"Error fetching user profile: {str(e)}")
        return None

async def save_user_profile(user_id: int, profile_data: dict) -> bool:
    """Save user profile to database."""
    try:
        if not db_pool:
            logging.error("Database pool not initialized")
            return False
            
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO profile (uid, data)
                VALUES ($1, $2)
                ON CONFLICT (uid) 
                DO UPDATE SET data = $2
            ''', user_id, json.dumps(profile_data))
        return True
    except Exception as e:
        logging.error(f"Error saving user profile: {str(e)}")
        return False

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
    """Make an async call to REI API with better error handling."""
    logger.info(f"Making REI API call with prompt: {prompt}")
    
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "rei-core-chat-001",
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.reisearch.box/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=300
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"REI API error: Status {resp.status}, Response: {error_text}")
                    raise Exception(f"REI API returned status {resp.status}")
                
                data = await resp.json()
                if not data.get("choices") or not data["choices"][0].get("message", {}).get("content"):
                    logger.error(f"Unexpected REI API response format: {data}")
                    raise Exception("Invalid response format from REI API")
                
                return data["choices"][0]["message"]["content"].strip()
                
    except aiohttp.ClientError as e:
        logger.error(f"Network error calling REI API: {str(e)}")
        raise Exception(f"Network error: {str(e)}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON response from REI API: {str(e)}")
        raise

# ───────────────────────────── telegram callbacks ────────────────────────── #
INIT_MENU = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=True
)
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile","📊 Trade"], ["🤖 Ask AI","❓ FAQ"]],
    resize_keyboard=True
)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command handler with error logging"""
    try:
        logger.info(f"Start command received from user {update.effective_user.id}")
        await update.message.reply_text(
            "👋 Welcome! Press ▶️ Start to begin.",
            reply_markup=INIT_MENU
        )
        logger.info("Start message sent successfully")
    except Exception as e:
        logger.error(f"Error in start command: {str(e)}", exc_info=True)
        try:
            await update.message.reply_text("Sorry, there was an error. Please try again.")
        except:
            logger.error("Could not send error message to user")

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
        data = ctx.user_data["ans"]
        uid = update_or_query.effective_chat.id if hasattr(update_or_query, 'effective_chat') else update_or_query.message.chat.id
        
        if await save_user_profile(uid, data):
            await update_or_query.message.reply_text("✅ Profile saved!", reply_markup=MAIN_MENU)
        else:
            await update_or_query.message.reply_text("❌ Failed to save profile. Please try again.", reply_markup=MAIN_MENU)
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
            data = ctx.user_data["ans"]
            if await save_user_profile(query.from_user.id, data):
                await query.message.reply_text("✅ Profile saved!", reply_markup=MAIN_MENU)
            else:
                await query.message.reply_text("❌ Failed to save profile. Please try again.", reply_markup=MAIN_MENU)
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

async def trade_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the trade flow."""
    logger.info("Starting trade flow")
    
    # Check if user has completed profile
    profile = await get_user_profile(update.effective_user.id)
    if not profile:
        buttons = [[InlineKeyboardButton("🔧 Setup Profile Now", callback_data="setup:start")]]
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "⚠️ Please set up your trading profile first!\n\n"
            "This helps me provide personalized trade suggestions and analysis based on:\n"
            "• Your experience level\n"
            "• Capital allocation\n"
            "• Risk tolerance\n"
            "• Preferred timeframes\n"
            "• And more...",
            reply_markup=markup
        )
        return
        
    # Use consistent callback data format: action:asset
    buttons = [
        [InlineKeyboardButton("BTC-PERP", callback_data="trade:BTC-PERP")],
        [InlineKeyboardButton("ETH-PERP", callback_data="trade:ETH-PERP")],
        [InlineKeyboardButton("Get Suggestion", callback_data="trade:SUGGEST")],
        [InlineKeyboardButton("Custom Asset", callback_data="trade:CUSTOM")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    logger.debug(f"Created markup with buttons: {[btn[0].callback_data for btn in buttons]}")
    
    try:
        await update.message.reply_text(
            "Choose an option:",
            reply_markup=markup
        )
        logger.info("Sent trade options message")
    except Exception as e:
        logger.error(f"Error sending trade options: {str(e)}")
        await update.message.reply_text(
            "Sorry, there was an error. Please try again or contact support.",
            reply_markup=MAIN_MENU
        )

async def format_profile_context(profile: dict) -> str:
    """Format user profile into context string for REI prompts."""
    if not profile:
        return ""
        
    return (
        "\nUser Profile Context:\n"
        f"- Experience Level: {profile.get('experience', 'unknown')}\n"
        f"- Capital: {profile.get('capital', 'unknown')} USD\n"
        f"- Risk Tolerance: {profile.get('risk', 'unknown')}\n"
        f"- Preferred Quote: {profile.get('quote', 'unknown')}\n"
        f"- Trading Timeframe: {profile.get('timeframe', 'unknown')}\n"
        f"- Max Leverage: {profile.get('leverage', 'unknown')}\n"
        f"- Funding Rate Preference: {profile.get('funding', 'unknown')}\n"
    )

async def button_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks."""
    query = update.callback_query
    if not query:
        logger.error("Received button_click call without callback query")
        return

    logger.info(f"Received callback query with data: {query.data}")
    
    try:
        # Always answer callback query first to prevent "loading" state
        await query.answer()
        
        # Split callback data into action and value
        if ":" not in query.data:
            logger.error(f"Invalid callback data format: {query.data}")
            await query.message.reply_text(
                "Sorry, there was an error. Please try again.",
                reply_markup=MAIN_MENU
            )
            return
            
        action, value = query.data.split(":", 1)
        
        # Special handling for setup:start action
        if action == "setup" and value == "start":
            return await setup_start(query, ctx)
            
        # For all other actions, check profile first
        profile = await get_user_profile(query.from_user.id)
        if not profile and action != "setup":
            buttons = [[InlineKeyboardButton("🔧 Setup Profile Now", callback_data="setup:start")]]
            markup = InlineKeyboardMarkup(buttons)
            await query.message.reply_text(
                "⚠️ Please set up your trading profile first!\n\n"
                "This helps me provide personalized trade suggestions and analysis based on:\n"
                "• Your experience level\n"
                "• Capital allocation\n"
                "• Risk tolerance\n"
                "• Preferred timeframes\n"
                "• And more...",
                reply_markup=markup
            )
            return
            
        profile_context = await format_profile_context(profile)
        
        if action == "trade":
            if value == "SUGGEST":
                logger.debug("Processing suggestion request")
                await query.message.reply_text("🧠 Analyzing market conditions...")
                try:
                    suggestion = await rei_call(
                        "Based on current market conditions and the user's profile, suggest a high-probability trade setup."
                        f"{profile_context}\n\n"
                        "Include:\n"
                        "1. Asset selection and reasoning\n"
                        "2. Entry strategy with specific levels\n"
                        "3. Stop loss placement\n"
                        "4. Take profit targets\n"
                        "5. Risk:reward ratio\n"
                        "6. Key market conditions supporting this trade\n"
                        "7. Compatibility with user's profile"
                    )
                    await query.message.reply_text(suggestion, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.error(f"Error getting trade suggestion: {str(e)}")
                    await query.message.reply_text(
                        "Sorry, I couldn't generate a trade suggestion at the moment. Please try again later.",
                        reply_markup=MAIN_MENU
                    )
                
            elif value == "CUSTOM":
                logger.debug("Processing custom asset request")
                await query.message.edit_text("Enter asset symbol (e.g. BTC):")
                
            elif value.endswith("-PERP"):
                logger.debug(f"Processing {value} analysis options")
                buttons = [
                    [InlineKeyboardButton("📊 Trade Setup (Entry/SL/TP)", callback_data=f"analysis:setup:{value}")],
                    [InlineKeyboardButton("📈 Market Analysis (Tech/Fund)", callback_data=f"analysis:market:{value}")]
                ]
                markup = InlineKeyboardMarkup(buttons)
                await query.message.edit_text(
                    f"Choose analysis type for {value}:",
                    reply_markup=markup
                )
                
        elif action == "analysis":
            try:
                analysis_type, asset = value.split(":", 1)
                logger.debug(f"Processing {analysis_type} request for {asset}")
                
                if analysis_type == "setup":
                    await query.message.reply_text(f"🎯 Generating trade setup for {asset}...")
                    try:
                        response = await rei_call(
                            f"Provide a detailed trade setup analysis for {asset}, tailored to the user's profile."
                            f"{profile_context}\n\n"
                            f"Include:\n"
                            f"1. Current Market Context\n"
                            f"   - Price action summary\n"
                            f"   - Key levels in play\n"
                            f"   - Market structure\n\n"
                            f"2. Trade Setup Details\n"
                            f"   - Entry zone/price with reasoning\n"
                            f"   - Stop loss placement and rationale\n"
                            f"   - Take profit targets (multiple levels)\n"
                            f"   - Position sizing based on user's capital and risk\n\n"
                            f"3. Risk Management\n"
                            f"   - Risk:reward ratio\n"
                            f"   - Maximum risk per trade (based on user's preference)\n"
                            f"   - Key invalidation points\n\n"
                            f"4. Important Considerations\n"
                            f"   - Potential catalysts\n"
                            f"   - Key risks to watch\n"
                            f"   - Timeframe alignment with user's preference\n"
                            f"   - Funding rate implications"
                        )
                    except Exception as e:
                        logger.error(f"Error generating trade setup: {str(e)}")
                        await query.message.reply_text(
                            "Sorry, I couldn't generate a trade setup at the moment. Please try again later.",
                            reply_markup=MAIN_MENU
                        )
                        return
                        
                else:  # market analysis
                    await query.message.reply_text(f"📊 Analyzing {asset} market conditions...")
                    try:
                        response = await rei_call(
                            f"Provide a comprehensive market analysis for {asset}, considering the user's profile."
                            f"{profile_context}\n\n"
                            f"Include:\n"
                            f"1. Technical Analysis\n"
                            f"   - Trend analysis (focus on {profile.get('timeframe', 'all')} timeframe)\n"
                            f"   - Support/resistance levels\n"
                            f"   - Chart patterns and formations\n"
                            f"   - Key technical indicators\n\n"
                            f"2. Market Structure\n"
                            f"   - Current market phase\n"
                            f"   - Recent price action\n"
                            f"   - Volume profile\n"
                            f"   - Market dominance\n\n"
                            f"3. Fundamental Analysis\n"
                            f"   - Recent news/developments\n"
                            f"   - Network metrics (if applicable)\n"
                            f"   - Funding rates (important for user's preference)\n"
                            f"   - Market sentiment\n\n"
                            f"4. Risk Assessment\n"
                            f"   - Volatility analysis\n"
                            f"   - Liquidity conditions\n"
                            f"   - Potential risks/catalysts\n"
                            f"   - Correlation with market\n"
                            f"   - Suitability for user's risk profile"
                        )
                    except Exception as e:
                        logger.error(f"Error generating market analysis: {str(e)}")
                        await query.message.reply_text(
                            "Sorry, I couldn't generate the market analysis at the moment. Please try again later.",
                            reply_markup=MAIN_MENU
                        )
                        return
                        
                await query.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
                
            except ValueError:
                logger.error(f"Invalid analysis value format: {value}")
                await query.message.reply_text(
                    "Sorry, there was an error processing your request. Please try again.",
                    reply_markup=MAIN_MENU
                )
            
        else:
            logger.warning(f"Unknown action in callback: {action}")
            await query.message.reply_text(
                "Invalid option. Please try again.",
                reply_markup=MAIN_MENU
            )
            
    except Exception as e:
        logger.error(f"Error in button_click: {str(e)}", exc_info=True)
        try:
            await query.message.reply_text(
                "An error occurred. Please try again or contact support.",
                reply_markup=MAIN_MENU
            )
        except Exception as nested_e:
            logger.error(f"Failed to send error message: {str(nested_e)}")

async def handle_custom_asset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle custom asset input from user."""
    asset = update.message.text.strip().upper()
    logger.info(f"Received custom asset input: {asset}")
    
    if not asset:
        await update.message.reply_text("Please enter a valid asset symbol.")
        return
        
    # Format the asset with -PERP suffix if not already present
    if not asset.endswith("-PERP"):
        asset = f"{asset}-PERP"
    
    # Create analysis options buttons
    buttons = [
        [InlineKeyboardButton("📊 Trade Setup (Entry/SL/TP)", callback_data=f"analysis:setup:{asset}")],
        [InlineKeyboardButton("📈 Market Analysis (Tech/Fund)", callback_data=f"analysis:market:{asset}")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    
    await update.message.reply_text(
        f"Choose analysis type for {asset}:",
        reply_markup=markup
    )

async def check_db_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to check database contents"""
    try:
        if not db_pool:
            await update.message.reply_text("❌ Database not connected")
            return
            
        async with db_pool.acquire() as conn:
            # Get total number of profiles
            count = await conn.fetchval('SELECT COUNT(*) FROM profile')
            
            # Get last 5 profiles
            rows = await conn.fetch('''
                SELECT uid, data->>'experience' as exp, 
                       data->>'capital' as cap,
                       data->>'timeframe' as tf
                FROM profile 
                ORDER BY uid DESC 
                LIMIT 5
            ''')
            
            # Format message
            msg = f"📊 Database Status:\n\nTotal Profiles: {count}\n\nLast 5 profiles:"
            for row in rows:
                msg += f"\n• User {row['uid']}: {row['exp']}, {row['cap']}, {row['tf']}"
            
            await update.message.reply_text(msg)
            
    except Exception as e:
        logger.error(f"Error checking database: {str(e)}")
        await update.message.reply_text("❌ Error checking database")

# ───────────────────────────── main ─────────────────────────────────────── #
async def main() -> None:
    """Start the bot."""
    logger.info("Starting bot")
    
    try:
        # Initialize environment and database
        init_env()
        await init_db()  # Now async
        init_assets()
        
        # Initialize bot
        app = Application.builder().token(BOT_TOKEN).build()
        logger.info("Bot application built")

        # Add conversation handler for setup
        setup_handler = ConversationHandler(
            entry_points=[
                CommandHandler('setup', setup_start),
                MessageHandler(filters.Regex('^🔧 Setup Profile$'), setup_start)
            ],
            states={
                SETUP: [CallbackQueryHandler(handle_setup, pattern=r'^setup:')]
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            name="setup_conversation",
            persistent=False
        )
        
        # Add handlers in specific order
        app.add_handler(CommandHandler('start', start))
        app.add_handler(MessageHandler(filters.Regex('^▶️ Start$'), main_menu))
        app.add_handler(setup_handler)  # Add setup conversation handler
        app.add_handler(CommandHandler('trade', trade_start))
        app.add_handler(MessageHandler(filters.Regex('^📊 Trade$'), trade_start))
        app.add_handler(CommandHandler('ask', ask_cmd))
        app.add_handler(MessageHandler(filters.Regex('^🤖 Ask AI$'), ask_cmd))
        app.add_handler(CommandHandler('faq', faq_cmd))
        app.add_handler(MessageHandler(filters.Regex('^❓ FAQ$'), faq_cmd))
        app.add_handler(CommandHandler('help', help_cmd))
        app.add_handler(CommandHandler('checkdb', check_db_cmd))
        
        # Add general callback handler for trade and analysis actions
        app.add_handler(CallbackQueryHandler(button_click, pattern=r'^(trade|analysis):'))
        
        # Add handler for custom asset input
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_asset))
        
        logger.info("All handlers registered")
        
        # Add error handler
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Exception while handling an update:", exc_info=context.error)
            
        app.add_error_handler(error_handler)
        
        # Start the bot
        logger.info("Starting polling")
        await app.initialize()
        await app.start()
        await app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Error during bot execution: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Stopping application...")
        try:
            if db_pool:
                await db_pool.close()
            await app.stop()
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}", exc_info=True)

if __name__ == '__main__':
    try:
        # Set up signal handlers
        import signal
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}")
            raise KeyboardInterrupt
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Run the bot
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user/signal")
    except Exception as e:
        logger.error(f"Bot stopped due to error: {str(e)}", exc_info=True)
