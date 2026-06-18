# bot/graph_traversal.py
"""
EVM graph traversal and buy search.
"""
import asyncio
import logging
import time
from collections import Counter, deque
from typing import Dict, List, Set

from bot.blacklist import is_blacklisted
from bot.config import (
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FOUND_TOKENS,
    DEFAULT_LOOKBACK_DAYS,
)
from bot.database import (
    add_found_token,
    create_request,
    get_visited_address_cache,
    set_visited_address_cache,
    update_request_status,
    update_task_progress,
)
from bot.services.price_service import PriceService
from bot.services.spam_filter import SpamFilterService
from bot.services.token_metadata import TokenMetadataService
from bot.token_filter import is_exactly_one_unit

logger = logging.getLogger(__name__)


class GraphTraversal:
    def __init__(
        self,
        session,
        start_address: str,
        network,
        metadata_service: TokenMetadataService,
        spam_filter: SpamFilterService,
        price_service: PriceService,
        user_id: int = 0,
        chat_id: int = 0,
        max_tokens: int = DEFAULT_MAX_FOUND_TOKENS,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_addresses: int = DEFAULT_MAX_ADDRESSES,
        max_branches: int = DEFAULT_MAX_BRANCHES,
    ):
        self.session = session
        self.start_address = start_address.lower()
        self.network = network
        self.metadata_service = metadata_service
        self.spam_filter = spam_filter
        self.price_service = price_service
        self.user_id = user_id
        self.chat_id = chat_id
        self.max_tokens = max_tokens
        self.lookback_days = lookback_days
        self.max_depth = max_depth
        self.max_addresses = max_addresses
        self.max_branches = max_branches
        self.request_id = None
        self.start_block = 0
        self.end_block = 99999999
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.total_transactions = 0
        self.found_tokens = []
        self.unique_token_addresses = set()

    async def run(self) -> List[Dict]:
        try:
            logger.info(
                "Start EVM graph traversal network=%s address=%s depth=%s",
                self.network.name,
                self.start_address,
                self.max_depth,
            )

            self.request_id = create_request(
                user_id=self.user_id,
                chat_id=self.chat_id,
                address=self.start_address,
                network=self.network.key,
                depth=self.max_depth,
                max_addresses=self.max_addresses,
                lookback_days=self.lookback_days,
                max_tokens=self.max_tokens,
                max_branches=self.max_branches,
            )

            days_ago_ts = int(time.time()) - (self.lookback_days * 86400)
            self.start_block = await self.network.get_block_by_timestamp(days_ago_ts)

            if hasattr(self.network, "web3") and self.network.web3:
                self.end_block = await self.network.web3.get_current_block(self.session)

            logger.info("EVM traversal period: blocks %s-%s", self.start_block, self.end_block)

            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1
            update_task_progress(self.request_id, processed_addresses=self.total_addresses)

            while (
                queue
                and self.total_addresses <= self.max_addresses
                and len(self.unique_token_addresses) < self.max_tokens
            ):
                addr, depth = queue.popleft()
                logger.debug("Analyze EVM address %s depth=%s", addr, depth)

                try:
                    if not get_visited_address_cache(self.network.key, addr, self.start_block):
                        buys = await self.network.get_incoming_buys(
                            addr,
                            self.start_block,
                            self.end_block,
                        )
                        self.total_transactions += len(buys)
                        await self._process_buys(addr, buys)
                        set_visited_address_cache(self.network.key, addr, self.start_block)

                except Exception as e:
                    logger.exception("Error processing buys for %s: %s", addr, e)

                if len(self.unique_token_addresses) >= self.max_tokens:
                    break

                if depth + 1 <= self.max_depth:
                    try:
                        transfers = await self.network.get_outgoing_related_transfers(
                            addr,
                            self.start_block,
                            self.end_block,
                        )
                        self.total_transactions += len(transfers)

                        related = self._aggregate_related_addresses(transfers)
                        sorted_recs = related.most_common(self.max_branches)

                        branch_added = 0
                        for to_addr, _score in sorted_recs:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break

                            if branch_added >= self.max_branches:
                                break

                            to_addr = to_addr.lower()

                            if not to_addr or to_addr == addr:
                                continue

                            if is_blacklisted(to_addr, is_solana=False):
                                continue

                            if to_addr in self.visited:
                                continue

                            if self.total_addresses >= self.max_addresses:
                                break

                            self.visited.add(to_addr)
                            queue.append((to_addr, depth + 1))
                            self.total_addresses += 1
                            branch_added += 1
                            update_task_progress(
                                self.request_id,
                                processed_addresses=self.total_addresses,
                            )

                    except Exception as e:
                        logger.exception("Error processing related transfers for %s: %s", addr, e)

                update_task_progress(
                    self.request_id,
                    processed_addresses=self.total_addresses,
                    processed_transactions=self.total_transactions,
                )

            update_request_status(self.request_id, "done", finished=True)

            logger.info(
                "EVM traversal done. Addresses=%s txs=%s tokens=%s",
                self.total_addresses,
                self.total_transactions,
                len(self.found_tokens),
            )

            return self.found_tokens

        except Exception as e:
            logger.exception("Critical EVM traversal error")

            if self.request_id:
                update_request_status(self.request_id, "error", str(e), finished=True)

            raise

    async def _process_buys(self, addr: str, buys: List[Dict]):
        for buy in buys:
            if len(self.unique_token_addresses) >= self.max_tokens:
                break

            token = (buy.get("token_address") or "").lower()

            if not token or token in self.unique_token_addresses:
                continue

            metadata = await self.metadata_service.get_evm_metadata(
                self.network.key,
                token,
                self.network.rpc_url,
            )

            decimals = int(metadata.get("decimals") or 18)
            raw_balance = int(buy.get("value") or 0)

            spam = await self.spam_filter.is_spam(
                network=self.network.key,
                token_address=token,
                symbol=metadata.get("symbol", "?"),
                name=metadata.get("name", "?"),
                decimals=decimals,
                raw_balance=raw_balance,
                is_native=False,
                strict=True,
            )

            if spam.get("is_spam"):
                continue

            if spam.get("exclude_by_one_unit"):
                continue

            amount = raw_balance / (10**decimals)

            price = await self.price_service.get_price(
                self.network.key,
                token,
                web3=getattr(self.network, "web3", None),
                weth_price_usd=0.0,
            )

            symbol = metadata.get("symbol") or "?"
            name = metadata.get("name") or "?"

            add_found_token(
                request_id=self.request_id,
                network=self.network.key,
                token_address=token,
                token_symbol=symbol,
                token_name=name,
                decimals=decimals,
                amount_raw=str(raw_balance),
                amount=f"{amount:.18f}".rstrip("0").rstrip("."),
                buyer_address=addr,
                tx_hash=buy.get("tx_hash", ""),
                block_number=int(buy.get("block_number") or 0),
                tx_timestamp=int(buy.get("tx_timestamp") or 0),
                is_buy_confirmed=bool(buy.get("is_buy_confirmed", True)),
                is_spam=False,
                spam_source=spam.get("source"),
                price_usd=price,
            )

            self.found_tokens.append(
                {
                    "token": token,
                    "symbol": symbol,
                    "name": name,
                    "decimals": decimals,
                    "amount": amount,
                    "buyer": addr,
                    "tx": buy.get("tx_hash", ""),
                    "block_number": int(buy.get("block_number") or 0),
                    "tx_timestamp": int(buy.get("tx_timestamp") or 0),
                    "is_buy_confirmed": bool(buy.get("is_buy_confirmed", True)),
                    "price_usd": price,
                }
            )

            self.unique_token_addresses.add(token)

    @staticmethod
    def _aggregate_related_addresses(transfers: List[Dict]) -> Counter:
        counter = Counter()

        for transfer in transfers:
            to_addr = transfer.get("to")

            if not to_addr:
                continue

            to_addr = to_addr.lower()

            if transfer.get("type") in ("native", "internal_native"):
                counter[to_addr] += int(transfer.get("value_wei") or 0) + 1
            else:
                counter[to_addr] += 1

        return counter