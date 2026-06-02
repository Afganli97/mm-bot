"""
Обработчики команд Telegram.
"""
import logging
import asyncio
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from bot.config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, NETWORKS,
    DEFAULT_MAX_DEPTH, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_TRANSFER_VALUE_ETH,
    DEFAULT_MAX_FOUND_TOKENS
)
from bot.database import get_all_api_usage, get_user_setting, set_user_setting, get_user_settings_dict
from bot.graph_traversal import GraphTraversal
from bot.token_filter import update_top_tokens
from bot.api_clients import (
    EVMExplorerClient, EVMWeb3Client, SolscanClient, CoingeckoClient, TokenInfoService
)
from bot.networks.ethereum import EthereumNetwork
from bot.networks.bsc import BscNetwork
from bot.networks.solana import SolanaNetwork

logger = logging.getLogger(__name__)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
MIN_USD_VALUE = 0.10   # минимальная стоимость токена для отображения (кроме нативного)

def _get_global_session(context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get('session')
    if session is None:
        from aiohttp import ClientSession
        return ClientSession()
    return session

def web3_is_address(addr: str) -> bool:
    from web3 import Web3
    return Web3.is_address(addr)

def get_network_for_address(address: str, session):
    networks = []
    if web3_is_address(address):
        # Ethereum
        eth_conf = NETWORKS["ethereum"]
        eth_explorer = EVMExplorerClient(eth_conf["chain_id"], eth_conf["weth"])
        eth_web3 = EVMWeb3Client(
            eth_conf["rpc_url"], eth_conf["chain_id"], eth_conf["weth"],
            router_address=eth_conf.get("dex_routers", [""])[0],
            stable_address=eth_conf.get("stablecoins", [""])[0]
        )
        networks.append(EthereumNetwork(eth_conf, session, eth_explorer, eth_web3))
        # BSC
        bsc_conf = NETWORKS["bsc"]
        bsc_web3 = EVMWeb3Client(
            bsc_conf["rpc_url"], bsc_conf["chain_id"], bsc_conf["weth"],
            router_address=bsc_conf.get("dex_routers", [""])[0],
            stable_address=bsc_conf.get("stablecoins", [""])[0]
        )
        networks.append(BscNetwork(bsc_conf, session, bsc_web3))
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(address)
        sol_conf = NETWORKS["solana"]
        networks.append(SolanaNetwork(sol_conf, session))
    except Exception:
        pass
    return networks

def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        logger.info(f"Попытка доступа от непривилегированного пользователя {user_id}")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text("👋 Привет! Я бот для анализа кошельков. Отправьте адрес, и я определю сеть. Затем выберите режим: баланс или история покупок.\n/help, /dashboard, /settings")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text("🔍 <b>Как пользоваться:</b>\n1. Отправьте адрес (Ethereum, BSC, Solana).\n2. Выберите действие.\n3. Для истории – бот найдет токены за 30 дней.\n/settings – изменить параметры.", parse_mode="HTML")

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    usage = get_all_api_usage()
    today = date.today().isoformat()
    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n"
    for service, count in usage.items():
        msg += f"{service}: {count}\n"
    await update.message.reply_text(msg, parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    if context.user_data.get('awaiting_setting'):
        await setting_value_input(update, context)
        return
    text = update.message.text.strip()
    session = _get_global_session(context)
    networks = get_network_for_address(text, session)
    if not networks:
        await update.message.reply_text("❌ Адрес не распознан.")
        return
    context.user_data['networks'] = networks
    context.user_data['address'] = text
    if len(networks) > 1:
        keyboard = [[InlineKeyboardButton(n.name, callback_data=f"net_{n.name}")] for n in networks]
        keyboard.append([InlineKeyboardButton("Все сети (история)", callback_data="net_all")])
        await update.message.reply_text("Адрес найден в нескольких сетях. Выберите сеть:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await show_mode_menu(update, context, networks[0])

async def show_mode_menu(update_or_query, context, network):
    keyboard = [
        [InlineKeyboardButton("💰 Баланс", callback_data=f"mode_balance_{network.name}")],
        [InlineKeyboardButton("📜 История покупок", callback_data=f"mode_history_{network.name}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if hasattr(update_or_query, 'callback_query'):
        await update_or_query.callback_query.edit_message_text(f"Выберите действие для сети {network.name}:", reply_markup=reply_markup)
    else:
        await update_or_query.message.reply_text(f"Выберите действие для сети {network.name}:", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("net_"):
        net_name = data[4:]
        networks = context.user_data.get('networks', [])
        if net_name == "all":
            await query.edit_message_text("⏳ Запущен анализ истории по всем сетям...")
            await run_all_history(query, context, networks)
        else:
            network = next((n for n in networks if n.name == net_name), None)
            if network:
                context.user_data['selected_network'] = network
                await show_mode_menu(query, context, network)
            else:
                await query.edit_message_text("Сеть не найдена.")
    elif data.startswith("mode_"):
        parts = data.split("_")
        action = parts[1]
        net_name = "_".join(parts[2:])
        network = next((n for n in context.user_data.get('networks', []) if n.name == net_name), None)
        if not network:
            await query.edit_message_text("Сеть не найдена.")
            return
        if action == "balance":
            await query.edit_message_text(f"⏳ Загружаем баланс сети {net_name}...")
            await show_balance(query, context, network)
        elif action == "history":
            await query.edit_message_text(f"⏳ Запущен анализ истории покупок ({net_name})...")
            await run_history(query, context, network)

async def get_token_price(session, token_address, network_name, network, weth_price_usd: float = 0.0) -> Optional[float]:
    addr = token_address.lower()

    # 1. Специфичный метод сети (если есть)
    if hasattr(network, 'get_token_price_usd'):
        try:
            price = await network.get_token_price_usd(session, addr)
            if price is not None:
                return price
        except Exception:
            pass

    # 2. CoinGecko
    platform = {"ethereum":"ethereum", "bsc":"binance-smart-chain", "solana":"solana"}.get(network_name)
    if platform:
        try:
            url = f"https://api.coingecko.com/api/v3/simple/token_price/{platform}?contract_addresses={addr}&vs_currencies=usd"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get(addr, {}).get("usd")
                    if price is not None:
                        return float(price)
        except Exception:
            pass

    # 3. RPC-роутер для EVM
    if hasattr(network, 'web3') and isinstance(network.web3, EVMWeb3Client):
        try:
            price = await network.web3.get_price_via_router(session, addr, weth_price_usd)
            if price is not None:
                return price
        except Exception:
            pass

    return None

async def show_balance(query, context, network):
    address = context.user_data['address']
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            native_balance = await network.get_balance(address)
            token_balances = await network.get_token_balances(address)

            # Цена нативного токена
            native_price = 0.0
            try:
                coin_id = {"ethereum":"ethereum", "bsc":"binancecoin", "solana":"solana"}.get(network.name.lower())
                if coin_id:
                    price_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
                    async with session.get(price_url, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            native_price = data.get(coin_id, {}).get("usd", 0.0)
            except Exception:
                pass

            total_usd = native_balance * native_price
            lines = [f"💰 <b>Баланс сети {network.name}</b>\n"
                     f"{network.native_symbol}: {native_balance:.6f} (≈ ${native_balance * native_price:.2f})"]

            for tok in token_balances:
                price = await get_token_price(session, tok['address'], network.name, network, native_price)
                if price is None:
                    continue   # Не удалось определить цену – пропускаем (мусор)
                usd_val = tok['balance'] * price
                if usd_val < MIN_USD_VALUE:
                    continue
                total_usd += usd_val
                link = f"https://dexscreener.com/{network.name.lower()}/{tok['address']}"
                lines.append(f"• <a href='{link}'>{tok['symbol']}</a>: {tok['balance']:.4f} (≈ ${usd_val:.2f})")

            lines.insert(1, f"<b>Общий баланс: ≈ ${total_usd:.2f}</b>")
            text = "\n".join(lines)
            await _send_long_message(context.bot, query.message.chat_id, text, parse_mode="HTML")
            await query.edit_message_text("✅ Готово.")
    except Exception as e:
        logger.exception("Ошибка баланса")
        await query.edit_message_text(f"❌ Ошибка: {e}")

async def run_history(query, context, network):
    address = context.user_data['address']
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            user_id = query.from_user.id
            max_tokens = int(get_user_setting(user_id, "max_tokens", str(DEFAULT_MAX_FOUND_TOKENS)))
            lookback_days = int(get_user_setting(user_id, "lookback_days", str(DEFAULT_LOOKBACK_DAYS)))
            max_depth = int(get_user_setting(user_id, "max_depth", str(DEFAULT_MAX_DEPTH)))
            traversal = GraphTraversal(session, address, network, max_tokens=max_tokens, lookback_days=lookback_days, max_depth=max_depth)
            found = await traversal.run()
            if not found:
                await query.edit_message_text("✅ Анализ завершён. Токены не найдены.")
                return
            unique = {}
            for item in found:
                addr = item['token']
                if addr not in unique:
                    unique[addr] = item
            token_lines = []
            for addr, data in unique.items():
                link = f"https://dexscreener.com/{network.name.lower()}/{addr}"
                token_lines.append(f"• <a href='{link}'>{data['symbol']}</a> (<code>{addr}</code>)")
            limit_note = "\n⚠️ <i>Достигнут лимит, поиск остановлен.</i>" if traversal.token_limit_reached else ""
            report = f"✅ <b>Анализ завершён!</b>\nПроверено адресов: {traversal.total_addresses}\nНайдено уникальных токенов: {len(unique)}\n{limit_note}\n" + "\n".join(token_lines)
            await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
            await query.edit_message_text("✅ Готово.")
    except Exception as e:
        logger.exception("Ошибка истории")
        await query.edit_message_text(f"❌ Ошибка: {e}")

async def run_all_history(query, context, networks):
    address = context.user_data['address']
    all_found = []
    for net in networks:
        try:
            from aiohttp import ClientSession
            async with ClientSession() as session:
                traversal = GraphTraversal(session, address, net, max_tokens=50)
                found = await traversal.run()
                all_found.extend(found)
        except Exception:
            pass
    if not all_found:
        await query.edit_message_text("Токены не найдены ни в одной сети.")
        return
    unique = {}
    for item in all_found:
        addr = item['token']
        if addr not in unique:
            unique[addr] = item
    lines = []
    for addr, data in unique.items():
        lines.append(f"• {data['symbol']} (<code>{addr}</code>)")
    report = f"✅ <b>История по всем сетям</b>\nНайдено токенов: {len(lines)}\n" + "\n".join(lines)
    await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
    await query.edit_message_text("✅ Готово.")

async def _send_long_message(bot, chat_id, text, parse_mode="HTML"):
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        await bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True)
    else:
        lines = text.split('\n')
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > TELEGRAM_MAX_MESSAGE_LENGTH:
                await bot.send_message(chat_id, chunk.strip(), parse_mode=parse_mode, disable_web_page_preview=True)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await bot.send_message(chat_id, chunk.strip(), parse_mode=parse_mode, disable_web_page_preview=True)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    user_id = update.effective_user.id
    settings_dict = get_user_settings_dict(user_id)
    keyboard = [
        [InlineKeyboardButton(f"Глубина: {settings_dict.get('max_depth', DEFAULT_MAX_DEPTH)}", callback_data="set_max_depth")],
        [InlineKeyboardButton(f"Дней: {settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS)}", callback_data="set_lookback_days")],
        [InlineKeyboardButton(f"Мин. сумма: {settings_dict.get('min_transfer', DEFAULT_MIN_TRANSFER_VALUE_ETH)}", callback_data="set_min_transfer")],
        [InlineKeyboardButton(f"Макс. токенов: {settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS)}", callback_data="set_max_tokens")],
        [InlineKeyboardButton("Сбросить на умолчания", callback_data="reset_settings")]
    ]
    await update.message.reply_text("⚙️ <b>Настройки</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    if data == "reset_settings":
        from bot.database import get_connection
        with get_connection() as conn:
            conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
            conn.commit()
        await query.edit_message_text("Настройки сброшены.")
        return
    setting_map = {
        "set_max_depth": ("max_depth", "Введите новую глубину обхода (число):"),
        "set_lookback_days": ("lookback_days", "Введите период анализа в днях (число):"),
        "set_min_transfer": ("min_transfer", "Введите минимальную сумму перевода (в ETH/BNB):"),
        "set_max_tokens": ("max_tokens", "Введите максимальное количество токенов в отчёте:")
    }
    key, prompt = setting_map.get(data, (None, None))
    if key:
        context.user_data['awaiting_setting'] = key
        await query.edit_message_text(prompt)

async def setting_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    key = context.user_data.pop('awaiting_setting', None)
    if not key:
        return await handle_message(update, context)
    value = update.message.text.strip()
    user_id = update.effective_user.id
    set_user_setting(user_id, key, value)
    await update.message.reply_text(f"✅ Настройка {key} обновлена: {value}")

def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(net_|mode_).*"))
    app.add_handler(CallbackQueryHandler(settings_button, pattern="^(set_|reset_settings).*"))