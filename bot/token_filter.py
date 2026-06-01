"""
Фильтрация токенов: исключение стейблкоинов и топ-100.
Для каждой сети используется свой список.
"""
import logging
from typing import List, Dict, Set
import aiohttp

from bot.config import STABLECOINS
from bot.api_clients import CoingeckoClient

logger = logging.getLogger(__name__)

# Глобальные кэши для каждой сети
_stable_addresses: Dict[str, Set[str]] = {}
_top_addresses: Dict[str, Set[str]] = {}

async def update_top_tokens(session: aiohttp.ClientSession, network_name: str = "ethereum"):
    global _top_addresses
    # Загрузка топ-100 из CoinGecko для конкретной сети
    # Пока только Ethereum реализован
    tokens = await CoingeckoClient.get_top_100(session, network_name)
    _top_addresses[network_name] = {t['address'] for t in tokens}
    _stable_addresses[network_name] = {s.lower() for s in STABLECOINS}  # берём глобальные стейблкоины

def is_excluded(token_address: str, network_name: str = "ethereum") -> bool:
    addr = token_address.lower()
    excluded = (addr in _stable_addresses.get(network_name, set())) or (addr in _top_addresses.get(network_name, set()))
    if excluded:
        logger.debug(f"Токен {addr} исключён в сети {network_name}")
    return excluded

def get_token_symbol(token_address: str, network_name: str = "ethereum") -> str:
    return _top_tokens.get(network_name, {}).get(token_address.lower(), "?")