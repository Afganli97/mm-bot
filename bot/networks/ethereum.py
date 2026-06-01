"""
Сеть Ethereum.
"""
import logging
from typing import List, Dict, Set, Optional
from ._base import BaseNetwork
from bot.api_clients import EVMExplorerClient, TokenInfoService

logger = logging.getLogger(__name__)

class EthereumNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient):
        super().__init__(network_config, session)
        self.explorer = explorer_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        return await self.explorer.get_account_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        txs = await self.explorer.get_token_transfers(self.session, address)
        balances = {}
        weth = self.config["weth"].lower()
        for tx in txs:
            contract = tx['contractAddress'].lower()
            if contract == weth:
                continue
            if tx['to'].lower() == address.lower():
                balances[contract] = balances.get(contract, 0) + int(tx['value'])
            if tx['from'].lower() == address.lower():
                balances[contract] = balances.get(contract, 0) - int(tx['value'])
        result = []
        for contract, bal in balances.items():
            if bal > 0:
                symbol = await TokenInfoService.get_symbol(self.session, contract, self.config["rpc_url"])
                result.append({"address": contract, "symbol": symbol, "balance": bal, "decimals": 18})
        return result

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()