"""
BSC network layer.

BSCScan API больше не используется.
История токенов берётся через публичный BSC RPC eth_getLogs.
"""

import asyncio
import logging
from typing import Dict, List, Optional

from ._base import BaseNetwork
from bot.api_clients import EVMWeb3Client
from bot.config import EVM_NETWORK_CALL_TIMEOUT_SECONDS


logger = logging.getLogger(__name__)


class BscNetwork(BaseNetwork):
    def __init__(
        self,
        network_config: Dict,
        session,
        web3_client: EVMWeb3Client,
        explorer_client: Optional[object] = None,
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
        return await asyncio.wait_for(
            self.web3.get_block_by_timestamp_approx(self.session, timestamp),
            timeout=EVM_NETWORK_CALL_TIMEOUT_SECONDS,
        )

    async def get_incoming_buys(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        try:
            txs = await asyncio.wait_for(
                self.web3.get_token_transfers(
                    self.session,
                    address,
                    direction="to",
                    from_block=start_block,
                    to_block=end_block,
                ),
                timeout=EVM_NETWORK_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("BSC RPC incoming timeout address=%s", address)
            return []
        except Exception as exc:
            logger.error("BSC RPC incoming error address=%s: %s", address, exc)
            return []

        result = []
        seen = set()

        for tx in txs:
            token = str(tx.get("token_address") or "").lower()

            if not token or token == self.config["weth"].lower():
                continue

            tx_hash = str(tx.get("tx_hash") or tx.get("transactionHash") or "").lower()
            key = (token, tx_hash)

            if key in seen:
                continue

            seen.add(key)

            result.append(
                {
                    "token_address": token,
                    "tx_hash": tx_hash,
                    "block_number": int(tx.get("block_number") or tx.get("blockNumber") or 0),
                    "blockNumber": int(tx.get("block_number") or tx.get("blockNumber") or 0),
                }
            )

        return result

    async def get_outgoing_transfers(
        self,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict]:
        try:
            txs = await asyncio.wait_for(
                self.web3.get_token_transfers(
                    self.session,
                    address,
                    direction="from",
                    from_block=start_block,
                    to_block=end_block,
                ),
                timeout=EVM_NETWORK_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("BSC RPC outgoing timeout address=%s", address)
            return []
        except Exception as exc:
            logger.error("BSC RPC outgoing error address=%s: %s", address, exc)
            return []

        result = []

        for tx in txs:
            to_addr = str(tx.get("to") or "").lower()

            if not to_addr:
                continue

            result.append(
                {
                    "to": to_addr,
                    "token_address": str(tx.get("token_address") or "").lower(),
                    "value_wei": int(tx.get("value_wei") or 0),
                    "blockNumber": int(tx.get("block_number") or tx.get("blockNumber") or 0),
                }
            )

        return result