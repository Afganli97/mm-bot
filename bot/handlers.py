"""
Обработчики команд Telegram.
Реализовано разделение логики валидации адресов, объединение отчетов EVM,
применение пользовательских настроек и интерактивный выбор сети для истории.
"""
import logging
import asyncio
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

from bot.config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, NETWORKS,
    DEFAULT_MAX_DEPTH, DEFAULT_LOOKBACK_DAYS, DEFAULT_MAX_FOUND_TOKENS,
    MIN_USD_VALUE, BIRDEYE_API_KEY, ALCHEMY_API_KEY
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
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text(
        "👋 Привет! Я бот для анализа кошельков.\n\n"
        "Отправьте EVM или Solana адрес, и я мгновенно покажу балансы во всех доступных сетях, "
        "после чего вы сможете запустить глубокий поиск истории покупок ММ.\n\n"
        "/help - Инструкция\n"
        "/dashboard - Лимиты API\n"
        "/settings - Настройки поиска"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    await update.message.reply_text(
        "🔍 <b>Как пользоваться:</b>\n"
        "1. Отправьте адрес кошелька (EVM или Solana).\n"
        "2. Бот мгновенно агрегирует балансы во всех сетях.\n"
        "3. Нажмите кнопку «История покупок», чтобы выявить связи ММ.\n"
        "/settings - изменить параметры обхода (глубина, дни, лимит токенов).",
        parse_mode="HTML"
    )

async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    usage = get_all_api_usage()
    today = date.today().isoformat()
    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n"
    for service, count in usage.items():
        msg += f"{service}: {count}\n"
    msg += "\n<i>Лимиты автоматически обнуляются в 00:00 UTC.</i>"
    await update.message.reply_text(msg, parse_mode="HTML")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    if context.user_data.get('awaiting_setting'):
        await setting_value_input(update, context)
        return
    text = update.message.text.strip()

    if is_solana_address(text):
        context.user_data['address'] = text
        context.user_data['address_type'] = 'solana'
        await update.message.reply_text("⏳ Собираю баланс в сети Solana...")
        await show_solana_balance(update, context)
    elif web3_is_address(text):
        context.user_data['address'] = text
        context.user_data['address_type'] = 'evm'
        await update.message.reply_text("⏳ Запущена агрегация мультичейн балансов EVM (Ethereum, BSC...)...")
        await show_multichain_evm_balances(update, context)
    else:
        await update.message.reply_text("❌ Формат адреса не распознан. Отправьте валидный адрес EVM или Solana.")
        return

async def _get_alchemy_token_balances(session, address: str) -> List[Dict]:
    alchemy_url = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    payload = {"jsonrpc": "2.0", "method": "alchemy_getTokenBalances", "params": [address], "id": 1}
    try:
        async with session.post(alchemy_url, json=payload, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [t for t in data.get("result", {}).get("tokenBalances", []) if t.get("tokenBalance", "0x0") != "0x0000000000000000000000000000000000000000000000000000000000000000"]
    except: pass
    return []

async def _get_decimals(session, contract_address: str, rpc_url: str) -> int:
    payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": contract_address, "data": "0x313ce567"}, "latest"], "id": 1}
    try:
        async with session.post(rpc_url, json=payload, timeout=10) as resp:
            if resp.status == 200:
                result = (await resp.json()).get("result")
                if result and result != "0x": return int(result, 16)
    except: pass
    return 18

def _is_spam_token(raw_balance: int, decimals: int) -> bool:
    if raw_balance == 0: return True
    if raw_balance == 10 ** decimals: return True
    return False

async def show_multichain_evm_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    moralis: MoralisClient = context.application.bot_data.get('moralis')
    ankr: AnkrClient = context.application.bot_data.get('ankr')
    
    if not ankr:
        await update.message.reply_text("❌ Ankr API не настроен, не могу получить мультичейн балансы.")
        return

    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            lines = [f"💰 <b>Мультичейн EVM Балансы</b>\n<code>{address}</code>\n"]
            total_usd_portfolio = 0.0

            # --- 1. Обработка Ethereum (Moralis + Alchemy) ---
            if "ethereum" in NETWORKS and moralis:
                eth_conf = NETWORKS["ethereum"]
                eth_web3 = EVMWeb3Client(eth_conf["rpc_url"], eth_conf["chain_id"], eth_conf["weth"],
                                         router=eth_conf.get("dex_routers", [""])[0], stable=eth_conf.get("stablecoins", [""])[0])
                evm_cascade = EVMPriceCascade(eth_web3)

                eth_tokens_moralis = await moralis.get_balances(session, address, chain="eth")
                all_eth_tokens = {}

                # 1.1 Добавление нативного ETH баланса
                native_balance = await eth_web3.get_balance(session, address)
                if native_balance > 0:
                    eth_price = await evm_cascade.get_price(session, eth_conf["weth"], "ethereum", 0.0) or 0.0
                    all_eth_tokens["native"] = {
                        "symbol": eth_conf["native_symbol"], "balance": native_balance,
                        "usd_val": native_balance * eth_price, "raw_balance": int(native_balance * 1e18),
                        "decimals": 18, "is_native": True
                    }

                # 1.2 Токены с Moralis
                for t in eth_tokens_moralis:
                    contract = (t.get("contract_address") or "").lower()
                    if not contract or contract == "0x0000000000000000000000000000000000000000": continue
                    balance = float(t.get("balance_formatted", 0))
                    all_eth_tokens[contract] = {
                        "symbol": str(t.get("symbol", "?")).strip(), "balance": balance,
                        "usd_val": float(t.get("usd_value", 0)), "raw_balance": int(balance * 1e18),
                        "decimals": 18, "is_native": False
                    }

                # 1.3 Токены с Alchemy (мелкокапы)
                if ALCHEMY_API_KEY:
                    for at in await _get_alchemy_token_balances(session, address):
                        contract = at.get("contractAddress", "").lower()
                        if not contract or contract in all_eth_tokens or contract == "0x0000000000000000000000000000000000000000": continue
                        raw_balance = int(at.get("tokenBalance", "0x0"), 16)
                        if raw_balance == 0: continue
                        symbol = await TokenInfoService.get_symbol(session, contract, eth_conf["rpc_url"])
                        decimals = await _get_decimals(session, contract, eth_conf["rpc_url"])
                        all_eth_tokens[contract] = {
                            "symbol": symbol, "balance": raw_balance / (10 ** decimals),
                            "usd_val": 0.0, "raw_balance": raw_balance, "decimals": decimals, "is_native": False
                        }

                filtered_eth = {k: v for k, v in all_eth_tokens.items() if not _is_spam_token(v["raw_balance"], v["decimals"])}
                
                for contract, data in filtered_eth.items():
                    if data["usd_val"] == 0.0 and not data["is_native"]:
                        price = await evm_cascade.get_price(session, contract, "ethereum", 0.0)
                        if price: data["usd_val"] = data["balance"] * price

                eth_sorted = sorted(filtered_eth.items(), key=lambda item: (item[1]["usd_val"] if item[1]["usd_val"] > 0 else -1), reverse=True)
                eth_total = sum(data["usd_val"] for _, data in eth_sorted if data["usd_val"] >= MIN_USD_VALUE)
                total_usd_portfolio += eth_total

                lines.append("⛓️ <b>Ethereum</b>")
                lines.append(f"Общая стоимость: ≈ ${eth_total:,.2f}")
                for contract, data in eth_sorted:
                    if data["usd_val"] > 0 and data["usd_val"] < MIN_USD_VALUE: continue
                    display = f"≈ ${data['usd_val']:,.2f}" if data["usd_val"] > 0 else "?"
                    link = f"https://dexscreener.com/ethereum/{eth_conf['weth']}" if data["is_native"] else f"https://dexscreener.com/ethereum/{contract}"
                    lines.append(f"• <a href='{link}'>{data['symbol']}</a>: {data['balance']:.4f} ({display})")
                lines.append("")

            # --- 2. Обработка BSC и других EVM сетей через Ankr ---
            other_chains = [key for key in NETWORKS if key not in ("ethereum", "solana")]
            if other_chains:
                ankr_data = await ankr.get_multichain_balances(session, address, chains=other_chains)
                grouped_assets = {chain: [] for chain in other_chains}
                for a in (ankr_data.get("assets", []) if ankr_data else []):
                    if a.get("blockchain") in grouped_assets: grouped_assets[a["blockchain"]].append(a)

                for chain in other_chains:
                    chain_name = NETWORKS[chain]["name"]
                    chain_total = 0.0
                    chain_lines = []

                    for a in grouped_assets[chain]:
                        usd_val = float(a.get("balanceUsd", 0))
                        sym = str(a.get("tokenSymbol", "?")).strip()
                        if usd_val < MIN_USD_VALUE and sym != NETWORKS[chain]["native_symbol"]: continue
                        
                        chain_total += usd_val
                        bal = float(a.get("balance", 0))
                        display = f"≈ ${usd_val:,.2f}" if usd_val > 0 else "?"
                        contract = a.get("contractAddress", "")
                        
                        # Выдача правильного линка для нативных и обычных токенов
                        if contract and contract.lower() != "0x0000000000000000000000000000000000000000":
                            chain_lines.append(f"• <a href='https://dexscreener.com/{chain}/{contract}'>{sym}</a>: {bal:.4f} ({display})")
                        elif sym == NETWORKS[chain]["native_symbol"] and NETWORKS[chain].get("weth"):
                            chain_lines.append(f"• <a href='https://dexscreener.com/{chain}/{NETWORKS[chain]['weth']}'>{sym}</a>: {bal:.4f} ({display})")
                        else:
                            chain_lines.append(f"• {sym}: {bal:.4f} ({display})")

                    total_usd_portfolio += chain_total
                    lines.append(f"⛓️ <b>{chain_name}</b>")
                    lines.append(f"Общая стоимость: ≈ ${chain_total:,.2f}")
                    lines.extend(chain_lines)
                    lines.append("")

            lines.insert(2, f"💼 <b>Общий баланс портфеля: ≈ ${total_usd_portfolio:,.2f}</b>\n")
            await _send_long_message(context.bot, update.effective_chat.id, "\n".join(lines).strip(), parse_mode="HTML")

            keyboard = [[InlineKeyboardButton("🔎 Найти историю покупок", callback_data="history_evm_menu")]]
            await update.message.reply_text("Анализ завершен. Желаете найти историю ранних покупок ММ?", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения Мультичейн EVM балансов")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def show_solana_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data['address']
    helius: HeliusClient = context.application.bot_data.get('helius')
    cascade: CascadePriceFetcher = context.application.bot_data.get('cascade')
    if not helius or not cascade: return
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            data = await helius.get_wallet_balances(session, address)
            if not data: return
            balances = data.get("balances", [])
            lines = [f"💰 <b>Баланс Solana</b>\n<code>{address}</code>"]

            no_price_mints = [tok["mint"] for tok in balances if tok.get("usdValue") is None]
            additional_prices = await cascade.get_prices(session, no_price_mints) if no_price_mints else {}

            total_usd = 0.0
            for tok in balances:
                symbol = str(tok.get("symbol") or tok.get("name", "?")).strip()
                bal = float(tok.get("balance", 0))
                mint = tok.get("mint")
                usd_val = tok.get("usdValue")
                if usd_val is not None: usd_val = float(usd_val)
                else: usd_val = bal * additional_prices.get(mint) if additional_prices.get(mint) else None

                if usd_val is not None and usd_val < MIN_USD_VALUE: continue
                if usd_val is not None:
                    total_usd += usd_val
                    price_display = f"≈ ${usd_val:,.2f}"
                else:
                    price_display = "?"

                link = f"https://dexscreener.com/solana/{mint}" if mint else ""
                lines.append(f"• <a href='{link}'>{symbol}</a>: {bal:.4f} ({price_display})" if link else f"• {symbol}: {bal:.4f} ({price_display})")

            lines.insert(1, f"Общая стоимость: ≈ ${total_usd:,.2f}\n")
            await _send_long_message(context.bot, update.effective_chat.id, "\n".join(lines), parse_mode="HTML")

            keyboard = [[InlineKeyboardButton("🔎 Поиск истории покупок (Solana)", callback_data="history_solana")]]
            await update.message.reply_text("Запустить поиск связей?", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception("Ошибка получения Solana баланса")

async def history_evm_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(conf["name"], callback_data=f"history_{chain_key}")] for chain_key, conf in NETWORKS.items() if chain_key != "solana"]
    await query.edit_message_text("Выберите EVM сеть, в которой необходимо проанализировать историю покупок MM:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "history_solana":
        await query.edit_message_text("⏳ Запущен анализ истории покупок Solana. Обхожу связанные адреса...")
        await run_solana_history(query, context)
    elif data.startswith("history_"):
        chain = data.split("_")[1]
        network_name = NETWORKS.get(chain, {}).get("name", chain.upper())
        await query.edit_message_text(f"⏳ Запущен анализ истории покупок в сети {network_name}. Строю граф связей...")
        await run_evm_history(query, context, chain)


async def run_evm_history(query, context, chain: str):
    address = context.user_data['address']
    user_id = query.from_user.id
    
    settings_dict = get_user_settings_dict(user_id)
    max_depth = int(settings_dict.get('max_depth', DEFAULT_MAX_DEPTH))
    lookback_days = int(settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS))
    max_tokens = int(settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS))

    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            conf = NETWORKS[chain]
            web3 = EVMWeb3Client(conf["rpc_url"], conf["chain_id"], conf["weth"], router=conf.get("dex_routers", [""])[0], stable=conf.get("stablecoins", [""])[0])
            
            # ФАБРИКА СЕТЕЙ: Разделяем потоки Ethereum (V2 API) и BSC (RPC Альтернатива)
            if chain == "ethereum":
                # Ethereum работает через Etherscan V2
                explorer = EVMExplorerClient(conf["chain_id"], conf["weth"])
                network = EthereumNetwork(conf, session, explorer, web3)
            elif chain == "bsc":
                # BSC работает через бесплатный RPC (без Etherscan)
                network = BscNetwork(conf, session, web3)
            else:
                await query.edit_message_text(f"❌ Сеть {chain} пока не поддерживается для истории.")
                return

            traversal = GraphTraversal(session, address, network, max_tokens=max_tokens, lookback_days=lookback_days, max_depth=max_depth)
            found = await traversal.run()
            
            if not found:
                await query.edit_message_text("✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено.")
                return
            
            unique = {item['token']: item for item in found}
            token_lines = [f"• <a href='https://dexscreener.com/{chain}/{addr}'>{data['symbol']}</a> (<code>{addr}</code>)" for addr, data in unique.items()]
            report = f"✅ <b>История покупок {conf['name']}</b>\nНайдено токенов: {len(unique)} (глубина: {max_depth})\n\n" + "\n".join(token_lines)
            
            # Импорт функции отправки длинных сообщений (убедитесь, что она есть в файле)
            await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
            
    except Exception as e:
        logger.exception("Ошибка истории EVM")
        await query.edit_message_text(f"❌ Ошибка во время обхода графа: {e}")


async def get_token_names_cascade(session, mints: List[str]) -> Dict[str, str]:
    names = {}
    if not mints: return names
    try:
        async with session.get(f"https://price.jup.ag/v4/price?ids={','.join(mints[:100])}", timeout=10) as resp:
            if resp.status == 200:
                for mint, info in (await resp.json()).get("data", {}).items():
                    if isinstance(info, dict): names[mint] = info.get("name") or info.get("symbol") or "?"
    except: pass
    remaining = [m for m in mints if m not in names]
    if not remaining: return names
    
    if BIRDEYE_API_KEY:
        for mint in remaining[:]:
            try:
                async with session.get(f"https://public-api.birdeye.so/defi/token_overview?address={mint}&x-chain=solana", headers={"X-API-KEY": BIRDEYE_API_KEY}, timeout=5) as resp:
                    if resp.status == 200:
                        name = (await resp.json()).get("data", {}).get("name") or (await resp.json()).get("data", {}).get("symbol")
                        if name: names[mint] = name; remaining.remove(mint)
            except: pass
            await asyncio.sleep(0.3)
            
    for mint in remaining[:]:
        try:
            async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5) as resp:
                if resp.status == 200:
                    pairs = (await resp.json()).get("pairs")
                    if pairs:
                        name = pairs[0].get("baseToken", {}).get("name") or pairs[0].get("baseToken", {}).get("symbol")
                        if name: names[mint] = name; remaining.remove(mint)
        except: pass
        await asyncio.sleep(0.3)
        
    for mint in remaining: names[mint] = f"{mint[:6]}...{mint[-4:]}"
    return names

async def run_solana_history(query, context):
    address = context.user_data['address']
    user_id = query.from_user.id
    settings_dict = get_user_settings_dict(user_id)
    max_depth = int(settings_dict.get('max_depth', DEFAULT_MAX_DEPTH))
    lookback_days = int(settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS))
    max_tokens = int(settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS))

    helius: HeliusClient = context.application.bot_data.get('helius')
    if not helius: return
    try:
        from aiohttp import ClientSession
        async with ClientSession() as session:
            traversal = SolanaTraversal(session, address, helius, max_depth=max_depth, max_tokens=max_tokens, lookback_days=lookback_days)
            found = await traversal.run()
            if not found:
                await query.edit_message_text("✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено.")
                return
            unique_mints = list({item['token'] for item in found})
            names = await get_token_names_cascade(session, unique_mints)
            token_lines = [f"• <a href='https://dexscreener.com/solana/{item['token']}'>{names.get(item['token'], '?')}</a> (<code>{item['token']}</code>)" for item in found]
            report = f"✅ <b>История покупок Solana</b>\nНайдено токенов: {len(found)} (глубина: {max_depth})\n\n" + "\n".join(token_lines)
            await _send_long_message(context.bot, query.message.chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.exception("Ошибка истории Solana")

async def _send_long_message(bot, chat_id, text, parse_mode="HTML"):
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH: await bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True)
    else:
        chunk = ""
        for line in text.split('\n'):
            if len(chunk) + len(line) + 1 > TELEGRAM_MAX_MESSAGE_LENGTH:
                await bot.send_message(chat_id, chunk.strip(), parse_mode=parse_mode, disable_web_page_preview=True)
                chunk = line + "\n"
            else: chunk += line + "\n"
        if chunk: await bot.send_message(chat_id, chunk.strip(), parse_mode=parse_mode, disable_web_page_preview=True)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    user_id = update.effective_user.id
    settings_dict = get_user_settings_dict(user_id)
    keyboard = [
        [InlineKeyboardButton(f"Глубина обхода: {settings_dict.get('max_depth', DEFAULT_MAX_DEPTH)}", callback_data="set_max_depth")],
        [InlineKeyboardButton(f"Период (дней): {settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS)}", callback_data="set_lookback_days")],
        [InlineKeyboardButton(f"Макс. коинов в отчете: {settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS)}", callback_data="set_max_tokens")],
        [InlineKeyboardButton("🔄 Сбросить на умолчания", callback_data="reset_settings")]
    ]
    await update.message.reply_text("⚙️ <b>Индивидуальные настройки поиска ММ:</b>\n<i>Нажмите на кнопку для изменения.</i>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

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
        await query.edit_message_text("✅ Настройки сброшены на стандартные.")
        return
    setting_map = {
        "set_max_depth": ("max_depth", "Введите новую максимальную глубину обхода связанных кошельков (число, например 3):"),
        "set_lookback_days": ("lookback_days", "Введите период анализа истории в днях (число, например 30):"),
        "set_max_tokens": ("max_tokens", "Введите максимальное количество токенов, после которого алгоритм остановится:")
    }
    key, prompt = setting_map.get(data, (None, None))
    if key:
        context.user_data['awaiting_setting'] = key
        await query.edit_message_text(prompt)

async def setting_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update): return
    key = context.user_data.pop('awaiting_setting', None)
    if not key: return await handle_message(update, context)
    value = update.message.text.strip()
    if not value.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите корректное число.")
        return
    set_user_setting(update.effective_user.id, key, value)
    await update.message.reply_text(f"✅ Настройка успешно обновлена! Новое значение: {value}")

def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(history_evm_menu_handler, pattern='history_evm_menu'))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(history_eth|history_bsc|history_polygon)$"))
    app.add_handler(CallbackQueryHandler(settings_button, pattern="^(set_|reset_settings).*"))
