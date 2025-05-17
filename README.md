# Ecliptica Perps Bot

A Telegram bot for cryptocurrency perpetual futures trading assistance with guided setup, interactive buttons, and AI-powered market analysis.

## Features

- Interactive setup wizard with button controls
- Guided trading flow
- AI-powered market analysis using REI Core
- Persistent user profiles
- Support for multiple trading pairs
- Funding rate awareness
- Risk management settings

## Setup

1. Clone the repository:
```bash
git clone <your-repo-url>
cd ecliptica1-perps-bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables in your deployment platform (e.g. Railway):
- `TELEGRAM_BOT_TOKEN`: Your Telegram bot token from BotFather
- `REICORE_API_KEY`: Your REI Core API key

## Usage

Start the bot:
```bash
python ecliptica_bot.py
```

Available commands:
- `/start` - Begin interaction with the bot
- `/setup` - Configure your trading profile
- `/trade` - Start the trading assistant
- `/ask` - Get AI market analysis
- `/faq` - View frequently asked questions
- `/help` - Show available commands

## Version

Current version: v0.6.18

## License

MIT License 