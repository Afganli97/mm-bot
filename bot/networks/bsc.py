"""
BSC network layer.

Предпочтительный вариант:
- BscScan V2 API для native BNB + internal BNB + WBNB/ERC20.

Fallback:
- RPC eth_getLogs только для ERC20/WBNB.
"""

import logging
from typing import Dict, List, Optional

from ._base import BaseNetwork
from bot.api_clients import BscScanExplorerClient, EVMWeb3Client


logger = logging.getLogger(__name__)


class BscNetwork(BaseNetwork):
    def __init__(
        self,
        network_config: Dict,
        session,
        web3_client: EVMWeb3Client,
        explorer_client: Optional[BscScanExplorerClient] = None,
    ):
        super().__init__(network_config, session)

        self.web3 = web3_client
        self.explorer = explorer_client

    async def validate_address(self, address: str) -> bool:
        from web3 import Web3

        return bool(Web3.is_address(address))

    async def get_balance(self, address: str) -> float:
        return await self.web3.get_balance(self.session, address)

    async def get_block_by_timestamp(self, timestamp: int) -> int:
        if self.explorer:
            return await self.explorer.get_block_by_timestamp(self.session, timestamp)

        return await self.web3.get_block_by_timestamp_approx(self.session, timestamp)

    async def get_incoming_buys(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        buys: List[Dict] = []

        if self.explorer:
            txs = await self.explorer.get_token_transfers(
                self.session,
                address,
                start_block=start_block,
                end_block=end_block,
                filter_by="to",
            )

            for tx in txs:
                token_address = str(tx.get("contractAddress") or "").lower()

                if not token_address:
                    continue

                if token_address == self.config["weth"].lower():
                    continue

                buys.append(
                    {
                        "token_address": token_address,
                        "tx_hash": tx.get("hash") or tx.get("transactionHash") or "",
                        "block_number": int(tx.get("blockNumber") or 0),
                        "blockNumber": int(tx.get("blockNumber") or 0),
                    }
                )

        rpc_txs = await self.web3.get_token_transfers(
            self.session,
            address,
            direction="to",
            from_block=start_block,
            to_block=end_block,
        )

        for tx in rpc_txs:
            token_address = str(tx.get("token_address") or "").lower()

            if not token_address:
                continue

            if token_address == self.config["weth"].lower():
                continue

            buys.append(
                {
                    "token_address": token_address,
                    "tx_hash": tx.get("tx_hash") or tx.get("transactionHash") or "",
                    "block_number": int(tx.get("block_number") or tx.get("blockNumber") or 0),
                    "blockNumber": int(tx.get("block_number") or tx.get("blockNumber") or 0),
                }
            )

        return buys

    async def get_outgoing_transfers(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        transfers: List[Dict] = []

        if self.explorer:
            normal_txs = await self.explorer.get_normal_transactions(
                self.session,
                address,
                start_block,
                end_block,
            )

            internal_txs = await self.explorer.get_internal_transactions(
                self.session,
                address,
                start_block,
                end_block,
            )

            weth_txs = await self.explorer.get_token_transfers(
                self.session,
                address,
                contract_address=self.config["weth"],
                start_block=start_block,
                end_block=end_block,
                filter_by="from",
            )

            for tx in normal_txs + internal_txs + weth_txs:
                to_addr = str(tx.get("to") or "").lower()

                if not to_addr:
                    continue

                transfers.append(
                    {
                        "to": to_addr,
                        "value_wei": int(tx.get("value") or 0),
                        "blockNumber": int(tx.get("blockNumber") or 0),
                    }
                )

        rpc_txs = await self.web3.get_token_transfers(
            self.session,
            address,
            direction="from",
            from_block=start_block,
            to_block=end_block,
        )

        for tx in rpc_txs:
            to_addr = str(tx.get("to") or "").lower()

            if not to_addr:
                continue

            transfers.append(
                {
                    "to": to_addr,
                    "token_address": str(tx.get("token_address") or "").lower(),
                    "value_wei": int(tx.get("value_wei") or 0),
                    "blockNumber": int(tx.get("blockNumber") or tx.get("block_number") or 0),
                }
            )

        return transfers
