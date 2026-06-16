"""
Фильтрация токенов.

Исключаем:
- нулевой адрес;
- основные стейблкоины.

Не исключаем:
- микрокапы;
- токены без CEX;
- 1 токен;
- низколиквидные токены.
"""

import logging


logger = logging.getLogger(__name__)


_ZERO_EVM = "0x0000000000000000000000000000000000000000"

_stable_addresses = {
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT Ethereum
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC Ethereum
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI Ethereum
    "0x55d398326f99059fF775485246999027B3197955",  # USDT BSC
    "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC BSC
    "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",  # DAI BSC
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC Solana
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT Solana
}

_stable_set = {
    addr.lower()
    for addr in _stable_addresses
}


def is_excluded(token_address: str) -> bool:
    if not token_address:
        return True

    token_address = token_address.lower()

    if token_address == _ZERO_EVM:
        return True

    return token_address in _stable_set


def get_token_symbol(token_address: str) -> str:
    return "?"


async def update_top_tokens(*args, **kwargs):
    """
    Оставлено для обратной совместимости.
    Топ-токены больше не используются.
    """

    pass