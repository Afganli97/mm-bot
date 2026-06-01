"""
Сеть Solana.
"""
import logging
from typing import List, Dict
from .base import BaseNetwork
from solders.pubkey import Pubkey
from bot.api_clients import SolscanClient

logger = logging.getLogger(__name__)

class SolanaNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session):
        super().__init__(network_config, session)
        self.solscan = SolscanClient()

    async def validate_address(self, address: str) -> bool:
        try:
            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    async def get_balance(self, address: str) -> float:
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]}
        async with self.session.post(self.rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            return data['result']['value'] / 1e9

    async def get_token_balances(self, address: str) -> List[Dict]:
        return await self.solscan.get_token_balances(self.session, address)

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        # Заглушка: Solana swap history не реализована в MVP
        return []