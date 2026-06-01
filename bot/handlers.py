"""
Обработчики команд Telegram.
Доступ разрешён только пользователям из ALLOWED_USER_IDS.
"""
import logging
from datetime import datetime, date
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from bot.config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, NETWORKS,
    DEFAULT_MAX_DEPTH, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_TRANSFER_VALUE_ETH,
    DEFAULT_MAX_FOUND_TOKENS, DEFAULT_MAX_ADDRESSES
)
from bot.database import get_all_api_usage, get_user_setting, set_user_setting, get_user_settings_dict
from bot.graph_traversal import GraphTraversal
from bot.token_filter import update_top_tokens
from bot.api_clients import TokenInfoService, EVMExplorerClient, SolscanClient
from bot.networks.ethereum import EthereumNetwork
from bot.networks.bsc import BscNetwork
from bot.networks.solana import SolanaNetwork
from web3 import Web3

logger = logging.getLogger(__name__)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Вспомогательные функции для создания сетевых объектов
def get_network_for_address(address: str, session) -> list:
    """Возвращает список сетей, которым принадлежит адрес."""
    networks = []
    # Ethereum
    if Web3.is_address(address):
        eth_net = NETWORKS["ethereum"]
        explorer = EVMExplorerClient(eth_net["explorer_api_url"], etherscan_rotator, eth_net["chain_id"], eth_net["weth"])
        networks.append(EthereumNetwork(eth_net, session, explorer))
    # BSC (проверка как EVM-адрес)
    if Web3.is_address(address):
        bsc_net = NETWORKS["bsc"]
        explorer = EVMExplorerClient(bsc_net["explorer_api_url"], bscscan_rotator, bsc_net["chain_id"], bsc_net["weth"])
        networks.append(BscNetwork(bsc_net, session, explorer))
    # Solana
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)
        sol_net = NETWORKS["solana"]
        networks.append(SolanaNetwork(sol_net, session))
    except Exception:
        pass
    return networks

def is_valid_address(addr: str) -> bool:
    # Проверяем хотя бы одну сеть
    return any(n.validate_address_sync(addr) for n in [EthereumNetwork, BscNetwork, SolanaNetwork])  # упрощённо

def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        logger.info(f"Попытка доступа от непривилегированного пользователя {user_id}")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    await update.message.reply_text("👋 Привет! Я бот для анализа кошельков. Отправьте адрес, и я определю сеть. Затем выберите режим: баланс или история покупок.\nКоманды: /help, /dashboard, /settings")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    await update.message.reply_text("🔍 <b>Как пользоваться:</b>\n"
                                    "1. Отправьте адрес кошелька (Ethereum, BSC, Solana).\n"
                                    "2. Выберите действие: «Баланс» или «История покупок».\n"
                                    "3. Для истории: бот найдет токены, купленные за последние 30 дней.\n"
                                    "4. Исключаются стейблкоины и топ-100.\n"
                                    "/settings - изменить параметры поиска.",
                                    parse_mode="HTML")

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    usage = get_all_api_usage()
    today = date.today().isoformat()
    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n"
    for service, count in usage.items():
        msg += f"{service}: {count}\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# Обработчик сообщений с адресом (теперь с выбором режима)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    text = update.message.text.strip()
    # Определяем сети
    networks = get_network_for_address(text, context.application.session)
    if not networks:
        await update.message.reply_text("❌ Адрес не распознан ни в одной поддерживаемой сети.")
        return
    # Если несколько сетей, предложим выбрать
    if len(networks) > 1:
        keyboard = [[InlineKeyboardButton(n.name, callback_data=f"mode_balance_{n.name}")] for n in networks]
        keyboard.append([InlineKeyboardButton("Все сети (история)", callback_data="mode_history_all")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Адрес найден в нескольких сетях. Выберите действие:", reply_markup=reply_markup)
    else:
        # Одна сеть - сразу предлагаем режим
        network = networks[0]
        keyboard = [
            [InlineKeyboardButton("💰 Баланс", callback_data=f"mode_balance_{network.name}")],
            [InlineKeyboardButton("📜 История покупок", callback_data=f"mode_history_{network.name}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"Выберите действие для сети {network.name}:", reply_markup=reply_markup)

    context.user_data['networks'] = networks
    context.user_data['address'] = text

# Обработчик нажатий на кнопки (баланс/история)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("mode_"):
        if data == "mode_history_all":
            # История по всем сетям
            networks = context.user_data.get('networks', [])
            await query.edit_message_text("⏳ Запущен анализ истории по всем сетям...")
            # Запускаем анализ для каждой сети и собираем результаты
            await run_all_history(query, context, networks)
        else:
            action, network_name = data.split("_")[1], "_".join(data.split("_")[2:])
            network = next((n for n in context.user_data.get('networks', []) if n.name == network_name), None)
            if not network:
                await query.edit_message_text("Сеть не найдена.")
                return
            if action == "balance":
                await query.edit_message_text(f"⏳ Загружаем баланс сети {network_name}...")
                await show_balance(query, context, network)
            elif action == "history":
                await query.edit_message_text(f"⏳ Запущен анализ истории покупок ({network_name})...")
                await run_history(query, context, network)

# Команда /settings
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return
    user_id = update.effective_user.id
    settings_dict = get_user_settings_dict(user_id)
    # Кнопки для изменения каждой настройки
    keyboard = [
        [InlineKeyboardButton(f"Глубина обхода: {settings_dict.get('max_depth', DEFAULT_MAX_DEPTH)}", callback_data="set_max_depth")],
        [InlineKeyboardButton(f"Период (дней): {settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS)}", callback_data="set_lookback_days")],
        [InlineKeyboardButton(f"Мин. сумма перевода: {settings_dict.get('min_transfer_value', DEFAULT_MIN_TRANSFER_VALUE_ETH)}", callback_data="set_min_transfer")],
        [InlineKeyboardButton(f"Макс. токенов: {settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS)}", callback_data="set_max_tokens")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ <b>Настройки</b>\nВыберите параметр для изменения:", reply_markup=reply_markup, parse_mode="HTML")

# Обработчик изменения настроек (диалог через состояние)
# Для простоты используем ConversationHandler
# В этом ответе не разворачиваем полностью, добавим позже

# Функции отображения баланса и истории
async def show_balance(query, context, network):
    address = context.user_data['address']
    try:
        native_balance = await network.get_balance(address)
        token_balances = await network.get_token_balances(address)
        # Здесь должен быть подсчёт USD, пока заглушка
        text = f"💰 <b>Баланс сети {network.name}</b>\n"
        text += f"{network.native_symbol}: {native_balance:.4f}\n"
        total_usd = 0
        for tok in token_balances:
            text += f"• {tok['symbol']}: {tok['balance']:.4f} (≈ ? USD)\n"
        await query.edit_message_text(text, parse_mode="HTML")
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")

async def run_history(query, context, network):
    address = context.user_data['address']
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            traversal = GraphTraversal(session, address, network, max_tokens=100)
            found = await traversal.run()
            # Формируем отчёт (аналогично старому)
            await query.edit_message_text("✅ Анализ завершён.")
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")

# Регистрация обработчиков (в main.py)
def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))