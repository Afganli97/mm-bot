"""
Solana network stub.

Сейчас Solana-балансы и история выполняются напрямую через:
- HeliusClient;
- SolanaTraversal.

Файл оставлен для совместимости импортов и будущего расширения.
"""

import logging
from typing import Dict, List, Optional

from ._base import BaseNetwork


logger = logging.getLogger(__name__)


class SolanaNetwork(BaseNetwork):
    def __init__(self, network_config: Dict, session):
        super().__init__(network_config, session)

    async def validate_address(self, address: str) -> bool:
        try:
            from solders.pubkey import Pubkey

            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    async def get_balance(self, address: str) -> float:
        return 0.0

    async def get_block_by_timestamp(self, timestamp: int) -> int:
        return 0

    async def get_incoming_buys(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        return []

    async def get_outgoing_transfers(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        return []

    async def get_token_balances(self, address: str) -> List[Dict]:
        return []

    async def get_swap_history(
        self,
        address,
        start_time,
        end_time,
        min_amount_native,
        max_tokens,
    ) -> List[Dict]:
        return []