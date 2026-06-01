"""
Сеть BSC.
"""
import logging
from typing import List, Dict
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
        current_block = await self.web3.get_current_block(self.session)
        from_block = max(0, current_block - 10000)
        token_addresses = await self.web3.get_token_list(self.session, address, from_block, current_block)
        results = []
        for token in token_addresses:
            try:
                balance = await self.web3.get_balance_of(self.session, token, address)
                if balance == 0:
                    continue
                symbol = await TokenInfoService.get_symbol(self.session, token, self.config["rpc_url"])
                decimals = await self._get_decimals(token)
                bal = balance / (10 ** decimals) if decimals else balance / 10**18
                results.append({"address": token, "symbol": symbol, "balance": bal, "decimals": decimals or 18})
            except Exception as e:
                logger.warning(f"Ошибка получения баланса токена {token}: {e}")
        return results

    async def _get_decimals(self, token_address: str) -> int:
        payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to": token_address, "data": "0x313ce567"}, "latest"],"id":1}
        try:
            async with self.session.post(self.config["rpc_url"], json=payload, timeout=5) as resp:
                data = await resp.json()
                if 'result' in data and data['result'] != '0x':
                    return int(data['result'], 16)
        except Exception:
            pass
        return 18

    async def get_swap_history(self, address, start_time, end_time, min_amount_native, max_tokens):
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()