"""
Базовый интерфейс для всех поддерживаемых сетей.

Алгоритм обхода не должен знать детали API-провайдера.
Каждая сеть реализует этот интерфейс.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import aiohttp


class BaseNetwork(ABC):
    def __init__(
        self,
        network_config: Dict,
        session: aiohttp.ClientSession,
    ):
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
        """
        Получить номер блока по timestamp.

        Для explorer-сетей можно точно.
        Для RPC-сетей можно приблизительно.
        """

        pass

    @abstractmethod
    async def get_incoming_buys(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        """
        Возвращает список входящих токенов, которые можно считать покупками.

        Формат:
        [
            {
                "token_address": "...",
                "tx_hash": "...",
                "block_number": 123
            }
        ]
        """

        pass

    @abstractmethod
    async def get_outgoing_transfers(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        """
        Возвращает исходящие переводы для расширения графа связей.

        Формат:
        [
            {
                "to": "...",
                "value_wei": 123,
                "blockNumber": 123
            }
        ]
        """

        pass

    @abstractmethod
    async def validate_address(self, address: str) -> bool:
        pass