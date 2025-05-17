"""
Subscription handlers for the Ecliptica Trading Bot.
This module provides subscription and payment processing functionality using Coinbase Commerce.
"""
import os
import logging
import json
import hmac
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import asyncpg
from coinbase_commerce.client import Client
from coinbase_commerce.error import SignatureVerificationError, WebhookInvalidPayload

import asyncio

# Configure logging
logger = logging.getLogger(__name__)

# Subscription configuration
SUBSCRIPTION_PLANS = {
    "monthly": {"name": "Monthly Plan", "price": 19.99, "days": 30, "description": "Full access to all features for 30 days"},
    "quarterly": {"name": "Quarterly Plan", "price": 49.99, "days": 90, "description": "Full access to all features for 90 days (save 17%)"},
    "annual": {"name": "Annual Plan", "price": 149.99, "days": 365, "description": "Full access to all features for a full year (save 37%)"}
}

# Valid promo codes
PROMO_CODES = {
    "ECLIPTICA2024": {"days": 30, "description": "Free 30-day trial"},
    "PERPSMASTER": {"days": 90, "description": "Free 90-day access for early supporters"}
}

# Free tier limits
FREE_ANALYSIS_LIMIT = 5  # Number of free trade analyses before requiring subscription

# Conversation states
SUBSCRIPTION, ENTER_CODE = 4, 5  # Assuming existing states are 0-3

# Coinbase Commerce API credentials
COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "")
COINBASE_WEBHOOK_SECRET = os.environ.get("COINBASE_WEBHOOK_SECRET", "")

# Main menu keyboard for returning to main menu
MAIN_MENU = ReplyKeyboardMarkup(
    [["🔧 Setup Profile", "📊 Trade"], 
     ["🤖 Ask AI", "❓ FAQ"],
     ["💰 Subscription", "🎟️ Enter Code"]],
    resize_keyboard=True
)

# Database pool reference - set this from main bot
db_pool = None

def set_db_pool(pool):
    """Set the database pool from the main bot."""
    global db_pool
    db_pool = pool

async def init_subscription_tables():
    """Initialize subscription-related database tables."""
    if not db_pool:
        logger.error("Database pool not initialized")
        return False
        
    try:
        async with db_pool.acquire() as conn:
            # Create subscriptions table with auto_renew and renewal tracking
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    uid BIGINT PRIMARY KEY,
                    plan_type TEXT,
                    start_date TIMESTAMP WITH TIME ZONE,
                    end_date TIMESTAMP WITH TIME ZONE,
                    payment_id TEXT,
                    status TEXT,
                    usage_count INTEGER DEFAULT 0,
                    promo_code TEXT,
                    auto_renew BOOLEAN DEFAULT FALSE,
                    last_renewal_attempt TIMESTAMP WITH TIME ZONE,
                    renewal_payment_id TEXT
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
                    completed_at TIMESTAMP WITH TIME ZONE,
                    is_renewal BOOLEAN DEFAULT FALSE
                )
            ''')
            
            # Check if existing subscriptions table needs to be updated with new columns
            columns = await conn.fetch('''
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'subscriptions'
            ''')
            column_names = [col['column_name'] for col in columns]
            
            # Add auto_renew column if it doesn't exist
            if 'auto_renew' not in column_names:
                logger.info("Adding auto_renew column to subscriptions table")
                await conn.execute('''
                    ALTER TABLE subscriptions 
                    ADD COLUMN auto_renew BOOLEAN DEFAULT FALSE
                ''')
                
            # Add last_renewal_attempt column if it doesn't exist
            if 'last_renewal_attempt' not in column_names:
                logger.info("Adding last_renewal_attempt column to subscriptions table")
                await conn.execute('''
                    ALTER TABLE subscriptions 
                    ADD COLUMN last_renewal_attempt TIMESTAMP WITH TIME ZONE
                ''')
                
            # Add renewal_payment_id column if it doesn't exist
            if 'renewal_payment_id' not in column_names:
                logger.info("Adding renewal_payment_id column to subscriptions table")
                await conn.execute('''
                    ALTER TABLE subscriptions 
                    ADD COLUMN renewal_payment_id TEXT
                ''')
                
            # Check payments table columns
            payment_columns = await conn.fetch('''
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'payments'
            ''')
            payment_column_names = [col['column_name'] for col in payment_columns]
            
            # Add is_renewal column if it doesn't exist
            if 'is_renewal' not in payment_column_names:
                logger.info("Adding is_renewal column to payments table")
                await conn.execute('''
                    ALTER TABLE payments 
                    ADD COLUMN is_renewal BOOLEAN DEFAULT FALSE
                ''')
                
            logger.info("Subscription tables initialized successfully")
            return True
            
    except Exception as e:
        logger.error(f"Error initializing subscription tables: {str(e)}")
        return False

# ─── Database operations ────────────────────────────────────────────────────── #
async def get_user_subscription(user_id: int) -> Optional[Dict]:
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

async def create_subscription(user_id: int, plan_type: str, payment_id: str = None, promo_code: str = None, auto_renew: bool = False) -> bool:
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
            auto_renew = False  # Promo codes can't auto-renew
            
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
                        status = 'active', promo_code = $6, auto_renew = $7,
                        last_renewal_attempt = NULL, renewal_payment_id = NULL
                    WHERE uid = $1
                    ''',
                    user_id, plan_type, now, end_date, payment_id, promo_code, auto_renew
                )
            else:
                # Create new subscription
                await conn.execute(
                    '''
                    INSERT INTO subscriptions
                    (uid, plan_type, start_date, end_date, payment_id, status, usage_count, 
                     promo_code, auto_renew)
                    VALUES ($1, $2, $3, $4, $5, 'active', 0, $6, $7)
                    ''',
                    user_id, plan_type, now, end_date, payment_id, promo_code, auto_renew
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

# ─── Subscription renewal and notifications ────────────────────────────────────── #
async def check_expiring_subscriptions() -> None:
    """
    Check for subscriptions that are about to expire and need renewal or notification.
    This should be run daily by a scheduled task.
    """
    if not db_pool:
        logger.error("Database pool not initialized")
        return
        
    try:
        now = datetime.now(timezone.utc)
        
        # Get subscriptions expiring in the next 7 days that have auto-renew enabled
        renewal_cutoff = now + timedelta(days=3)  # Try to renew 3 days before expiry
        notification_cutoff = now + timedelta(days=7)  # Notify 7 days before expiry
        
        async with db_pool.acquire() as conn:
            # Get subscriptions for renewal
            renewal_subs = await conn.fetch('''
                SELECT uid, plan_type, end_date, payment_id 
                FROM subscriptions 
                WHERE status = 'active' 
                  AND auto_renew = TRUE 
                  AND end_date < $1 
                  AND (last_renewal_attempt IS NULL OR last_renewal_attempt < $2)
                  AND plan_type != 'promo'
            ''', renewal_cutoff, now - timedelta(days=1))  # Don't retry more often than daily
            
            for sub in renewal_subs:
                await process_subscription_renewal(sub)
                
            # Get subscriptions for notification (include both auto-renew and non-auto-renew)
            notification_subs = await conn.fetch('''
                SELECT uid, plan_type, end_date, auto_renew
                FROM subscriptions 
                WHERE status = 'active' 
                  AND end_date BETWEEN $1 AND $2
            ''', now, notification_cutoff)
            
            for sub in notification_subs:
                await send_expiration_notification(sub)
                
    except Exception as e:
        logger.error(f"Error checking expiring subscriptions: {str(e)}")

async def process_subscription_renewal(subscription) -> None:
    """
    Process a subscription renewal.
    """
    user_id = subscription['uid']
    plan_type = subscription['plan_type']
    
    try:
        logger.info(f"Processing renewal for user {user_id}, plan {plan_type}")
        
        # Update last_renewal_attempt timestamp
        async with db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE subscriptions 
                SET last_renewal_attempt = $1
                WHERE uid = $2
            ''', datetime.now(timezone.utc), user_id)
        
        # Create payment charge for renewal
        success, checkout_url, charge_id = await create_renewal_charge(user_id, plan_type)
        
        if success and charge_id:
            # Store the renewal payment ID
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    UPDATE subscriptions 
                    SET renewal_payment_id = $1
                    WHERE uid = $2
                ''', charge_id, user_id)
                
            # Send renewal notification to user
            await send_renewal_notification(user_id, plan_type, checkout_url)
        else:
            logger.error(f"Failed to create renewal charge for user {user_id}")
            
    except Exception as e:
        logger.error(f"Error processing subscription renewal: {str(e)}")

async def create_renewal_charge(user_id: int, plan_type: str) -> tuple[bool, str, str]:
    """
    Create a Coinbase Commerce charge for subscription renewal.
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
        
        # Create metadata to track the user
        metadata = {
            "user_id": str(user_id),
            "plan_type": plan_type,
            "is_renewal": "true"
        }
        
        # Create the charge
        charge_data = {
            "name": f"Ecliptica Bot - {plan['name']} Renewal",
            "description": f"Renewal of your {plan['name']} subscription",
            "local_price": {
                "amount": str(plan['price']),
                "currency": "USD"
            },
            "pricing_type": "fixed_price",
            "metadata": metadata,
            "redirect_url": f"https://t.me/your_bot_username?start=renewal_{uuid.uuid4()}",
            "cancel_url": f"https://t.me/your_bot_username?start=cancel_renewal"
        }
        
        charge = client.charge.create(**charge_data)
        
        # Store the payment in our database
        async with db_pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO payments
                (payment_id, uid, amount, currency, plan_type, status, is_renewal)
                VALUES ($1, $2, $3, $4, $5, 'pending', TRUE)
                ''',
                charge.id, user_id, plan['price'], 'USD', plan_type
            )
            
        # Return the hosted URL and charge ID
        return True, charge.hosted_url, charge.id
        
    except Exception as e:
        logger.error(f"Error creating renewal charge: {str(e)}")
        return False, "", str(e)

async def send_expiration_notification(subscription) -> None:
    """
    Send notification to user about subscription expiring soon.
    """
    try:
        user_id = subscription['uid']
        plan_type = subscription['plan_type']
        end_date = subscription['end_date']
        auto_renew = subscription.get('auto_renew', False)
        
        days_left = (end_date - datetime.now(timezone.utc)).days
        
        if days_left <= 0:
            return  # Don't send notification if already expired
            
        plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'subscription')
        
        if auto_renew:
            message = (
                f"ℹ️ *Subscription Renewal Notice*\n\n"
                f"Your {plan_name} subscription will automatically renew in *{days_left} days*.\n\n"
                f"• You will be charged the subscription fee\n"
                f"• Access will continue uninterrupted\n\n"
                f"To disable auto-renewal, use /manage_subscription"
            )
        else:
            message = (
                f"ℹ️ *Subscription Expiration Notice*\n\n"
                f"Your {plan_name} subscription will expire in *{days_left} days*.\n\n"
                f"To continue using premium features, please renew your subscription before it expires.\n\n"
                f"Use /subscribe to renew your subscription."
            )
            
        # Send the message
        from telegram import Bot
        from telegram.constants import ParseMode
        bot = Bot(token=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        await bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN
        )
        
        logger.info(f"Sent expiration notification to user {user_id}, days left: {days_left}")
            
    except Exception as e:
        logger.error(f"Error sending expiration notification: {str(e)}")

async def send_renewal_notification(user_id: int, plan_type: str, checkout_url: str) -> None:
    """
    Send notification to user about pending subscription renewal.
    """
    try:
        plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'subscription')
        
        message = (
            f"🔄 *Subscription Renewal*\n\n"
            f"Your {plan_name} subscription is due for renewal.\n\n"
            f"Please complete the payment to continue your subscription without interruption."
        )
        
        # Create payment button
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.constants import ParseMode
        
        bot = Bot(token=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        button = InlineKeyboardButton("Complete Renewal Payment", url=checkout_url)
        markup = InlineKeyboardMarkup([[button]])
        
        await bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup
        )
        
        logger.info(f"Sent renewal notification to user {user_id}")
            
    except Exception as e:
        logger.error(f"Error sending renewal notification: {str(e)}")

async def toggle_auto_renewal(user_id: int) -> tuple[bool, str]:
    """
    Toggle auto-renewal for a user's subscription.
    Returns (success, message)
    """
    try:
        if not db_pool:
            return False, "Database connection error"
            
        async with db_pool.acquire() as conn:
            # Get current auto-renew setting
            row = await conn.fetchrow('''
                SELECT auto_renew, plan_type 
                FROM subscriptions 
                WHERE uid = $1 AND status = 'active'
            ''', user_id)
            
            if not row:
                return False, "No active subscription found"
                
            current_setting = row['auto_renew']
            plan_type = row['plan_type']
            
            # Don't allow auto-renew for promo subscriptions
            if plan_type == 'promo' and not current_setting:
                return False, "Auto-renewal cannot be enabled for promotional subscriptions"
                
            # Toggle the setting
            new_setting = not current_setting
            
            await conn.execute('''
                UPDATE subscriptions 
                SET auto_renew = $1
                WHERE uid = $2
            ''', new_setting, user_id)
            
            if new_setting:
                return True, "Auto-renewal has been enabled for your subscription"
            else:
                return True, "Auto-renewal has been disabled for your subscription"
                
    except Exception as e:
        logger.error(f"Error toggling auto-renewal: {str(e)}")
        return False, "An error occurred while updating your subscription settings"

# ─── Subscription helpers ───────────────────────────────────────────────────── #
async def check_subscription_access(user_id: int) -> Tuple[bool, str]:
    """
    Check if a user has access to premium features.
    Returns (has_access, message)
    """
    # Get subscription status
    subscription = await get_user_subscription(user_id)
    
    if subscription and subscription['is_active']:
        # User has an active subscription (either paid or via promo code)
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
        return True, f"You have {remaining} free trade analyses remaining. Subscribe for unlimited access."
    
    # No access - need to subscribe or use a promo code
    return False, "You've reached the limit of free trade analyses. Please subscribe or use a promo code for unlimited access."

async def create_payment_charge(user_id: int, plan_type: str) -> Tuple[bool, str, str]:
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

# ─── Command handlers ────────────────────────────────────────────────────────── #
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
        f"You have used {usage_count}/{FREE_ANALYSIS_LIMIT} free trade analyses.\n"
        "Choose a subscription plan below or enter a promo code:",
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
    
    return -1  # End conversation

async def handle_subscription_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle subscription callback queries."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    
    if ":" not in query.data:
        logger.error(f"Invalid subscription callback format: {query.data}")
        await query.message.reply_text("An error occurred. Please try again.")
        return -1  # End conversation
    
    parts = query.data.split(":")
    action = parts[1]
    
    if action.startswith("select"):
        # User selected a plan - existing handler code
        plan_id = parts[2]
        
        if plan_id not in SUBSCRIPTION_PLANS:
            await query.message.reply_text("Invalid plan selected. Please try again.")
            return -1  # End conversation
            
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
            
        return -1  # End conversation
            
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
        return -1  # End conversation
        
    elif action == "show":
        # Redirect to subscription command
        await subscription_cmd(update, ctx)
        return SUBSCRIPTION
        
    elif action == "auto_renew":
        # Handle auto-renewal toggle
        enable = parts[2] == "on"
        
        async with db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE subscriptions 
                SET auto_renew = $1
                WHERE uid = $2 AND status = 'active'
            ''', enable, user_id)
            
        # Show updated subscription management
        await manage_subscription_cmd(update, ctx)
        return -1
        
    elif action == "renew_now":
        # Handle manual renewal request - show subscription plans
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
            "Select a subscription plan to renew:",
            reply_markup=markup
        )
        return SUBSCRIPTION
        
    elif action == "cancel_subscription":
        # Cancel the subscription
        async with db_pool.acquire() as conn:
            await conn.execute('''
                UPDATE subscriptions 
                SET status = 'cancelled', auto_renew = FALSE
                WHERE uid = $1 AND status = 'active'
            ''', user_id)
            
        await query.message.edit_text(
            "Your subscription has been cancelled. You will have access until the end of your current billing period.",
            reply_markup=MAIN_MENU
        )
        return -1
        
    elif action == "back_to_menu":
        # Return to main menu
        await query.message.edit_text(
            "Returning to main menu.",
            reply_markup=MAIN_MENU
        )
        return -1
        
    else:
        logger.warning(f"Unknown subscription action: {action}")
        await query.message.edit_text("Invalid option. Please try again.")
        return -1  # End conversation

async def manage_subscription_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle subscription management command."""
    user_id = update.effective_user.id
    
    # Get current subscription status
    subscription = await get_user_subscription(user_id)
    
    if not subscription or not subscription['is_active']:
        await update.message.reply_text(
            "❌ You don't have an active subscription to manage.\n\n"
            "Use /subscribe to purchase a subscription.",
            reply_markup=MAIN_MENU
        )
        return
        
    # Format subscription details
    plan_type = subscription['plan_type']
    end_date = subscription['end_date']
    days_remaining = subscription['days_remaining']
    auto_renew = subscription.get('auto_renew', False)
    promo_code = subscription.get('promo_code')
    
    if promo_code:
        plan_text = f"Promo Code: *{promo_code}*"
    else:
        plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'Unknown')
        plan_text = f"Plan: *{plan_name}*"
    
    auto_renew_status = "Enabled" if auto_renew else "Disabled"
    
    # Format expiration date
    expiry_date = end_date.strftime("%Y-%m-%d")
    
    # Create subscription management message
    message = (
        f"📊 *Subscription Management*\n\n"
        f"{plan_text}\n"
        f"Expires: *{expiry_date}* ({days_remaining} days remaining)\n"
        f"Auto-Renewal: *{auto_renew_status}*\n\n"
    )
    
    # Create management buttons
    buttons = []
    
    # Only show auto-renew toggle for paid subscriptions (not promos)
    if not promo_code:
        if auto_renew:
            buttons.append([InlineKeyboardButton("Disable Auto-Renewal", callback_data="sub:auto_renew:off")])
        else:
            buttons.append([InlineKeyboardButton("Enable Auto-Renewal", callback_data="sub:auto_renew:on")])
    
    # Add renewal button if nearing expiration
    if days_remaining < 14:  # Allow renewal in the last 14 days
        buttons.append([InlineKeyboardButton("Renew Now", callback_data="sub:renew_now")])
    
    # Cancel subscription option
    buttons.append([InlineKeyboardButton("Cancel Subscription", callback_data="sub:cancel_subscription")])
    
    # Back button
    buttons.append([InlineKeyboardButton("Back to Main Menu", callback_data="sub:back_to_menu")])
    
    markup = InlineKeyboardMarkup(buttons)
    
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=markup
    )

# Update webhook handler for renewals
async def handle_webhook(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Coinbase Commerce webhook for payment confirmation."""
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
        is_renewal = metadata.get('is_renewal') == 'true'
        
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
            
            # Check if this is a renewal or new subscription
            if is_renewal:
                logger.info(f"Processing renewal payment for user {user_id}")
                
                # Find the subscription with this renewal_payment_id
                existing_sub = await conn.fetchrow('''
                    SELECT * FROM subscriptions
                    WHERE uid = $1 AND renewal_payment_id = $2
                ''', int(user_id), charge_id)
                
                if existing_sub:
                    # It's a renewal of an existing subscription - extend it
                    await create_subscription(
                        int(user_id), 
                        plan_type, 
                        charge_id, 
                        None, 
                        True  # Keep auto-renew enabled
                    )
                    
                    # Send confirmation message
                    plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'Subscription')
                    await ctx.bot.send_message(
                        chat_id=int(user_id),
                        text=f"✅ Renewal payment confirmed! Your *{plan_name}* subscription has been extended. Thank you!",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    # Create normal subscription but mark as renewal
                    await create_subscription(int(user_id), plan_type, charge_id, None, True)
                    
                    # Send confirmation message
                    plan_name = SUBSCRIPTION_PLANS.get(plan_type, {}).get('name', 'Subscription')
                    await ctx.bot.send_message(
                        chat_id=int(user_id),
                        text=f"✅ Renewal payment confirmed! Your *{plan_name}* subscription has been activated. Thank you!",
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                # Normal subscription creation
                await create_subscription(int(user_id), plan_type, charge_id)
                
                # Send confirmation message
                plan = SUBSCRIPTION_PLANS.get(plan_type, {})
                plan_name = plan.get('name', 'Subscription')
                
                await ctx.bot.send_message(
                    chat_id=int(user_id),
                    text=f"✅ Payment confirmed! Your *{plan_name}* has been activated. Thank you for subscribing to Ecliptica Trading Bot!",
                    parse_mode=ParseMode.MARKDOWN
                )
        
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")

# ─── Usage in main bot handlers ───────────────────────────────────────────────── #
async def check_access_for_analysis(user_id: int, query) -> bool:
    """
    Check if user has access to analysis and handle response if not.
    Returns True if user has access, False otherwise.
    """
    # Check subscription access
    has_access, message = await check_subscription_access(user_id)
    
    if not has_access:
        # User doesn't have access - show subscription options and promo code option
        buttons = [
            [InlineKeyboardButton("Subscribe Now", callback_data="sub:show")],
            [InlineKeyboardButton("Enter Promo Code", callback_data="sub:promo")]
        ]
        markup = InlineKeyboardMarkup(buttons)
        
        await query.message.reply_text(
            f"⚠️ {message}",
            reply_markup=markup
        )
        return False
    
    # Increment usage count if not on a paid plan
    subscription = await get_user_subscription(user_id)
    if not subscription or not subscription['is_active']:
        count = await increment_usage_count(user_id)
        logger.info(f"User {user_id} trade analysis count incremented to {count}")
        
        # Show free tier message if applicable
        if message:
            await query.message.reply_text(f"ℹ️ {message}")
    
    return True 

# Add a function to start a scheduler for renewal checks

async def start_subscription_scheduler():
    """Start a scheduler to run renewal checks regularly."""
    logger.info("Starting subscription renewal scheduler")
    
    async def renewal_check_task():
        try:
            logger.info("Running scheduled subscription renewal check")
            await check_expiring_subscriptions()
        except Exception as e:
            logger.error(f"Error in scheduled renewal check: {str(e)}")
    
    # Create an ongoing task to periodically check for renewals
    async def scheduler_loop():
        while True:
            await renewal_check_task()
            # Run once per day (86400 seconds)
            await asyncio.sleep(86400)
    
    # Start the scheduler as a background task
    asyncio.create_task(scheduler_loop())
    logger.info("Subscription scheduler started successfully") 