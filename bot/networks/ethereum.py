"""
Сеть Ethereum.
"""
import logging
from typing import List, Dict, Optional
from ._base import BaseNetwork
from bot.api_clients import EVMExplorerClient, BlockscoutClient, EVMWeb3Client, TokenInfoService

logger = logging.getLogger(__name__)

class EthereumNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient,
                 blockscout_client: BlockscoutClient, web3_client: EVMWeb3Client):
        super().__init__(network_config, session)
        self.explorer = explorer_client
        self.blockscout = blockscout_client
        self.web3 = web3_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        # Blockscout – основной
        if self.blockscout:
            try:
                return await self.blockscout.get_native_balance(self.session, 1, address)
            except Exception as e:
                logger.warning(f"Blockscout native balance failed: {e}")
        # Fallback Etherscan
        return await self.explorer.get_account_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        if self.blockscout:
            try:
                raw_tokens = await self.blockscout.get_token_balances(self.session, 1, address)
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
                logger.warning(f"Blockscout token list failed: {e}")
        # Fallback Etherscan (упрощённо)
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
                result.append({"address": contract, "symbol": symbol, "balance": bal / 10**18, "decimals": 18})
        return result

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()