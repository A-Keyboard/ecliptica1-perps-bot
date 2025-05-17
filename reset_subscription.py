#!/usr/bin/env python3
"""
Reset a user's subscription for testing purposes.
This script resets a user's subscription to free status or applies a promo code.

Usage:
  python reset_subscription.py <user_id> [promo_code]
  
  If promo_code is provided, it will apply that code to the user.
  If not provided, it will reset the user to free status.
"""

import asyncio
import asyncpg
import sys
import os
from datetime import datetime, timezone, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Valid promo codes (copy from main bot)
PROMO_CODES = {
    "ECLIPTICA2024": {"days": 30, "description": "Free 30-day trial"},
    "PERPSMASTER": {"days": 90, "description": "Free 90-day access for early supporters"},
    "UNLIMITED2024": {"days": 3650, "description": "Unlimited 10-year access for testing"}
}

async def reset_user(user_id: int, promo_code: str = None):
    """Reset a user's subscription status."""
    # Get database URL from environment
    database_url = (
        os.environ.get('POSTGRES_URL') or 
        os.environ.get('DATABASE_URL') or
        os.environ.get('POSTGRESQL_URL')
    )
    
    if not database_url:
        logger.error("Database URL not found in environment variables")
        return False
    
    logger.info(f"Connecting to database: {database_url[:10]}...")
    
    try:
        # Connect to database
        pool = await asyncpg.create_pool(database_url)
        
        if promo_code:
            # Check if promo code is valid
            if promo_code not in PROMO_CODES:
                logger.error(f"Invalid promo code: {promo_code}")
                return False
                
            # Apply promo code
            promo = PROMO_CODES[promo_code]
            now = datetime.now(timezone.utc)
            days = promo["days"]
            end_date = now + timedelta(days=days)
            
            logger.info(f"Applying promo code {promo_code} with {days} days to user {user_id}")
            
            async with pool.acquire() as conn:
                # Check if user exists
                exists = await conn.fetchval("SELECT uid FROM subscriptions WHERE uid = $1", user_id)
                
                if exists:
                    # Update existing subscription
                    await conn.execute(
                        """
                        UPDATE subscriptions
                        SET plan_type = 'promo', start_date = $2, end_date = $3, 
                            status = 'active', promo_code = $4
                        WHERE uid = $1
                        """,
                        user_id, now, end_date, promo_code
                    )
                    logger.info(f"Updated subscription for user {user_id}")
                else:
                    # Create new subscription
                    await conn.execute(
                        """
                        INSERT INTO subscriptions
                        (uid, plan_type, start_date, end_date, status, usage_count, promo_code)
                        VALUES ($1, 'promo', $2, $3, 'active', 0, $4)
                        """,
                        user_id, now, end_date, promo_code
                    )
                    logger.info(f"Created new subscription for user {user_id}")
                
                # Verify the change
                subscription = await conn.fetchrow(
                    "SELECT * FROM subscriptions WHERE uid = $1",
                    user_id
                )
                
                if subscription:
                    logger.info(f"User {user_id} subscription: {dict(subscription)}")
                    return True
        else:
            # Reset to free status
            logger.info(f"Resetting user {user_id} to free status")
            
            async with pool.acquire() as conn:
                # Check if user exists
                exists = await conn.fetchval("SELECT uid FROM subscriptions WHERE uid = $1", user_id)
                
                if exists:
                    # Set to free status
                    await conn.execute(
                        """
                        UPDATE subscriptions
                        SET plan_type = NULL, start_date = NULL, end_date = NULL, 
                            status = 'free', usage_count = 0, promo_code = NULL
                        WHERE uid = $1
                        """,
                        user_id
                    )
                    logger.info(f"Reset subscription for user {user_id} to free status")
                else:
                    # Create free subscription
                    await conn.execute(
                        """
                        INSERT INTO subscriptions
                        (uid, status, usage_count)
                        VALUES ($1, 'free', 0)
                        """,
                        user_id
                    )
                    logger.info(f"Created free subscription for user {user_id}")
                
                # Verify the change
                subscription = await conn.fetchrow(
                    "SELECT * FROM subscriptions WHERE uid = $1",
                    user_id
                )
                
                if subscription:
                    logger.info(f"User {user_id} subscription: {dict(subscription)}")
                    return True
                    
        await pool.close()
        return True
        
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: python reset_subscription.py <user_id> [promo_code]")
        sys.exit(1)
    
    try:
        user_id = int(sys.argv[1])
        promo_code = sys.argv[2] if len(sys.argv) > 2 else None
        
        logger.info(f"Starting reset for user {user_id}" + (f" with promo code {promo_code}" if promo_code else ""))
        result = asyncio.run(reset_user(user_id, promo_code))
        
        if result:
            logger.info("Operation completed successfully")
        else:
            logger.error("Operation failed")
    except ValueError:
        logger.error("User ID must be an integer")
        sys.exit(1)

if __name__ == "__main__":
    main() 