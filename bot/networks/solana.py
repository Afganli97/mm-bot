"""
Сеть Solana (заглушка, используется только для совместимости импортов).
Все запросы выполняются напрямую через HeliusClient в handlers.py.
"""
import logging
from typing import List, Dict, Optional
from ._base import BaseNetwork

logger = logging.getLogger(__name__)

class SolanaNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session):
        super().__init__(network_config, session)

    async def validate_address(self, address: str) -> bool:
        try:
            from solders.pubkey import Pubkey
            Pubkey.from_string(address)
            return True
        except:
            return False

    async def get_balance(self, address: str) -> float:
        return 0.0

    async def get_token_balances(self, address: str) -> List[Dict]:
        return []

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        return []