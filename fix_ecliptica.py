"""
Fixes for ecliptica_bot.py:

1. Add filtering to handle_custom_asset to prevent "Subscription" and "Enter Code" from being treated as trading pairs
2. Add state check to all entry points to prevent multiple processing

Apply these changes to ecliptica_bot.py
"""

# ---- Fix 1: Modify handle_custom_asset to filter non-trading pairs ----
def fix_handle_custom_asset():
    code = """async def handle_custom_asset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    \"\"\"Handle custom asset input from user.\"\"\"
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
    """
    return code

# ---- Fix 2: Update handler initialization to add proper handlers for special buttons ----
def fix_init_handlers():
    code = """def init_handlers(application: Application) -> None:
    \"\"\"Initialize all handlers for the application.\"\"\"
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
            MessageHandler(filters.Regex('^💰 Subscription$'), subscription_cmd)
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
    application.add_handler(CallbackQueryHandler(button_click, pattern=r'^(trade|analysis|sub):'))
    
    # Add the custom asset handler as the LAST handler
    # It should only receive messages that aren't caught by any of the above handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_asset))
    """
    return code

if __name__ == "__main__":
    print("Run the following steps to fix the issues:")
    print("1. Replace handle_custom_asset function with:")
    print(fix_handle_custom_asset())
    print("\n2. Replace init_handlers function with:")
    print(fix_init_handlers()) 