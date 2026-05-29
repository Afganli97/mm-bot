"""
Основной алгоритм обхода адресов и поиска покупок.
"""
import asyncio
import logging
from collections import deque
from typing import List, Dict, Set, Optional
import aiohttp

from bot.config import (
    MAX_DEPTH, MAX_BRANCHES_PER_ADDRESS, LOOKBACK_DAYS,
    MIN_TRANSFER_VALUE_ETH, MAX_ADDRESSES_PER_TASK,
    WETH_ADDRESS, DEX_ROUTERS
)
from bot.database import (
    create_request, update_request_status, add_found_token,
    get_visited_address_cache, set_visited_address_cache,
    update_task_progress, get_connection
)
from bot.api_clients import EtherscanClient, etherscan_rotator
from bot.token_filter import is_excluded, get_token_symbol

logger = logging.getLogger(__name__)

class GraphTraversal:
    def __init__(self, session: aiohttp.ClientSession, start_address: str, user_id: int, chat_id: int):
        self.session = session
        self.start_address = start_address.lower()
        self.user_id = user_id
        self.chat_id = chat_id
        self.request_id = None
        self.start_block = 0
        self.end_block = 99999999  # текущий блок будем получать динамически
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens = []  # локальный список для отчёта

    async def run(self) -> List[Dict]:
        """Запускает обход, возвращает список найденных токенов."""
        try:
            # 1. Создать задачу в БД
            self.request_id = create_request(self.user_id, self.chat_id, self.start_address, MAX_DEPTH)
            # 2. Определить блоки периода
            now_ts = int(asyncio.get_event_loop().time())
            thirty_days_ago_ts = now_ts - LOOKBACK_DAYS * 86400
            self.start_block = await EtherscanClient.get_block_by_timestamp(self.session, thirty_days_ago_ts)
            # Актуальный конечный блок — latest (можно запросить, но возьмём 99999999)
            logger.info(f"Анализ с блока {self.start_block} (30 дней назад)")

            # 3. BFS
            queue = deque([(self.start_address, 0)])  # (адрес, глубина)
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < MAX_ADDRESSES_PER_TASK:
                addr, depth = queue.popleft()
                logger.debug(f"Обработка {addr} (глубина {depth})")

                # Получаем исходящие переводы ETH/WETH
                try:
                    transfers = await self._get_outgoing_transfers(addr)
                except Exception as e:
                    logger.error(f"Ошибка получения переводов для {addr}: {e}")
                    continue

                # Получатели, сгруппированные по сумме
                recipients = self._aggregate_recipients(transfers)
                # Сортируем по убыванию суммы, берём top MAX_BRANCHES
                sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:MAX_BRANCHES_PER_ADDRESS]

                for to_addr, _ in sorted_recs:
                    # Анализ покупок получателя (делаем всегда)
                    try:
                        buys = await self._find_buys(to_addr)
                        for buy in buys:
                            if not is_excluded(buy['token_address']):
                                symbol = get_token_symbol(buy['token_address'])
                                add_found_token(self.request_id, buy['token_address'], symbol,
                                                to_addr, buy['tx_hash'], buy['block_number'])
                                self.found_tokens.append({
                                    'token': buy['token_address'],
                                    'symbol': symbol,
                                    'buyer': to_addr,
                                    'tx': buy['tx_hash']
                                })
                    except Exception as e:
                        logger.error(f"Ошибка поиска покупок для {to_addr}: {e}")

                    # Если глубина позволяет и адрес не посещён, добавляем в очередь
                    if depth + 1 < MAX_DEPTH and to_addr not in self.visited:
                        self.visited.add(to_addr)
                        queue.append((to_addr, depth + 1))
                        self.total_addresses += 1
                        update_task_progress(self.request_id, self.total_addresses)

            # Завершение
            update_request_status(self.request_id, 'done', finished=True)
            logger.info(f"Обход завершён. Проверено адресов: {self.total_addresses}, найдено токенов: {len(self.found_tokens)}")
            return self.found_tokens

        except Exception as e:
            logger.exception("Ошибка во время обхода")
            if self.request_id:
                update_request_status(self.request_id, 'error', str(e), finished=True)
            raise

    async def _get_outgoing_transfers(self, address: str) -> List[Dict]:
        """Возвращает список исходящих переводов ETH и WETH от address."""
        # ETH внутренние
        eth_txs = await EtherscanClient.get_internal_transactions(
            self.session, address, self.start_block, self.end_block
        )
        # WETH
        weth_txs = await EtherscanClient.get_token_transfers(
            self.session, address, contract_address=WETH_ADDRESS,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="from"
        )
        # Приводим к единому формату: {to, value_wei}
        transfers = []
        for tx in eth_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value'])
            })
        for tx in weth_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value'])
            })
        return transfers

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        """Группирует получателей по сумме переводов в wei, фильтрует пыль."""
        agg = {}
        for t in transfers:
            to = t['to']
            agg[to] = agg.get(to, 0) + t['value_wei']
        # Фильтр минимальной суммы (ETH)
        min_wei = int(MIN_TRANSFER_VALUE_ETH * 10**18)
        return {addr: val for addr, val in agg.items() if val >= min_wei}

    async def _find_buys(self, address: str) -> List[Dict]:
        """
        Ищет покупки токенов: входящие токены от DEX-роутеров.
        Возвращает список словарей с ключами token_address, tx_hash, block_number.
        """
        buys = []
        # Проверяем кэш (если уже проверяли после start_block, то не запрашиваем)
        if get_visited_address_cache(address, self.start_block):
            return buys  # адрес уже был проверен за этот период, покупки уже учтены
        # Получаем входящие токены
        txs = await EtherscanClient.get_token_transfers(
            self.session, address,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="to"
        )
        for tx in txs:
            if tx['contractAddress'].lower() == WETH_ADDRESS.lower():
                continue
            if tx['from'].lower() in [r.lower() for r in DEX_ROUTERS]:
                buys.append({
                    'token_address': tx['contractAddress'].lower(),
                    'tx_hash': tx['hash'],
                    'block_number': int(tx['blockNumber'])
                })
        # Сохраняем в кэш, что адрес проверен (с текущим конечным блоком или start_block?)
        # Будем сохранять start_block, т.к. он начало периода
        set_visited_address_cache(address, self.start_block)
        return buys
