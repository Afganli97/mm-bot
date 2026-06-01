"""
Сеть BSC.
"""
import logging
from typing import List, Dict, Set, Optional
from ._base import BaseNetwork
from bot.api_clients import EVMWeb3Client, TokenInfoService

logger = logging.getLogger(__name__)

class BscNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, web3_client: EVMWeb3Client):
        super().__init__(network_config, session)
        self.web3 = web3_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        return await self.web3.get_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        transfers_in = await self.web3.get_token_transfers(self.session, address, direction="to")
        transfers_out = await self.web3.get_token_transfers(self.session, address, direction="from")
        balances = {}
        for t in transfers_in:
            token = t["token_address"].lower()
            balances[token] = balances.get(token, 0) + t["value"]
        for t in transfers_out:
            token = t["token_address"].lower()
            balances[token] = balances.get(token, 0) - t["value"]
        result = []
        for token, bal in balances.items():
            if bal > 0:
                symbol = await TokenInfoService.get_symbol(self.session, token, self.config["rpc_url"])
                result.append({"address": token, "symbol": symbol, "balance": bal / 10**18, "decimals": 18})
        return result

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()