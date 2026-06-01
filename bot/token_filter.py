"""
Фильтрация токенов: исключение стейблкоинов и топ-100.
Для каждой сети используется свой список.
"""
import logging
from typing import Dict, Set
import aiohttp

from bot.api_clients import CoingeckoClient

logger = logging.getLogger(__name__)

_top_addresses: Dict[str, Set[str]] = {}
_stable_addresses: Dict[str, Set[str]] = {}

async def update_top_tokens(session: aiohttp.ClientSession, network_name: str):
    global _top_addresses, _stable_addresses
    # Стейблкоины берём из конфига сети (пока передадим глобальные, потом можно расширить)
    from bot.config import NETWORKS
    net_config = NETWORKS.get(network_name, {})
    stable_list = [s.lower() for s in net_config.get("stablecoins", [])]
    _stable_addresses[network_name] = set(stable_list)

    tokens = await CoingeckoClient.get_top_100(session, network_name)
    _top_addresses[network_name] = {t["address"].lower() for t in tokens}
    logger.info(f"Обновлены фильтры для {network_name}: {len(tokens)} топ-токенов")

def is_excluded(token_address: str, network_name: str = "ethereum") -> bool:
    addr = token_address.lower()
    return (addr in _stable_addresses.get(network_name, set())) or \
           (addr in _top_addresses.get(network_name, set()))

def get_token_symbol(token_address: str, network_name: str = "ethereum") -> str:
    # Если токен есть в топ-100, вернём его символ (но здесь нет сохранения символов)
    return "?"