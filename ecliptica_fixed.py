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