"""
Сеть BSC.
"""
import logging
from typing import List, Dict
from ._base import BaseNetwork
from bot.api_clients import EVMWeb3Client, EVMExplorerClient

logger = logging.getLogger(__name__)

class BscNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient, web3_client: EVMWeb3Client = None):
        super().__init__(network_config, session)
        self.explorer = explorer_client  # Теперь BSC работает с Etherscan V2 API (chainid=56)
        self.web3 = web3_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        if self.web3:
            return await self.web3.get_balance(self.session, address)
        return 0.0

    async def get_token_balances(self, address: str) -> List[Dict]:
        return []

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()
