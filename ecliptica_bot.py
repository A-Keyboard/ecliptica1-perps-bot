# ecliptica_bot.py — v0.6.8
"""
Ecliptica Perps Assistant — Telegram trading bot with guided /trade flow and formatted AI responses

v0.6.8
──────
• Added structured /trade wizard: select asset, direction, difficulty
• Prompt enforces concise trade-format output from REI
• Unstructured /ask remains for advanced queries
• 300 s timeout, retry logic, serialized REI calls

Dependencies:
    python-telegram-bot==20.7
    requests
    python-dotenv
"""
from __future__ import annotations
import os, json, sqlite3, logging, textwrap, time, functools, asyncio
import requests
from datetime import datetime, timezone
from typing import Final
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    CallbackQueryHandler, MessageHandler, ContextTypes, filters
)

# Serialize REI calls across users
token_lock = asyncio.Lock()

# Load environment
load_dotenv()
BOT_TOKEN: Final[str] = os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
REI_KEY:   Final[str] = os.environ.get("REICORE_API_KEY","").strip()
DB = "ecliptica.db"

# Asset universe (Perps instruments)
# Dynamically fetched from public exchange APIs (no API key required)
async def fetch_binance_assets() -> list[str]:
    """Fetch perpetual futures symbols from Binance public API"""
    try:
        res = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
        res.raise_for_status()
        data = res.json()
        return [s['symbol'] for s in data.get('symbols', []) if s.get('contractType') == 'PERPETUAL']
    except Exception:
        logging.warning("Binance assets fetch failed, falling back to static list.")
        return []

async def fetch_bybit_assets() -> list[str]:
    """Fetch perpetual symbols from Bybit public API"""
    try:
        res = requests.get("https://api.bybit.com/v2/public/symbols", timeout=10)
        res.raise_for_status()
        data = res.json().get('result', [])
        return [item['name'] for item in data if item.get('status') == 'Trading' and 'perpetual' in item.get('typ', '').lower()]
    except Exception:
        logging.warning("Bybit assets fetch failed, falling back to static list.")
        return []

# At startup: combine
ASSETS: Final[list[str]] = []

async def init_assets():
    global ASSETS
    # try dynamic fetch, else fallback to hard-coded
    binance = await fetch_binance_assets()
    bybit   = await fetch_bybit_assets()
    static  = [
        # fallback static sample list
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "BTCUSD", "ETHUSD", "BTC-PERP",
    ]
    ASSETS = sorted(set(binance + bybit + static))

# Profile questions
QUESTS: Final[list[tuple[str,str]]] = [
    ("experience", "Your perps experience? (0-3m / 3-12m / >12m)"),
    ("capital",    "Capital allocated (USD)"),
    ("risk",       "Max loss % (e.g. 2)"),
    ("quote",      "Quote currency (USDT / USD-C / BTC)"),
    ("timeframe",  "Timeframe (scalp / intraday / swing / position)"),
    ("leverage",   "Leverage multiple (1 if none)"),
    ("funding",    "Comfort paying funding 8h? (yes / unsure / prefer spot)"),
]
SETUP = 0
TRADE_ASSET, TRADE_TYPE, TRADE_DIFF = 1, 2, 3

# ───────────────────────────── Database Helpers ───────────────────────────── #
def init_db() -> None:
    with sqlite3.connect(DB) as con:
        con.execute("CREATE TABLE IF NOT EXISTS profile (uid INTEGER PRIMARY KEY, data TEXT)")

def save_profile(uid: int, data: dict[str, str]) -> None:
    with sqlite3.connect(DB) as con:
        con.execute("REPLACE INTO profile VALUES(?,?)", (uid, json.dumps(data))) as con:
        con.execute("REPLACE INTO profile VALUES(?,?)", (uid, json.dumps(data))) as con:
        con.execute("REPLACE INTO profile VALUES(?,?)", (uid, json.dumps(data))) as con:
        con.execute("REPLACE INTO profile VALUES(?,?)",(uid,json.dumps(data)))

...")
}]}
