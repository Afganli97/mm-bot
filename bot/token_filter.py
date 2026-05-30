"""
Фильтрация токенов: исключение стейблкоинов и топ-100.
"""
import logging
from typing import List, Dict, Set
import aiohttp

from bot.config import STABLECOINS
from bot.api_clients import CoingeckoClient

logger = logging.getLogger(__name__)

_top_tokens: Dict[str, str] = {}
_top_addresses: Set[str] = set()
_stable_addresses: Set[str] = {addr.lower() for addr in STABLECOINS}

async def update_top_tokens(session: aiohttp.ClientSession):
    global _top_tokens, _top_addresses
    try:
        tokens = await CoingeckoClient.get_top_100(session)
        _top_tokens = {t['address']: t['symbol'] for t in tokens}
        _top_addresses = set(_top_tokens.keys())
        logger.info(f"Обновлён список топ-100: {len(tokens)} токенов")
        if len(tokens) == 0:
            logger.warning("CoinGecko вернул 0 токенов топ-100, фильтрация не работает")
    except Exception as e:
        logger.exception("Ошибка обновления топ-100")

def is_excluded(token_address: str) -> bool:
    addr = token_address.lower()
    excluded = addr in _stable_addresses or addr in _top_addresses
    if excluded:
        logger.debug(f"Токен {addr} исключён (стейблкоин или топ-100)")
    return excluded

def get_token_symbol(token_address: str) -> str:
    return _top_tokens.get(token_address.lower(), "?")