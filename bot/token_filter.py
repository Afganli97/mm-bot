"""
Фильтрация токенов: исключение стейблкоинов и топ-100.
"""
import logging
from typing import List, Dict, Set
import aiohttp

from bot.config import STABLECOINS
from bot.api_clients import CoingeckoClient

logger = logging.getLogger(__name__)

# Кэш в памяти (обновляется через фоновую задачу)
_top_tokens: Dict[str, str] = {}  # address -> symbol
_top_addresses: Set[str] = set()
_stable_addresses: Set[str] = {addr.lower() for addr in STABLECOINS}

async def update_top_tokens(session: aiohttp.ClientSession):
    """Периодическое обновление топ-100 токенов."""
    global _top_tokens, _top_addresses
    try:
        tokens = await CoingeckoClient.get_top_100(session)
        _top_tokens = {t['address']: t['symbol'] for t in tokens}
        _top_addresses = set(_top_tokens.keys())
        logger.info(f"Обновлён топ-100: {len(tokens)} токенов")
    except Exception as e:
        logger.exception("Ошибка обновления топ-100")

def is_excluded(token_address: str) -> bool:
    """Проверяет, входит ли токен в стейблкоины или топ-100."""
    addr = token_address.lower()
    return addr in _stable_addresses or addr in _top_addresses

def get_token_symbol(token_address: str) -> str:
    """Возвращает символ токена, если он есть в топ-100, иначе '?'."""
    return _top_tokens.get(token_address.lower(), "?")
