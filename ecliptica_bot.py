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
import traceback
import sys
import signal

# Set up logging first
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add file handler to also log to a file
try:
    file_handler = logging.FileHandler('bot.log')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    logger.info("File logging initialized")
except Exception as e:
    logger.error(f"Failed to set up file logging: {str(e)}")

# Add a signal handler for debugging deadlocks
def timeout_handler(signum, frame):
    current_stack = ''.join(traceback.format_stack())
    logger.critical(f"WATCHDOG TIMEOUT! Process appears stuck. Current stack:\n{current_stack}")
    # Log to stderr as well to ensure it appears in Railway logs
    print(f"CRITICAL: WATCHDOG TIMEOUT! Process appears stuck. Current stack:\n{current_stack}", file=sys.stderr)
    
# Set up a watchdog timer for 2 minutes (will be reset on each command)
def start_watchdog():
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(120)  # 2 minutes
    
def stop_watchdog():
    signal.alarm(0)

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
    """Make an async call to REI API with better error handling, retry logic, and chunking for long prompts."""
    logger.info(f"Making REI API call with prompt length: {len(prompt)}")
    print(f"STDOUT: Making REI API call with prompt length: {len(prompt)}", file=sys.stdout)
    print(f"STDERR: Making REI API call with prompt length: {len(prompt)}", file=sys.stderr)
    
    # Start watchdog timer
    start_watchdog()
    
    try:
        # Check if prompt is very long
        if len(prompt) > 2000:
            logger.info("Long prompt detected, splitting request into context and questions")
            
            # Split the prompt into context and questions
            parts = prompt.split("Include:")
            if len(parts) == 2:
                context = parts[0].strip()
                questions = "Include:" + parts[1].strip()
                
                logger.info(f"Splitting prompt: context length {len(context)}, questions length {len(questions)}")
                
                # Make the first call with just the context to prime the model
                logger.info("Making initial context call")
                try:
                    context_response = await _rei_call_internal(
                        f"{context}\n\nPlease confirm you understand this context and are ready for questions.",
                        max_tokens=100
                    )
                    logger.info(f"Context call successful: {context_response[:50]}...")
                except Exception as e:
                    logger.error(f"Context call failed: {str(e)}")
                    # Continue anyway to the main call
                
                # Now make the main call with reference to the context
                logger.info("Making main call with questions")
                result = await _rei_call_internal(
                    f"Using the context I provided earlier about {context[:50]}...\n\n{questions}", 
                    max_tokens=4000
                )
                # Stop watchdog timer as we're done
                stop_watchdog()
                return result
            
        # For shorter prompts, just make a regular call
        result = await _rei_call_internal(prompt)
        # Stop watchdog timer as we're done
        stop_watchdog()
        return result
    except Exception as e:
        # Make sure to stop the watchdog if we hit an exception
        stop_watchdog()
        logger.error(f"rei_call failed with exception: {str(e)}", exc_info=True)
        raise

async def _rei_call_internal(prompt: str, max_tokens: int = 2000) -> str:
    """Internal implementation of the REI API call with retries."""
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "rei-core-chat-001",
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "response_format": {"type": "text"},
        "stream": False
    }
    
    logger.debug(f"Request headers (excluding auth): {{'Content-Type': {headers['Content-Type']}}}")
    logger.debug(f"Request body: {json.dumps(body, indent=2)}")
    
    # Retry parameters
    max_retries = 2
    retry_count = 0
    last_error = None
    
    while retry_count <= max_retries:
        try:
            # Reset watchdog timer for this attempt
            start_watchdog()
            
            # Use a timeout of 90 seconds to stay under Cloudflare's ~100 second limit
            logger.info(f"REI API call attempt {retry_count + 1}/{max_retries + 1}")
            print(f"STDOUT: REI API call attempt {retry_count + 1}/{max_retries + 1}", file=sys.stdout)
            print(f"STDERR: REI API call attempt {retry_count + 1}/{max_retries + 1}", file=sys.stderr)
            
            async with aiohttp.ClientSession() as session:
                # Create a task for the post request to be able to add a shield
                post_task = asyncio.create_task(
                    session.post(
                        "https://api.reisearch.box/v1/chat/completions",
                        headers=headers,
                        json=body,
                        timeout=90  # Reduced to 90 seconds to avoid Cloudflare timeout
                    )
                )
                
                # Use shield to ensure task continues running even if waiting coroutine is cancelled
                shielded_task = asyncio.shield(post_task)
                
                try:
                    # Wait for the response with timeout
                    resp = await asyncio.wait_for(shielded_task, timeout=95)
                except asyncio.TimeoutError:
                    # Cancel the task if it times out
                    if not post_task.done():
                        post_task.cancel()
                    raise
                
                # Process the response
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"REI API error: Status {resp.status}, Response: {error_text}")
                    print(f"STDERR: REI API error: Status {resp.status}, Response: {error_text}", file=sys.stderr)
                    if resp.status == 401:
                        raise Exception("Invalid API key or unauthorized access")
                    elif resp.status == 404:
                        raise Exception("Agent not found")
                    elif resp.status == 524:
                        raise Exception(f"Cloudflare timeout (524) - origin server took too long to respond")
                    else:
                        raise Exception(f"REI API returned status {resp.status}")
                
                try:
                    data = await resp.json()
                    logger.debug(f"API Response: {json.dumps(data, indent=2)}")
                except json.JSONDecodeError as e:
                    raw_response = await resp.text()
                    logger.error(f"Failed to parse JSON response. Raw response: {raw_response}")
                    raise
                
                if not data.get("choices") or not data["choices"][0].get("message"):
                    logger.error(f"Unexpected REI API response format: {data}")
                    raise Exception("Invalid response format from REI API")
                
                message = data["choices"][0]["message"]
                if message.get("tool_calls"):
                    # Handle tool calls if present
                    logger.error(f"Received tool calls in response, which we don't support: {message['tool_calls']}")
                    raise Exception("Received tool calls response which is not supported")
                
                if not message.get("content"):
                    logger.error(f"No content in message: {message}")
                    raise Exception("No content in API response")
                    
                content = message["content"].strip()
                logger.info(f"Successfully received response of length: {len(content)}")
                print(f"STDOUT: Successfully received response of length: {len(content)}", file=sys.stdout)
                
                # Stop watchdog for this attempt since we succeeded
                stop_watchdog()
                return content
                
        except asyncio.TimeoutError as e:
            # Reset the watchdog timer since we've handled the timeout
            stop_watchdog()
            logger.error(f"Timeout error on attempt {retry_count + 1}: {str(e)}")
            print(f"STDERR: Timeout error on attempt {retry_count + 1}: {str(e)}", file=sys.stderr)
            last_error = e
            retry_count += 1
            if retry_count <= max_retries:
                logger.info(f"Retrying in 5 seconds...")
                await asyncio.sleep(5)  # Wait before retrying
            continue
        except aiohttp.ClientError as e:
            # Reset the watchdog timer
            stop_watchdog()
            logger.error(f"Network error calling REI API: {str(e)}")
            print(f"STDERR: Network error calling REI API: {str(e)}", file=sys.stderr)
            raise Exception(f"Network error: {str(e)}")
        except json.JSONDecodeError as e:
            # Reset the watchdog timer
            stop_watchdog()
            logger.error(f"Invalid JSON response from REI API: {str(e)}")
            print(f"STDERR: Invalid JSON response from REI API: {str(e)}", file=sys.stderr)
            raise
        except Exception as e:
            # Reset the watchdog timer
            stop_watchdog()
            # Check if it's a Cloudflare timeout error
            if "524" in str(e):
                logger.warning(f"Cloudflare timeout detected on attempt {retry_count + 1}")
                print(f"STDERR: Cloudflare timeout detected on attempt {retry_count + 1}", file=sys.stderr)
                last_error = e
                retry_count += 1
                if retry_count <= max_retries:
                    logger.info(f"Retrying with a shorter prompt in 5 seconds...")
                    # If it's a retry for a Cloudflare timeout, simplify the prompt
                    if retry_count > 0 and len(prompt) > 500:
                        prompt = prompt[:500] + "...\n\nPlease provide a concise response due to previous timeout issues."
                        body["messages"][0]["content"] = prompt
                        body["max_tokens"] = min(max_tokens, 1000)  # Reduce token count for faster response
                    await asyncio.sleep(5)
                continue
            logger.error(f"Unexpected error in rei_call: {str(e)}", exc_info=True)
            print(f"STDERR: Unexpected error in rei_call: {str(e)}", file=sys.stderr)
            raise
    
    # If we get here, all retries failed
    logger.error(f"All {max_retries + 1} attempts failed. Last error: {str(last_error)}")
    print(f"STDERR: All {max_retries + 1} attempts failed. Last error: {str(last_error)}", file=sys.stderr)
    raise Exception(f"Failed to get response after {max_retries + 1} attempts: {str(last_error)}")

# Add an alternative REI API call function that uses a different endpoint
async def rei_call_alternative(prompt: str) -> str:
    """Make an API call to a more reliable REI API endpoint as fallback."""
    logger.info(f"Making alternative REI API call with prompt length: {len(prompt)}")
    print(f"STDOUT: Trying alternative API endpoint with prompt length: {len(prompt)}", file=sys.stdout)
    
    headers = {"Authorization": f"Bearer {REI_KEY}", "Content-Type": "application/json"}
    
    # Use a simpler model with lower timeout
    body = {
        "model": "gpt-3.5-turbo",  # Use a simpler model as fallback
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.7
    }
    
    try:
        # Use a short timeout to avoid waiting too long
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",  # OpenAI-compatible endpoint
                headers=headers,
                json=body,
                timeout=60  # Shorter timeout
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Alternative API error: Status {resp.status}, Response: {error_text}")
                    raise Exception(f"Alternative API returned status {resp.status}")
                
                data = await resp.json()
                if not data.get("choices") or not data["choices"][0].get("message", {}).get("content"):
                    logger.error(f"Unexpected alternative API response format: {data}")
                    raise Exception("Invalid response format from alternative API")
                
                content = data["choices"][0]["message"]["content"].strip()
                logger.info(f"Successfully received alternative response of length: {len(content)}")
                return content
                
    except Exception as e:
        logger.error(f"Alternative API call failed: {str(e)}")
        # If even this fails, use the fallback response
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

# Add a fallback response function
def get_fallback_response(asset: str = "", analysis_type: str = "") -> str:
    """Generate a simple fallback response when the REI API fails."""
    if not asset:
        return ("I'm currently experiencing issues connecting to my analysis service. "
                "Please try again in a few minutes. If the issue persists, consider using a simpler request.")
    
    if analysis_type == "market":
        return (f"I'm sorry, I couldn't retrieve a full market analysis for {asset} at the moment due to technical issues. "
                f"Some key points about {asset} to consider:\n\n"
                f"• Check the current price and 24h change on your preferred exchange\n"
                f"• Look at the daily timeframe for major support/resistance levels\n"
                f"• Monitor funding rates if taking a leveraged position\n"
                f"• Consider overall market sentiment and correlation with BTC\n\n"
                f"Please try again later for a more detailed analysis.")
    elif analysis_type == "setup":
        return (f"I'm sorry, I couldn't generate a complete trade setup for {asset} at this time due to technical issues. "
                f"For a basic approach to trading {asset}:\n\n"
                f"• Look for key support/resistance levels on the 4h and daily charts\n"
                f"• Consider setting stops 2-5% below your entry (based on your risk profile)\n"
                f"• Target profit taking at previous resistance levels\n"
                f"• Always manage your position size based on your risk tolerance\n\n"
                f"Please try again later for a more detailed trade setup.")
    else:
        return (f"I'm having trouble connecting to my analysis service to provide information about {asset}. "
                f"Please try again in a few minutes, or try a different request.")

# Update the button_click handler to use our new handler function
async def button_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks."""
    query = update.callback_query
    if not query:
        logger.error("Received button_click call without callback query")
        return

    logger.info(f"Received callback query with data: {query.data}")
    
    try:
        # Start watchdog timer for this command
        start_watchdog()
        
        # Always answer callback query first to prevent "loading" state
        await query.answer()
        
        # Split callback data into action and value
        if ":" not in query.data:
            logger.error(f"Invalid callback data format: {query.data}")
            await query.message.reply_text(
                "Sorry, there was an error. Please try again.",
                reply_markup=MAIN_MENU
            )
            stop_watchdog()
            return
            
        action, value = query.data.split(":", 1)
        logger.debug(f"Processing action: {action}, value: {value}")
        
        # Special handling for setup:start action
        if action == "setup" and value == "start":
            stop_watchdog()
            return await setup_start(query, ctx)
            
        # For all other actions, check profile first
        profile = await get_user_profile(query.from_user.id)
        logger.debug(f"Retrieved profile for user {query.from_user.id}: {profile is not None}")
        
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
            stop_watchdog()
            return
            
        profile_context = await format_profile_context(profile)
        logger.debug("Formatted profile context successfully")
        
        if action == "analysis":
            try:
                analysis_type, asset = value.split(":", 1)
                logger.info(f"Processing {analysis_type} analysis request for {asset}")
                
                if analysis_type == "market":
                    await handle_market_analysis(query, asset, profile_context, profile)
                elif analysis_type == "setup":
                    # Similar handler for setup could be implemented here
                    await query.message.reply_text(f"🎯 Generating trade setup for {asset}...")
                    # Use fallback for now
                    await query.message.reply_text(get_fallback_response(asset, "setup"))
                    
            except Exception as e:
                logger.error(f"Error in analysis handler: {str(e)}", exc_info=True)
                await query.message.reply_text(
                    "An unexpected error occurred. Please try again later.",
                    reply_markup=MAIN_MENU
                )
        
        elif action == "trade":
            logger.info(f"Processing trade action with value: {value}")
            
            if value == "SUGGEST":
                logger.debug("Processing suggestion request")
                await query.message.reply_text("🧠 Analyzing market conditions...")
                try:
                    start_time = datetime.now()
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
                    end_time = datetime.now()
                    duration = (end_time - start_time).total_seconds()
                    logger.info(f"Trade suggestion completed in {duration} seconds")
                    
                    await query.message.reply_text(suggestion, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    logger.error(f"Error getting trade suggestion: {str(e)}")
                    # Use a generic fallback
                    await query.message.reply_text(
                        get_fallback_response("", ""),
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
            else:
                logger.warning(f"Unknown trade value: {value}")
                await query.message.reply_text(
                    "Invalid option. Please try again.",
                    reply_markup=MAIN_MENU
                )
        
        else:
            logger.warning(f"Unknown action in callback: {action}")
            await query.message.reply_text(
                "Invalid option. Please try again.",
                reply_markup=MAIN_MENU
            )
        
        # Make sure watchdog is stopped before returning
        stop_watchdog()
            
    except Exception as e:
        # Make sure watchdog is stopped in case of error
        stop_watchdog()
        logger.error(f"Error in button_click: {str(e)}", exc_info=True)
        try:
            await query.message.reply_text(
                "An error occurred. Please try again or contact support.",
                reply_markup=MAIN_MENU
            )
        except Exception as nested_e:
            logger.error(f"Failed to send error message: {str(nested_e)}")

async def handle_market_analysis(query, asset, profile_context, profile):
    """Handle market analysis request with reliable fallbacks."""
    await query.message.reply_text(f"📊 Analyzing {asset} market conditions...")
    try:
        logger.debug("Preparing market analysis prompt")
        prompt = (
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
        
        logger.debug("Calling REI API for market analysis")
        start_time = datetime.now()
        logger.debug(f"Starting REI API call at {start_time}")
        
        # Try primary API first
        try:
            response = await rei_call(prompt)
            logger.info("Primary API call succeeded")
        except Exception as primary_e:
            logger.warning(f"Primary API call failed: {str(primary_e)}")
            
            # Try alternative API
            try:
                logger.info("Trying alternative API endpoint")
                shorter_prompt = (
                    f"Provide a concise market analysis for {asset} with these key points:\n"
                    f"- Current trend and price action\n"
                    f"- Key support/resistance levels\n"
                    f"- Technical indicators and patterns\n"
                    f"- Market sentiment and outlook\n"
                    f"- Risk assessment and recommendation"
                )
                response = await rei_call_alternative(shorter_prompt)
                logger.info("Alternative API call succeeded")
            except Exception as alt_e:
                logger.error(f"Alternative API also failed: {str(alt_e)}")
                # Fall back to static response
                response = get_fallback_response(asset, "market")
                logger.info("Using static fallback response")
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        logger.info(f"Total processing time: {duration} seconds")
        
        # Split response into chunks if too long
        if len(response) > 4096:
            logger.debug("Response too long, splitting into chunks")
            chunks = [response[i:i+4096] for i in range(0, len(response), 4096)]
            for i, chunk in enumerate(chunks):
                logger.debug(f"Sending chunk {i+1}/{len(chunks)} of length {len(chunk)}")
                try:
                    await query.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
                except Exception as chunk_e:
                    logger.error(f"Error sending chunk {i+1}: {str(chunk_e)}")
                    # If markdown fails, try sending without parsing
                    await query.message.reply_text(chunk)
        else:
            logger.debug("Sending single response message")
            try:
                await query.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
            except Exception as send_e:
                logger.error(f"Error sending response with markdown: {str(send_e)}")
                # If markdown fails, try sending without parsing
                await query.message.reply_text(response)
                
    except Exception as e:
        logger.error(f"Complete failure in market analysis: {str(e)}", exc_info=True)
        # Last resort fallback
        await query.message.reply_text(
            get_fallback_response(asset, "market"),
            reply_markup=MAIN_MENU
        )

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

def init_handlers(application: Application) -> None:
    """Initialize all handlers for the application."""
    # Setup conversation handler
    setup_conv = ConversationHandler(
        entry_points=[
            CommandHandler('setup', setup_start),
            MessageHandler(filters.Regex('^🔧 Setup Profile$'), setup_start)
        ],
        states={
            SETUP: [CallbackQueryHandler(handle_setup, pattern=r'^setup:')]
        },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
    
    # Add all handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.Regex('^▶️ Start$'), main_menu))
    application.add_handler(setup_conv)
    application.add_handler(CommandHandler('trade', trade_start))
    application.add_handler(MessageHandler(filters.Regex('^📊 Trade$'), trade_start))
    application.add_handler(CommandHandler('ask', ask_cmd))
    application.add_handler(MessageHandler(filters.Regex('^🤖 Ask AI$'), ask_cmd))
    application.add_handler(CommandHandler('faq', faq_cmd))
    application.add_handler(MessageHandler(filters.Regex('^❓ FAQ$'), faq_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('checkdb', check_db_cmd))
    application.add_handler(CallbackQueryHandler(button_click, pattern=r'^(trade|analysis):'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_asset))

async def post_init(application: Application) -> None:
    """Post-initialization tasks."""
    await init_db()
    init_assets()

async def post_stop(application: Application) -> None:
    """Cleanup tasks."""
    if db_pool:
        await db_pool.close()

def main() -> None:
    """Start the bot."""
    # Initialize environment first
    init_env()
    
    # Create application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    
    # Add handlers
    init_handlers(application)
    
    # Add error handler
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Exception while handling an update: {context.error}")
    application.add_error_handler(error_handler)
    
    # Run application
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
