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

                meta = tx_data.get("meta", {})
                if meta.get("err"):
                    continue

                # Анализируем изменение балансов токенов именно для этого адреса
                pre = {item["mint"]: float(item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                       for item in meta.get("preTokenBalances", [])
                       if item.get("owner") == addr and item.get("mint") != "So11111111111111111111111111111111111111111"}
                post = {item["mint"]: float(item.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                        for item in meta.get("postTokenBalances", [])
                        if item.get("owner") == addr and item.get("mint") != "So11111111111111111111111111111111111111111"}

                for mint, post_amt in post.items():
                    pre_amt = pre.get(mint, 0.0)
                    if post_amt > pre_amt:
                        # Это покупка
                        if is_excluded(mint):
                            continue
                        if mint not in self.unique_tokens:
                            # Пытаемся получить символ из postTokenBalances
                            symbol = "?"
                            for b in meta.get("postTokenBalances", []):
                                if b.get("mint") == mint and b.get("owner") == addr:
                                    symbol = b.get("symbol", "?")
                                    break
                            self.found_tokens.append({
                                'token': mint,
                                'symbol': symbol,
                                'buyer': addr,
                                'tx': sig
                            })
                            self.unique_tokens.add(mint)
                            logger.info(f"Solana покупка: {mint} ({symbol}) у {addr}")

            # Углубляемся только для следующих адресов (получателей SOL/токенов), но в этой версии опускаем для простоты
            if depth + 1 < self.max_depth:
                # Не добавляем новые адреса – оставляем только первый уровень
                pass

        logger.info(f"Solana обход завершён. Адресов: {self.total_addresses}, токенов: {len(self.found_tokens)}")
        return self.found_tokens