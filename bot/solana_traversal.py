"""
Обход цепочки адресов и поиск покупок в Solana через Helius RPC.
Покупка определяется по увеличению баланса токена (postTokenBalances > preTokenBalances).
"""
import asyncio
import logging
from collections import deque
from typing import List, Dict, Set
import aiohttp
from bot.api_clients import HeliusClient
from bot.token_filter import is_excluded

logger = logging.getLogger(__name__)

class SolanaTraversal:
    def __init__(self, session: aiohttp.ClientSession, start_address: str, helius: HeliusClient,
                 max_depth: int = 3, max_tokens: int = 100, lookback_days: int = 30):
        self.session = session
        self.start_address = start_address
        self.helius = helius
        self.max_depth = max_depth
        self.max_tokens = max_tokens
        self.lookback_days = lookback_days
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens = []
        self.unique_tokens = set()

    async def run(self) -> List[Dict]:
        queue = deque([(self.start_address, 0)])
        self.visited.add(self.start_address)
        self.total_addresses = 1

        while queue and self.total_addresses < 2000 and len(self.unique_tokens) < self.max_tokens:
            addr, depth = queue.popleft()
            logger.debug(f"Обработка Solana адреса {addr} (глубина {depth})")

            signatures = await self.helius.get_signatures_for_address(self.session, addr, limit=100)
            for sig_info in signatures:
                if len(self.unique_tokens) >= self.max_tokens:
                    break
                sig = sig_info.get("signature")
                if not sig:
                    continue
                tx_data = await self.helius.get_transaction(self.session, sig)
                if not tx_data:
                    continue

                meta = tx_data.get("meta", {})
                if meta.get("err"):
                    continue

                pre = {item["mint"]: float(item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                       for item in meta.get("preTokenBalances", [])
                       if item.get("owner") == addr and item.get("mint") != "So11111111111111111111111111111111111111111"}
                post = {item["mint"]: float(item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                        for item in meta.get("postTokenBalances", [])
                        if item.get("owner") == addr and item.get("mint") != "So11111111111111111111111111111111111111111"}

                for mint, post_amt in post.items():
                    pre_amt = pre.get(mint, 0.0)
                    if post_amt > pre_amt:
                        if is_excluded(mint):
                            continue
                        if mint not in self.unique_tokens:
                            self.found_tokens.append({
                                'token': mint,
                                'symbol': '?',
                                'buyer': addr,
                                'tx': sig
                            })
                            self.unique_tokens.add(mint)
                            logger.info(f"Solana покупка: {mint} у {addr}")

                if depth + 1 < self.max_depth:
                    for instr in tx_data.get("transaction", {}).get("message", {}).get("instructions", []):
                        if not isinstance(instr, dict):
                            continue
                        if instr.get("parsed", {}).get("type") == "transfer":
                            dest = instr["parsed"]["info"].get("destination")
                            if dest and dest not in self.visited and dest != addr:
                                self.visited.add(dest)
                                queue.append((dest, depth + 1))
                                self.total_addresses += 1
                                if self.total_addresses >= 2000:
                                    break
                        elif instr.get("parsed", {}).get("type") == "transferChecked":
                            dest = instr["parsed"]["info"].get("destination")
                            if dest and dest not in self.visited and dest != addr:
                                self.visited.add(dest)
                                queue.append((dest, depth + 1))
                                self.total_addresses += 1
                                if self.total_addresses >= 2000:
                                    break

        logger.info(f"Solana обход завершён. Адресов: {self.total_addresses}, токенов: {len(self.found_tokens)}")
        return self.found_tokens