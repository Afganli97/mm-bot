"""
Главный модуль запуска бота.
"""
import logging
import asyncio
from aiohttp import ClientSession
from telegram.ext import ApplicationBuilder

from bot.config import TELEGRAM_BOT_TOKEN, LOG_LEVEL, LOG_FILE, ETHERSCAN_API_KEYS
from bot.database import init_db
from bot.handlers import register_handlers
from bot.token_filter import update_top_tokens

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

async def post_init(app):
    init_db()
    async with ClientSession() as session:
        await update_top_tokens(session)
    app.bot_data['etherscan_keys'] = ETHERSCAN_API_KEYS

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Не задан TELEGRAM_BOT_TOKEN в .env")
        exit(1)
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    register_handlers(application)
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()