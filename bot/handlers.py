"""
Обработчики команд Telegram.
"""
import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from bot.config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, NETWORKS,
    DEFAULT_MAX_DEPTH, DEFAULT_LOOKBACK_DAYS, DEFAULT_MIN_TRANSFER_VALUE_ETH,
    DEFAULT_MAX_FOUND_TOKENS, MIN_USD_VALUE, BIRDEYE_API_KEY, ALCHEMY_API_KEY
)
from bot.database import get_all_api_usage, get_user_setting, set_user_setting, get_user_settings_dict
from bot.graph_traversal import GraphTraversal
from bot.api_clients import (
    EVMExplorerClient, AnkrClient, EVMWeb3Client, HeliusClient, CascadePriceFetcher,
    JupiterMassPrice, BirdeyePrice, DexScreenerPrice, MoralisClient, EVMPriceCascade, TokenInfoService
)
from bot.networks.ethereum import EthereumNetwork
from bot.networks.bsc import BscNetwork
from bot.networks.solana import SolanaNetwork
from bot.solana_traversal import SolanaTraversal

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

    if web3_is_address(text):
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

# Вспомогательные функции для Alchemy и токенов
async def _get_alchemy_token_balances(session, address: str) -> List[Dict]:
    """Возвращает список всех токенов с ненулевым балансом через Alchemy."""
    alchemy_url = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    payload = {
        "jsonrpc": "2.0",
        "method": "alchemy_getTokenBalances",
        "params": [address],
        "id": 1
    }
    try:
        async with session.post(alchemy_url, json=payload, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data.get("result", {})
                tokens = result.get("tokenBalances", [])
                return [t for t in tokens if t.get("tokenBalance", "0x0") != "0x0000000000000000000000000000000000000000000000000000000000000000"]
    except Exception as e:
        logger.warning(f"Alchemy token balances failed: {e}")
    return []

async def _get_decimals(session, contract_address: str, rpc_url: str) -> int:
    """Получает decimals токена через eth_call."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract_address, "data": "0x313ce567"}, "latest"],
        "id": 1
    }
    try:
        async with session.post(rpc_url, json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data.get("result")
                if result and result != "0x":
                    return int(result, 16)
    except:
        pass
    return 18

def _is_spam_token(raw_balance: int, decimals: int) -> bool:
    """
    Возвращает True, если токен похож на спам:
    - баланс равен ровно 0
    - баланс равен ровно 1 минимальной единице (т.е. 1 токен)
    """
    if raw_balance == 0:
        return True
    if raw_balance == 10 ** decimals:
        return True
    return False

async def show_evm_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    moralis: MoralisClient = context.application.bot_data.get('moralis')
    ankr: AnkrClient = context.application.bot_data.get('ankr')
    if not moralis or not ankr:
        await update.message.reply_text("❌ Moralis или Ankr не настроен.")
        return

    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            # 1. Moralis (основной список)
            eth_tokens_moralis = await moralis.get_balances(session, address, chain="eth")
            # 2. BSC через Ankr
            ankr_data = await ankr.get_multichain_balances(session, address, chains=["bsc"])
            bsc_assets = ankr_data.get("assets", []) if ankr_data else []

            lines = [f"💰 <b>Баланс кошелька</b>\n<code>{address}</code>"]

            # --- Ethereum ---
            eth_conf = NETWORKS["ethereum"]
            rpc_url = eth_conf["rpc_url"]
            eth_web3 = EVMWeb3Client(rpc_url, eth_conf["chain_id"], eth_conf["weth"],
                                     router=eth_conf.get("dex_routers", [""])[0],
                                     stable=eth_conf.get("stablecoins", [""])[0])
            evm_cascade = EVMPriceCascade(eth_web3)

            # Единый словарь токенов: contract_address -> {symbol, balance, usd_val, raw_balance, decimals}
            all_eth_tokens = {}

            # Заполняем из Moralis
            for t in eth_tokens_moralis:
                contract = (t.get("contract_address") or "").lower()
                if not contract or contract == "0x0000000000000000000000000000000000000000":
                    continue
                symbol = t.get("symbol", "?")
                balance = float(t.get("balance_formatted", 0))
                usd_val = float(t.get("usd_value", 0))
                # decimals из ответа Moralis нет, берём 18 по умолчанию
                decimals = 18
                # Вычисляем raw_balance приблизительно
                raw_balance = int(balance * (10 ** decimals))
                all_eth_tokens[contract] = {
                    "symbol": symbol,
                    "balance": balance,
                    "usd_val": usd_val,
                    "raw_balance": raw_balance,
                    "decimals": decimals
                }

            # 3. Alchemy – дополняем недостающие токены
            alchemy_tokens = await _get_alchemy_token_balances(session, address)
            for at in alchemy_tokens:
                contract = at.get("contractAddress", "").lower()
                if not contract or contract == "0x0000000000000000000000000000000000000000":
                    continue
                if contract in all_eth_tokens:          # уже есть из Moralis – пропускаем
                    continue
                raw_balance = int(at.get("tokenBalance", "0x0"), 16)
                if raw_balance == 0:
                    continue
                symbol = await TokenInfoService.get_symbol(session, contract, rpc_url)
                decimals = await _get_decimals(session, contract, rpc_url)
                balance = raw_balance / (10 ** decimals)
                all_eth_tokens[contract] = {
                    "symbol": symbol,
                    "balance": balance,
                    "usd_val": 0.0,
                    "raw_balance": raw_balance,
                    "decimals": decimals
                }

            # 4. Фильтруем спам: удаляем токены, у которых raw_balance == 0 или == 1 токен (10^decimals)
            filtered_tokens = {}
            for contract, data in all_eth_tokens.items():
                if not _is_spam_token(data["raw_balance"], data["decimals"]):
                    filtered_tokens[contract] = data
            all_eth_tokens = filtered_tokens

            # 5. Для всех токенов, где цена 0 – пробуем каскад
            for contract, data in all_eth_tokens.items():
                if data["usd_val"] == 0.0:
                    price = await evm_cascade.get_price(session, contract, "ethereum", 0.0)
                    if price:
                        data["usd_val"] = data["balance"] * price

            # 6. Сортируем: с ценой (по убыванию), затем без цены
            eth_sorted = sorted(all_eth_tokens.items(),
                               key=lambda item: (item[1]["usd_val"] if item[1]["usd_val"] > 0 else -1),
                               reverse=True)

            eth_total = 0.0
            eth_lines = []
            for contract, data in eth_sorted:
                if data["usd_val"] > 0 and data["usd_val"] < MIN_USD_VALUE:
                    continue
                if data["usd_val"] > 0:
                    eth_total += data["usd_val"]
                    display = f"≈ ${data['usd_val']:,.2f}"
                else:
                    display = "?"
                link = f"https://dexscreener.com/ethereum/{contract}"
                eth_lines.append(f"• <a href='{link}'>{data['symbol']}</a>: {data['balance']:.4f} ({display})")

            lines.append("\n⛓️ <b>Ethereum</b>")
            lines.append(f"Общая стоимость в сети: ≈ ${eth_total:,.2f}")
            lines.extend(eth_lines)

            # --- BSC (Ankr) ---
            bsc_total = 0.0
            bsc_lines = []
            for a in bsc_assets:
                sym = a.get("tokenSymbol", "?")
                bal = float(a.get("balance", 0))
                usd_val = float(a.get("balanceUsd", 0))
                if usd_val < MIN_USD_VALUE and sym != "BNB":
                    continue
                if usd_val > 0:
                    bsc_total += usd_val
                    display = f"≈ ${usd_val:,.2f}"
                else:
                    display = "?"
                contract = a.get("contractAddress", "")
                if contract and contract.lower() != "0x0000000000000000000000000000000000000000":
                    link = f"https://dexscreener.com/bsc/{contract}"
                    bsc_lines.append(f"• <a href='{link}'>{sym}</a>: {bal:.4f} ({display})")
                else:
                    bsc_lines.append(f"• {sym}: {bal:.4f} ({display})")

            lines.append("\n⛓️ <b>BSC</b>")
            lines.append(f"Общая стоимость в сети: ≈ ${bsc_total:,.2f}")
            lines.extend(bsc_lines)

            text = "\n".join(lines)
            await _send_long_message(context.bot, update.effective_chat.id, text, parse_mode="HTML")

            keyboard = [[InlineKeyboardButton("📜 История покупок", callback_data="history_evm")]]
            await update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения EVM балансов")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def show_solana_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    helius: HeliusClient = context.application.bot_data.get('helius')
    cascade: CascadePriceFetcher = context.application.bot_data.get('cascade')
    if not helius or not cascade:
        await update.message.reply_text("❌ Helius или каскад цен не настроен.")
        return
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            data = await helius.get_wallet_balances(session, address)
            if not data:
                await update.message.reply_text("❌ Не удалось получить баланс Solana.")
                return
            balances = data.get("balances", [])
            lines = [f"💰 <b>Баланс Solana</b>\n<code>{address}</code>"]

            no_price_mints = [tok["mint"] for tok in balances if tok.get("usdValue") is None]
            additional_prices = {}
            if no_price_mints:
                additional_prices = await cascade.get_prices(session, no_price_mints)

            total_usd = 0.0
            for tok in balances:
                symbol = tok.get("symbol") or tok.get("name", "?")
                bal = float(tok.get("balance", 0))
                mint = tok.get("mint")
                usd_val = tok.get("usdValue")
                if usd_val is not None:
                    usd_val = float(usd_val)
                else:
                    price = additional_prices.get(mint)
                    if price is not None:
                        usd_val = bal * price
                    else:
                        usd_val = None

                if usd_val is not None and usd_val < MIN_USD_VALUE:
                    continue

                if usd_val is not None:
                    total_usd += usd_val
                    price_display = f"≈ ${usd_val:,.2f}"
                else:
                    price_display = "?"

                link = f"https://dexscreener.com/solana/{mint}" if mint else ""
                if link:
                    lines.append(f"• <a href='{link}'>{symbol}</a>: {bal:.4f} ({price_display})")
                else:
                    lines.append(f"• {symbol}: {bal:.4f} ({price_display})")

            lines.insert(1, f"Общая стоимость: ≈ ${total_usd:,.2f}")
            text = "\n".join(lines)
            await _send_long_message(context.bot, update.effective_chat.id, text, parse_mode="HTML")

            keyboard = [[InlineKeyboardButton("📜 История покупок", callback_data="history_solana")]]
            await update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения Solana баланса")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# Обработчик кнопок
async def history_evm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logger.info(f"Button clicked: history_evm")
    keyboard = [
        [InlineKeyboardButton("Ethereum", callback_data="history_eth")],
        [InlineKeyboardButton("BSC", callback_data="history_bsc")]
    ]
    await query.edit_message_text("Выберите сеть для истории:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    logger.info(f"Button clicked: {data}")

    if data == "history_solana":
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
                                     router=conf.get("dex_routers", [""])[0],
                                     stable=conf.get("stablecoins", [""])[0])
                network = EthereumNetwork(conf, session, explorer, web3)
            else:
                conf = NETWORKS["bsc"]
                web3 = EVMWeb3Client(conf["rpc_url"], conf["chain_id"], conf["weth"],
                                     router=conf.get("dex_routers", [""])[0],
                                     stable=conf.get("stablecoins", [""])[0])
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
    except Exception as e:
        logger.exception("Ошибка истории EVM")
        await query.edit_message_text(f"❌ Ошибка: {e}")

# Каскадное получение имён токенов Solana
async def get_token_names_cascade(session, mints: List[str]) -> Dict[str, str]:
    names = {}
    if not mints:
        return names
    # 1. Jupiter
    try:
        ids = ",".join(mints[:100])
        url = f"https://price.jup.ag/v4/price?ids={ids}"
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                    for mint, info in data["data"].items():
                        if isinstance(info, dict):
                            name = info.get("name") or info.get("symbol") or "?"
                            names[mint] = name
    except Exception as e:
        logger.warning(f"Jupiter имена недоступны: {e}")
    remaining = [m for m in mints if m not in names]
    if not remaining:
        return names
    # 2. Birdeye
    if BIRDEYE_API_KEY:
        for mint in remaining[:]:
            try:
                url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}&x-chain=solana"
                async with session.get(url, headers={"X-API-KEY": BIRDEYE_API_KEY}, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                            token_data = data["data"]
                            if isinstance(token_data, dict):
                                name = token_data.get("name") or token_data.get("symbol")
                                if name:
                                    names[mint] = name
                                    remaining.remove(mint)
            except:
                pass
            await asyncio.sleep(0.3)
    # 3. DexScreener
    for mint in remaining[:]:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs")
                    if pairs and isinstance(pairs, list) and len(pairs) > 0:
                        base = pairs[0].get("baseToken")
                        if isinstance(base, dict):
                            name = base.get("name") or base.get("symbol")
                            if name:
                                names[mint] = name
                                remaining.remove(mint)
        except:
            pass
        await asyncio.sleep(0.3)
    # 4. Сокращённый адрес
    for mint in remaining:
        names[mint] = f"{mint[:6]}...{mint[-4:]}"
    return names

async def run_solana_history(query, context):
    address = context.user_data['address']
    helius: HeliusClient = context.application.bot_data.get('helius')
    if not helius:
        await query.edit_message_text("❌ Helius не настроен.")
        return
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            traversal = SolanaTraversal(session, address, helius, max_depth=3, max_tokens=100)
            found = await traversal.run()
            if not found:
                await query.edit_message_text("✅ Анализ завершён. Токены не найдены.")
                return
            unique_mints = list({item['token'] for item in found})
            try:
                names = await get_token_names_cascade(session, unique_mints)
            except Exception as e:
                logger.warning(f"Не удалось получить имена токенов: {e}")
                names = {}
            token_lines = []
            for item in found:
                addr = item['token']
                symbol = names.get(addr, "?")
                link = f"https://dexscreener.com/solana/{addr}"
                token_lines.append(f"• <a href='{link}'>{symbol}</a> (<code>{addr}</code>)")
            report = f"✅ <b>История покупок Solana</b>\nНайдено токенов: {len(found)}\n" + "\n".join(token_lines)
            await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.exception("Ошибка истории Solana")
        await query.edit_message_text(f"❌ Ошибка: {e}")

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
    app.add_handler(CallbackQueryHandler(history_evm_handler, pattern='history_evm'))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(history_eth|history_bsc|history_solana)$"))
    app.add_handler(CallbackQueryHandler(settings_button, pattern="^(set_|reset_settings).*"))