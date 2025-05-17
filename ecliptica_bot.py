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
import uuid
import hashlib
from coinbase_commerce.client import Client
from coinbase_commerce.error import SignatureVerificationError, WebhookInvalidPayload

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
(SETUP, SELECTING_ASSET, ANALYZING_ASSET, TRADING, SUBSCRIPTION, ENTER_CODE) = range(6)

# Subscription configuration
SUBSCRIPTION_PLANS = {
    "monthly": {"name": "Monthly Plan", "price": 19.99, "days": 30, "description": "Full access to all features for 30 days"},
    "quarterly": {"name": "Quarterly Plan", "price": 49.99, "days": 90, "description": "Full access to all features for 90 days (save 17%)"},
    "annual": {"name": "Annual Plan", "price": 149.99, "days": 365, "description": "Full access to all features for a full year (save 37%)"}
}

# Valid promo codes (in a real system, store these securely and don't hardcode)
PROMO_CODES = {
    "ECLIPTICA2024": {"days": 30, "description": "Free 30-day trial"},
    "PERPSMASTER": {"days": 90, "description": "Free 90-day access for early supporters"},
    "UNLIMITED2024": {"days": 3650, "description": "Unlimited 10-year access for testing"}
}

# Number of free market analyses before requiring subscription
FREE_ANALYSIS_LIMIT = 3

# Setup questions + options
QUESTS: Final[List[tuple[str, str]]] = [
    ("experience", "Your perps experience?"),
    ("capital",    "Capital allocated (USD)"),
    ("risk",       "Max loss % (e.g. 2)"),
    ("quote",      "Quote currency"),
    ("timeframe",  "Timeframe"),
    ("leverage",   "Leverage multiple"),
    ("funding",    "Comfort paying funding 8h?"),
    ("verbosity",  "Response detail level?"),
]
OPTIONS: Final[Dict[str, List[str]]] = {
    "experience": ["0-3m", "3-12m", ">12m"],
    "capital":    ["1k", "5k", "10k+"],
    "risk":       ["1%", "2%", "5%"],
    "quote":      ["USDT", "USD-C", "BTC"],
    "timeframe":  ["scalp", "intraday", "swing", "position"],
    "leverage":   ["1x", "3x", "5x", "10x"],
    "funding":    ["yes", "unsure", "prefer spot"],
    "verbosity":  ["brief", "balanced", "detailed"],
}

# Add new constants
TOP_ASSETS_COUNT = 5  # Number of top volume assets to show

# Add a global dictionary to track user states
user_states = {}  # Maps user_id -> {"processing": bool, "last_request_time": datetime}

async def check_user_state(user_id: int) -> bool:
    """
    Check if a user has a request in progress.
    Returns True if the user is free to make a new request, False otherwise.
    """
    global user_states
    
    now = datetime.now()
    
    # If user not in states or not processing, they're free
    if user_id not in user_states or not user_states[user_id].get("processing", False):
        # Update state to mark as free
        user_states[user_id] = {"processing": False, "last_request_time": now}
        return True
        
    # Check if it's been more than 5 minutes since their last request (failsafe for stuck states)
    last_request = user_states[user_id].get("last_request_time", now - timedelta(minutes=10))
    if (now - last_request).total_seconds() > 300:  # 5 minutes
        logger.warning(f"Force clearing stuck state for user {user_id} after 5 minutes")
        user_states[user_id] = {"processing": False, "last_request_time": now}
        return True
        
    # User has a request in progress
    return False

async def set_user_processing(user_id: int, processing: bool) -> None:
    """Set a user's processing state."""
    global user_states
    
    now = datetime.now()
    if user_id not in user_states:
        user_states[user_id] = {"processing": processing, "last_request_time": now}
    else:
        user_states[user_id]["processing"] = processing
        if processing:
            user_states[user_id]["last_request_time"] = now

# ───────────────────────────── environment ─────────────────────────────────── #
def init_env() -> None:
    load_dotenv()
    global BOT_TOKEN, REI_KEY, COINBASE_API_KEY, COINBASE_WEBHOOK_SECRET
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    REI_KEY = os.environ.get("REICORE_API_KEY", "").strip()
    COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "").strip()
    COINBASE_WEBHOOK_SECRET = os.environ.get("COINBASE_WEBHOOK_SECRET", "").strip()
    
    # Log environment status (without exposing sensitive data)
    logger.info("Environment initialization:")
    logger.info(f"BOT_TOKEN present: {bool(BOT_TOKEN)}")
    logger.info(f"REI_KEY present: {bool(REI_KEY)}")
    logger.info(f"COINBASE_API_KEY present: {bool(COINBASE_API_KEY)}")
    logger.info(f"COINBASE_WEBHOOK_SECRET present: {bool(COINBASE_WEBHOOK_SECRET)}")
    
    if not BOT_TOKEN:
        raise Exception("TELEGRAM_BOT_TOKEN not set")
    if not REI_KEY:
        raise Exception("REICORE_API_KEY not set")
    if not COINBASE_API_KEY:
        logger.warning("COINBASE_API_KEY not set - subscription features will be limited")
    if not COINBASE_WEBHOOK_SECRET:
        logger.warning("COINBASE_WEBHOOK_SECRET not set - webhook verification disabled")

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
            # Create profile table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS profile (
                    uid BIGINT PRIMARY KEY,
                    data JSONB
                )
            ''')
            
            # Create subscriptions table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    uid BIGINT PRIMARY KEY,
                    plan_type TEXT,
                    start_date TIMESTAMP WITH TIME ZONE,
                    end_date TIMESTAMP WITH TIME ZONE,
                    payment_id TEXT,
                    status TEXT,
                    usage_count INTEGER DEFAULT 0,
                    promo_code TEXT
                )
            ''')
            
            # Create payment tracking table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    uid BIGINT,
                    amount NUMERIC(10, 2),
                    currency TEXT,
                    plan_type TEXT,
                    status TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    completed_at TIMESTAMP WITH TIME ZONE
                )
            ''')
            
            # Verify tables were created
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
                
            # Verify subscription table
            sub_table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'subscriptions')"
            )
            if sub_table_exists:
                logger.info("Subscriptions table exists and is ready")
                # Count active subscriptions
                count = await conn.fetchval("SELECT COUNT(*) FROM subscriptions WHERE end_date > NOW()")
                logger.info(f"Current number of active subscriptions: {count}")
            else:
                logger.error("Failed to create subscriptions table!")
                
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

# ───────────────────────────── subscription ─────────────────────────────────── #
async def get_user_subscription(user_id: int) -> dict:
    """Get a user's subscription status."""
    try:
        if not db_pool:
            logging.error("Database pool not initialized")
            return None
            
        async with db_pool.acquire() as conn:
            # Get the subscription record
            row = await conn.fetchrow(
                '''
                SELECT uid, plan_type, start_date, end_date, payment_id, status, usage_count, promo_code
                FROM subscriptions
                WHERE uid = $1
                ''',
                user_id
            )
            
            if not row:
                return None
                
            # Convert to dict
            subscription = dict(row)
            
            # Check if subscription is active
            now = datetime.now(timezone.utc)
            is_active = subscription['end_date'] > now and subscription['status'] == 'active'
            
            # Add additional fields
            subscription['is_active'] = is_active
            subscription['days_remaining'] = (subscription['end_date'] - now).days if is_active else 0
            
            return subscription
            
    except Exception as e:
        logging.error(f"Error fetching user subscription: {str(e)}")
        return None

async def create_subscription(user_id: int, plan_type: str, payment_id: str = None, promo_code: str = None) -> bool:
    """Create or update a user subscription."""
    try:
        if not db_pool:
            logging.error("Database pool not initialized")
            return False
            
        # Get plan details
        plan = None
        if plan_type in SUBSCRIPTION_PLANS:
            plan = SUBSCRIPTION_PLANS[plan_type]
        elif promo_code and promo_code in PROMO_CODES:
            # For promo codes, create a custom plan
            promo = PROMO_CODES[promo_code]
            plan = {"days": promo["days"]}
            plan_type = "promo"
            
        if not plan:
            logging.error(f"Invalid plan type or promo code: {plan_type} / {promo_code}")
            return False
            
        # Calculate dates
        now = datetime.now(timezone.utc)
        days = plan["days"]
        end_date = now + timedelta(days=days)
        
        async with db_pool.acquire() as conn:
            # Check if subscription already exists
            existing = await conn.fetchval(
                'SELECT uid FROM subscriptions WHERE uid = $1',
                user_id
            )
            
            if existing:
                # Update existing subscription
                await conn.execute(
                    '''
                    UPDATE subscriptions
                    SET plan_type = $2, start_date = $3, end_date = $4, payment_id = $5,
                        status = 'active', promo_code = $6
                    WHERE uid = $1
                    ''',
                    user_id, plan_type, now, end_date, payment_id, promo_code
                )
            else:
                # Create new subscription
                await conn.execute(
                    '''
                    INSERT INTO subscriptions
                    (uid, plan_type, start_date, end_date, payment_id, status, usage_count, promo_code)
                    VALUES ($1, $2, $3, $4, $5, 'active', 0, $6)
                    ''',
                    user_id, plan_type, now, end_date, payment_id, promo_code
                )
                
            return True
            
    except Exception as e:
        logging.error(f"Error creating subscription: {str(e)}")
        return False

async def increment_usage_count(user_id: int) -> int:
    """Increment the usage count for a user and return the new count."""
    try:
        if not db_pool:
            logging.error("Database pool not initialized")
            return 0
            
        async with db_pool.acquire() as conn:
            # Check if user has a subscription record
            exists = await conn.fetchval(
                'SELECT uid FROM subscriptions WHERE uid = $1',
                user_id
            )
            
            if not exists:
                # Create a default record with count 1
                await conn.execute(
                    '''
                    INSERT INTO subscriptions
                    (uid, usage_count, status)
                    VALUES ($1, 1, 'free')
                    ''',
                    user_id
                )
                return 1
                
            # Update usage count
            new_count = await conn.fetchval(
                '''
                UPDATE subscriptions
                SET usage_count = usage_count + 1
                WHERE uid = $1
                RETURNING usage_count
                ''',
                user_id
            )
            
            return new_count
            
    except Exception as e:
        logging.error(f"Error incrementing usage count: {str(e)}")
        return 0

async def check_subscription_access(user_id: int) -> tuple[bool, str]:
    """
    Check if a user has access to premium features.
    Returns (has_access, message)
    """
    # Get subscription status
    subscription = await get_user_subscription(user_id)
    
    if subscription and subscription['is_active']:
        # User has an active subscription
        return True, ""
        
    # Check usage count for free tier
    if subscription:
        usage_count = subscription['usage_count']
    else:
        # No subscription record yet, first use
        usage_count = 0
        
    if usage_count < FREE_ANALYSIS_LIMIT:
        # Still within free usage limits
        remaining = FREE_ANALYSIS_LIMIT - usage_count
        return True, f"You have {remaining} free analyses remaining. Subscribe for unlimited access."
    
    # No access - need to subscribe
    return False, "You've reached the limit of free analyses. Please subscribe to continue using premium features."

async def create_payment_charge(user_id: int, plan_type: str) -> tuple[bool, str, str]:
    """
    Create a Coinbase Commerce charge for subscription payment.
    Returns (success, checkout_url, charge_id)
    """
    try:
        if not COINBASE_API_KEY:
            return False, "", "Coinbase API key not configured"
            
        # Get plan details
        if plan_type not in SUBSCRIPTION_PLANS:
            return False, "", "Invalid plan type"
            
        plan = SUBSCRIPTION_PLANS[plan_type]
        
        # Create a client
        client = Client(api_key=COINBASE_API_KEY)
        
        # Create a unique charge ID
        charge_id = str(uuid.uuid4())
        
        # Create metadata to track the user
        metadata = {
            "user_id": str(user_id),
            "plan_type": plan_type
        }
        
        # Create the charge
        charge_data = {
            "name": f"Ecliptica Bot - {plan['name']}",
            "description": plan['description'],
            "local_price": {
                "amount": str(plan['price']),
                "currency": "USD"
            },
            "pricing_type": "fixed_price",
            "metadata": metadata,
            "redirect_url": f"https://t.me/your_bot_username?start=payment_{charge_id}",
            "cancel_url": f"https://t.me/your_bot_username?start=cancel_{charge_id}"
        }
        
        charge = client.charge.create(**charge_data)
        
        # Store the payment in our database
        async with db_pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO payments
                (payment_id, uid, amount, currency, plan_type, status)
                VALUES ($1, $2, $3, $4, $5, 'pending')
                ''',
                charge.id, user_id, plan['price'], 'USD', plan_type
            )
            
        # Return the hosted URL and charge ID
        return True, charge.hosted_url, charge.id
        
    except Exception as e:
        logging.error(f"Error creating payment charge: {str(e)}")
        return False, "", str(e)

async def verify_promo_code(code: str) -> bool:
    """Verify if a promo code is valid."""
    return code.upper() in PROMO_CODES

# Add cache to store responses
RESPONSE_CACHE = {}  # Format: {asset: {"response": str, "timestamp": datetime, "type": "market|setup"}}
MAX_CACHE_AGE = 3600  # Cache responses for 1 hour

async def rei_call(prompt: str, asset_name: str = None, analysis_type: str = None) -> str:
    """Make an async call to REI API with better error handling, retry logic, and chunking for long prompts."""
    logger.info(f"Making REI API call with prompt length: {len(prompt)}")
    print(f"STDOUT: Making REI API call with prompt length: {len(prompt)}", file=sys.stdout)
    print(f"STDERR: Making REI API call with prompt length: {len(prompt)}", file=sys.stderr)
    
    # Check cache first if asset_name is provided
    if asset_name and analysis_type:
        cache_key = f"{asset_name}:{analysis_type}"
        if cache_key in RESPONSE_CACHE:
            cache_entry = RESPONSE_CACHE[cache_key]
            cache_age = (datetime.now() - cache_entry["timestamp"]).total_seconds()
            
            # Use cached response if it's fresh enough
            if cache_age < MAX_CACHE_AGE:
                logger.info(f"Using cached response for {cache_key}, age: {cache_age:.1f} seconds")
                return cache_entry["response"]
            else:
                logger.info(f"Cached response for {cache_key} expired ({cache_age:.1f} seconds old)")
    
    # Start watchdog timer
    start_watchdog()
    
    try:
        # For shorter prompts, just make a regular call
        result = await _rei_call_internal(prompt)
        
        # Cache the result if asset_name is provided
        if asset_name and analysis_type:
            cache_key = f"{asset_name}:{analysis_type}"
            RESPONSE_CACHE[cache_key] = {
                "response": result,
                "timestamp": datetime.now(),
                "type": analysis_type
            }
            logger.info(f"Cached response for {cache_key}")
        
        # Stop watchdog timer as we're done
        stop_watchdog()
        return result
    except Exception as e:
        # Make sure to stop the watchdog if we hit an exception
        stop_watchdog()
        logger.error(f"rei_call failed with exception: {str(e)}", exc_info=True)
        
        # Try alternative API if available
        try:
            logger.info("Primary API failed, trying alternative API")
            result = await rei_call_alternative(prompt)
            
            # Cache the alternative result too
            if asset_name and analysis_type:
                cache_key = f"{asset_name}:{analysis_type}"
                RESPONSE_CACHE[cache_key] = {
                    "response": result,
                    "timestamp": datetime.now(),
                    "type": analysis_type
                }
                logger.info(f"Cached alternative response for {cache_key}")
                
            return result
        except Exception as alt_e:
            logger.error(f"Alternative API also failed: {str(alt_e)}")
            
            # Use fallback if both APIs fail
            if asset_name:
                fallback = get_fallback_response(asset_name, analysis_type or "")
                logger.info(f"Using fallback response for {asset_name}")
                return fallback
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
    max_retries = 1  # Reduced from 2 to make fallback happen faster
    retry_count = 0
    last_error = None
    
    while retry_count <= max_retries:
        try:
            # Reset watchdog timer for this attempt
            start_watchdog()
            
            # Use a timeout of 30 seconds to avoid long waits
            logger.info(f"REI API call attempt {retry_count + 1}/{max_retries + 1}")
            print(f"STDOUT: REI API call attempt {retry_count + 1}/{max_retries + 1}", file=sys.stdout)
            print(f"STDERR: REI API call attempt {retry_count + 1}/{max_retries + 1}", file=sys.stderr)
            
            async with aiohttp.ClientSession() as session:
                # Use shorter 30 seconds timeout
                resp = await session.post(
                    "https://api.reisearch.box/v1/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=30  # Reduced to 30 seconds to fail faster and try fallback
                )
                
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
                logger.info(f"Retrying in 3 seconds...")
                await asyncio.sleep(3)  # Shorter wait before retrying
            continue
        except Exception as e:
            # Reset the watchdog timer
            stop_watchdog()
            logger.error(f"Error in _rei_call_internal: {str(e)}")
            last_error = e
            retry_count += 1
            if retry_count <= max_retries:
                logger.info(f"Retrying in 3 seconds...")
                await asyncio.sleep(3)
            continue
    
    # If we get here, all retries failed
    logger.error(f"All {max_retries + 1} attempts failed. Last error: {str(last_error)}")
    print(f"STDERR: All {max_retries + 1} attempts failed. Last error: {str(last_error)}", file=sys.stderr)
    raise Exception(f"Failed to get response after {max_retries + 1} attempts: {str(last_error)}")

# Add an alternative REI API call function that uses a different endpoint
async def rei_call_alternative(prompt: str) -> str:
    """Make an API call to a more reliable API endpoint as fallback."""
    logger.info(f"Making alternative API call with prompt length: {len(prompt)}")
    print(f"STDOUT: Trying alternative API endpoint with prompt length: {len(prompt)}", file=sys.stdout)
    
    # Use a more reliable API that doesn't rely on custom REI endpoint
    # Use OpenAI-compatible endpoint with default key
    api_key = REI_KEY  # Reuse the same key for simplicity
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    # Use a simpler model with lower timeout
    body = {
        "model": "gpt-3.5-turbo",  # Use a standard, stable model
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
        "temperature": 0.3
    }
    
    try:
        # Use a short timeout to avoid waiting too long
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",  # Standard OpenAI API endpoint
                headers=headers,
                json=body,
                timeout=30  # Short timeout
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

# Add a fallback response function
def get_fallback_response(asset: str = "", analysis_type: str = "") -> str:
    """Generate a detailed fallback response when the API fails."""
    if not asset:
        return ("I'm currently experiencing issues connecting to my analysis service. "
                "Please try again in a few minutes. If the issue persists, consider using a simpler request.")
    
    if analysis_type == "market":
        asset_name = asset.replace("-PERP", "")
        return (f"I'm sorry, I couldn't retrieve a full market analysis for {asset} at the moment due to technical issues. "
                f"Some key points about {asset} to consider:\n\n"
                f"• Check the current price and 24h change on your preferred exchange\n"
                f"• Look at the daily timeframe for major support/resistance levels\n"
                f"• Monitor funding rates if taking a leveraged position\n"
                f"• Consider overall market sentiment and correlation with BTC\n\n"
                f"For {asset_name} specifically:\n"
                f"• The daily chart can provide insights into the broader trend\n"
                f"• Recent market volatility may create opportunities for scalping\n"
                f"• Watch for key technical levels that may act as support/resistance\n"
                f"• Consider correlation with overall market conditions\n\n"
                f"Please try again later for a more detailed analysis.")
    elif analysis_type == "setup":
        asset_name = asset.replace("-PERP", "")
        return (f"I'm sorry, I couldn't generate a complete trade setup for {asset} at this time due to technical issues. "
                f"For a basic approach to trading {asset}:\n\n"
                f"• Look for key support/resistance levels on the 4h and daily charts\n"
                f"• Consider setting stops 2-5% below your entry (based on your risk profile)\n"
                f"• Target profit taking at previous resistance levels\n"
                f"• Always manage your position size based on your risk tolerance\n\n"
                f"For {asset_name} specifically:\n"
                f"• Consider monitoring volume for confirmation of trend\n"
                f"• Look for confluence between multiple timeframes\n"
                f"• Set a clear risk-to-reward ratio based on your trading plan\n"
                f"• Always have a clear exit strategy before entering a trade\n\n"
                f"Please try again later for a more detailed trade setup.")
    else:
        return (f"I'm having trouble connecting to my analysis service to provide information about {asset}. "
                f"Please try again in a few minutes, or try a different request.")

# ───────────────────────────── telegram callbacks ────────────────────────── #
INIT_MENU = ReplyKeyboardMarkup(
    [["▶️ Start"]], resize_keyboard=True, one_time_keyboard=True
)
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], 
     ["🤖 Ask AI", "❓ FAQ"],
     ["💰 Subscription", "🎫 Enter Code"]],
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

# ─── Subscription handlers ────────────────────────────────────────────────── #
async def subscription_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle subscription command and show subscription options."""
    user_id = update.effective_user.id
    
    # Get current subscription status
    subscription = await get_user_subscription(user_id)
    
    if subscription and subscription['is_active']:
        # User already has an active subscription
        plan_type = subscription['plan_type']
        days_remaining = subscription['days_remaining']
        
        if subscription['promo_code']:
            # This is a promo subscription
            await update.message.reply_text(
                f"✅ You currently have an active subscription using promo code *{subscription['promo_code']}*.\n\n"
                f"Your subscription expires in *{days_remaining} days*.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            # Paid subscription
            plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'Custom Plan')
            await update.message.reply_text(
                f"✅ You currently have an active *{plan_name}* subscription.\n\n"
                f"Your subscription expires in *{days_remaining} days*.",
                parse_mode=ParseMode.MARKDOWN
            )
        
        # Option to extend/upgrade
        buttons = [
            [InlineKeyboardButton("Extend Subscription", callback_data="sub:extend")],
            [InlineKeyboardButton("Back to Main Menu", callback_data="sub:cancel")]
        ]
        markup = InlineKeyboardMarkup(buttons)
        
        await update.message.reply_text(
            "Would you like to extend your subscription?",
            reply_markup=markup
        )
        return SUBSCRIPTION
    
    # User doesn't have an active subscription - show plans
    buttons = []
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        # Create a button for each plan
        price_text = f"${plan['price']:.2f}"
        if plan_id == "quarterly":
            price_text += " (Save 17%)"
        elif plan_id == "annual":
            price_text += " (Save 37%)"
            
        buttons.append([
            InlineKeyboardButton(
                f"{plan['name']} - {price_text}",
                callback_data=f"sub:select:{plan_id}"
            )
        ])
    
    # Add promo code option and cancel
    buttons.append([InlineKeyboardButton("I have a promo code", callback_data="sub:promo")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="sub:cancel")])
    
    markup = InlineKeyboardMarkup(buttons)
    
    # Show usage count for free tier
    usage_count = 0
    if subscription:
        usage_count = subscription['usage_count']
    
    remaining = max(0, FREE_ANALYSIS_LIMIT - usage_count)
    
    await update.message.reply_text(
        "📊 *Ecliptica Trading Bot Subscription*\n\n"
        "Get unlimited access to premium trading analysis and recommendations.\n\n"
        f"You have used {usage_count}/{FREE_ANALYSIS_LIMIT} free analyses.\n"
        f"Choose a subscription plan below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup
    )
    
    return SUBSCRIPTION

async def enter_code_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle promo code entry command."""
    await update.message.reply_text(
        "Please enter your promo code for free access.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ENTER_CODE

async def handle_code_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Process promo code entry from user."""
    code = update.message.text.strip().upper()
    user_id = update.effective_user.id
    
    # Verify the code
    if await verify_promo_code(code):
        # Valid code, create subscription
        promo_details = PROMO_CODES[code]
        success = await create_subscription(user_id, None, None, code)
        
        if success:
            await update.message.reply_text(
                f"✅ Success! Your promo code *{code}* has been activated.\n\n"
                f"You now have *{promo_details['days']} days* of free access to all premium features.\n\n"
                f"*Description:* {promo_details['description']}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=MAIN_MENU
            )
        else:
            await update.message.reply_text(
                "❌ There was an error activating your promo code. Please try again later.",
                reply_markup=MAIN_MENU
            )
    else:
        # Invalid code
        await update.message.reply_text(
            "❌ Invalid promo code. Please check and try again, or subscribe to a premium plan.",
            reply_markup=MAIN_MENU
        )
    
    return ConversationHandler.END

async def handle_subscription_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle subscription callback queries."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if ":" not in query.data:
        logger.error(f"Invalid subscription callback format: {query.data}")
        await query.message.reply_text("An error occurred. Please try again.")
        return ConversationHandler.END
    
    action = query.data.split(":", 1)[1]
    
    if action.startswith("select:"):
        # User selected a plan
        plan_id = action.split(":", 1)[1]
        
        if plan_id not in SUBSCRIPTION_PLANS:
            await query.message.reply_text("Invalid plan selected. Please try again.")
            return ConversationHandler.END
            
        # Create Coinbase charge
        success, checkout_url, charge_id = await create_payment_charge(user_id, plan_id)
        
        if success and checkout_url:
            # Store the charge ID in user data for reference
            ctx.user_data["payment_charge_id"] = charge_id
            
            # Create payment button
            button = InlineKeyboardButton("Pay Now", url=checkout_url)
            markup = InlineKeyboardMarkup([[button]])
            
            plan = SUBSCRIPTION_PLANS[plan_id]
            
            await query.message.edit_text(
                f"🔗 *Payment for {plan['name']}*\n\n"
                f"• Amount: ${plan['price']:.2f} USD\n"
                f"• Duration: {plan['days']} days\n\n"
                "Click the button below to complete your payment. Once payment is confirmed, "
                "your subscription will be activated automatically.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup
            )
        else:
            # Payment creation failed
            await query.message.edit_text(
                "❌ There was an error setting up the payment. Please try again later."
            )
            
        return ConversationHandler.END
            
    elif action == "promo":
        # User wants to enter promo code
        await query.message.edit_text("Please enter your promo code:")
        return ENTER_CODE
        
    elif action == "extend":
        # Handle subscription extension - just show the plans again
        buttons = []
        for plan_id, plan in SUBSCRIPTION_PLANS.items():
            price_text = f"${plan['price']:.2f}"
            if plan_id == "quarterly":
                price_text += " (Save 17%)"
            elif plan_id == "annual":
                price_text += " (Save 37%)"
                
            buttons.append([
                InlineKeyboardButton(
                    f"{plan['name']} - {price_text}",
                    callback_data=f"sub:select:{plan_id}"
                )
            ])
        
        buttons.append([InlineKeyboardButton("Cancel", callback_data="sub:cancel")])
        markup = InlineKeyboardMarkup(buttons)
        
        await query.message.edit_text(
            "Select a subscription plan to extend your access:",
            reply_markup=markup
        )
        return SUBSCRIPTION
        
    elif action == "cancel":
        # User cancelled the subscription process
        await query.message.edit_text("Subscription process cancelled.")
        return ConversationHandler.END
        
    else:
        logger.warning(f"Unknown subscription action: {action}")
        await query.message.edit_text("Invalid option. Please try again.")
        return ConversationHandler.END

async def handle_webhook(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Coinbase Commerce webhook for payment confirmation."""
    # This needs to be configured in Railway as a webhook endpoint
    try:
        # Get the request data
        request_data = json.loads(update.message.text)
        
        # Verify the webhook signature if available
        if COINBASE_WEBHOOK_SECRET:
            signature = request_data.get('signature', '')
            payload = request_data.get('payload', {})
            
            # Verify the signature
            expected_sig = hmac.new(
                COINBASE_WEBHOOK_SECRET.encode('utf-8'),
                json.dumps(payload).encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            if signature != expected_sig:
                logger.warning("Invalid webhook signature")
                return
        
        # Extract charge information
        event_type = request_data.get('type', '')
        if event_type != 'charge:confirmed':
            # Only process confirmed charges
            logger.info(f"Ignoring webhook event of type: {event_type}")
            return
            
        # Get the charge data
        charge_data = request_data.get('data', {}).get('object', {})
        charge_id = charge_data.get('id', '')
        metadata = charge_data.get('metadata', {})
        user_id = metadata.get('user_id', '')
        plan_type = metadata.get('plan_type', '')
        
        if not charge_id or not user_id or not plan_type:
            logger.error("Missing required data in webhook payload")
            return
            
        # Update payment status in database
        async with db_pool.acquire() as conn:
            # Mark payment as completed
            await conn.execute(
                '''
                UPDATE payments
                SET status = 'completed', completed_at = NOW()
                WHERE payment_id = $1
                ''',
                charge_id
            )
            
            # Create or update subscription
            await create_subscription(int(user_id), plan_type, charge_id)
            
        # Send confirmation message to user
        plan = SUBSCRIPTION_PLANS.get(plan_type, {})
        plan_name = plan.get('name', 'Subscription')
        
        await ctx.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ Payment confirmed! Your *{plan_name}* has been activated. Thank you for subscribing to Ecliptica Trading Bot!",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")

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
        f"- Preferred Response Detail: {profile.get('verbosity', 'balanced')}\n"
    )

# Helper function to adjust prompt based on verbosity
def adjust_for_verbosity(prompt: str, verbosity: str = "balanced") -> str:
    """Adjust the prompt based on user's verbosity preference"""
    if verbosity == "brief":
        return f"{prompt}\n\nPlease provide a concise and brief response focusing only on the most important points. Aim for clarity and brevity over comprehensiveness."
    elif verbosity == "detailed":
        return f"{prompt}\n\nPlease provide a comprehensive and detailed response covering all aspects thoroughly. Include additional context and explanations where helpful."
    else:  # balanced (default)
        return prompt

# Create keyboard versions for waiting states
WAITING_MESSAGE = "⏳ Processing request... Please wait."
WAITING_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("⏳ Please wait...", callback_data="wait:ignore")]
])

# Add a function to show waiting state
async def show_waiting_state(query, message=None):
    """Replace buttons with waiting indicator."""
    try:
        if message is None:
            message = WAITING_MESSAGE
            
        if isinstance(query, Update):
            # If it's a direct command, just send new message
            await query.message.reply_text(message)
            return
            
        # For button clicks, try to edit the message
        try:
            # Try to edit existing message if it has inline keyboard
            await query.message.edit_text(
                f"{query.message.text_html}\n\n{message}",
                reply_markup=WAITING_KEYBOARD,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            # If edit fails, send a new message
            logger.debug(f"Couldn't edit message, sending new: {str(e)}")
            await query.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error showing waiting state: {str(e)}")
        # Don't raise - this is a UI enhancement, not critical functionality

# Update the button_click handler to set waiting states
async def button_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button clicks."""
    query = update.callback_query
    if not query:
        logger.error("Received button_click call without callback query")
        return

    user_id = query.from_user.id
    logger.info(f"Received callback query with data: {query.data} from user {user_id}")
    
    # For wait:ignore callbacks, just acknowledge and do nothing
    if query.data.startswith("wait:"):
        await query.answer("Still processing your previous request. Please wait...", show_alert=True)
        return
    
    # Check if user already has a request in progress
    is_available = await check_user_state(user_id)
    if not is_available:
        logger.warning(f"User {user_id} already has a request in progress, ignoring new request")
        await query.answer("Please wait for your current request to complete", show_alert=True)
        return
    
    try:
        # Mark user as processing
        await set_user_processing(user_id, True)
        
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
            await set_user_processing(user_id, False)
            return
            
        action, value = query.data.split(":", 1)
        logger.debug(f"Processing action: {action}, value: {value}")
        
        # Special handling for setup:start action
        if action == "setup" and value == "start":
            stop_watchdog()
            result = await setup_start(query, ctx)
            await set_user_processing(user_id, False)
            return result
            
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
            await set_user_processing(user_id, False)
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
                    await handle_trade_setup(query, asset, profile_context, profile)
                else:
                    logger.warning(f"Unknown analysis type: {analysis_type}")
                    await query.message.reply_text(
                        "Invalid analysis type. Please try again.",
                        reply_markup=MAIN_MENU
                    )
                    
            except Exception as e:
                logger.error(f"Error in analysis handler: {str(e)}", exc_info=True)
                await query.message.reply_text(
                    "An unexpected error occurred. Please try again later.",
                    reply_markup=MAIN_MENU
                )
        
        elif action == "trade":
            logger.info(f"Processing trade action with value: {value}")
            
            # Get user's verbosity preference for suggestion prompts
            verbosity = profile.get('verbosity', 'balanced')
            
            if value == "SUGGEST":
                # Show waiting state
                await show_waiting_state(query, "🧠 Analyzing market conditions... This may take a minute.")
                logger.debug("Processing suggestion request")
                
                try:
                    start_time = datetime.now()
                    
                    base_prompt = (
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
                    
                    # Adjust prompt for verbosity
                    prompt = adjust_for_verbosity(base_prompt, verbosity)
                    
                    try:
                        suggestion = await rei_call(prompt)
                    except Exception as primary_e:
                        logger.warning(f"Primary API call failed for suggestion: {str(primary_e)}")
                        # Use a simple fallback
                        suggestion = get_fallback_response("", "")
                    
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
        # Mark user as no longer processing
        await set_user_processing(user_id, False)
            
    except Exception as e:
        # Make sure watchdog is stopped in case of error
        stop_watchdog()
        # Mark user as no longer processing even if there was an error
        await set_user_processing(user_id, False)
        logger.error(f"Error in button_click: {str(e)}", exc_info=True)
        try:
            await query.message.reply_text(
                "An error occurred. Please try again or contact support.",
                reply_markup=MAIN_MENU
            )
        except Exception as nested_e:
            logger.error(f"Failed to send error message: {str(nested_e)}")

# Update the handlers to use the waiting state
async def handle_market_analysis(query, asset, profile_context, profile):
    """Handle market analysis request with reliable fallbacks."""
    user_id = query.from_user.id
    try:
        # Check subscription access
        has_access, message = await check_subscription_access(user_id)
        
        if not has_access:
            # User doesn't have access - show subscription options
            buttons = [[InlineKeyboardButton("Subscribe Now", callback_data="sub:show")]]
            markup = InlineKeyboardMarkup(buttons)
            
            await query.message.reply_text(
                f"⚠️ {message}",
                reply_markup=markup
            )
            return
            
        # Increment usage count if not on a paid plan
        subscription = await get_user_subscription(user_id)
        if not subscription or not subscription['is_active']:
            count = await increment_usage_count(user_id)
            logger.info(f"User {user_id} analysis count incremented to {count}")
            
            # Show free tier message if applicable
            if message:
                await query.message.reply_text(f"ℹ️ {message}")
        
        # Show waiting state with specific message for this analysis
        await show_waiting_state(query, f"📊 Analyzing {asset} market conditions... (this may take a minute)")
        
        logger.debug("Preparing market analysis prompt")
        
        # Get user's verbosity preference
        verbosity = profile.get('verbosity', 'balanced')
        logger.info(f"Using {verbosity} verbosity for user {user_id}")
        
        base_prompt = (
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
        
        # Adjust prompt based on verbosity
        prompt = adjust_for_verbosity(base_prompt, verbosity)
        
        logger.debug("Calling REI API for market analysis")
        start_time = datetime.now()
        logger.debug(f"Starting REI API call at {start_time}")
        
        # Call REI API with asset name and analysis type for caching
        try:
            response = await rei_call(prompt, asset, "market")
            logger.info("Market analysis API call succeeded")
        except Exception as e:
            logger.error(f"All API attempts failed: {str(e)}")
            # Fallback is now handled within rei_call
            response = get_fallback_response(asset, "market")
            logger.info("Using default fallback response")
        
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
    finally:
        # Always release the user's processing state
        await set_user_processing(user_id, False)

# Update the trade setup handler to use the waiting state
async def handle_trade_setup(query, asset, profile_context, profile):
    """Handle trade setup request with reliable fallbacks."""
    user_id = query.from_user.id
    try:
        # Check subscription access (similar to market analysis)
        has_access, message = await check_subscription_access(user_id)
        
        if not has_access:
            buttons = [[InlineKeyboardButton("Subscribe Now", callback_data="sub:show")]]
            markup = InlineKeyboardMarkup(buttons)
            
            await query.message.reply_text(
                f"⚠️ {message}",
                reply_markup=markup
            )
            return
            
        # Increment usage if needed
        subscription = await get_user_subscription(user_id)
        if not subscription or not subscription['is_active']:
            count = await increment_usage_count(user_id)
            logger.info(f"User {user_id} analysis count incremented to {count}")
            
            if message:
                await query.message.reply_text(f"ℹ️ {message}")
            
        # Show waiting state with specific message for this setup
        await show_waiting_state(query, f"🎯 Generating trade setup for {asset}... (this may take a minute)")
        
        logger.debug("Preparing trade setup prompt")
        
        # Get user's verbosity preference
        verbosity = profile.get('verbosity', 'balanced')
        logger.info(f"Using {verbosity} verbosity for user {user_id}")
        
        base_prompt = (
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
        
        # Adjust prompt based on verbosity
        prompt = adjust_for_verbosity(base_prompt, verbosity)
        
        logger.debug("Calling REI API for trade setup")
        start_time = datetime.now()
        logger.debug(f"Starting REI API call at {start_time}")
        
        # Call REI API with asset name and analysis type for caching
        try:
            response = await rei_call(prompt, asset, "setup")
            logger.info("Trade setup API call succeeded")
        except Exception as e:
            logger.error(f"All API attempts failed: {str(e)}")
            # Fallback is now handled within rei_call
            response = get_fallback_response(asset, "setup")
            logger.info("Using default fallback response")
        
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
        logger.error(f"Complete failure in trade setup: {str(e)}", exc_info=True)
        # Last resort fallback
        await query.message.reply_text(
            get_fallback_response(asset, "setup"),
            reply_markup=MAIN_MENU
        )
    finally:
        # Always release the user's processing state
        await set_user_processing(user_id, False)

async def handle_custom_asset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle custom asset input from user."""
    asset = update.message.text.strip().upper()
    user_id = update.effective_user.id
    logger.info(f"Received custom asset input: {asset} from user {user_id}")
    
    # Check if user has a processing request
    is_available = await check_user_state(user_id)
    if not is_available:
        logger.warning(f"User {user_id} already has a request in progress, ignoring")
        await update.message.reply_text("Please wait for your current request to complete.")
        return
        
    # Skip special commands/keywords that should not be treated as assets
    special_keywords = ["SUBSCRIPTION", "ENTER CODE", "FAQ", "ASK AI", "TRADE", "SETUP"]
    
    for keyword in special_keywords:
        if keyword in asset:
            logger.info(f"Ignoring special keyword: {asset}")
            return
        
    if not asset:
        await update.message.reply_text("Please enter a valid asset symbol.")
        return
        
    # Format the asset with -PERP suffix if not already present
    if not asset.endswith("-PERP"):
        asset = f"{asset}-PERP"
    
    # Mark user as processing
    await set_user_processing(user_id, True)
    
    # Create analysis options buttons
    buttons = [
        [InlineKeyboardButton("📊 Trade Setup (Entry/SL/TP)", callback_data=f"analysis:setup:{asset}")],
        [InlineKeyboardButton("📈 Market Analysis (Tech/Fund)", callback_data=f"analysis:market:{asset}")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    
    try:
        await update.message.reply_text(
            f"Choose analysis type for {asset}:",
            reply_markup=markup
        )
    except Exception as e:
        logger.error(f"Error creating analysis options: {str(e)}")
        # Release user processing state on error
        await set_user_processing(user_id, False)

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
    
    # Subscription conversation handler
    sub_conv = ConversationHandler(
        entry_points=[
            CommandHandler('subscription', subscription_cmd),
            MessageHandler(filters.Regex('^💰 Subscription$'), subscription_cmd),
            CallbackQueryHandler(handle_subscription_callback, pattern=r'^sub:')
        ],
        states={
            SUBSCRIPTION: [CallbackQueryHandler(handle_subscription_callback, pattern=r'^sub:')],
            ENTER_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code_entry)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Add Enter Code handler
    enter_code_conv = ConversationHandler(
        entry_points=[
            CommandHandler('entercode', enter_code_cmd),
            MessageHandler(filters.Regex('^🎫 Enter Code$'), enter_code_cmd)
        ],
        states={
            ENTER_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code_entry)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    # Add all handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.Regex('^▶️ Start$'), main_menu))
    application.add_handler(setup_conv)
    application.add_handler(sub_conv)
    application.add_handler(enter_code_conv)
    application.add_handler(CommandHandler('trade', trade_start))
    application.add_handler(MessageHandler(filters.Regex('^📊 Trade$'), trade_start))
    application.add_handler(CommandHandler('ask', ask_cmd))
    application.add_handler(MessageHandler(filters.Regex('^🤖 Ask AI$'), ask_cmd))
    application.add_handler(CommandHandler('faq', faq_cmd))
    application.add_handler(MessageHandler(filters.Regex('^❓ FAQ$'), faq_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('checkdb', check_db_cmd))
    application.add_handler(CallbackQueryHandler(button_click, pattern=r'^(trade|analysis):'))
    application.add_handler(CallbackQueryHandler(handle_subscription_callback, pattern=r'^sub:'))
    
    # Add the custom asset handler as the LAST handler
    # It should only receive messages that aren't caught by any of the above handlers
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
