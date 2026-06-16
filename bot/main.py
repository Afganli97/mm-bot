"""
Главный модуль запуска Telegram-бота.
"""

import logging
from pathlib import Path

from aiohttp import ClientSession
from telegram.ext import ApplicationBuilder

from bot.api_clients import AnkrClient, CascadePriceFetcher, HeliusClient, MoralisClient
from bot.config import (
    ANKR_API_KEY,
    ANKR_API_URL,
    HELIUS_API_KEY,
    LOG_FILE,
    LOG_LEVEL,
    MORALIS_API_KEY,
    TELEGRAM_BOT_TOKEN,
)
from bot.database import init_db
from bot.handlers import register_handlers


Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
    force=True,
)

logger = logging.getLogger(__name__)


async def post_init(app):
    session = None

    try:
        init_db()

        session = ClientSession()
        app.bot_data["session"] = session

        if ANKR_API_KEY:
            app.bot_data["ankr"] = AnkrClient(ANKR_API_URL)
        else:
            app.bot_data["ankr"] = None
            logger.warning("ANKR_API_KEY не задан. Мультичейн EVM-балансы будут неполными.")

        helius = HeliusClient(HELIUS_API_KEY) if HELIUS_API_KEY else None
        app.bot_data["helius"] = helius

        app.bot_data["cascade"] = CascadePriceFetcher(helius)

        if MORALIS_API_KEY:
            app.bot_data["moralis"] = MoralisClient(MORALIS_API_KEY)
        else:
            app.bot_data["moralis"] = None
            logger.warning("MORALIS_API_KEY не задан. Ethereum ERC20-балансы могут быть неполными.")

        logger.info("Бот инициализирован")

    except Exception:
        logger.exception("Ошибка инициализации бота")

        if session:
            await session.close()

        raise


async def post_shutdown(app):
    session = app.bot_data.get("session")

    if session:
        await session.close()
        logger.info("HTTP-сессия закрыта")


async def error_handler(update, context):
    error = context.error
    logger.exception("Unhandled Telegram error: %s", error)

    if update and getattr(update, "effective_chat", None):
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Ошибка обработки запроса. Попробуйте позже или отправьте команду /start.",
                disable_notification=True,
            )
        except Exception:
            logger.debug("Не удалось отправить ошибку пользователю", exc_info=True)


def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не задан в .env")
        exit(1)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    register_handlers(application)
    application.add_error_handler(error_handler)

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()