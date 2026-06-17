"""
Основной алгоритм обхода адресов и поиска покупок для EVM.

Важно:
- спам-проверка НЕ делается во время обхода;
- сначала собираются все найденные токены;
- потом спам-проверка делается в handlers.py только по уникальным токенам.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Dict, List, Set

from bot.blacklist import is_blacklisted
from bot.config import (
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES_PER_ADDRESS,
    EVM_NETWORK_CALL_TIMEOUT_SECONDS,
)
from bot.database import (
    create_request,
    get_visited_address_cache,
    set_visited_address_cache,
    update_request_status,
    update_task_progress,
)
from bot.token_filter import is_excluded


logger = logging.getLogger(__name__)


class GraphTraversal:
    def __init__(
        self,
        session,
        start_address: str,
        network,
        max_tokens: int = 100,
        lookback_days: int = 30,
        max_depth: int = 3,
        max_addresses: int = DEFAULT_MAX_ADDRESSES,
        max_branches_per_address: int = DEFAULT_MAX_BRANCHES_PER_ADDRESS,
        user_id: int = 0,
        chat_id: int = 0,
    ):
        self.session = session
        self.start_address = start_address.lower()
        self.network = network
        self.max_tokens = int(max_tokens)
        self.lookback_days = int(lookback_days)
        self.max_depth = int(max_depth)
        self.max_addresses = int(max_addresses)
        self.max_branches_per_address = int(max_branches_per_address)
        self.user_id = int(user_id)
        self.chat_id = int(chat_id)

        self.request_id = None
        self.start_block = 0
        self.end_block = 99999999
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens: List[Dict] = []
        self.unique_token_addresses: Set[str] = set()

    async def run(self) -> List[Dict]:
        try:
            chain_id = str(self.network.chain_id or "0")

            logger.info(
                "Начало EVM-обхода graph=%s address=%s depth=%s max_addresses=%s",
                self.network.name,
                self.start_address,
                self.max_depth,
                self.max_addresses,
            )

            self.request_id = create_request(
                user_id=self.user_id,
                chat_id=self.chat_id,
                address=self.start_address,
                depth=self.max_depth,
                network=self.network.name,
                chain_id=chain_id,
                max_addresses=self.max_addresses,
            )

            days_ago_ts = time.time() - (self.lookback_days * 86400)

            self.start_block = await self.network.get_block_by_timestamp(days_ago_ts)

            if hasattr(self.network, "web3") and self.network.web3:
                self.end_block = await self.network.web3.get_current_block(self.session)

            if self.start_block > self.end_block:
                self.start_block, self.end_block = self.end_block, self.start_block

            logger.info("Период обхода: blocks=%s-%s", self.start_block, self.end_block)

            queue = deque([(self.start_address, 0)])

            self.visited.add(self.start_address)
            self.total_addresses = 1

            update_task_progress(self.request_id, self.total_addresses)

            while queue and self.total_addresses < self.max_addresses:
                if len(self.unique_token_addresses) >= self.max_tokens:
                    break

                addr, depth = queue.popleft()

                logger.debug(
                    "Анализ EVM-адреса %s depth=%s total=%s",
                    addr,
                    depth,
                    self.total_addresses,
                )

                try:
                    if not get_visited_address_cache(addr, self.start_block, chain_id=chain_id):
                        try:
                            buys = await asyncio.wait_for(
                                self.network.get_incoming_buys(
                                    addr,
                                    self.start_block,
                                    self.end_block,
                                ),
                                timeout=EVM_NETWORK_CALL_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            logger.warning("EVM get_incoming_buys timeout address=%s", addr)
                            buys = []
                        except Exception as exc:
                            logger.error("EVM get_incoming_buys error address=%s: %s", addr, exc)
                            buys = []

                        for buy in buys:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break

                            token = str(buy.get("token_address") or "").lower()

                            if not token:
                                continue

                            if token in self.unique_token_addresses:
                                continue

                            if is_excluded(token):
                                continue

                            self.found_tokens.append(
                                {
                                    "token": token,
                                    "symbol": "?",
                                    "buyer": addr,
                                    "tx_hash": buy.get("tx_hash") or buy.get("transactionHash") or "",
                                    "block_number": int(buy.get("block_number") or buy.get("blockNumber") or 0),
                                }
                            )

                            self.unique_token_addresses.add(token)

                        set_visited_address_cache(addr, self.start_block, chain_id=chain_id)

                except Exception as exc:
                    logger.error("Ошибка при поиске покупок для %s: %s", addr, exc)

                if len(self.unique_token_addresses) >= self.max_tokens:
                    break

                if depth + 1 <= self.max_depth:
                    try:
                        try:
                            transfers = await asyncio.wait_for(
                                self.network.get_outgoing_transfers(
                                    addr,
                                    self.start_block,
                                    self.end_block,
                                ),
                                timeout=EVM_NETWORK_CALL_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            logger.warning("EVM get_outgoing_transfers timeout address=%s", addr)
                            transfers = []
                        except Exception as exc:
                            logger.error("EVM get_outgoing_transfers error address=%s: %s", addr, exc)
                            transfers = []

                        recipients = self._aggregate_recipients(transfers)

                        sorted_recs = sorted(
                            recipients.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )[: self.max_branches_per_address]

                        for to_addr, _value in sorted_recs:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break

                            to_addr = to_addr.lower()

                            if is_blacklisted(to_addr, is_solana=False):
                                logger.debug("EVM: адрес в blacklist, пропущен: %s", to_addr)
                                continue

                            if to_addr in self.visited:
                                continue

                            self.visited.add(to_addr)
                            queue.append((to_addr, depth + 1))

                            self.total_addresses += 1
                            update_task_progress(self.request_id, self.total_addresses)

                            if self.total_addresses >= self.max_addresses:
                                break

                    except Exception as exc:
                        logger.error("Ошибка при анализе получателей для %s: %s", addr, exc)

            update_request_status(self.request_id, "done", finished=True)

            logger.info("EVM-обход завершён. Адресов=%s, токенов=%s", self.total_addresses, len(self.found_tokens))

            return self.found_tokens

        except Exception as exc:
            logger.exception("Критическая ошибка EVM-обхода")

            if self.request_id:
                update_request_status(self.request_id, "error", str(exc), finished=True)

            raise

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        agg: Dict[str, int] = {}

        for transfer in transfers:
            to_addr = str(transfer.get("to") or "").lower()

            if not to_addr:
                continue

            agg[to_addr] = agg.get(to_addr, 0) + int(transfer.get("value_wei") or 0)

        return {
            addr: value
            for addr, value in agg.items()
            if value > 0
        }