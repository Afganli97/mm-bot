"""
Главный модуль запуска бота.
"""
import logging
from aiohttp import ClientSession
from telegram.ext import ApplicationBuilder

from bot.config import TELEGRAM_BOT_TOKEN, LOG_LEVEL, LOG_FILE, ETHERSCAN_API_KEYS, BLOCKSCOUT_API_KEY
from bot.database import init_db
from bot.handlers import register_handlers
from bot.token_filter import update_top_tokens
from bot.api_clients import BlockscoutClient, blockscout_rotator

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

async def post_init(app):
    init_db()
    session = ClientSession()
    app.bot_data['session'] = session
    if BLOCKSCOUT_API_KEY and blockscout_rotator:
        app.bot_data['blockscout'] = BlockscoutClient(blockscout_rotator)
    else:
        logger.warning("Blockscout API ключ не задан, балансы EVM будут через резервные методы")
    for net_name in ["ethereum", "bsc", "solana"]:
        await update_top_tokens(session, net_name)
    app.bot_data['etherscan_keys'] = ETHERSCAN_API_KEYS

async def post_shutdown(app):
    session = app.bot_data.get('session')
    if session:
        await session.close()

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Не задан TELEGRAM_BOT_TOKEN в .env")
        exit(1)
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    register_handlers(application)
    logger.info("Бот запущен")
    application.run_polling()

if __name__ == "__main__":
    main()