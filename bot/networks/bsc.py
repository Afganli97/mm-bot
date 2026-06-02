"""
Сеть BSC.
"""
import logging
from typing import List, Dict
from ._base import BaseNetwork
from bot.api_clients import BlockscoutClient, EVMWeb3Client, TokenInfoService

logger = logging.getLogger(__name__)

class BscNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, blockscout_client: BlockscoutClient, web3_client: EVMWeb3Client):
        super().__init__(network_config, session)
        self.blockscout = blockscout_client
        self.web3 = web3_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        if self.blockscout:
            try:
                return await self.blockscout.get_native_balance(self.session, 56, address)
            except Exception as e:
                logger.warning(f"Blockscout native balance BSC failed: {e}")
        return await self.web3.get_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        if self.blockscout:
            try:
                raw_tokens = await self.blockscout.get_token_balances(self.session, 56, address)
                result = []
                for t in raw_tokens:
                    bal = t["balance"] / (10 ** t["decimals"]) if t["decimals"] else t["balance"] / 10**18
                    if bal > 0:
                        result.append({
                            "address": t["address"],
                            "symbol": t["symbol"],
                            "balance": bal,
                            "decimals": t["decimals"]
                        })
                return result
            except Exception as e:
                logger.warning(f"Blockscout token list BSC failed: {e}")
        # Fallback RPC – без сканирования, только balanceOf для известных токенов (пусто)
        return []

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()