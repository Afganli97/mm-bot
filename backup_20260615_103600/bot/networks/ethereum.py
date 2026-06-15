"""
Сеть Ethereum. Использует Etherscan API V2 для истории покупок.
"""
import logging
from typing import List, Dict
from ._base import BaseNetwork
from bot.api_clients import EVMExplorerClient, EVMWeb3Client

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
        if self.web3: return await self.web3.get_balance(self.session, address)
        return await self.explorer.get_account_balance(self.session, address)

    # --- Реализация интерфейса через Etherscan V2 ---
    
    async def get_block_by_timestamp(self, timestamp: int) -> int:
        return await self.explorer.get_block_by_timestamp(self.session, timestamp)

    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        txs = await self.explorer.get_token_transfers(self.session, address, start_block=start_block, end_block=end_block, filter_by="to")
        buys = []
        for tx in txs:
            if tx['contractAddress'].lower() == self.config["weth"].lower():
                continue
            buys.append({'token_address': tx['contractAddress'].lower(), 'tx_hash': tx['hash'], 'block_number': int(tx['blockNumber'])})
        return buys

    async def get_outgoing_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        normal_txs = await self.explorer.get_normal_transactions(self.session, address, start_block, end_block)
        internal_txs = await self.explorer.get_internal_transactions(self.session, address, start_block, end_block)
        weth_txs = await self.explorer.get_token_transfers(self.session, address, contract_address=self.config["weth"], start_block=start_block, end_block=end_block, filter_by="from")
        
        transfers = []
        for tx in normal_txs + internal_txs + weth_txs:
            transfers.append({'to': tx['to'].lower(), 'value_wei': int(tx['value']), 'blockNumber': int(tx['blockNumber'])})
        return transfers
