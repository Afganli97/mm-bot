"""
Telegram handlers.
"""

import asyncio
import logging
import time
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
    EVMExplorerClient,
    EVMPriceCascade,
    EVMWeb3Client,
    HeliusClient,
    MoralisClient,
    TokenInfoService,
    get_evm_token_symbols,
    get_token_meta_cascade,
)
from bot.blacklist import is_blacklisted
from bot.config import (
    ALCHEMY_API_KEY,
    ALLOWED_USER_IDS,
    BALANCE_SPAM_TOTAL_TIMEOUT_SECONDS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES_PER_ADDRESS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FOUND_TOKENS,
    DEFAULT_SOLANA_HISTORY_TIMEOUT_SECONDS,
    ETHERSCAN_API_KEYS,
    HARD_MAX_ADDRESSES,
    HARD_MAX_BRANCHES_PER_ADDRESS,
    HARD_MAX_DEPTH,
    HARD_MAX_FOUND_TOKENS,
    HARD_MAX_LOOKBACK_DAYS,
    HISTORY_SPAM_TOTAL_TIMEOUT_SECONDS,
    MAX_BALANCE_SPAM_CHECKS,
    MAX_HISTORY_SPAM_CHECKS,
    MAX_SOLANA_HISTORY_NAME_LOOKUPS,
    MAX_SOLANA_PRICE_LOOKUPS_PER_BALANCE,
    NETWORKS,
    SOLANA_BALANCE_TIMEOUT_SECONDS,
    SOLANA_HISTORY_NAME_LOOKUP_TIMEOUT_SECONDS,
    SOLANA_PRICE_LOOKUP_TIMEOUT_SECONDS,
    SPAM_CHECK_TIMEOUT_SECONDS,
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
from bot.solana_traversal import SolanaTraversal
from bot.token_reputation import TokenReputationService


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

    return bool(ALLOWED_USER_IDS and user_id in ALLOWED_USER_IDS)


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
    try:
        value = int(settings_dict.get(key))
    except Exception:
        value = default

    return max(min_value, min(max_value, value))


def _get_effective_settings(user_id: int) -> Dict[str, int]:
    raw_settings = get_user_settings_dict(user_id)

    return {
        "max_depth": _get_int_setting(raw_settings, "max_depth", DEFAULT_MAX_DEPTH, 1, HARD_MAX_DEPTH),
        "lookback_days": _get_int_setting(raw_settings, "lookback_days", DEFAULT_LOOKBACK_DAYS, 1, HARD_MAX_LOOKBACK_DAYS),
        "max_tokens": _get_int_setting(raw_settings, "max_tokens", DEFAULT_MAX_FOUND_TOKENS, 1, HARD_MAX_FOUND_TOKENS),
        "max_addresses": _get_int_setting(raw_settings, "max_addresses", DEFAULT_MAX_ADDRESSES, 100, HARD_MAX_ADDRESSES),
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    await update.message.reply_text(
        "👋 Привет! Я бот для анализа кошельков.\n\n"
        "Отправьте EVM или Solana адрес, и я покажу балансы.\n"
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
        "1. Отправьте адрес EVM или Solana.\n"
        "2. Бот агрегирует балансы.\n"
        "3. Нажмите «История покупок», чтобы найти связанные кошельки.\n"
        "4. В /settings можно изменить глубину, период, лимит токенов и адресов.\n\n"
        "<b>Важно:</b> бот использует бесплатные/лимитные API.",
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

        if limit > 0:
            msg += f"<b>{service}</b>: {used}/{limit}, осталось {max(limit - used, 0)}\n"
        else:
            msg += f"<b>{service}</b>: {used}, лимит неизвестен\n"

    await update.message.reply_text(msg, parse_mode="HTML")


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


async def _run_spam_checks(
    session,
    reputation: TokenReputationService,
    items: List[Dict[str, Any]],
    max_items: int,
    total_timeout: int,
) -> Dict[tuple, Dict[str, Any]]:
    unique_items: Dict[tuple, Dict[str, Any]] = {}

    for item in items:
        address = str(item.get("address") or "").lower()
        network = str(item.get("network") or "").lower()

        if not address or not network:
            continue

        key = (network, address)

        if key in unique_items:
            continue

        unique_items[key] = item

    limited_items = list(unique_items.values())[:max_items]
    checked: Dict[tuple, Dict[str, Any]] = {}
    started = time.monotonic()

    for item in limited_items:
        if time.monotonic() - started > total_timeout:
            checked[(item["network"], item["address"])] = {
                "is_spam": False,
                "checked": False,
                "reason": "total_timeout",
            }
            continue

        key = (item["network"], item["address"])

        try:
            result = await asyncio.wait_for(
                reputation.check_token(
                    session,
                    address=item["address"],
                    network=item["network"],
                    symbol=item.get("symbol"),
                    raw_balance=item.get("raw_balance"),
                    decimals=item.get("decimals"),
                    is_native=bool(item.get("is_native")),
                ),
                timeout=SPAM_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Spam check timeout token=%s/%s", item.get("network"), item.get("address"))
            result = {"is_spam": False, "checked": False, "reason": "timeout"}
        except Exception as exc:
            logger.debug("Spam check error token=%s/%s: %s", item.get("network"), item.get("address"), exc)
            result = {"is_spam": False, "checked": False, "reason": "error"}

        checked[key] = result

    return checked


async def _apply_balance_spam_filter(
    session,
    reputation: TokenReputationService,
    token_entries: List[Dict[str, Any]],
) -> Tuple[set, List[str]]:
    checks = await _run_spam_checks(
        session,
        reputation,
        token_entries,
        MAX_BALANCE_SPAM_CHECKS,
        BALANCE_SPAM_TOTAL_TIMEOUT_SECONDS,
    )

    hidden_keys = {
        key
        for key, result in checks.items()
        if result.get("is_spam")
    }

    unchecked_count = sum(
        1
        for result in checks.values()
        if not result.get("checked")
    )

    hidden_count = len(hidden_keys)
    notes = []

    if hidden_count:
        notes.append(f"Скрыто подозрительных токенов: {hidden_count}")

    if unchecked_count:
        notes.append(f"Не проверено сервисами из-за timeout/ошибки: {unchecked_count}")

    return hidden_keys, notes


async def _apply_history_spam_filter(
    session,
    reputation: TokenReputationService,
    token_entries: List[Dict[str, Any]],
) -> Tuple[set, List[str]]:
    checks = await _run_spam_checks(
        session,
        reputation,
        token_entries,
        MAX_HISTORY_SPAM_CHECKS,
        HISTORY_SPAM_TOTAL_TIMEOUT_SECONDS,
    )

    hidden_keys = {
        key
        for key, result in checks.items()
        if result.get("is_spam")
    }

    unchecked_count = sum(
        1
        for result in checks.values()
        if not result.get("checked")
    )

    hidden_count = len(hidden_keys)
    notes = []

    if hidden_count:
        notes.append(f"Скрыто подозрительных токенов: {hidden_count}")

    if unchecked_count:
        notes.append(f"Не проверено сервисами из-за timeout/ошибки: {unchecked_count}")

    return hidden_keys, notes


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

    reputation = TokenReputationService()

    lines = [
        f"💰 <b>Мультичейн EVM Балансы</b>",
        f"<code>{address}</code>",
    ]

    total_usd_portfolio = 0.0
    spam_check_items: List[Dict[str, Any]] = []

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
                native_price = await evm_cascade.get_price(session, eth_conf["weth"], "ethereum") or 0.0

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
                    decimals = int(token.get("tokenDecimal") or token.get("decimals") or 18)
                    raw_balance = int(token.get("balance") or 0)

                    if raw_balance <= 0 and balance > 0 and decimals >= 0:
                        raw_balance = int(balance * (10**decimals))

                    usd_value = float(token.get("usd_value") or 0)
                    symbol = str(token.get("symbol") or "?").strip()

                    all_eth_tokens[contract] = {
                        "symbol": symbol,
                        "balance": balance,
                        "usd_val": usd_value,
                        "raw_balance": raw_balance,
                        "decimals": decimals,
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

                    if not contract or contract == "0x0000000000000000000000000000000000000000":
                        continue

                    if contract in all_eth_tokens:
                        continue

                    raw_balance = int(token.get("tokenBalance", "0x0"), 16)

                    if raw_balance <= 0:
                        continue

                    decimals = await _get_decimals(session, contract, eth_conf["rpc_url"])
                    decimals = max(0, decimals)
                    symbol = await TokenInfoService.get_symbol(session, contract, eth_conf["rpc_url"])

                    all_eth_tokens[contract] = {
                        "symbol": symbol,
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
            if data.get("balance", 0) <= 0:
                all_eth_tokens.pop(contract, None)
                continue

            if data.get("usd_val", 0) <= 0 and not data.get("is_native"):
                meta = await get_token_meta_cascade(
                    session,
                    contract,
                    "ethereum",
                    eth_conf["rpc_url"],
                )

                price = meta.get("price_usd")

                if price is None:
                    price = await evm_cascade.get_price(session, contract, "ethereum")

                if price:
                    data["usd_val"] = float(data["balance"]) * float(price)

                if meta.get("symbol") and meta.get("symbol") != "?":
                    data["symbol"] = meta["symbol"]

            spam_check_items.append(
                {
                    "network": "ethereum",
                    "address": contract,
                    "symbol": data.get("symbol"),
                    "raw_balance": data.get("raw_balance"),
                    "decimals": data.get("decimals"),
                    "is_native": data.get("is_native", False),
                }
            )

        hidden_keys, spam_notes = await _apply_balance_spam_filter(
            session,
            reputation,
            spam_check_items,
        )

        eth_sorted = sorted(
            [
                (key, value)
                for key, value in all_eth_tokens.items()
                if (
                    value.get("balance", 0) > 0
                    and ("ethereum", key) not in hidden_keys
                )
            ],
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

    other_chains = [
        chain_key
        for chain_key, conf in NETWORKS.items()
        if conf.get("chain_id") is not None and chain_key != "ethereum"
    ]

    if other_chains and ankr:
        try:
            ankr_data = await ankr.get_multichain_balances(session, address, chains=other_chains)

            for chain in other_chains:
                chain_conf = NETWORKS[chain]
                chain_assets = []

                for asset in ankr_data.get("assets", []) if ankr_data else []:
                    if asset.get("blockchain") == chain:
                        chain_assets.append(asset)

                if not chain_assets:
                    continue

                chain_total = 0.0
                chain_lines = []

                for asset in chain_assets:
                    balance = float(asset.get("balance") or 0)

                    if balance <= 0:
                        continue

                    usd_val = float(asset.get("balanceUsd") or 0)
                    sym = str(asset.get("tokenSymbol") or "?").strip()
                    contract = asset.get("contractAddress") or ""
                    decimals = int(asset.get("tokenDecimals") or asset.get("decimals") or 18)
                    raw_balance = int(asset.get("balanceRaw") or asset.get("rawBalance") or 0)

                    if raw_balance <= 0 and balance > 0 and decimals >= 0:
                        raw_balance = int(balance * (10**decimals))

                    if contract and contract.lower() != "0x0000000000000000000000000000000000000000":
                        spam_check_items.append(
                            {
                                "network": chain,
                                "address": contract,
                                "symbol": sym,
                                "raw_balance": raw_balance,
                                "decimals": decimals,
                                "is_native": False,
                            }
                        )

                        chain_total += usd_val
                        chain_lines.append(
                            _token_line(
                                chain,
                                sym,
                                contract,
                                balance,
                                usd_val,
                            )
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

    if spam_notes:
        lines.append("")
        lines.append("<b>Спам-фильтр:</b>")

        for note in spam_notes:
            lines.append(f"• {note}")

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


async def show_solana_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data.get("address")

    if not address:
        await update.message.reply_text("❌ Сначала отправьте адрес кошелька.")
        return

    session = _get_global_session(context)

    helius: Optional[HeliusClient] = context.application.bot_data.get("helius")
    cascade = context.application.bot_data.get("cascade")

    if not helius:
        await update.message.reply_text(
            "⚠️ Helius API ключ не задан. Балансы Solana недоступны."
        )
        return

    native_sol_mint = getattr(
        HeliusClient,
        "NATIVE_SOL_MINT",
        "So11111111111111111111111111111111111111111",
    )

    async def fetch_helius_rest_balances() -> List[Dict[str, Any]]:
        if not helius.api_key:
            return []

        url = f"{HeliusClient.BASE_URL}/wallet/{address}/balances?api-key={helius.api_key}"

        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("balances", []) or []
        except Exception as exc:
            logger.debug("Helius REST balance fallback error: %s", exc)

        return []

    balances: List[Dict[str, Any]] = []

    try:
        data = await asyncio.wait_for(
            helius.get_wallet_balances(session, address),
            timeout=SOLANA_BALANCE_TIMEOUT_SECONDS,
        )

        if isinstance(data, dict):
            balances = data.get("balances", []) or []

        non_native = [
            token
            for token in balances
            if token.get("mint") and token.get("mint") != native_sol_mint
        ]

        if not non_native:
            rest_balances = await fetch_helius_rest_balances()

            if rest_balances:
                logger.info("Helius RPC дал только native SOL, используем REST fallback. address=%s", address)
                balances = rest_balances

    except asyncio.TimeoutError:
        logger.warning("Helius RPC balance timeout. Пробуем REST fallback. address=%s", address)
        balances = await fetch_helius_rest_balances()

        if not balances:
            await update.message.reply_text(
                f"⚠️ Helius не ответил за {SOLANA_BALANCE_TIMEOUT_SECONDS} сек. Попробуйте позже."
            )
            return

    except Exception as exc:
        logger.exception("Ошибка получения Solana-баланса")
        await update.message.reply_text(f"❌ Ошибка получения Solana-баланса: {exc}")
        return

    if not balances:
        await update.message.reply_text("Solana-балансов не найдено.")
        return

    lines = [
        f"💰 <b>Баланс Solana</b>",
        f"<code>{address}</code>",
    ]

    additional_prices: Dict[str, float] = {}

    if cascade and MAX_SOLANA_PRICE_LOOKUPS_PER_BALANCE > 0:
        no_price_mints = [
            token.get("mint")
            for token in balances
            if token.get("mint")
            and float(token.get("balance") or token.get("uiAmount") or 0) > 0
            and token.get("usdValue") is None
        ][:MAX_SOLANA_PRICE_LOOKUPS_PER_BALANCE]

        if no_price_mints:
            try:
                additional_prices = await asyncio.wait_for(
                    cascade.get_prices(
                        session,
                        no_price_mints,
                        network="solana",
                    ),
                    timeout=SOLANA_PRICE_LOOKUP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Solana price lookup timeout. mints=%s", len(no_price_mints))
                additional_prices = {}

    total_usd = 0.0
    spam_check_items = []

    for token in balances:
        balance = float(token.get("balance") or token.get("uiAmount") or 0)

        if balance <= 0:
            continue

        mint = token.get("mint") or token.get("tokenAddress") or ""
        symbol = str(token.get("symbol") or token.get("name") or "?").strip() or "?"

        raw_amount = 0

        try:
            raw_amount = int(token.get("rawAmount") or token.get("raw_amount") or 0)
        except Exception:
            raw_amount = 0

        decimals = int(token.get("decimals") or 0)

        if raw_amount <= 0 and balance > 0 and decimals >= 0:
            raw_amount = int(balance * (10**decimals))

        usd_val = token.get("usdValue")

        if usd_val is None and mint in additional_prices:
            usd_val = balance * additional_prices[mint]

        if usd_val is not None:
            try:
                usd_val = float(usd_val)
                total_usd += usd_val
            except Exception:
                usd_val = None

        is_native_sol = mint == native_sol_mint

        if not is_native_sol:
            spam_check_items.append(
                {
                    "network": "solana",
                    "address": mint,
                    "symbol": symbol,
                    "raw_balance": raw_amount,
                    "decimals": decimals,
                    "is_native": False,
                }
            )

        display = _format_usd(usd_val)
        link = _token_link("solana", mint) if mint else ""

        if link:
            lines.append(f"• <a href='{link}'>{symbol}</a>: {_format_balance(balance)} ({display})")
        else:
            lines.append(f"• {symbol}: {_format_balance(balance)} ({display})")

    reputation = TokenReputationService()
    hidden_keys, spam_notes = await _apply_balance_spam_filter(
        session,
        reputation,
        spam_check_items,
    )

    if spam_notes:
        lines.append("")
        lines.append("<b>Спам-фильтр:</b>")

        for note in spam_notes:
            lines.append(f"• {note}")

    lines.insert(1, f"Общая известная стоимость: {_format_usd(total_usd)}")

    if len(lines) <= 2:
        await update.message.reply_text("Положительных Solana-балансов не найдено.")
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
                "🔎 Поиск истории покупок Solana",
                callback_data="history_solana",
            )
        ]
    ]

    await update.message.reply_text(
        "Запустить поиск связей Solana?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_evm_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                await query.edit_message_text("❌ Для истории Ethereum нужны ETHERSCAN_API_KEYS в .env.")
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

            network = BscNetwork(conf, session, web3, None)

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

        try:
            found = await asyncio.wait_for(
                traversal.run(),
                timeout=600,
            )
        except asyncio.TimeoutError:
            await query.edit_message_text(
                f"⏱ История покупок {conf['name']} превысила лимит времени. "
                "Уменьшите глубину, период или максимальное количество адресов."
            )
            return

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено."
            )
            return

        reputation = TokenReputationService()

        spam_entries = [
            {
                "network": chain,
                "address": item["token"],
                "symbol": item.get("symbol", "?"),
                "raw_balance": None,
                "decimals": None,
                "is_native": False,
            }
            for item in found
        ]

        hidden_keys, spam_notes = await _apply_history_spam_filter(
            session,
            reputation,
            spam_entries,
        )

        filtered_found = [
            item
            for item in found
            if (chain, str(item["token"]).lower()) not in hidden_keys
        ]

        if not filtered_found:
            await query.edit_message_text(
                "✅ Анализ завершён. Найдено только подозрительные/неподтверждённые токены."
            )
            return

        unique_items = {
            item["token"].lower(): item
            for item in filtered_found
        }

        metas = await get_evm_token_symbols(
            session,
            list(unique_items.keys()),
            chain,
            conf["rpc_url"],
        )

        for item in filtered_found:
            meta = metas.get(item["token"].lower(), {})
            item["symbol"] = (
                meta.get("symbol")
                if isinstance(meta, dict) and meta.get("symbol")
                else item.get("symbol")
                or "?"
            )

        unique = {
            item["token"]: item
            for item in filtered_found
        }

        token_lines = [
            f"• <a href='https://dexscreener.com/{chain}/{addr}'>{data.get('symbol') or '?'}</a> "
            f"(<code>{addr}</code>)"
            for addr, data in unique.items()
        ]

        report = (
            f"✅ <b>История покупок {conf['name']}</b>\n"
            f"Найдено токенов: {len(unique)}\n"
            f"Глубина: {max_depth}\n"
            f"Период: {lookback_days} дней\n"
            f"Адресов проверено/лимит: до {max_addresses}\n"
        )

        if spam_notes:
            report += "\n<b>Спам-фильтр:</b>\n"

            for note in spam_notes:
                report += f"• {note}\n"

        report += "\n" + "\n".join(token_lines)

        await _send_long_message(
            context.bot,
            chat_id,
            report,
            parse_mode="HTML",
        )

    except Exception as exc:
        logger.exception("Ошибка истории EVM")
        await query.edit_message_text(f"❌ Ошибка во время обхода графа: {exc}")


async def get_token_names_cascade(session, mints: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}

    mints = list(dict.fromkeys(mints or []))[:MAX_SOLANA_HISTORY_NAME_LOOKUPS]

    if not mints:
        return names

    remaining = list(mints)

    for mint in list(remaining):
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=5,
            ) as resp:
                if resp.status == 200:
                    pairs = (await resp.json()).get("pairs")

                    if pairs:
                        base_token = pairs[0].get("baseToken", {})
                        name = base_token.get("name") or base_token.get("symbol")

                        if name:
                            names[mint] = name

                            if mint in remaining:
                                remaining.remove(mint)
        except Exception as exc:
            logger.debug("DexScreener name lookup error for %s: %s", mint, exc)

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

        try:
            found = await asyncio.wait_for(
                traversal.run(),
                timeout=DEFAULT_SOLANA_HISTORY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await query.edit_message_text(
                f"⏱ Solana history превысила лимит времени "
                f"({DEFAULT_SOLANA_HISTORY_TIMEOUT_SECONDS} сек). "
                "Уменьшите глубину, период или максимальное количество адресов."
            )
            return

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Ранних покупок токенов по связям ММ не найдено."
            )
            return

        reputation = TokenReputationService()

        spam_entries = [
            {
                "network": "solana",
                "address": item["token"],
                "symbol": item.get("symbol", "?"),
                "raw_balance": None,
                "decimals": None,
                "is_native": False,
            }
            for item in found
        ]

        hidden_keys, spam_notes = await _apply_history_spam_filter(
            session,
            reputation,
            spam_entries,
        )

        filtered_found = [
            item
            for item in found
            if ("solana", str(item["token"]).lower()) not in hidden_keys
        ]

        if not filtered_found:
            await query.edit_message_text(
                "✅ Анализ завершён. Найдено только подозрительные/неподтверждённые токены."
            )
            return

        unique_mints = list(
            {
                item["token"]
                for item in filtered_found
            }
        )

        try:
            names = await asyncio.wait_for(
                get_token_names_cascade(session, unique_mints),
                timeout=SOLANA_HISTORY_NAME_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Solana token name lookup timeout. Выводим адреса без имён.")
            names = {}

        token_lines = [
            f"• <a href='https://dexscreener.com/solana/{item['token']}'>"
            f"{names.get(item['token'], '?')}</a> "
            f"(<code>{item['token']}</code>)"
            for item in filtered_found
        ]

        report = (
            f"✅ <b>История покупок Solana</b>\n"
            f"Найдено токенов: {len(filtered_found)}\n"
            f"Глубина: {max_depth}\n"
            f"Период: {lookback_days} дней\n"
            f"Адресов проверено/лимит: до {max_addresses}\n"
        )

        if spam_notes:
            report += "\n<b>Спам-фильтр:</b>\n"

            for note in spam_notes:
                report += f"• {note}\n"

        report += "\n" + "\n".join(token_lines)

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


async def settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def setting_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(f"❌ Значение должно быть от {min_value} до {max_value}.")
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