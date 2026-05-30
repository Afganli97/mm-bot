"""
Обработчики команд Telegram.
Доступ разрешён только пользователям из ALLOWED_USER_IDS.
"""
import logging
from datetime import datetime, date
import re
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

from bot.config import TELEGRAM_BOT_TOKEN, MAX_DEPTH, ALLOWED_USER_IDS
from bot.database import get_all_api_usage, get_connection
from bot.graph_traversal import GraphTraversal
from bot.token_filter import update_top_tokens
from web3 import Web3

logger = logging.getLogger(__name__)

def is_valid_address(addr: str) -> bool:
    try:
        return Web3.is_address(addr)
    except Exception as e:
        logger.warning(f"Ошибка валидации адреса {addr}: {e}")
        return False

def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        logger.info(f"Попытка доступа от непривилегированного пользователя {user_id}")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    logger.info(f"Пользователь {update.effective_user.id} вызвал /start")
    await update.message.reply_text(
        "👋 Привет! Я бот для анализа цепочек покупок токенов маркет-мейкерами.\n"
        "Отправь мне ERC-20 адрес, и я найду токены, купленные им и связанными адресами за последние 30 дней.\n"
        "Доступные команды:\n"
        "/help – справка\n"
        "/dashboard – лимиты API"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    logger.info(f"Пользователь {update.effective_user.id} вызвал /help")
    await update.message.reply_text(
        f"🔍 <b>Как пользоваться:</b>\n"
        f"1. Отправьте Ethereum-адрес, с которого начать анализ.\n"
        f"2. Бот пройдёт по цепочке переводов ETH/WETH на глубину {MAX_DEPTH} и найдёт покупки токенов.\n"
        "3. Исключаются стейблкоины и топ-100 монет.\n"
        "4. Результат придёт в этом же чате.\n\n"
        "⚠️ Время анализа зависит от количества адресов. Пожалуйста, ожидайте.",
        parse_mode="HTML"
    )

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    logger.info(f"Пользователь {update.effective_user.id} вызвал /dashboard")
    usage = get_all_api_usage()
    today = date.today().isoformat()
    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n"
    for i in range(len(context.bot_data.get('etherscan_keys', []))):
        used = usage.get(f"etherscan_{i}", 0)
        msg += f"Etherscan ключ {i+1}: {used}/100,000 ({used/100000*100:.1f}%)\n"
    alchemy_used = usage.get("alchemy_0", 0)
    msg += f"Alchemy: {alchemy_used} запросов\n"
    infura_used = usage.get("infura_0", 0)
    msg += f"Infura: {infura_used}/100,000 ({infura_used/100000*100:.1f}%)\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    text = update.message.text.strip()
    if is_valid_address(text):
        logger.info(f"Пользователь {update.effective_user.id} отправил адрес {text}")
        await update.message.reply_text("⏳ Запущен анализ цепочки... Это может занять время. Я сообщу результат позже.")
        asyncio_task = context.application.create_task(
            run_analysis(update.effective_user.id, update.effective_chat.id, text, context)
        )
    else:
        logger.info(f"Некорректный адрес от {update.effective_user.id}: {text}")
        await update.message.reply_text("❌ Некорректный адрес Ethereum. Пожалуйста, проверьте и отправьте снова.")

async def run_analysis(user_id: int, chat_id: int, address: str, context: ContextTypes.DEFAULT_TYPE):
    from aiohttp import ClientSession
    try:
        async with ClientSession() as session:
            await update_top_tokens(session)
            traversal = GraphTraversal(session, address, user_id, chat_id)
            found = await traversal.run()
            if found:
                token_lines = []
                for item in found:
                    token_lines.append(f"• <code>{item['token']}</code> ({item['symbol']}) — покупатель: <code>{item['buyer']}</code>")
                report = (f"✅ <b>Анализ завершён!</b>\n"
                          f"Проверено адресов: {traversal.total_addresses}\n"
                          f"Найдено уникальных токенов: {len(found)}\n\n"
                          + "\n".join(token_lines))
            else:
                report = (f"✅ <b>Анализ завершён.</b>\n"
                          f"Проверено адресов: {traversal.total_addresses}\n"
                          f"Токены, удовлетворяющие условиям, не найдены.")
            await context.bot.send_message(chat_id, report, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.exception("Ошибка в задаче анализа")
        # Отправляем детали ошибки в чат
        error_msg = f"❌ Произошла ошибка: {str(e)}"
        await context.bot.send_message(chat_id, error_msg)