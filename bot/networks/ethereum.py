"""
Сеть Ethereum.
"""
import logging
from typing import List, Dict, Optional
from ._base import BaseNetwork
from bot.api_clients import EVMExplorerClient, EVMWeb3Client, TokenInfoService

logger = logging.getLogger(__name__)

class EthereumNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient, web3_client: EVMWeb3Client = None):
        super().__init__(network_config, session)
        self.explorer = explorer_client
        self.web3 = web3_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        return await self.explorer.get_account_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        # Токены теперь берутся через Ankr, здесь fallback не требуется
        return []

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()