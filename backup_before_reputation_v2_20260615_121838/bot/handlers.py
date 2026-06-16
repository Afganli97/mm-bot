"""
Telegram handlers.

Здесь находится вся логика:
- валидация адресов;
- EVM/Solana балансы;
- история покупок;
- dashboard;
- настройки пользователя.
"""

import asyncio
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.api_clients import (
    AnkrClient,
    BirdeyePrice,
    BscScanExplorerClient,
    CascadePriceFetcher,
    DexScreenerPrice,
    EVMExplorerClient,
    EVMPriceCascade,
    EVMWeb3Client,
    HeliusClient,
    MoralisClient,
    TokenInfoService,
)
from bot.blacklist import is_blacklisted
from bot.config import (
    ALCHEMY_API_KEY,
    ALLOWED_USER_IDS,
    BIRDEYE_API_KEY,
    BSCSCAN_API_KEYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES_PER_ADDRESS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FOUND_TOKENS,
    ETHERSCAN_API_KEYS,
    HARD_MAX_ADDRESSES,
    HARD_MAX_BRANCHES_PER_ADDRESS,
    HARD_MAX_DEPTH,
    HARD_MAX_FOUND_TOKENS,
    HARD_MAX_LOOKBACK_DAYS,
    MIN_USD_VALUE,
    NETWORKS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
)
from bot.database import (
    delete_user_settings,
    get_all_api_usage,
    get_user_settings_dict,
    set_user_setting,
)
from bot.graph_traversal import GraphTraversal
from bot.networks.bsc import BscNetwork
from bot.networks.ethereum import EthereumNetwork
from bot.networks.solana import SolanaNetwork
from bot.solana_traversal import SolanaTraversal


logger = logging.getLogger(__name__)


SETTING_RANGES: Dict[str, Tuple[int, int]] = {
    "max_depth": (1, HARD_MAX_DEPTH),
    "lookback_days": (1, HARD_MAX_LOOKBACK_DAYS),
    "max_tokens": (1, HARD_MAX_FOUND_TOKENS),
    "max_addresses": (100, HARD_MAX_ADDRESSES),
}


def _get_global_session(context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get("session")

    if session is None:
        from aiohttp import ClientSession

        session = ClientSession()
        context.application.bot_data["session"] = session

    return session


def web3_is_address(addr: str) -> bool:
    from web3 import Web3

    return bool(Web3.is_address(addr))


def is_solana_address(addr: str) -> bool:
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(addr)
        return True
    except Exception:
        return False


def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None

    if user_id is None:
        return False

    if not ALLOWED_USER_IDS:
        return False

    return user_id in ALLOWED_USER_IDS


def _format_usd(value: Optional[float]) -> str:
    if value is None:
        return "?"

    value = float(value)

    if value <= 0:
        return "?"

    if value < 0.01:
        return f"≈ ${value:,.6f}"

    if value < 1:
        return f"≈ ${value:,.4f}"

    return f"≈ ${value:,.2f}"


def _format_balance(value: float) -> str:
    if value >= 1:
        return f"{value:.4f}"

    if value > 0:
        return f"{value:.8f}"

    return "0"


def _is_spam_token(raw_balance: int, decimals: int) -> bool:
    """
    Базовый фильтр мусора.

    Важно:
    - не считаем спамом 1 токен;
    - не считаем спамом микрокапы;
    - отсекаем только нулевые/битые балансы.
    """

    if raw_balance <= 0:
        return True

    if decimals < 0:
        return True

    return False


def _token_link(chain: str, address: str) -> str:
    if not address:
        return ""

    return f"https://dexscreener.com/{chain}/{address}"


def _token_line(
    chain: str,
    symbol: str,
    address: str,
    balance: float,
    usd_value: Optional[float],
) -> str:
    symbol = symbol or "?"
    display = _format_usd(usd_value)
    balance_text = _format_balance(balance)

    link = _token_link(chain, address)

    if link:
        return f"• <a href='{link}'>{symbol}</a>: {balance_text} ({display})"

    return f"• {symbol}: {balance_text} ({display})"


def _get_int_setting(
    settings_dict: Dict[str, str],
    key: str,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    raw = settings_dict.get(key)

    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default

    return max(min_value, min(max_value, value))


def _get_effective_settings(user_id: int) -> Dict[str, int]:
    raw_settings = get_user_settings_dict(user_id)

    return {
        "max_depth": _get_int_setting(
            raw_settings,
            "max_depth",
            DEFAULT_MAX_DEPTH,
            1,
            HARD_MAX_DEPTH,
        ),
        "lookback_days": _get_int_setting(
            raw_settings,
            "lookback_days",
            DEFAULT_LOOKBACK_DAYS,
            1,
            HARD_MAX_LOOKBACK_DAYS,
        ),
        "max_tokens": _get_int_setting(
            raw_settings,
            "max_tokens",
            DEFAULT_MAX_FOUND_TOKENS,
            1,
            HARD_MAX_FOUND_TOKENS,
        ),
        "max_addresses": _get_int_setting(
            raw_settings,
            "max_addresses",
            DEFAULT_MAX_ADDRESSES,
            100,
            HARD_MAX_ADDRESSES,
        ),
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    await update.message.reply_text(
        "👋 Привет! Я бот для анализа кошельков.\n\n"
        "Отправьте EVM или Solana адрес, и я покажу балансы во всех доступных сетях.\n"
        "После этого можно запустить глубокий поиск истории покупок ММ.\n\n"
        "/help - Инструкция\n"
        "/dashboard - Лимиты API\n"
        "/settings - Настройки поиска"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    await update.message.reply_text(
        "🔍 <b>Как пользоваться:</b>\n"
        "1. Отправьте адрес кошелька EVM или Solana.\n"
        "2. Бот агрегирует балансы по доступным сетям.\n"
        "3. Нажмите «История покупок», чтобы найти связанные кошельки и ранние покупки.\n"
        "4. В /settings можно изменить глубину, период, лимит токенов и лимит адресов.\n\n"
        "<b>Важно:</b> бот использует только бесплатные/лимитные API, поэтому настройки ограничены, "
        "чтобы не упереться в лимиты.",
        parse_mode="HTML",
    )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    usage = get_all_api_usage()
    today = date.today().isoformat()

    msg = f"📊 <b>API лимиты на сегодня ({today} UTC):</b>\n\n"

    for service, info in usage.items():
        used = int(info.get("used", 0))
        limit = int(info.get("limit", 0) or 0)
        keys = info.get("keys", {})

        if limit > 0:
            remaining = max(limit - used, 0)
            msg += f"<b>{service}</b>: {used}/{limit}, осталось {remaining}\n"
        else:
            msg += f"<b>{service}</b>: {used}, лимит неизвестен\n"

        if keys:
            key_parts = []

            for key_idx, key_used in sorted(keys.items()):
                key_parts.append(f"key[{key_idx}]={key_used}")

            if key_parts:
                msg += "  " + ", ".join(key_parts) + "\n"

        msg += "\n"

    msg += "<i>Старые счётчики автоматически очищаются в 00:00 UTC.</i>"

    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    if context.user_data.get("awaiting_setting"):
        await setting_value_input(update, context)
        return

    text = update.message.text.strip()

    if is_solana_address(text):
        context.user_data["address"] = text
        context.user_data["address_type"] = "solana"

        await update.message.reply_text("⏳ Собираю баланс в сети Solana...")
        await show_solana_balance(update, context)
        return

    if web3_is_address(text):
        context.user_data["address"] = text.lower()
        context.user_data["address_type"] = "evm"

        await update.message.reply_text(
            "⏳ Запущена агрегация мультичейн балансов EVM: Ethereum, BSC..."
        )
        await show_multichain_evm_balances(update, context)
        return

    await update.message.reply_text(
        "❌ Формат адреса не распознан. Отправьте валидный адрес EVM или Solana."
    )


async def _get_alchemy_token_balances(
    session,
    address: str,
) -> List[Dict[str, Any]]:
    if not ALCHEMY_API_KEY:
        return []

    alchemy_url = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

    payload = {
        "jsonrpc": "2.0",
        "method": "alchemy_getTokenBalances",
        "params": [address],
        "id": 1,
    }

    try:
        async with session.post(alchemy_url, json=payload, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()

                return [
                    token
                    for token in data.get("result", {}).get("tokenBalances", [])
                    if token.get("tokenBalance", "0x0")
                    not in (
                        "0x0",
                        "0x0000000000000000000000000000000000000000000000000000000000000000",
                    )
                ]
    except Exception as exc:
        logger.debug("Alchemy token balances error: %s", exc)

    return []


async def _get_decimals(session, contract_address: str, rpc_url: str) -> int:
    return await TokenInfoService.get_decimals(session, contract_address, rpc_url)


async def show_multichain_evm_balances(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    address = context.user_data.get("address")

    if not address:
        await update.message.reply_text("❌ Сначала отправьте адрес кошелька.")
        return

    session = _get_global_session(context)

    moralis: Optional[MoralisClient] = context.application.bot_data.get("moralis")
    ankr: Optional[AnkrClient] = context.application.bot_data.get("ankr")
    cascade: Optional[CascadePriceFetcher] = context.application.bot_data.get("cascade")

    lines = [
        f"💰 <b>Мультичейн EVM Балансы</b>",
        f"<code>{address}</code>",
    ]

    total_usd_portfolio = 0.0

    # Ethereum: native ETH + Moralis + Alchemy
    if "ethereum" in NETWORKS:
        eth_conf = NETWORKS["ethereum"]

        eth_web3 = EVMWeb3Client(
            eth_conf["rpc_url"],
            eth_conf["chain_id"],
            eth_conf["weth"],
            router=eth_conf.get("dex_routers", [""])[0],
            stable=eth_conf.get("stablecoins", [""])[0],
        )

        evm_cascade = EVMPriceCascade(eth_web3)
        all_eth_tokens: Dict[str, Dict[str, Any]] = {}

        try:
            native_balance = await eth_web3.get_balance(session, address)

            if native_balance > 0:
                native_price = await evm_cascade.get_price(
                    session,
                    eth_conf["weth"],
                    "ethereum",
                ) or 0.0

                all_eth_tokens["native"] = {
                    "symbol": eth_conf["native_symbol"],
                    "balance": native_balance,
                    "usd_val": native_balance * native_price,
                    "raw_balance": int(native_balance * 10**18),
                    "decimals": 18,
                    "is_native": True,
                    "address": eth_conf["weth"],
                }
        except Exception as exc:
            logger.warning("Не удалось получить native ETH баланс: %s", exc)

        if moralis:
            try:
                eth_tokens_moralis = await moralis.get_balances(session, address, chain="eth")

                for token in eth_tokens_moralis:
                    contract = (token.get("contract_address") or "").lower()

                    if not contract or contract == "0x0000000000000000000000000000000000000000":
                        continue

                    if contract in all_eth_tokens:
                        continue

                    balance = float(token.get("balance_formatted") or 0)
                    usd_value = float(token.get("usd_value") or 0)

                    all_eth_tokens[contract] = {
                        "symbol": str(token.get("symbol") or "?").strip(),
                        "balance": balance,
                        "usd_val": usd_value,
                        "raw_balance": int(balance * 10**18),
                        "decimals": 18,
                        "is_native": False,
                        "address": contract,
                    }
            except Exception as exc:
                logger.warning("Moralis Ethereum balances error: %s", exc)

        if ALCHEMY_API_KEY:
            try:
                alchemy_tokens = await _get_alchemy_token_balances(session, address)

                for token in alchemy_tokens:
                    contract = token.get("contractAddress", "").lower()

                    if not contract:
                        continue

                    if contract in all_eth_tokens:
                        continue

                    if contract == "0x0000000000000000000000000000000000000000":
                        continue

                    raw_balance = int(token.get("tokenBalance", "0x0"), 16)

                    if raw_balance <= 0:
                        continue

                    decimals = await _get_decimals(session, contract, eth_conf["rpc_url"])
                    decimals = max(0, decimals)

                    all_eth_tokens[contract] = {
                        "symbol": await TokenInfoService.get_symbol(
                            session,
                            contract,
                            eth_conf["rpc_url"],
                        ),
                        "balance": raw_balance / (10**decimals),
                        "usd_val": 0.0,
                        "raw_balance": raw_balance,
                        "decimals": decimals,
                        "is_native": False,
                        "address": contract,
                    }
            except Exception as exc:
                logger.warning("Alchemy Ethereum balances error: %s", exc)

        for contract, data in list(all_eth_tokens.items()):
            if _is_spam_token(int(data.get("raw_balance", 0)), int(data.get("decimals", 18))):
                all_eth_tokens.pop(contract, None)
                continue

            if data.get("usd_val", 0) <= 0 and not data.get("is_native"):
                price = await evm_cascade.get_price(
                    session,
                    contract,
                    "ethereum",
                )

                if price:
                    data["usd_val"] = float(data["balance"]) * price

        filtered_eth = {
            key: value
            for key, value in all_eth_tokens.items()
            if value.get("balance", 0) > 0
        }

        eth_sorted = sorted(
            filtered_eth.items(),
            key=lambda item: (
                float(item[1].get("usd_val") or 0)
                if float(item[1].get("usd_val") or 0) > 0
                else -1
            ),
            reverse=True,
        )

        eth_total = sum(
            float(data.get("usd_val") or 0)
            for _, data in eth_sorted
            if float(data.get("usd_val") or 0) > 0
        )

        total_usd_portfolio += eth_total

        lines.append("")
        lines.append("⛓️ <b>Ethereum</b>")
        lines.append(f"Общая известная стоимость: {_format_usd(eth_total)}")

        if eth_sorted:
            for contract, data in eth_sorted:
                lines.append(
                    _token_line(
                        "ethereum",
                        data["symbol"],
                        data["address"],
                        float(data["balance"]),
                        float(data.get("usd_val") or 0),
                    )
                )
        else:
            lines.append("Пусто")

    # Остальные EVM-сети через Ankr
    other_chains = [
        chain_key
        for chain_key, conf in NETWORKS.items()
        if conf.get("chain_id") is not None and chain_key != "ethereum"
    ]

    if other_chains:
        if ankr:
            try:
                ankr_data = await ankr.get_multichain_balances(
                    session,
                    address,
                    chains=other_chains,
                )

                grouped_assets = {
                    chain: []
                    for chain in other_chains
                }

                for asset in ankr_data.get("assets", []) if ankr_data else []:
                    blockchain = asset.get("blockchain")

                    if blockchain in grouped_assets:
                        grouped_assets[blockchain].append(asset)

                for chain in other_chains:
                    chain_conf = NETWORKS[chain]
                    chain_assets = grouped_assets.get(chain, [])

                    if not chain_assets:
                        continue

                    chain_total = 0.0
                    chain_lines: List[str] = []

                    for asset in chain_assets:
                        balance = float(asset.get("balance") or 0)

                        if balance <= 0:
                            continue

                        usd_val = float(asset.get("balanceUsd") or 0)
                        symbol = str(asset.get("tokenSymbol") or "?").strip()
                        contract = asset.get("contractAddress") or ""

                        chain_total += usd_val

                        if contract and contract.lower() != "0x0000000000000000000000000000000000000000":
                            chain_lines.append(
                                _token_line(
                                    chain,
                                    symbol,
                                    contract,
                                    balance,
                                    usd_val,
                                )
                            )
                        elif symbol == chain_conf.get("native_symbol") and chain_conf.get("weth"):
                            chain_lines.append(
                                _token_line(
                                    chain,
                                    symbol,
                                    chain_conf["weth"],
                                    balance,
                                    usd_val,
                                )
                            )
                        else:
                            chain_lines.append(
                                f"• {symbol}: {_format_balance(balance)} ({_format_usd(usd_val)})"
                            )

                    total_usd_portfolio += chain_total

                    lines.append("")
                    lines.append(f"⛓️ <b>{chain_conf['name']}</b>")
                    lines.append(f"Общая известная стоимость: {_format_usd(chain_total)}")

                    if chain_lines:
                        lines.extend(chain_lines)
                    else:
                        lines.append("Пусто")

            except Exception as exc:
                logger.warning("Ankr multichain balances error: %s", exc)
                lines.append("")
                lines.append("⚠️ Ankr API не ответил. Остальные EVM-балансы могут быть неполными.")
        else:
            lines.append("")
            lines.append("⚠️ Ankr API не настроен. Показан только Ethereum, если доступен RPC.")

    lines.insert(2, f"💼 <b>Общий известный баланс портфеля: {_format_usd(total_usd_portfolio)}</b>")

    if len(lines) <= 3:
        await update.message.reply_text("Балансов не найдено.")
        return

    await _send_long_message(
        context.bot,
        update.effective_chat.id,
        "\n".join(lines).strip(),
        parse_mode="HTML",
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "🔎 Найти историю покупок",
                callback_data="history_evm_menu",
            )
        ]
    ]

    await update.message.reply_text(
        "Анализ балансов завершён. Запустить историю покупок?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_solana_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    address = context.user_data.get("address")

    if not address:
        await update.message.reply_text("❌ Сначала отправьте адрес кошелька.")
        return

    session = _get_global_session(context)

    helius: Optional[HeliusClient] = context.application.bot_data.get("helius")
    cascade: Optional[CascadePriceFetcher] = context.application.bot_data.get("cascade")

    if not helius:
        await update.message.reply_text(
            "⚠️ Helius API ключ не задан. Балансы Solana недоступны."
        )
        return

    if not cascade:
        await update.message.reply_text(
            "⚠️ CascadePriceFetcher не инициализирован."
        )
        return

    try:
        data = await helius.get_wallet_balances(session, address)

        if not data:
            await update.message.reply_text("Solana-балансов не найдено.")
            return

        balances = data.get("balances", [])

        if not balances:
            await update.message.reply_text("Solana-балансов не найдено.")
            return

        lines = [
            f"💰 <b>Баланс Solana</b>",
            f"<code>{address}</code>",
        ]

        no_price_mints = [
            token.get("mint")
            for token in balances
            if token.get("mint")
            and float(token.get("balance") or 0) > 0
            and token.get("usdValue") is None
        ]

        additional_prices = await cascade.get_prices(
            session,
            no_price_mints,
            network="solana",
        ) if no_price_mints else {}

        total_usd = 0.0

        for token in balances:
            balance = float(token.get("balance") or 0)

            if balance <= 0:
                continue

            symbol = str(token.get("symbol") or token.get("name") or "?").strip()
            mint = token.get("mint")

            usd_val = token.get("usdValue")

            if usd_val is None and mint in additional_prices:
                usd_val = additional_prices[mint]

            if usd_val is not None:
                usd_val = float(usd_val)
                total_usd += usd_val

            link = _token_link("solana", mint) if mint else ""

            if link:
                lines.append(
                    f"• <a href='{link}'>{symbol}</a>: {_format_balance(balance)} ({_format_usd(usd_val)})"
                )
            else:
                lines.append(
                    f"• {symbol}: {_format_balance(balance)} ({_format_usd(usd_val)})"
                )

        lines.insert(1, f"Общая известная стоимость: {_format_usd(total_usd)}")

        await _send_long_message(
            context.bot,
            update.effective_chat.id,
            "\n".join(lines).strip(),
            parse_mode="HTML",
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔎 Поиск истории покупок Solana",
                    callback_data="history_solana",
                )
            ]
        ]

        await update.message.reply_text(
            "Запустить поиск связей Solana?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as exc:
        logger.exception("Ошибка получения Solana-баланса")
        await update.message.reply_text(f"❌ Ошибка получения Solana-баланса: {exc}")


async def history_evm_menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                conf["name"],
                callback_data=f"history_{chain_key}",
            )
        ]
        for chain_key, conf in NETWORKS.items()
        if conf.get("chain_id") is not None
    ]

    await query.edit_message_text(
        "Выберите EVM-сеть для анализа истории покупок MM:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data

    if data == "history_evm_menu":
        await history_evm_menu_handler(update, context)
        return

    if data == "history_solana":
        await query.edit_message_text(
            "⏳ Запущен анализ истории покупок Solana. Обхожу связанные адреса..."
        )
        await run_solana_history(query, context)
        return

    if data.startswith("history_"):
        chain = data.replace("history_", "", 1)

        if chain not in NETWORKS or NETWORKS[chain].get("chain_id") is None:
            await query.edit_message_text(f"❌ Сеть {chain} пока не поддерживается для истории.")
            return

        network_name = NETWORKS[chain]["name"]

        await query.edit_message_text(
            f"⏳ Запущен анализ истории покупок в сети {network_name}. Строю граф связей..."
        )

        await run_evm_history(query, context, chain)
        return


async def run_evm_history(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    chain: str,
):
    address = context.user_data.get("address")

    if not address:
        await query.edit_message_text("❌ Сначала отправьте адрес кошелька.")
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    settings = _get_effective_settings(user_id)

    max_depth = settings["max_depth"]
    lookback_days = settings["lookback_days"]
    max_tokens = settings["max_tokens"]
    max_addresses = settings["max_addresses"]

    session = _get_global_session(context)

    try:
        conf = NETWORKS[chain]

        if chain == "ethereum":
            if not ETHERSCAN_API_KEYS:
                await query.edit_message_text(
                    "❌ Для истории Ethereum нужны ETHERSCAN_API_KEYS в .env."
                )
                return

            web3 = EVMWeb3Client(
                conf["rpc_url"],
                conf["chain_id"],
                conf["weth"],
                router=conf.get("dex_routers", [""])[0],
                stable=conf.get("stablecoins", [""])[0],
            )

            explorer = EVMExplorerClient(conf["chain_id"], conf["weth"])
            network = EthereumNetwork(conf, session, explorer, web3)

        elif chain == "bsc":
            web3 = EVMWeb3Client(
                conf["rpc_url"],
                conf["chain_id"],
                conf["weth"],
                router=conf.get("dex_routers", [""])[0],
                stable=conf.get("stablecoins", [""])[0],
            )

            explorer = BscScanExplorerClient(conf["chain_id"], conf["weth"]) if BSCSCAN_API_KEYS else None

            if not explorer:
                logger.warning(
                    "BSCSCAN_API_KEYS не задан. BSC history будет работать через RPC, "
                    "но native BNB transfers могут быть неполными."
                )

            network = BscNetwork(conf, session, web3, explorer)

        else:
            await query.edit_message_text(f"❌ Сеть {chain} пока не поддерживается для истории.")
            return

        traversal = GraphTraversal(
            session=session,
            start_address=address,
            network=network,
            max_tokens=max_tokens,
            lookback_days=lookback_days,
            max_depth=max_depth,
            max_addresses=max_addresses,
            max_branches_per_address=DEFAULT_MAX_BRANCHES_PER_ADDRESS,
            user_id=user_id,
            chat_id=chat_id,
        )

        found = await traversal.run()

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено."
            )
            return

        unique = {
            item["token"]: item
            for item in found
        }

        token_lines = [
            f"• <a href='https://dexscreener.com/{chain}/{addr}'>{data['symbol']}</a> "
            f"(<code>{addr}</code>)"
            for addr, data in unique.items()
        ]

        report = (
            f"✅ <b>История покупок {conf['name']}</b>\n"
            f"Найдено токенов: {len(unique)}\n"
            f"Глубина: {max_depth}\n"
            f"Период: {lookback_days} дней\n"
            f"Адресов проверено/лимит: до {max_addresses}\n\n"
            + "\n".join(token_lines)
        )

        await _send_long_message(
            context.bot,
            chat_id,
            report,
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.exception("Ошибка истории EVM")
        await query.edit_message_text(f"❌ Ошибка во время обхода графа: {exc}")


async def get_token_names_cascade(
    session,
    mints: List[str],
) -> Dict[str, str]:
    names: Dict[str, str] = {}

    if not mints:
        return names

    unique_mints = list(dict.fromkeys(mints))
    remaining = list(unique_mints)

    if BIRDEYE_API_KEY:
        birdeye = BirdeyePrice()

        for mint in list(remaining):
            data = await birdeye.get_token_overview(session, mint)

            name = data.get("name") or data.get("symbol")

            if name:
                names[mint] = name
                remaining.remove(mint)

            await asyncio.sleep(0.2)

    dexscreener = DexScreenerPrice()

    for mint in list(remaining):
        pairs = await dexscreener.get_pairs(session, mint)

        if pairs:
            base_token = pairs[0].get("baseToken", {})
            name = base_token.get("name") or base_token.get("symbol")

            if name:
                names[mint] = name
                remaining.remove(mint)

        await asyncio.sleep(0.2)

    for mint in remaining:
        names[mint] = f"{mint[:6]}...{mint[-4:]}"

    return names


async def run_solana_history(
    query,
    context: ContextTypes.DEFAULT_TYPE,
):
    address = context.user_data.get("address")

    if not address:
        await query.edit_message_text("❌ Сначала отправьте адрес кошелька.")
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id if query.message else user_id

    settings = _get_effective_settings(user_id)

    max_depth = settings["max_depth"]
    lookback_days = settings["lookback_days"]
    max_tokens = settings["max_tokens"]
    max_addresses = settings["max_addresses"]

    helius: Optional[HeliusClient] = context.application.bot_data.get("helius")

    if not helius:
        await query.edit_message_text(
            "⚠️ Helius API ключ не задан. Solana history недоступна."
        )
        return

    session = _get_global_session(context)

    try:
        traversal = SolanaTraversal(
            session=session,
            start_address=address,
            helius=helius,
            max_depth=max_depth,
            max_tokens=max_tokens,
            lookback_days=lookback_days,
            max_addresses=max_addresses,
            max_branches_per_address=DEFAULT_MAX_BRANCHES_PER_ADDRESS,
            user_id=user_id,
            chat_id=chat_id,
        )

        found = await traversal.run()

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено."
            )
            return

        unique_mints = list(
            {
                item["token"]
                for item in found
            }
        )

        names = await get_token_names_cascade(session, unique_mints)

        token_lines = [
            f"• <a href='https://dexscreener.com/solana/{item['token']}'>"
            f"{names.get(item['token'], '?')}</a> "
            f"(<code>{item['token']}</code>)"
            for item in found
        ]

        report = (
            f"✅ <b>История покупок Solana</b>\n"
            f"Найдено токенов: {len(found)}\n"
            f"Глубина: {max_depth}\n"
            f"Период: {lookback_days} дней\n"
            f"Адресов проверено/лимит: до {max_addresses}\n\n"
            + "\n".join(token_lines)
        )

        await _send_long_message(
            context.bot,
            chat_id,
            report,
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.exception("Ошибка истории Solana")
        await query.edit_message_text(f"❌ Ошибка во время обхода Solana: {exc}")


async def _send_long_message(
    bot,
    chat_id: int,
    text: str,
    parse_mode: str = "HTML",
) -> None:
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        await bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return

    chunk = ""

    for line in text.split("\n"):
        if len(line) > TELEGRAM_MAX_MESSAGE_LENGTH:
            if chunk.strip():
                await bot.send_message(
                    chat_id,
                    chunk.strip(),
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                chunk = ""

            for start in range(0, len(line), TELEGRAM_MAX_MESSAGE_LENGTH - 100):
                part = line[start:start + TELEGRAM_MAX_MESSAGE_LENGTH - 100]

                await bot.send_message(
                    chat_id,
                    part,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )

            continue

        if len(chunk) + len(line) + 1 > TELEGRAM_MAX_MESSAGE_LENGTH:
            if chunk.strip():
                await bot.send_message(
                    chat_id,
                    chunk.strip(),
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )

            chunk = line + "\n"
        else:
            chunk += line + "\n"

    if chunk.strip():
        await bot.send_message(
            chat_id,
            chunk.strip(),
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    user_id = update.effective_user.id
    settings_dict = _get_effective_settings(user_id)

    keyboard = [
        [
            InlineKeyboardButton(
                f"Глубина обхода: {settings_dict['max_depth']} (1-{HARD_MAX_DEPTH})",
                callback_data="set_max_depth",
            )
        ],
        [
            InlineKeyboardButton(
                f"Период, дней: {settings_dict['lookback_days']} (1-{HARD_MAX_LOOKBACK_DAYS})",
                callback_data="set_lookback_days",
            )
        ],
        [
            InlineKeyboardButton(
                f"Макс. токенов: {settings_dict['max_tokens']} (1-{HARD_MAX_FOUND_TOKENS})",
                callback_data="set_max_tokens",
            )
        ],
        [
            InlineKeyboardButton(
                f"Макс. адресов: {settings_dict['max_addresses']} (100-{HARD_MAX_ADDRESSES})",
                callback_data="set_max_addresses",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Сбросить на умолчания",
                callback_data="reset_settings",
            )
        ],
    ]

    await update.message.reply_text(
        "⚙️ <b>Индивидуальные настройки поиска ММ:</b>\n"
        "<i>Нажмите на кнопку для изменения.</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def settings_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "reset_settings":
        delete_user_settings(user_id)

        await query.edit_message_text("✅ Настройки сброшены на стандартные.")
        return

    setting_map = {
        "set_max_depth": (
            "max_depth",
            "Введите новую максимальную глубину обхода связанных кошельков, число от 1 до 5:",
        ),
        "set_lookback_days": (
            "lookback_days",
            "Введите период анализа истории в днях, число от 1 до 90:",
        ),
        "set_max_tokens": (
            "max_tokens",
            "Введите максимальное количество токенов, после которого алгоритм остановится, число от 1 до 500:",
        ),
        "set_max_addresses": (
            "max_addresses",
            "Введите максимальное количество адресов для обхода, число от 100 до 2000:",
        ),
    }

    key, prompt = setting_map.get(data, (None, None))

    if key:
        context.user_data["awaiting_setting"] = key
        await query.edit_message_text(prompt)


async def setting_value_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not _check_access(update):
        return

    key = context.user_data.get("awaiting_setting")

    if not key:
        await handle_message(update, context)
        return

    if key not in SETTING_RANGES:
        context.user_data.pop("awaiting_setting", None)
        await update.message.reply_text("❌ Неизвестная настройка.")
        return

    value_text = update.message.text.strip()

    try:
        value = int(value_text)
    except ValueError:
        await update.message.reply_text("❌ Пожалуйста, введите корректное целое число.")
        return

    min_value, max_value = SETTING_RANGES[key]

    if value < min_value or value > max_value:
        await update.message.reply_text(
            f"❌ Значение должно быть от {min_value} до {max_value}."
        )
        return

    set_user_setting(update.effective_user.id, key, str(value))
    context.user_data.pop("awaiting_setting", None)

    await update.message.reply_text(
        f"✅ Настройка <b>{key}</b> успешно обновлена: <code>{value}</code>",
        parse_mode="HTML",
    )

    await settings(update, context)


def register_handlers(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("settings", settings))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            history_evm_menu_handler,
            pattern=r"^history_evm_menu$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^history_",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            settings_button,
            pattern=r"^(set_.*|reset_settings)$",
        )
    )