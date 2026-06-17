"""
Token filtering rules.
"""
import logging

logger = logging.getLogger(__name__)

STABLE_ADDRESSES = {
    # Ethereum
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    # BSC
    "0x55d398326f99059fF775485246999027B3197955",
    "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
    # Solana
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

STABLE_SET = {addr.lower() for addr in STABLE_ADDRESSES}

BLACKLIST_TOKENS = {
    # Можно добавлять известные scam/mint/token addresses.
}

BLACKLIST_TOKENS_LOWER = {addr.lower() for addr in BLACKLIST_TOKENS}


def is_stablecoin(token_address: str) -> bool:
    return token_address.lower() in STABLE_SET


def is_blacklisted_token(token_address: str) -> bool:
    return token_address.lower() in BLACKLIST_TOKENS_LOWER


def is_excluded(token_address: str) -> bool:
    """
    Оставлено для обратной совместимости.
    Сейчас stablecoins не считаются spam автоматически.
    """
    return False


def is_exactly_one_unit(raw_balance: int, decimals: int, is_native: bool = False) -> bool:
    """
    Исключает non-native токены, которые находятся ровно в количестве 1 единица.
    Нативные ETH/BNB/SOL не исключаются по этому правилу.
    """
    if is_native:
        return False

    if raw_balance is None or decimals is None:
        return False

    try:
        return int(raw_balance) == 10**int(decimals)
    except Exception:
        return False


def get_token_symbol(token_address: str) -> str:
    return "?"


async def update_top_tokens(*args, **kwargs):
    pass