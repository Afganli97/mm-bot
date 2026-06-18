"""
Telegram handlers.
"""
import logging
from typing import Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.api_clients import (
    AlchemyClient,
    AnkrClient,
    BscScanClient,
    EVMExplorerClient,
    EVMWeb3Client,
    HeliusClient,
    MoralisClient,
)
from bot.blacklist import is_blacklisted
from bot.config import (
    ALLOWED_USER_IDS,
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FOUND_TOKENS,
    DEFAULT_LOOKBACK_DAYS,
    EVM_NETWORKS,
    MIN_USD_VALUE,
    NETWORKS,
    SETTING_CAPS,
)
from bot.database import (
    get_user_settings_dict,
    reset_api_usage,
    reset_user_settings,
    set_user_setting,
)
from bot.graph_traversal import GraphTraversal
from bot.networks.bsc import BscNetwork
from bot.networks.ethereum import EthereumNetwork
from bot.rate_limits import RateLimitTracker
from bot.services.price_service import PriceService
from bot.services.spam_filter import SpamFilterService
from bot.services.token_metadata import TokenMetadataService
from bot.solana_traversal import SolanaTraversal
from bot.utils.address import detect_address_type
from bot.utils.telegram import send_long_message

logger = logging.getLogger(__name__)


def _get_global_session(context: ContextTypes.DEFAULT_TYPE):
    session = context.application.bot_data.get("session")

    if session is None:
        from aiohttp import ClientSession

        session = ClientSession()
        context.application.bot_data["session"] = session

    return session


def _check_access(update: Update) -> bool:
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return False

    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    await update.message.reply_text(
        "👋 Привет! Я приватный бот для анализа кошельков.\n\n"
        "Отправь EVM или Solana адрес.\n\n"
        "/help - инструкция\n"
        "/dashboard - лимиты API\n"
        "/settings - настройки поиска"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    await update.message.reply_text(
        "🔍 Как пользоваться:\n"
        "1. Отправь EVM или Solana адрес.\n"
        "2. Бот покажет баланс во всех доступных сетях.\n"
        "3. Нажми кнопку истории покупок.\n\n"
        "/settings - изменить глубину, период, лимиты поиска."
    )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    rows = RateLimitTracker.get_dashboard_rows()

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()

    lines = [f"📊 API лимиты на сегодня ({today} UTC):"]

    for row in rows:
        lines.append(
            f"{row['service']}#{row['key_index']}: "
            f"{row['used']} / {row['limit']} "
            f"(осталось {row['remaining']})"
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Сбросить локальные счётчики", callback_data="reset_api_usage")]
    ]

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def dashboard_reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _check_access(update):
        await query.answer("Нет доступа", show_alert=True)
        return

    reset_api_usage()

    await query.edit_message_text("✅ Локальные счётчики API сброшены.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    if context.user_data.get("awaiting_setting"):
        await setting_value_input(update, context)
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    address_type = detect_address_type(text)

    if address_type == "solana":
        context.user_data["address"] = text
        context.user_data["address_type"] = "solana"

        await update.message.reply_text("⏳ Собираю баланс Solana...")
        await show_solana_balance(update, context)

    elif address_type == "evm":
        context.user_data["address"] = text
        context.user_data["address_type"] = "evm"

        await update.message.reply_text("⏳ Собираю мультичейн EVM балансы...")
        await show_multichain_evm_balances(update, context)

    else:
        await update.message.reply_text(
            "❌ Формат адреса не распознан. Отправь валидный EVM или Solana адрес."
        )


async def show_multichain_evm_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data["address"]
    session = _get_global_session(context)

    ankr: AnkrClient = context.application.bot_data.get("ankr")
    moralis: MoralisClient = context.application.bot_data.get("moralis")
    alchemy: AlchemyClient = context.application.bot_data.get("alchemy")

    lines = [
        "💰 Мультичейн EVM Балансы",
        f"Адрес: `{address}`",
    ]

    total_usd_portfolio = 0.0

    for chain, conf in EVM_NETWORKS.items():
        web3 = EVMWeb3Client(
            conf["rpc_url"],
            conf["chain_id"],
            conf["weth"],
            router=(conf.get("dex_routers") or [""])[0],
            stable=(conf.get("stablecoins") or [""])[0],
        )

        metadata_service = TokenMetadataService(session)
        spam_filter = SpamFilterService(session)
        price_service = PriceService(session)

        chain_lines: List[str] = []
        chain_total = 0.0

        # Native balance
        try:
            native_raw = await web3.get_balance_raw(session, address)

            if native_raw > 0:
                native_balance = native_raw / (10 ** conf["native_decimals"])
                native_price = await price_service.get_price(
                    chain,
                    conf["weth"],
                    web3=web3,
                    weth_price_usd=0.0,
                )

                native_usd = native_balance * native_price if native_price else 0.0

                if native_usd >= MIN_USD_VALUE or native_raw > 0:
                    chain_total += native_usd
                    display = f"≈ ${native_usd:,.2f}" if native_usd > 0 else "?"
                    chain_lines.append(
                        f"• {conf['native_symbol']}: {native_balance:.8f} ({display})"
                    )

        except Exception as e:
            logger.debug("Native balance error %s: %s", chain, e)

        tokens: Dict[str, Dict] = {}

        # Moralis balances
        try:
            if moralis:
                moralis_tokens = await moralis.get_balances(
                    session,
                    address,
                    chain=conf.get("moralis_chain", chain),
                )

                for t in moralis_tokens:
                    contract = (t.get("contract_address") or "").lower()

                    if not contract:
                        continue

                    if contract == "0x0000000000000000000000000000000000000000":
                        continue

                    balance_raw = t.get("balance")
                    balance_formatted = t.get("balance_formatted")

                    try:
                        raw = int(balance_raw)
                    except Exception:
                        raw = None

                    decimals = int(t.get("decimals") or 18)

                    try:
                        balance = float(balance_formatted) if balance_formatted not in (None, "") else None
                    except Exception:
                        balance = None

                    tokens[contract] = {
                        "raw": raw,
                        "balance": balance,
                        "symbol": t.get("symbol") or "?",
                        "name": t.get("name") or "?",
                        "decimals": decimals,
                        "usd_val": float(t.get("usd_value") or 0),
                    }

        except Exception as e:
            logger.debug("Moralis balances error %s: %s", chain, e)

        # Alchemy balances for Ethereum
        try:
            if alchemy and chain == "ethereum":
                alchemy_tokens = await alchemy.get_token_balances(session, address)

                for t in alchemy_tokens:
                    contract = (t.get("contractAddress") or "").lower()

                    if not contract:
                        continue

                    if contract in tokens:
                        continue

                    raw = int(t.get("tokenBalance", "0x0"), 16)
                    decimals = 18
                    balance = raw / (10 ** decimals)

                    tokens[contract] = {
                        "raw": raw,
                        "balance": balance,
                        "symbol": "?",
                        "name": "?",
                        "decimals": decimals,
                        "usd_val": 0.0,
                    }

        except Exception as e:
            logger.debug("Alchemy balances error: %s", e)

        # Ankr balances
        try:
            if ankr and ankr.api_url:
                ankr_data = await ankr.get_multichain_balances(
                    session,
                    address,
                    chains=[conf.get("ankr_chain", chain)],
                )

                for asset in (ankr_data.get("assets", []) if ankr_data else []):
                    contract = (asset.get("contractAddress") or "").lower()

                    if not contract:
                        continue

                    if contract == "0x0000000000000000000000000000000000000000":
                        continue

                    if contract in tokens:
                        continue

                    raw_value = asset.get("balanceRaw") or asset.get("raw")

                    try:
                        raw = int(raw_value)
                    except Exception:
                        raw = None

                    decimals = int(asset.get("decimals") or 18)

                    try:
                        balance = float(asset.get("balance") or 0)
                    except Exception:
                        balance = None

                    tokens[contract] = {
                        "raw": raw,
                        "balance": balance,
                        "symbol": asset.get("tokenSymbol") or "?",
                        "name": asset.get("tokenName") or "?",
                        "decimals": decimals,
                        "usd_val": float(asset.get("balanceUsd") or 0),
                    }

        except Exception as e:
            logger.debug("Ankr balances error %s: %s", chain, e)

        # Process all tokens
        for contract, token_data in list(tokens.items()):
            try:
                metadata = await metadata_service.get_evm_metadata(
                    chain,
                    contract,
                    conf["rpc_url"],
                )

                decimals = int(metadata.get("decimals") or token_data.get("decimals") or 18)

                raw = token_data.get("raw")

                if raw is None and token_data.get("balance") is not None:
                    raw = int(float(token_data["balance"]) * (10 ** decimals))

                spam = await spam_filter.is_spam(
                    network=chain,
                    token_address=contract,
                    symbol=metadata.get("symbol") or token_data.get("symbol") or "?",
                    decimals=decimals,
                    raw_balance=raw,
                    is_native=False,
                )

                if spam.get("is_spam"):
                    continue

                if spam.get("exclude_by_one_unit"):
                    continue

                balance = token_data.get("balance")

                if balance is None and raw is not None:
                    balance = raw / (10 ** decimals)

                usd_total = float(token_data.get("usd_val") or 0)

                if usd_total > 0 and balance and balance > 0:
                    price_usd = usd_total / balance
                else:
                    price_usd = await price_service.get_price(
                        chain,
                        contract,
                        web3=web3,
                        weth_price_usd=0.0,
                    )

                token_usd = None

                if price_usd and balance is not None:
                    token_usd = float(balance) * float(price_usd)
                    chain_total += token_usd

                symbol = metadata.get("symbol") or token_data.get("symbol") or "?"
                name = metadata.get("name") or token_data.get("name") or "?"

                display = f"≈ ${token_usd:,.2f}" if token_usd is not None and token_usd > 0 else "?"

                chain_lines.append(
                    f"• {symbol} ({name}): {balance:.8f} ({display})"
                )

            except Exception as e:
                logger.debug("Token balance processing error %s %s: %s", chain, contract, e)

        total_usd_portfolio += chain_total

        lines.append("")
        lines.append(f"⛓️ {conf['name']}")
        lines.append(f"Общая стоимость: ≈ ${chain_total:,.2f}")
        lines.extend(chain_lines)

    lines.insert(2, f"💼 Общий баланс портфеля: ≈ ${total_usd_portfolio:,.2f}")

    await send_long_message(
        context.bot,
        update.effective_chat.id,
        "\n".join(lines),
        parse_mode=None,
    )

    keyboard = [
        [InlineKeyboardButton("🔎 Найти историю покупок", callback_data="history_evm_menu")]
    ]

    await update.message.reply_text(
        "Анализ баланса завершён. Запустить историю покупок?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_solana_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data["address"]
    session = _get_global_session(context)
    helius: HeliusClient = context.application.bot_data.get("helius")

    if not helius:
        await update.message.reply_text(
            "❌ Helius API не настроен, не могу получить баланс Solana."
        )
        return

    metadata_service = TokenMetadataService(session, helius=helius)
    spam_filter = SpamFilterService(session)
    price_service = PriceService(session)

    lines = [
        "💰 Баланс Solana",
        f"Адрес: `{address}`",
    ]

    total_usd = 0.0

    try:
        data = await helius.get_wallet_balances(session, address)

        native_balance = data.get("nativeBalance") or data.get("solBalance") or {}

        if isinstance(native_balance, dict):
            lamports = int(native_balance.get("lamports") or 0)
            sol_amount = lamports / (10 ** 9)

            if sol_amount > 0:
                sol_price = await price_service.get_price(
                    "solana",
                    "So11111111111111111111111111111111111111111",
                )

                sol_usd = sol_amount * sol_price if sol_price else 0.0
                total_usd += sol_usd

                display = f"≈ ${sol_usd:,.2f}" if sol_usd > 0 else "?"

                lines.append(f"• SOL: {sol_amount:.9f} ({display})")

        tokens = data.get("tokens") or data.get("balances") or []

        for tok in tokens:
            try:
                mint = tok.get("mint")

                if not mint:
                    continue

                raw = int(tok.get("amount") or 0)
                decimals = int(tok.get("decimals") or 0)
                balance = float(tok.get("uiAmount") or 0)

                if balance <= 0:
                    continue

                metadata = await metadata_service.get_solana_metadata(mint, hint=tok)

                spam = await spam_filter.is_spam(
                    network="solana",
                    token_address=mint,
                    symbol=metadata.get("symbol") or "?",
                    decimals=decimals,
                    raw_balance=raw,
                    is_native=False,
                )

                if spam.get("is_spam"):
                    continue

                if spam.get("exclude_by_one_unit"):
                    continue

                price = await price_service.get_price("solana", mint)
                usd_val = balance * price if price else None

                if usd_val is not None:
                    total_usd += usd_val
                    display = f"≈ ${usd_val:,.2f}"
                else:
                    display = "?"

                symbol = metadata.get("symbol") or tok.get("symbol") or "?"
                name = metadata.get("name") or tok.get("name") or "?"

                lines.append(f"• {symbol} ({name}): {balance:.8f} ({display})")

            except Exception as e:
                logger.debug("Solana token balance error: %s", e)

    except Exception as e:
        logger.exception("Solana balance error")

        await update.message.reply_text(
            f"❌ Ошибка получения Solana баланса: {e}"
        )

        return

    lines.insert(2, f"💼 Общий баланс портфеля: ≈ ${total_usd:,.2f}")

    await send_long_message(
        context.bot,
        update.effective_chat.id,
        "\n".join(lines),
        parse_mode=None,
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
        "Баланс Solana получен. Запустить поиск истории покупок?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_evm_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _check_access(update):
        await query.answer("Нет доступа", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton(conf["name"], callback_data=f"history_{chain_key}")]
        for chain_key, conf in EVM_NETWORKS.items()
    ]

    await query.edit_message_text(
        "Выберите EVM сеть для анализа истории покупок:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _check_access(update):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data

    if data == "history_solana":
        await query.edit_message_text("⏳ Запущен анализ истории покупок Solana...")
        await run_solana_history(query, context)

    elif data.startswith("history_"):
        chain = data.split("_", 1)[1]
        network_name = NETWORKS.get(chain, {}).get("name", chain.upper())

        await query.edit_message_text(
            f"⏳ Запущен анализ истории покупок {network_name}..."
        )

        await run_evm_history(query, context, chain)


async def run_evm_history(query, context, chain: str):
    address = context.user_data.get("address")

    if not address:
        await query.edit_message_text("❌ Сначала отправь адрес кошелька.")
        return

    if chain not in NETWORKS:
        await query.edit_message_text(f"❌ Сеть {chain} не найдена в конфиге.")
        return

    user_id = query.from_user.id

    settings_dict = get_user_settings_dict(user_id)

    max_depth = int(settings_dict.get("max_depth", DEFAULT_MAX_DEPTH))
    lookback_days = int(settings_dict.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    max_tokens = int(settings_dict.get("max_tokens", DEFAULT_MAX_FOUND_TOKENS))
    max_addresses = int(settings_dict.get("max_addresses", DEFAULT_MAX_ADDRESSES))
    max_branches = int(settings_dict.get("max_branches", DEFAULT_MAX_BRANCHES))

    try:
        session = _get_global_session(context)
        conf = NETWORKS[chain]

        web3 = EVMWeb3Client(
            conf["rpc_url"],
            conf["chain_id"],
            conf["weth"],
            router=(conf.get("dex_routers") or [""])[0],
            stable=(conf.get("stablecoins") or [""])[0],
        )

        metadata_service = TokenMetadataService(session)
        spam_filter = SpamFilterService(session)
        price_service = PriceService(session)

        if chain == "ethereum":
            explorer = EVMExplorerClient(conf["chain_id"])
            network = EthereumNetwork(conf, session, explorer, web3)

        elif chain == "bsc":
            bscscan = context.application.bot_data.get("bscscan")
            network = BscNetwork(conf, session, web3, bscscan=bscscan)

        else:
            await query.edit_message_text(f"❌ Сеть {chain} пока не поддерживается для истории.")
            return

        traversal = GraphTraversal(
            session=session,
            start_address=address,
            network=network,
            metadata_service=metadata_service,
            spam_filter=spam_filter,
            price_service=price_service,
            user_id=user_id,
            chat_id=query.message.chat_id,
            max_tokens=max_tokens,
            lookback_days=lookback_days,
            max_depth=max_depth,
            max_addresses=max_addresses,
            max_branches=max_branches,
        )

        found = await traversal.run()

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Покупок токенов по связанным адресам не найдено."
            )
            return

        grouped = {}

        for item in found:
            token = item["token"]

            grouped.setdefault(
                token,
                {
                    "symbol": item.get("symbol") or "?",
                    "name": item.get("name") or "?",
                    "buyers": set(),
                    "txs": [],
                    "amounts": [],
                    "price_usd": item.get("price_usd"),
                },
            )

            grouped[token]["buyers"].add(item.get("buyer"))
            grouped[token]["txs"].append(item.get("tx"))
            grouped[token]["amounts"].append(item.get("amount"))

        lines = [
            f"✅ История покупок {conf['name']}",
            f"Найдено токенов: {len(grouped)}",
            f"Глубина: {max_depth}",
            f"Период: {lookback_days} дней",
            "",
        ]

        for token, data in grouped.items():
            buyers = ", ".join(list(data["buyers"])[:5])
            tx = data["txs"][-1]
            amount = data["amounts"][-1]
            price = data["price_usd"]

            price_text = f" ≈ ${price:,.6f}" if price else ""

            lines.append(f"• {data['symbol']} ({data['name']})")
            lines.append(f"  Token: `{token}`")
            lines.append(f"  Покупатель: `{buyers}`")
            lines.append(f"  Кол-во: {amount}{price_text}")
            lines.append(f"  Tx: `{tx}`")
            lines.append("")

        await send_long_message(
            context.bot,
            query.message.chat_id,
            "\n".join(lines),
            parse_mode=None,
        )

    except Exception as e:
        logger.exception("EVM history error")

        await query.edit_message_text(
            f"❌ Ошибка во время анализа истории EVM: {e}"
        )


async def run_solana_history(query, context):
    address = context.user_data.get("address")

    if not address:
        await query.edit_message_text("❌ Сначала отправь адрес кошелька.")
        return

    user_id = query.from_user.id

    settings_dict = get_user_settings_dict(user_id)

    max_depth = int(settings_dict.get("max_depth", DEFAULT_MAX_DEPTH))
    lookback_days = int(settings_dict.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    max_tokens = int(settings_dict.get("max_tokens", DEFAULT_MAX_FOUND_TOKENS))
    max_addresses = int(settings_dict.get("max_addresses", DEFAULT_MAX_ADDRESSES))
    max_branches = int(settings_dict.get("max_branches", DEFAULT_MAX_BRANCHES))

    helius: HeliusClient = context.application.bot_data.get("helius")

    if not helius:
        await query.edit_message_text("❌ Helius API не настроен.")
        return

    try:
        session = _get_global_session(context)

        metadata_service = TokenMetadataService(session, helius=helius)
        spam_filter = SpamFilterService(session)
        price_service = PriceService(session)

        traversal = SolanaTraversal(
            session=session,
            start_address=address,
            helius=helius,
            metadata_service=metadata_service,
            spam_filter=spam_filter,
            price_service=price_service,
            user_id=user_id,
            chat_id=query.message.chat_id,
            max_depth=max_depth,
            max_tokens=max_tokens,
            lookback_days=lookback_days,
            max_addresses=max_addresses,
            max_branches=max_branches,
        )

        found = await traversal.run()

        if not found:
            await query.edit_message_text(
                "✅ Анализ завершён. Покупок токенов по связанным Solana-адресам не найдено."
            )
            return

        grouped = {}

        for item in found:
            token = item["token"]

            grouped.setdefault(
                token,
                {
                    "symbol": item.get("symbol") or "?",
                    "name": item.get("name") or "?",
                    "buyers": set(),
                    "txs": [],
                    "amounts": [],
                    "price_usd": item.get("price_usd"),
                },
            )

            grouped[token]["buyers"].add(item.get("buyer"))
            grouped[token]["txs"].append(item.get("tx"))
            grouped[token]["amounts"].append(item.get("amount"))

        lines = [
            "✅ История покупок Solana",
            f"Найдено токенов: {len(grouped)}",
            f"Глубина: {max_depth}",
            f"Период: {lookback_days} дней",
            "",
        ]

        for token, data in grouped.items():
            buyers = ", ".join(list(data["buyers"])[:5])
            tx = data["txs"][-1]
            amount = data["amounts"][-1]
            price = data["price_usd"]

            price_text = f" ≈ ${price:,.6f}" if price else ""

            lines.append(f"• {data['symbol']} ({data['name']})")
            lines.append(f"  Mint: `{token}`")
            lines.append(f"  Покупатель: `{buyers}`")
            lines.append(f"  Кол-во: {amount}{price_text}")
            lines.append(f"  Tx: `{tx}`")
            lines.append("")

        await send_long_message(
            context.bot,
            query.message.chat_id,
            "\n".join(lines),
            parse_mode=None,
        )

    except Exception as e:
        logger.exception("Solana history error")

        await query.edit_message_text(
            f"❌ Ошибка во время анализа истории Solana: {e}"
        )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _check_access(update):
        return

    user_id = update.effective_user.id
    settings_dict = get_user_settings_dict(user_id)

    keyboard = [
        [
            InlineKeyboardButton(
                f"Глубина: {settings_dict.get('max_depth', DEFAULT_MAX_DEPTH)}",
                callback_data="set_max_depth",
            )
        ],
        [
            InlineKeyboardButton(
                f"Период дней: {settings_dict.get('lookback_days', DEFAULT_LOOKBACK_DAYS)}",
                callback_data="set_lookback_days",
            )
        ],
        [
            InlineKeyboardButton(
                f"Макс. токенов: {settings_dict.get('max_tokens', DEFAULT_MAX_FOUND_TOKENS)}",
                callback_data="set_max_tokens",
            )
        ],
        [
            InlineKeyboardButton(
                f"Макс. адресов: {settings_dict.get('max_addresses', DEFAULT_MAX_ADDRESSES)}",
                callback_data="set_max_addresses",
            )
        ],
        [
            InlineKeyboardButton(
                f"Веток/адрес: {settings_dict.get('max_branches', DEFAULT_MAX_BRANCHES)}",
                callback_data="set_max_branches",
            )
        ],
        [InlineKeyboardButton("🔄 Сбросить настройки", callback_data="reset_settings")],
    ]

    await update.message.reply_text(
        "⚙️ Настройки поиска:\nнажми на параметр, чтобы изменить.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def settings_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _check_access(update):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data

    if data == "reset_settings":
        reset_user_settings(query.from_user.id)

        await query.edit_message_text("✅ Настройки сброшены на стандартные.")
        return

    setting_map = {
        "set_max_depth": (
            "max_depth",
            "Введите новую максимальную глубину обхода связанных кошельков:",
        ),
        "set_lookback_days": (
            "lookback_days",
            "Введите период анализа истории в днях:",
        ),
        "set_max_tokens": (
            "max_tokens",
            "Введите максимальное количество найденных токенов:",
        ),
        "set_max_addresses": (
            "max_addresses",
            "Введите максимальное количество адресов для обхода:",
        ),
        "set_max_branches": (
            "max_branches",
            "Введите максимальное количество веток на один адрес:",
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
        return

    if not update.message or not update.message.text:
        return

    value = update.message.text.strip()

    if not value.isdigit():
        await update.message.reply_text("❌ Введите корректное положительное число.")
        return

    value_int = int(value)
    cap = SETTING_CAPS.get(key)

    if cap is not None and value_int > cap:
        await update.message.reply_text(f"❌ Значение слишком большое. Максимум: {cap}")
        return

    set_user_setting(update.effective_user.id, key, str(value_int))
    context.user_data.pop("awaiting_setting", None)

    await update.message.reply_text(f"✅ Настройка обновлена: {key} = {value_int}")


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
            pattern=r"^history_(ethereum|bsc|solana)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            settings_button,
            pattern=r"^(set_max_depth|set_lookback_days|set_max_tokens|set_max_addresses|set_max_branches|reset_settings)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            dashboard_reset_handler,
            pattern=r"^reset_api_usage$",
        )
    )