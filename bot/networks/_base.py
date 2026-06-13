"""
Базовый класс для всех поддерживаемых сетей.
Создает единый интерфейс (Фабрику), изолируя алгоритм обхода от API провайдеров.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Set
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

    @abstractmethod
    async def get_balance(self, address: str) -> float:
        pass

    @abstractmethod
    async def get_block_by_timestamp(self, timestamp: int) -> int:
        """Получить номер блока по времени (через Explorer или RPC-приближение)"""
        pass

    @abstractmethod
    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        """Возвращает список токенов (ERC20), поступивших на кошелек: [{'token_address', 'tx_hash', 'block_number'}]"""
        pass

    @abstractmethod
    async def get_outgoing_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        """Возвращает список получателей средств (Native + ERC20): [{'to', 'value_wei', 'blockNumber'}]"""
        pass

    @abstractmethod
    async def validate_address(self, address: str) -> bool:
        pass
