# Ecliptica Bot Subscription System

This document explains how to set up and use the Coinbase Commerce subscription system for the Ecliptica Trading Bot.

## Overview

The subscription system allows:
- Free tier with limited trade analyses (5 by default)
- Multiple subscription plans (monthly, quarterly, annual)
- Promo code redemption for free access periods
- Payment processing via Coinbase Commerce
- Subscription management and auto-renewal
- Expiration notifications

## Setup Instructions

### 1. Environment Variables

Add these environment variables to your Railway deployment:

```
COINBASE_API_KEY=your_api_key_here
COINBASE_WEBHOOK_SECRET=your_webhook_secret_here
```

Get these from your Coinbase Commerce dashboard under Settings > API Keys.

### 2. Database Tables

The system uses three database tables:
- `profile` - Existing user profile table
- `subscriptions` - User subscription records
- `payments` - Payment tracking

The tables are automatically created when the bot starts.

### 3. Webhook Configuration

Set up a webhook in your Coinbase Commerce dashboard pointing to:
```
https://your-railway-app-url.up.railway.app/webhook
```

This allows payment confirmations to automatically activate subscriptions.

## Usage

### Free Tier

Users get 5 free trade analysis requests before being prompted to subscribe.

Only actual trade analyses count toward this limit - profile setup and FAQ queries do not count.

### Promo Codes

Default promo codes:
- `ECLIPTICA2024` - 30 days free
- `PERPSMASTER` - 90 days free

Edit these in the `subscription_handlers.py` file. You can share these codes with users for free access.

### Subscription Plans

Default plans:
- Monthly: $19.99
- Quarterly: $49.99 (save 17%)
- Annual: $149.99 (save 37%)

Edit these in the `subscription_handlers.py` file.

### Subscription Management

Users can manage their subscriptions using the `/manage_subscription` command, which allows them to:
- View subscription details and expiration date
- Toggle auto-renewal on/off
- Renew subscriptions early
- Cancel subscriptions

### Auto-Renewal

The system supports automatic subscription renewals:
- Users can enable/disable auto-renewal
- Renewal payments are processed 3 days before subscription expiration
- Users receive notifications about upcoming renewals

### Expiration Notifications

Users receive notifications when:
- Their subscription is about to expire (7 days before)
- A renewal payment is needed (for auto-renewal)
- A payment has been successfully processed

## Integration in Main Bot

### Import Module

```python
# In ecliptica_bot.py
import subscription_handlers

# In post_init function
async def post_init(application: Application) -> None:
    await init_db()
    init_assets()
    
    # Initialize subscription tables
    await subscription_handlers.init_subscription_tables()
    
    # Pass the database pool to subscription handlers
    subscription_handlers.set_db_pool(db_pool)
    
    # Start subscription scheduler for renewals and notifications
    await subscription_handlers.start_subscription_scheduler()
```

### Add Access Checks

In your analysis handlers add:

```python
# Check subscription access first
user_id = query.from_user.id
has_access = await subscription_handlers.check_access_for_analysis(user_id, query)
if not has_access:
    return  # Stop processing if no access
```

### Register Commands

Add these commands to your command handlers:

```python
# In init_handlers function
application.add_handler(CommandHandler('subscribe', subscription_handlers.subscription_cmd))
application.add_handler(CommandHandler('manage_subscription', subscription_handlers.manage_subscription_cmd))
```

## Customization

### Changing Plans

Edit the `SUBSCRIPTION_PLANS` dictionary in `subscription_handlers.py` to modify prices and durations.

### Free Tier Limit

Change the `FREE_ANALYSIS_LIMIT` variable to adjust how many free analyses users get (default: 5).

### Promo Codes

Edit the `PROMO_CODES` dictionary to add or modify promotional codes.

### Renewal Settings

Adjust renewal settings in the `check_expiring_subscriptions` function:
- `renewal_cutoff` - When to start processing renewals (default: 3 days before expiry)
- `notification_cutoff` - When to send expiration notifications (default: 7 days before expiry)

## Testing

You can use Coinbase Commerce test mode to simulate payments without real transactions.

To test renewals manually, you can run:
```python
await subscription_handlers.check_expiring_subscriptions()
``` 