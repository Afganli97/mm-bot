"""
Обход цепочки адресов и поиск покупок в Solana через Helius RPC.
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
                 max_depth: int = 3, max_tokens: int = 100, lookback_days: int = 30,
                 min_usd_value: float = 0.0):
        self.session = session
        self.start_address = start_address
        self.helius = helius
        self.max_depth = max_depth
        self.max_tokens = max_tokens
        self.lookback_days = lookback_days
        self.min_usd_value = min_usd_value
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

            # Получаем транзакции адреса
            signatures = await self.helius.get_signatures_for_address(self.session, addr, limit=50)
            for sig_info in signatures:
                if len(self.unique_tokens) >= self.max_tokens:
                    break
                sig = sig_info.get("signature")
                if not sig:
                    continue
                tx_data = await self.helius.get_transaction(self.session, sig)
                if not tx_data:
                    continue
                # Извлекаем переводы SOL/токенов и свопы
                # Упрощённо: ищем инструкции swap (Jupiter, Orca, Raydium)
                instructions = tx_data.get("transaction", {}).get("message", {}).get("instructions", [])
                for instr in instructions:
                    program = instr.get("programId")
                    if program in ("JUP6LbhbzKjY1YJGgBX2RqHGrWFnQHk9mvQLyXZ9iH7",
                                  "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
                                  "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grHm7Fjkh"):
                        # Нашли своп, получаем купленный токен (исходя из postTokenBalances)
                        meta = tx_data.get("meta", {})
                        post_balances = meta.get("postTokenBalances", [])
                        pre_balances = meta.get("preTokenBalances", [])
                        for post in post_balances:
                            mint = post.get("mint")
                            if mint in ("So11111111111111111111111111111111111111111",):
                                continue
                            pre_amt = next((p.get("uiTokenAmount", {}).get("uiAmount", 0) for p in pre_balances if p.get("mint") == mint), 0)
                            post_amt = post.get("uiTokenAmount", {}).get("uiAmount", 0)
                            if post_amt > pre_amt:
                                symbol = post.get("symbol", "?")
                                if not is_excluded(mint):
                                    if mint not in self.unique_tokens:
                                        self.found_tokens.append({
                                            'token': mint,
                                            'symbol': symbol,
                                            'buyer': addr,
                                            'tx': sig
                                        })
                                        self.unique_tokens.add(mint)
                                        logger.info(f"Solana найден токен: {mint} ({symbol})")

                # Рекурсивно добавляем получателей (упрощённо: из meta.innerInstructions)
                # Не углубляемся для простоты
            if depth + 1 < self.max_depth:
                # Не добавляем новых адресов для обхода в этой версии
                pass

        logger.info(f"Solana обход завершён. Адресов: {self.total_addresses}, токенов: {len(self.found_tokens)}")
        return self.found_tokens