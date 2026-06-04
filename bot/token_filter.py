"""
Фильтрация токенов: исключение стейблкоинов.
Топ-100 больше не используется.
"""
import logging

logger = logging.getLogger(__name__)

_stable_addresses = {
    # Ethereum
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
    # BSC
    "0x55d398326f99059fF775485246999027B3197955",  # USDT
    "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC
    "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",  # DAI
    # Solana
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

_stable_set = {addr.lower() for addr in _stable_addresses}

def is_excluded(token_address: str) -> bool:
    """Возвращает True, если токен является стейблкоином."""
    return token_address.lower() in _stable_set

def get_token_symbol(token_address: str) -> str:
    return "?"

async def update_top_tokens(*args, **kwargs):
    pass