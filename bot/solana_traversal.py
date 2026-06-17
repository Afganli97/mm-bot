"""
Solana graph traversal and buy search through Helius/public Solana RPC.
Uses pagination, blockTime lookback, innerInstructions.
"""
import asyncio
import logging
import time
from collections import Counter, deque
from typing import Dict, List, Set

from bot.api_clients import HeliusClient
from bot.blacklist import is_blacklisted
from bot.config import (
    DEFAULT_MAX_ADDRESSES,
    DEFAULT_MAX_BRANCHES,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FOUND_TOKENS,
    DEFAULT_LOOKBACK_DAYS,
    SOLANA_NETWORK,
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

SOL_MINT = "So11111111111111111111111111111111111111111"


class SolanaTraversal:
    def __init__(
        self,
        session,
        start_address: str,
        helius: HeliusClient,
        metadata_service: TokenMetadataService,
        spam_filter: SpamFilterService,
        price_service: PriceService,
        user_id: int = 0,
        chat_id: int = 0,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_tokens: int = DEFAULT_MAX_FOUND_TOKENS,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_addresses: int = DEFAULT_MAX_ADDRESSES,
        max_branches: int = DEFAULT_MAX_BRANCHES,
    ):
        self.session = session
        self.start_address = start_address
        self.helius = helius
        self.metadata_service = metadata_service
        self.spam_filter = spam_filter
        self.price_service = price_service
        self.user_id = user_id
        self.chat_id = chat_id
        self.max_depth = max_depth
        self.max_tokens = max_tokens
        self.lookback_days = lookback_days
        self.max_addresses = max_addresses
        self.max_branches = max_branches
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.total_transactions = 0
        self.found_tokens = []
        self.unique_tokens = set()
        self.request_id = None
        self.queue = deque()
        self.cutoff_ts = int(time.time()) - (self.lookback_days * 86400)
        self.dex_programs = {p.lower() for p in SOLANA_NETWORK.get("dex_programs", [])}
        self.stablecoins = {s.lower() for s in SOLANA_NETWORK.get("stablecoins", [])}

    async def run(self) -> List[Dict]:
        try:
            logger.info("Start Solana traversal address=%s depth=%s", self.start_address, self.max_depth)

            self.request_id = create_request(
                user_id=self.user_id,
                chat_id=self.chat_id,
                address=self.start_address,
                network="solana",
                depth=self.max_depth,
                max_addresses=self.max_addresses,
                lookback_days=self.lookback_days,
                max_tokens=self.max_tokens,
                max_branches=self.max_branches,
            )

            self.queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1
            update_task_progress(self.request_id, processed_addresses=self.total_addresses)

            stop_all = False

            while self.queue and self.total_addresses <= self.max_addresses and len(self.unique_tokens) < self.max_tokens and not stop_all:
                addr, depth = self.queue.popleft()
                logger.debug("Process Solana address %s depth=%s", addr, depth)

                before = None

                while True:
                    signatures = await self.helius.get_signatures_for_address(
                        self.session,
                        addr,
                        limit=100,
                        before=before,
                    )

                    if not signatures:
                        break

                    page_had_old = False

                    for sig_info in signatures:
                        sig = sig_info.get("signature")
                        if not sig:
                            continue

                        block_time = int(sig_info.get("blockTime") or 0)

                        if block_time and block_time < self.cutoff_ts:
                            page_had_old = True
                            stop_all = True
                            break

                        tx_data = await self.helius.get_transaction(self.session, sig)
                        if not tx_data:
                            continue

                        self.total_transactions += 1

                        tx_block_time = int(tx_data.get("blockTime") or block_time or 0)
                        if tx_block_time and tx_block_time < self.cutoff_ts:
                            page_had_old = True
                            stop_all = True
                            continue

                        await self._process_transaction(addr, depth, sig, tx_data)

                        if len(self.unique_tokens) >= self.max_tokens:
                            break

                        if self.total_addresses >= self.max_addresses:
                            break

                    update_task_progress(
                        self.request_id,
                        processed_addresses=self.total_addresses,
                        processed_transactions=self.total_transactions,
                    )

                    if page_had_old or stop_all:
                        break

                    before = signatures[-1].get("signature")

                    if len(signatures) < 100:
                        break

                    if self.total_addresses >= self.max_addresses:
                        break

                await asyncio.sleep(0.05)

            update_request_status(self.request_id, "done", finished=True)
            logger.info(
                "Solana traversal done. Addresses=%s txs=%s tokens=%s",
                self.total_addresses,
                self.total_transactions,
                len(self.found_tokens),
            )

            return self.found_tokens

        except Exception as e:
            logger.exception("Critical Solana traversal error")
            if self.request_id:
                update_request_status(self.request_id, "error", str(e), finished=True)
            raise

    async def _process_transaction(self, addr: str, depth: int, sig: str, tx_data: Dict):
        meta = tx_data.get("meta", {})

        if meta.get("err"):
            return

        pre = self._token_balance_map(meta.get("preTokenBalances", []), addr)
        post = self._token_balance_map(meta.get("postTokenBalances", []), addr)

        tx_has_dex = self._tx_has_dex_program(tx_data)
        payment_decreased = self._payment_decreased(pre, post)

        for mint, post_item in post.items():
            if len(self.unique_tokens) >= self.max_tokens:
                break

            if mint == SOL_MINT:
                continue

            if mint in self.stablecoins:
                continue

            pre_amt = float(pre.get(mint, {}).get("uiAmount") or 0)
            post_amt = float(post_item.get("uiAmount") or 0)

            if post_amt <= pre_amt:
                continue

            metadata = await self.metadata_service.get_solana_metadata(mint, hint=post_item)
            decimals = int(metadata.get("decimals") or post_item.get("decimals") or 0)
            raw_amount = int(post_item.get("amount") or 0)

            spam = await self.spam_filter.is_spam(
                network="solana",
                token_address=mint,
                symbol=metadata.get("symbol", "?"),
                decimals=decimals,
                raw_balance=raw_amount,
                is_native=False,
            )

            if spam.get("is_spam"):
                continue

            if spam.get("exclude_by_one_unit"):
                continue

            is_buy = tx_has_dex or payment_decreased

            if not is_buy:
                continue

            price = await self.price_service.get_price("solana", mint)
            amount = raw_amount / (10**decimals) if decimals else float(post_amt)

            add_found_token(
                request_id=self.request_id,
                network="solana",
                token_address=mint,
                token_symbol=metadata.get("symbol") or "?",
                token_name=metadata.get("name") or "?",
                decimals=decimals,
                amount_raw=str(raw_amount),
                amount=f"{amount:.18f}".rstrip("0").rstrip("."),
                buyer_address=addr,
                tx_hash=sig,
                block_number=int(tx_data.get("slot") or 0),
                tx_timestamp=int(tx_data.get("blockTime") or 0),
                is_buy_confirmed=is_buy,
                is_spam=False,
                spam_source=spam.get("source"),
                price_usd=price,
            )

            self.found_tokens.append(
                {
                    "token": mint,
                    "symbol": metadata.get("symbol") or "?",
                    "name": metadata.get("name") or "?",
                    "decimals": decimals,
                    "amount": amount,
                    "buyer": addr,
                    "tx": sig,
                    "block_number": int(tx_data.get("slot") or 0),
                    "tx_timestamp": int(tx_data.get("blockTime") or 0),
                    "is_buy_confirmed": is_buy,
                    "price_usd": price,
                }
            )

            self.unique_tokens.add(mint)

        if depth + 1 <= self.max_depth:
            related = self._collect_related_addresses(addr, tx_data)
            sorted_related = related.most_common(self.max_branches)

            branch_added = 0
            for to_addr, _score in sorted_related:
                if branch_added >= self.max_branches:
                    break

                if self.total_addresses >= self.max_addresses:
                    break

                if not to_addr or to_addr == addr:
                    continue

                if is_blacklisted(to_addr, is_solana=True):
                    continue

                if to_addr in self.visited:
                    continue

                self.visited.add(to_addr)
                self.queue.append((to_addr, depth + 1))
                self.total_addresses += 1
                branch_added += 1
                update_task_progress(self.request_id, processed_addresses=self.total_addresses)

    @staticmethod
    def _token_balance_map(items: List[Dict], owner: str) -> Dict[str, Dict]:
        result = {}

        for item in items:
            if item.get("owner") != owner:
                continue

            mint = item.get("mint")
            if not mint:
                continue

            ui_amount = item.get("uiTokenAmount", {}).get("uiAmount")
            if ui_amount is None:
                amount_raw = int(item.get("uiTokenAmount", {}).get("amount") or 0)
                decimals = int(item.get("uiTokenAmount", {}).get("decimals") or 0)
                ui_amount = amount_raw / (10**decimals) if decimals else 0

            result[mint] = {
                "mint": mint,
                "amount": str(item.get("uiTokenAmount", {}).get("amount") or 0),
                "decimals": int(item.get("uiTokenAmount", {}).get("decimals") or 0),
                "uiAmount": float(ui_amount or 0),
                "token_info": item.get("token_info") or item.get("tokenInfo") or {},
            }

        return result

    def _tx_has_dex_program(self, tx_data: Dict) -> bool:
        for program in self._iter_program_ids(tx_data):
            if program.lower() in self.dex_programs:
                return True
        return False

    def _payment_decreased(self, pre: Dict[str, Dict], post: Dict[str, Dict]) -> bool:
        for mint in [SOL_MINT] + list(self.stablecoins):
            pre_amt = float(pre.get(mint, {}).get("uiAmount") or 0)
            post_amt = float(post.get(mint, {}).get("uiAmount") or 0)
            if post_amt < pre_amt:
                return True
        return False

    def _collect_related_addresses(self, addr: str, tx_data: Dict) -> Counter:
        counter = Counter()

        for instr in self._iter_instructions(tx_data):
            parsed = instr.get("parsed", {})
            info = parsed.get("info", {})

            source = info.get("source") or info.get("authority")
            destination = info.get("destination")

            if source and destination:
                source = source.lower()
                destination = destination.lower()

                if source == addr.lower():
                    counter[destination] += 1
                elif destination == addr.lower():
                    counter[source] += 1

        return counter

    def _iter_instructions(self, tx_data: Dict):
        message = tx_data.get("transaction", {}).get("message", {})

        for instr in message.get("instructions", []) or []:
            if isinstance(instr, dict):
                yield instr

        meta = tx_data.get("meta", {})
        for inner in meta.get("innerInstructions", []) or []:
            for instr in inner.get("instructions", []) or []:
                if isinstance(instr, dict):
                    yield instr

    def _iter_program_ids(self, tx_data: Dict):
        message = tx_data.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", []) or []

        normalized = []
        for acc in account_keys:
            if isinstance(acc, str):
                normalized.append(acc.lower())
            elif isinstance(acc, dict):
                pubkey = acc.get("pubkey") or acc.get("address")
                if pubkey:
                    normalized.append(pubkey.lower())

        for instr in self._iter_instructions(tx_data):
            program_id = instr.get("programId")
            if program_id:
                yield program_id.lower()
                continue

            program_id_index = instr.get("programIdIndex")
            if program_id_index is not None:
                try:
                    idx = int(program_id_index)
                    if 0 <= idx < len(normalized):
                        yield normalized[idx]
                except Exception:
                    pass