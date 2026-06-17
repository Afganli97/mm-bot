"""
Ethereum network adapter.
"""
from typing import Dict, List

from bot.api_clients import EVMExplorerClient, EVMWeb3Client
from bot.networks.base import BaseNetwork


class EthereumNetwork(BaseNetwork):
    def __init__(
        self,
        config: dict,
        session,
        explorer: EVMExplorerClient,
        web3: EVMWeb3Client,
    ):
        self._config = config
        self.key = config["key"]
        self._name = config["name"]
        self._rpc_url = config["rpc_url"]
        self._session = session
        self._explorer = explorer
        self.web3 = web3
        self._tx_cache: Dict[str, Dict] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def rpc_url(self) -> str:
        return self._rpc_url

    async def get_block_by_timestamp(self, timestamp: int) -> int:
        return await self._explorer.get_block_by_timestamp(self._session, timestamp)

    async def get_incoming_buys(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        txs = await self._explorer.get_token_transfers(
            self._session,
            address,
            start_block=start_block,
            end_block=end_block,
            filter_by="to",
        )

        result = []
        routers = {r.lower() for r in self._config.get("dex_routers", [])}
        aggregators = {a.lower() for a in self._config.get("aggregators", [])}
        dex_targets = routers | aggregators

        for tx in txs:
            tx_hash = tx.get("hash") or tx.get("transactionHash")
            if not tx_hash:
                continue

            tx_data = await self.get_transaction(tx_hash)
            tx_to = (tx_data.get("to") or "").lower()

            is_confirmed = bool(tx_to and tx_to in dex_targets)

            result.append(
                {
                    "token_address": tx.get("contractAddress", "").lower(),
                    "tx_hash": tx_hash,
                    "block_number": int(tx.get("blockNumber", 0)),
                    "tx_timestamp": int(tx.get("timeStamp", 0) or 0),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "value": tx.get("value", "0"),
                    "is_buy_confirmed": is_confirmed,
                }
            )

        return result

    async def get_outgoing_related_transfers(self, address: str, start_block: int, end_block: int) -> List[Dict]:
        result = []

        normal = await self._explorer.get_normal_transactions(
            self._session,
            address,
            start_block,
            end_block,
            filter_by_from=True,
        )

        for tx in normal:
            if tx.get("to") and tx.get("to").lower() != address.lower() and int(tx.get("value", 0) or 0) > 0:
                result.append(
                    {
                        "type": "native",
                        "to": tx.get("to", "").lower(),
                        "value": str(tx.get("value", "0")),
                        "value_wei": int(tx.get("value", 0) or 0),
                        "tx_hash": tx.get("hash"),
                        "block_number": int(tx.get("blockNumber", 0)),
                    }
                )

        internal = await self._explorer.get_internal_transactions(
            self._session,
            address,
            start_block,
            end_block,
            filter_by_from=True,
        )

        for tx in internal:
            if tx.get("to") and tx.get("to").lower() != address.lower() and int(tx.get("value", 0) or 0) > 0:
                result.append(
                    {
                        "type": "internal_native",
                        "to": tx.get("to", "").lower(),
                        "value": str(tx.get("value", "0")),
                        "value_wei": int(tx.get("value", 0) or 0),
                        "tx_hash": tx.get("transactionHash"),
                        "block_number": int(tx.get("blockNumber", 0)),
                    }
                )

        token_txs = await self._explorer.get_token_transfers(
            self._session,
            address,
            start_block=start_block,
            end_block=end_block,
            filter_by="from",
        )

        for tx in token_txs:
            if tx.get("to") and tx.get("to").lower() != address.lower():
                result.append(
                    {
                        "type": "token",
                        "token_address": tx.get("contractAddress", "").lower(),
                        "to": tx.get("to", "").lower(),
                        "value": tx.get("value", "0"),
                        "tx_hash": tx.get("hash") or tx.get("transactionHash"),
                        "block_number": int(tx.get("blockNumber", 0)),
                    }
                )

        return result

    async def get_transaction(self, tx_hash: str) -> Dict:
        if tx_hash in self._tx_cache:
            return self._tx_cache[tx_hash]

        tx = await self.web3.get_transaction(self._session, tx_hash)
        self._tx_cache[tx_hash] = tx
        return tx

    async def get_native_balance(self, address: str) -> float:
        return await self.web3.get_balance(self._session, address)