"""
Base network interface.
"""
from abc import ABC, abstractmethod
from typing import Dict, List


class BaseNetwork(ABC):
    key: str

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def rpc_url(self) -> str:
        pass

    @abstractmethod
    async def get_block_by_timestamp(self, timestamp: int) -> int:
        pass

    @abstractmethod
    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        pass

    @abstractmethod
    async def get_outgoing_related_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        pass

    @abstractmethod
    async def get_transaction(self, tx_hash: str) -> Dict:
        pass

    @abstractmethod
    async def get_native_balance(self, address: str) -> float:
        pass