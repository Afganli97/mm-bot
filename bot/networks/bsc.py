"""
Сеть BSC.
"""
import logging
from .ethereum import EthereumNetwork
from bot.api_clients import EVMExplorerClient

logger = logging.getLogger(__name__)

class BscNetwork(EthereumNetwork):
    def __init__(self, network_config: dict, session, explorer_client: EVMExplorerClient):
        super().__init__(network_config, session, explorer_client)

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3
        return Web3.is_address(address)