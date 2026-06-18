"""
Main bot entrypoint.
"""
import logging

from aiohttp import ClientSession
from telegram.ext import ApplicationBuilder

from bot.api_clients import AlchemyClient, AnkrClient, BscScanClient, HeliusClient, MoralisClient
from bot.config import (
    ALCHEMY_API_KEY,
    ANKR_API_URL,
    BSCSCAN_API_KEYS,
    HELIUS_API_KEY,
    LOG_FILE,
    LOG_LEVEL,
    MORALIS_API_KEY,
    TELEGRAM_BOT_TOKEN,
)
from bot.database import init_db, migrate_db
from bot.handlers import register_handlers
from bot.rate_limits import RateLimitTracker

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


async def post_init(app):
    init_db()
    migrate_db()

    session = ClientSession()
    app.bot_data["session"] = session
    app.bot_data["rate_limiter"] = RateLimitTracker()

    app.bot_data["ankr"] = AnkrClient(ANKR_API_URL) if ANKR_API_URL else None

    if HELIUS_API_KEY:
        app.bot_data["helius"] = HeliusClient(HELIUS_API_KEY)
    else:
        logger.warning("Helius API key не задан. Solana-балансы и Solana-history будут недоступны.")
        app.bot_data["helius"] = None

    if MORALIS_API_KEY:
        app.bot_data["moralis"] = MoralisClient(MORALIS_API_KEY)
    else:
        logger.warning("Moralis API key не задан. Балансы будут собираться через остальные бесплатные источники.")
        app.bot_data["moralis"] = None

    if ALCHEMY_API_KEY:
        app.bot_data["alchemy"] = AlchemyClient(ALCHEMY_API_KEY)
    else:
        logger.warning("Alchemy API key не задан. Ethereum-балансы будут неполными без Moralis/Ankr.")
        app.bot_data["alchemy"] = None

    app.bot_data["bscscan"] = BscScanClient() if BSCSCAN_API_KEYS else None

    logger.info("Bot post_init completed")


async def post_shutdown(app):
    session = app.bot_data.get("session")
    if session:
        await session.close()


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Не задан TELEGRAM_BOT_TOKEN в .env")
        exit(1)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    register_handlers(application)
    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()