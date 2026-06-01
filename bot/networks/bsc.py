"""
Сеть BSC.
Баланс BNB – напрямую RPC.
Токены ERC‑20 – через Alchemy API alchemy_getTokenBalances.
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
        # Пробуем Alchemy API
        if self.web3.is_alchemy:
            try:
                raw_balances = await self.web3.get_token_balances_alchemy(self.session, address)
                result = []
                for b in raw_balances:
                    symbol = await TokenInfoService.get_symbol(self.session, b["address"], self.config["rpc_url"])
                    decimals = await self._get_decimals(b["address"])
                    # b["balance"] – это сырое значение в минимальных единицах
                    balance = b["balance"] / (10 ** decimals) if decimals else b["balance"] / 10**18
                    if balance > 0:
                        result.append({"address": b["address"], "symbol": symbol, "balance": balance, "decimals": decimals or 18})
                return result
            except Exception as e:
                logger.warning(f"Alchemy getTokenBalances не сработал: {e}, пробуем eth_getLogs")

        # Fallback – медленный, только последние 10000 блоков
        from_block = await self.web3.get_current_block(self.session) - 10000
        transfers_in = await self.web3.get_token_transfers(self.session, address, direction="to",
                                                           from_block=from_block, to_block=99999999)
        transfers_out = await self.web3.get_token_transfers(self.session, address, direction="from",
                                                            from_block=from_block, to_block=99999999)
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
                decimals = await self._get_decimals(token)
                result.append({"address": token, "symbol": symbol, "balance": bal / (10**decimals), "decimals": decimals})
        return result

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

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()