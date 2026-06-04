"""
Обработчики команд Telegram.
"""
import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from bot.config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, NETWORKS,
    DEFAULT_MAX_DEPTH, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_TRANSFER_VALUE_ETH,
    DEFAULT_MAX_FOUND_TOKENS, MIN_USD_VALUE
)
from bot.database import get_all_api_usage, get_user_setting, set_user_setting, get_user_settings_dict
from bot.graph_traversal import GraphTraversal
from bot.api_clients import (
    EVMExplorerClient, AnkrClient, EVMWeb3Client, CascadePriceFetcher, TokenInfoService
)
from bot.networks.ethereum import EthereumNetwork
from bot.networks.bsc import BscNetwork
from bot.networks.solana import SolanaNetwork

logger = logging.getLogger(__name__)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

def _get_global_session(context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get('session')
    if session is None:
        from aiohttp import ClientSession
        session = ClientSession()
        context.application.bot_data['session'] = session
    return session

def web3_is_address(addr: str) -> bool:
    from web3 import Web3
    return Web3.is_address(addr)

def is_solana_address(addr: str) -> bool:
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(addr)
        return True
    except:
        return False

def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS:
        logger.info(f"Попытка доступа от непривилегированного пользователя {user_id}")
        return False
    return True

# Стандартные команды
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text(
        "👋 Привет! Я бот для анализа кошельков. Отправьте адрес, и я сразу покажу балансы.\n"
        "/help, /dashboard, /settings"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text(
        "🔍 <b>Как пользоваться:</b>\n"
        "1. Отправьте адрес кошелька (EVM или Solana).\n"
        "2. Бот мгновенно покажет балансы во всех поддерживаемых сетях.\n"
        "3. Нажмите кнопку «История покупок», чтобы проанализировать торговую историю.\n"
        "/settings - изменить параметры поиска.",
        parse_mode="HTML"
    )

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    usage = get_all_api_usage()
    today = date.today().isoformat()
    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n"
    for service, count in usage.items():
        msg += f"{service}: {count}\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# Основной обработчик
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    if context.user_data.get('awaiting_setting'):
        await setting_value_input(update, context)
        return
    text = update.message.text.strip()
    session = _get_global_session(context)

    # Определяем тип адреса
    if web3_is_address(text):
        # EVM адрес
        context.user_data['address'] = text
        context.user_data['address_type'] = 'evm'
        await update.message.reply_text("⏳ Собираю балансы Ethereum и BSC...")
        await show_evm_balances(update, context)
    elif is_solana_address(text):
        context.user_data['address'] = text
        context.user_data['address_type'] = 'solana'
        await update.message.reply_text("⏳ Собираю баланс Solana...")
        await show_solana_balance(update, context)
    else:
        await update.message.reply_text("❌ Адрес не распознан (ни EVM, ни Solana).")
        return

async def show_evm_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    ankr: AnkrClient = context.application.bot_data.get('ankr')
    if not ankr:
        await update.message.reply_text("❌ Ankr не настроен.")
        return

    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            data = await ankr.get_multichain_balances(session, address, chains=["eth", "bsc"])
            if not data:
                await update.message.reply_text("❌ Не удалось получить данные от Ankr.")
                return

            assets = data.get("assets", [])
            total_usd = float(data.get("totalBalanceUsd", 0))
            lines = [f"💰 <b>Баланс кошелька</b>\n<code>{address}</code>\n"
                     f"Общая стоимость: ≈ ${total_usd:,.2f}\n"]

            # Группируем по сетям
            eth_assets = [a for a in assets if a.get("blockchain") == "eth"]
            bsc_assets = [a for a in assets if a.get("blockchain") == "bsc"]

            for chain_name, chain_assets, native_sym in [
                ("Ethereum", eth_assets, "ETH"),
                ("BSC", bsc_assets, "BNB")
            ]:
                lines.append(f"\n⛓️ <b>{chain_name}</b>")
                for a in chain_assets:
                    sym = a.get("tokenSymbol", "?")
                    bal = float(a.get("balance", 0))
                    usd_val = float(a.get("balanceUsd", 0))
                    if usd_val < MIN_USD_VALUE and sym != native_sym:
                        continue
                    display = f"≈ ${usd_val:,.2f}" if usd_val > 0 else "?"
                    contract = a.get("tokenAddress", "")
                    link = f"https://dexscreener.com/{chain_name.lower()}/{contract}" if contract else ""
                    if link:
                        lines.append(f"• <a href='{link}'>{sym}</a>: {bal:.4f} ({display})")
                    else:
                        lines.append(f"• {sym}: {bal:.4f} ({display})")

            text = "\n".join(lines)
            await _send_long_message(context.bot, update.effective_chat.id, text, parse_mode="HTML")

            # Добавляем кнопку «История покупок»
            keyboard = [[InlineKeyboardButton("📜 История покупок", callback_data="history_evm")]]
            await update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения EVM балансов")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def show_solana_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            rpc_url = NETWORKS["solana"]["rpc_url"]

            # 1. Нативный SOL
            payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]}
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                data = await resp.json()
                sol_balance = data.get("result", {}).get("value", 0) / 1e9

            # 2. Токены через getTokenAccountsByOwner
            token_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    address,
                    {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                    {"encoding": "jsonParsed"}
                ]
            }
            tokens = []
            async with session.post(rpc_url, json=token_payload, timeout=10) as resp:
                if resp.status == 200:
                    token_data = await resp.json()
                    accounts = token_data.get("result", {}).get("value", [])
                    for acc in accounts:
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        mint = info.get("mint")
                        symbol = info.get("tokenSymbol", "?")
                        decimals = info.get("tokenAmount", {}).get("decimals", 0)
                        amount = info.get("tokenAmount", {}).get("uiAmount", 0)
                        if amount > 0 and mint:
                            tokens.append({
                                "mint": mint,
                                "symbol": symbol,
                                "balance": amount,
                                "decimals": decimals
                            })

            # 3. Цены через Jupiter (массовый запрос)
            prices = {}
            if tokens:
                mint_ids = ",".join([t["mint"] for t in tokens])
                try:
                    async with session.get(f"https://price.jup.ag/v4/price?ids={mint_ids}") as resp:
                        if resp.status == 200:
                            j_data = await resp.json()
                            for mint, info in j_data.get("data", {}).items():
                                prices[mint] = float(info.get("price", 0))
                except Exception as e:
                    logger.warning(f"Ошибка получения цен Jupiter: {e}")

            # 4. Цена SOL
            sol_price = 0.0
            try:
                async with session.get("https://price.jup.ag/v4/price?ids=SOL") as r:
                    if r.status == 200:
                        j_data = await r.json()
                        sol_price = float(j_data.get("data", {}).get("SOL", {}).get("price", 0))
            except: pass

            total_usd = sol_balance * sol_price

            lines = [f"💰 <b>Баланс Solana</b>\n<code>{address}</code>\n"
                     f"SOL: {sol_balance:.4f} (≈ ${total_usd:,.2f})"]

            unknown_count = 0
            for tok in tokens:
                price = prices.get(tok["mint"])
                if price is not None:
                    usd_val = tok["balance"] * price
                    if usd_val < MIN_USD_VALUE:
                        continue
                    price_display = f"≈ ${usd_val:.2f}"
                    total_usd += usd_val
                else:
                    price_display = "?"
                    unknown_count += 1
                lines.append(f"• {tok['symbol']}: {tok['balance']:.4f} ({price_display})")

            lines.insert(1, f"<b>Общая стоимость: ≈ ${total_usd:,.2f}</b>")
            if unknown_count > 0:
                lines.append(f"\n⚠️ Токенов с неизвестной ценой: {unknown_count}")

            text = "\n".join(lines)
            await _send_long_message(context.bot, update.effective_chat.id, text, parse_mode="HTML")

            keyboard = [[InlineKeyboardButton("📜 История покупок", callback_data="history_solana")]]
            await update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения Solana баланса")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# Обработчик кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "history_evm":
        keyboard = [
            [InlineKeyboardButton("Ethereum", callback_data="history_eth")],
            [InlineKeyboardButton("BSC", callback_data="history_bsc")]
        ]
        await query.edit_message_text("Выберите сеть для истории:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "history_solana":
        await query.edit_message_text("⏳ Запущен анализ истории покупок Solana...")
        await run_solana_history(query, context)
    elif data.startswith("history_"):
        chain = data.split("_")[1]
        await query.edit_message_text(f"⏳ Запущен анализ истории покупок {chain}...")
        await run_evm_history(query, context, chain)

async def run_evm_history(query, context, chain: str):
    address = context.user_data['address']
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            if chain == "eth":
                conf = NETWORKS["ethereum"]
                explorer = EVMExplorerClient(conf["chain_id"], conf["weth"])
                web3 = EVMWeb3Client(conf["rpc_url"], conf["chain_id"], conf["weth"],
                                     router_address=conf.get("dex_routers", [""])[0],
                                     stable_address=conf.get("stablecoins", [""])[0])
                network = EthereumNetwork(conf, session, explorer, web3)
            else:  # bsc
                conf = NETWORKS["bsc"]
                web3 = EVMWeb3Client(conf["rpc_url"], conf["chain_id"], conf["weth"],
                                     router_address=conf.get("dex_routers", [""])[0],
                                     stable_address=conf.get("stablecoins", [""])[0])
                network = BscNetwork(conf, session, web3)

            traversal = GraphTraversal(session, address, network, max_tokens=100, lookback_days=30, max_depth=3)
            found = await traversal.run()
            if not found:
                await query.edit_message_text("✅ Анализ завершён. Токены не найдены.")
                return
            unique = {}
            for item in found:
                addr = item['token']
                if addr not in unique:
                    unique[addr] = item
            token_lines = [f"• <a href='https://dexscreener.com/{chain}/{addr}'>{data['symbol']}</a> (<code>{addr}</code>)" for addr, data in unique.items()]
            report = f"✅ <b>История покупок {chain.upper()}</b>\nНайдено токенов: {len(unique)}\n" + "\n".join(token_lines)
            await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
            await query.edit_message_text("✅ Готово.")
    except Exception as e:
        logger.exception("Ошибка истории EVM")
        await query.edit_message_text(f"❌ Ошибка: {e}")

async def run_solana_history(query, context):
    await query.edit_message_text("📜 История покупок для Solana пока недоступна.")

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
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ <b>Настройки</b>", reply_markup=reply_markup, parse_mode="HTML")

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
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(history_|history_eth|history_bsc|history_solana)$"))
    app.add_handler(CallbackQueryHandler(settings_button, pattern="^(set_|reset_settings).*"))