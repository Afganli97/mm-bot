"""
Сеть Solana.
"""
import logging
from typing import List, Dict
from ._base import BaseNetwork
from solders.pubkey import Pubkey  # noqa
from bot.api_clients import SolscanClient

logger = logging.getLogger(__name__)

class SolanaNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session):
        super().__init__(network_config, session)
        self.solscan = SolscanClient()

    async def validate_address(self, address: str) -> bool:
        try:
            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    async def get_balance(self, address: str) -> float:
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]}
        async with self.session.post(self.rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            return data['result']['value'] / 1e9

    async def get_token_balances(self, address: str) -> List[Dict]:
        return await self.solscan.get_token_balances(self.session, address)

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        """
        Ищет свопы токенов через Solscan API.
        Анализирует последние 50 транзакций и находит покупки токенов.
        """
        transactions = await self.solscan.get_transactions(self.session, address, limit=50)
        found = []
        dex_programs = set(self.config.get("dex_programs", []))

        for tx in transactions:
            # Проверка временного диапазона (поле blockTime в Unix секундах)
            block_time = tx.get("blockTime")
            if block_time is None or block_time < start_time or block_time > end_time:
                continue

            # Парсинг инструкций, ищем взаимодействие с DEX программами
            instructions = tx.get("instructions", [])
            swap_detected = False
            for instr in instructions:
                program_id = instr.get("programId", "")
                if program_id in dex_programs:
                    swap_detected = True
                    break

            if not swap_detected:
                continue

            # Извлекаем входящие токены (postTokenBalances)
            pre = {t["mint"]: t["uiAmount"] for t in tx.get("preTokenBalances", [])}
            post = {t["mint"]: t["uiAmount"] for t in tx.get("postTokenBalances", [])}
            for mint, post_amount in post.items():
                pre_amount = pre.get(mint, 0.0)
                if post_amount > pre_amount:
                    # Это купленный токен
                    symbol = await self._get_token_symbol(mint)
                    found.append({
                        "token_address": mint,
                        "symbol": symbol,
                        "buyer": address,
                        "tx_hash": tx["txHash"],
                        "block_number": tx.get("slot", 0)
                    })

        # Ограничение по количеству уникальных токенов
        unique = {}
        for item in found:
            addr = item["token_address"]
            if addr not in unique:
                unique[addr] = item
            if len(unique) >= max_tokens:
                break
        return list(unique.values())

    async def _get_token_symbol(self, mint: str) -> str:
        """Получает символ токена через Solscan или возвращает '?'."""
        # Упрощённо: используем кэш или запрос к API метаданных Solscan
        try:
            tokens = await self.solscan.get_token_balances(self.session, mint)  # заглушка
            if tokens:
                return tokens[0].get("symbol", "?")
        except Exception:
            pass
        return "?"