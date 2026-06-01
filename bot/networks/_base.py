"""
Базовый класс для всех поддерживаемых сетей.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Tuple, Set
import aiohttp

class BaseNetwork(ABC):
    def __init__(self, network_config: dict, session: aiohttp.ClientSession):
        self.config = network_config
        self.session = session

    @property
    def name(self) -> str:
        return self.config["name"]

    @property
    def chain_id(self) -> Optional[int]:
        return self.config.get("chain_id")

    @property
    def native_symbol(self) -> str:
        return self.config["native_symbol"]

    @property
    def rpc_url(self) -> str:
        return self.config["rpc_url"]

    @property
    def stablecoins(self) -> List[str]:
        return self.config.get("stablecoins", [])

    @abstractmethod
    async def get_balance(self, address: str) -> float:
        """Возвращает баланс нативного токена в токенах (например, ETH, BNB)."""
        pass

    @abstractmethod
    async def get_token_balances(self, address: str) -> List[Dict[str, any]]:
        """Возвращает список словарей с балансами токенов: {address, symbol, balance, decimals}. """
        pass

    @abstractmethod
    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        """Возвращает список купленных токенов (через любой агрегатор) за период."""
        pass

    @abstractmethod
    async def validate_address(self, address: str) -> bool:
        """Проверяет, является ли адрес валидным для этой сети."""
        pass