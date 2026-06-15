"""
Сеть BSC. Использует альтернативный метод (RPC eth_getLogs) для обхода истории,
так как Etherscan V2 сделал кросс-чейн запросы платными.
"""
import logging
from typing import List, Dict
from ._base import BaseNetwork
from bot.api_clients import EVMWeb3Client

logger = logging.getLogger(__name__)

class BscNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session, web3_client: EVMWeb3Client):
        super().__init__(network_config, session)
        self.web3 = web3_client  # Полностью опираемся на RPC, без Etherscan

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)

    async def get_balance(self, address: str) -> float:
        return await self.web3.get_balance(self.session, address)

    # --- Реализация интерфейса через альтернативу (RPC Web3) ---
    
    async def get_block_by_timestamp(self, timestamp: int) -> int:
        return await self.web3.get_block_by_timestamp_approx(self.session, timestamp)

    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        # Получаем входящие трансферы ERC20 через логи RPC
        txs = await self.web3.get_token_transfers(self.session, address, direction="to", from_block=start_block, to_block=end_block)
        buys = []
        for tx in txs:
            if tx['token_address'].lower() == self.config["weth"].lower():
                continue
            buys.append(tx)
        return buys

    async def get_outgoing_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        # Получаем исходящие трансферы (мы следим за движением ERC20/WBNB)
        return await self.web3.get_token_transfers(self.session, address, direction="from", from_block=start_block, to_block=end_block)
