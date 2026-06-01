"""
Сеть Ethereum.
Баланс ETH запрашивается через Etherscan.
Балансы токенов запрашиваются через Alchemy API (прямой метод alchemy_getTokenBalances).
"""
import logging
from typing import List, Dict, Set, Optional
from ._base import BaseNetwork
from bot.api_clients import EVMExplorerClient, EVMWeb3Client, TokenInfoService

logger = logging.getLogger(__name__)

class EthereumNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient):
        super().__init__(network_config, session)
        self.explorer = explorer_client
        # Создаём RPC‑клиент для вызова Alchemy API (на том же RPC URL, который указан в конфиге)
        self.web3 = EVMWeb3Client(
            rpc_url=network_config["rpc_url"],
            chain_id=network_config["chain_id"],
            weth_address=network_config["weth"]
        )

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        # Нативный ETH через Etherscan (быстро и надёжно)
        return await self.explorer.get_account_balance(self.session, address)

    async def get_token_balances(self, address: str) -> List[Dict]:
        """
        Получает все токены с ненулевым балансом напрямую через Alchemy API.
        Если Alchemy недоступен, используется медленный fallback (eth_getLogs).
        """
        # Пробуем Alchemy getTokenBalances
        if self.web3.is_alchemy:
            try:
                raw_balances = await self.web3.get_token_balances_alchemy(self.session, address)
                result = []
                for b in raw_balances:
                    symbol = await TokenInfoService.get_symbol(self.session, b["address"], self.config["rpc_url"])
                    # Узнаём decimals (можно закэшировать, но пока запросим один раз)
                    decimals = await self._get_decimals(b["address"])
                    balance = b["balance"] / 10**decimals if decimals else b["balance"] / 10**18
                    result.append({"address": b["address"], "symbol": symbol, "balance": balance, "decimals": decimals or 18})
                return result
            except Exception as e:
                logger.warning(f"Alchemy getTokenBalances не сработал для Ethereum: {e}, переходим к eth_getLogs")

        # Fallback – суммирование Transfer логов (не рекомендуется, но оставлено на крайний случай)
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

    async def _get_decimals(self, token_address: str) -> int:
        """Возвращает decimals токена через RPC вызов."""
        payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to": token_address, "data": "0x313ce567"}, "latest"],"id":1}
        try:
            async with self.session.post(self.config["rpc_url"], json=payload, timeout=5) as resp:
                data = await resp.json()
                if 'result' in data and data['result'] != '0x':
                    return int(data['result'], 16)
        except Exception:
            pass
        return 18  # по умолчанию

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        from bot.graph_traversal import GraphTraversal
        traversal = GraphTraversal(self.session, address, self, max_tokens=max_tokens, lookback_days=30)
        return await traversal.run()