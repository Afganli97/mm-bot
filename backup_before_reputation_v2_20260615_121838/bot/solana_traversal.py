"""
Обход цепочки адресов и поиск покупок в Solana через Helius RPC.

Покупка определяется по увеличению token balance:
postTokenBalances > preTokenBalances.

Исправления:
- lookback_days реально фильтрует транзакции по blockTime;
- max_addresses берётся из настроек;
- запросы Helius считаются в api_usage;
- задачи пишутся в requests/task_progress;
- blacklist CEX/DEX применяется к связанным адресам.
"""

import logging
import time
from collections import deque
from typing import Any, Dict, List, Set

from bot.api_clients import HeliusClient
from bot.blacklist import is_blacklisted
from bot.config import DEFAULT_MAX_ADDRESSES, DEFAULT_MAX_BRANCHES_PER_ADDRESS
from bot.database import (
    create_request,
    update_request_status,
    update_task_progress,
)
from bot.token_filter import is_excluded


logger = logging.getLogger(__name__)


class SolanaTraversal:
    def __init__(
        self,
        session,
        start_address: str,
        helius: HeliusClient,
        max_depth: int = 3,
        max_tokens: int = 100,
        lookback_days: int = 30,
        max_addresses: int = DEFAULT_MAX_ADDRESSES,
        max_branches_per_address: int = DEFAULT_MAX_BRANCHES_PER_ADDRESS,
        user_id: int = 0,
        chat_id: int = 0,
    ):
        self.session = session
        self.start_address = start_address
        self.helius = helius
        self.max_depth = int(max_depth)
        self.max_tokens = int(max_tokens)
        self.lookback_days = int(lookback_days)
        self.max_addresses = int(max_addresses)
        self.max_branches_per_address = int(max_branches_per_address)
        self.user_id = int(user_id)
        self.chat_id = int(chat_id)

        self.request_id = None
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens: List[Dict[str, Any]] = []
        self.unique_tokens: Set[str] = set()

    async def run(self) -> List[Dict[str, Any]]:
        try:
            logger.info(
                "Начало Solana-обхода address=%s depth=%s max_addresses=%s",
                self.start_address,
                self.max_depth,
                self.max_addresses,
            )

            self.request_id = create_request(
                user_id=self.user_id,
                chat_id=self.chat_id,
                address=self.start_address,
                depth=self.max_depth,
                network="Solana",
                chain_id="solana",
                max_addresses=self.max_addresses,
            )

            cutoff_ts = int(time.time()) - (self.lookback_days * 86400)

            queue = deque(
                [
                    (
                        self.start_address,
                        0,
                    )
                ]
            )

            self.visited.add(self.start_address)
            self.total_addresses = 1

            update_task_progress(self.request_id, self.total_addresses)

            while queue and self.total_addresses < self.max_addresses:
                if len(self.unique_tokens) >= self.max_tokens:
                    break

                addr, depth = queue.popleft()

                logger.debug(
                    "Обработка Solana-адреса %s depth=%s total=%s",
                    addr,
                    depth,
                    self.total_addresses,
                )

                signatures = await self.helius.get_signatures_for_address(
                    self.session,
                    addr,
                    limit=100,
                )

                older_than_lookback = False

                for sig_info in signatures:
                    if len(self.unique_tokens) >= self.max_tokens:
                        break

                    block_time = sig_info.get("blockTime")

                    if block_time:
                        if int(block_time) < cutoff_ts:
                            older_than_lookback = True
                            break

                    sig = sig_info.get("signature")

                    if not sig:
                        continue

                    tx_data = await self.helius.get_transaction(self.session, sig)

                    if not tx_data:
                        continue

                    meta = tx_data.get("meta") or {}

                    if meta.get("err"):
                        continue

                    pre = {
                        item["mint"]: float(
                            item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
                        )
                        for item in meta.get("preTokenBalances", [])
                        if item.get("owner") == addr
                        and item.get("mint")
                        and item.get("mint")
                        != "So11111111111111111111111111111111111111111"
                    }

                    post = {
                        item["mint"]: float(
                            item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0
                        )
                        for item in meta.get("postTokenBalances", [])
                        if item.get("owner") == addr
                        and item.get("mint")
                        and item.get("mint")
                        != "So11111111111111111111111111111111111111111"
                    }

                    for mint, post_amount in post.items():
                        pre_amount = pre.get(mint, 0.0)

                        if post_amount <= pre_amount:
                            continue

                        if is_excluded(mint):
                            continue

                        if mint in self.unique_tokens:
                            continue

                        self.found_tokens.append(
                            {
                                "token": mint,
                                "symbol": "?",
                                "buyer": addr,
                                "tx": sig,
                                "block_time": tx_data.get("blockTime") or sig_info.get("blockTime"),
                            }
                        )

                        self.unique_tokens.add(mint)

                        logger.info("Solana покупка: %s у %s", mint, addr)

                if older_than_lookback:
                    logger.debug("Solana: старые транзакции для %s, переходим дальше", addr)

                if depth + 1 <= self.max_depth:
                    for instr in self._iter_instructions(tx_data if "tx_data" in locals() else {}):
                        if len(self.unique_tokens) >= self.max_tokens:
                            break

                        if not isinstance(instr, dict):
                            continue

                        parsed = instr.get("parsed") or {}
                        info = parsed.get("info") or {}
                        instr_type = parsed.get("type")

                        if instr_type not in ("transfer", "transferChecked"):
                            continue

                        dest = info.get("destination")

                        if not dest or dest == addr:
                            continue

                        if dest in self.visited:
                            continue

                        if is_blacklisted(dest, is_solana=True):
                            logger.debug("Solana: адрес в blacklist, пропущен: %s", dest)
                            continue

                        self.visited.add(dest)
                        queue.append(
                            (
                                dest,
                                depth + 1,
                            )
                        )

                        self.total_addresses += 1
                        update_task_progress(self.request_id, self.total_addresses)

                        if self.total_addresses >= self.max_addresses:
                            break

                update_task_progress(self.request_id, self.total_addresses)

            update_request_status(
                self.request_id,
                "done",
                finished=True,
            )

            logger.info(
                "Solana-обход завершён. Адресов=%s, токенов=%s",
                self.total_addresses,
                len(self.found_tokens),
            )

            return self.found_tokens

        except Exception as exc:
            logger.exception("Критическая ошибка Solana-обхода")

            if self.request_id:
                update_request_status(
                    self.request_id,
                    "error",
                    str(exc),
                    finished=True,
                )

            raise

    @staticmethod
    def _iter_instructions(tx_data: Dict[str, Any]):
        if not tx_data:
            return

        message = tx_data.get("transaction", {}).get("message", {})

        for instr in message.get("instructions", []) or []:
            yield instr

        for inner in tx_data.get("meta", {}).get("innerInstructions", []) or []:
            for instr in inner.get("instructions", []) or []:
                yield instr