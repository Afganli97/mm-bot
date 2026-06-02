
"""
Фильтрация токенов: исключение стейблкоинов.
Топ-100 больше не используется.
"""
import logging
from typing import Set

from bot.config import STABLECOINS

logger = logging.getLogger(__name__)

_stable_addresses: Set[str] = {addr.lower() for addr in STABLECOINS}

def is_excluded(token_address: str) -> bool:
    return token_address.lower() in _stable_addresses

def get_token_symbol(token_address: str) -> str:
    return "?"