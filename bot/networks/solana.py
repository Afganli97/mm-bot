"""
Solana network adapter.
"""
from typing import Dict, List

from bot.api_clients import HeliusClient
from bot.networks.base import BaseNetwork


class SolanaNetwork(BaseNetwork):
    def __init__(self, config: dict, session, helius: HeliusClient):
        self._config = config
        self.key = config["key"]
        self._name = config["name"]
        self._rpc_url = config["rpc_url"]
        self._session = session
        self.helius = helius

    @property
    def name(self) -> str:
        return self._name

    @property
    def rpc_url(self) -> str:
        return self._rpc_url

    async def get_block_by_timestamp(self, timestamp: int) -> int:
        return 0

    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        return []

    async def get_outgoing_related_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        return []

    async def get_transaction(self, tx_hash: str) -> Dict:
        return await self.helius.get_transaction(self._session, tx_hash)

    async def get_native_balance(self, address: str) -> float:
        data = await self.helius.get_wallet_balances(self._session, address)
        native = data.get("nativeBalance") or data.get("solBalance") or {}
        if isinstance(native, dict):
            return float(native.get("lamports", 0) or 0) / 10**9
        return 0.0