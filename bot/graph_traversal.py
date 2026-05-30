"""
Основной алгоритм обхода адресов и поиска покупок.
Покупка = получение токена (не WETH) в блоке, где этот же адрес отправил ETH/WETH.
Для каждого адреса загружаются его исходящие ETH/WETH, затем входящие токены в тех же блоках.
"""
import asyncio
import logging
import time
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
        self.end_block = 99999999
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens = []

    async def run(self) -> List[Dict]:
        try:
            logger.info(f"Начало анализа для адреса {self.start_address}")
            self.request_id = create_request(self.user_id, self.chat_id, self.start_address, MAX_DEPTH)

            now_ts = int(time.time())
            thirty_days_ago_ts = now_ts - LOOKBACK_DAYS * 86400
            self.start_block = await EtherscanClient.get_block_by_timestamp(self.session, thirty_days_ago_ts)
            logger.info(f"Период анализа: блоки {self.start_block} - текущий")

            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < MAX_ADDRESSES_PER_TASK:
                addr, depth = queue.popleft()
                logger.debug(f"Обработка адреса {addr} (глубина {depth}, всего обработано {self.total_addresses})")

                # 1. Получаем исходящие переводы (обычные + внутренние + WETH) текущего адреса
                try:
                    transfers, outgoing_blocks = await self._get_outgoing_transfers_and_blocks(addr)
                except Exception as e:
                    logger.error(f"Ошибка получения переводов для {addr}: {e}", exc_info=True)
                    continue

                # 2. Анализируем покупки самого addr (если ещё не проверяли)
                if outgoing_blocks and not get_visited_address_cache(addr, self.start_block):
                    try:
                        buys = await self._find_buys(addr, outgoing_blocks)
                        logger.debug(f"У {addr} найдено {len(buys)} покупок (как отправитель)")
                        for buy in buys:
                            if not is_excluded(buy['token_address']):
                                symbol = get_token_symbol(buy['token_address'])
                                add_found_token(self.request_id, buy['token_address'], symbol,
                                                addr, buy['tx_hash'], buy['block_number'])
                                self.found_tokens.append({
                                    'token': buy['token_address'],
                                    'symbol': symbol,
                                    'buyer': addr,
                                    'tx': buy['tx_hash']
                                })
                                logger.info(f"Найден токен: {buy['token_address']} ({symbol}) у покупателя {addr}")
                    except Exception as e:
                        logger.error(f"Ошибка при поиске покупок для {addr}: {e}", exc_info=True)
                else:
                    logger.debug(f"Адрес {addr} уже проверялся или нет исходящих блоков, пропускаем поиск покупок")

                # 3. Агрегируем получателей и фильтруем
                recipients = self._aggregate_recipients(transfers)
                sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:MAX_BRANCHES_PER_ADDRESS]
                logger.debug(f"Для {addr} отобрано {len(sorted_recs)} получателей")

                # 4. Для каждого получателя также загружаем его исходящие переводы и ищем покупки
                for to_addr, _ in sorted_recs:
                    if not get_visited_address_cache(to_addr, self.start_block):
                        # Получатель ещё не анализировался – загрузим его исходящие переводы
                        try:
                            _, recv_blocks = await self._get_outgoing_transfers_and_blocks(to_addr)
                            if recv_blocks:
                                buys = await self._find_buys(to_addr, recv_blocks)
                                logger.debug(f"У получателя {to_addr} найдено {len(buys)} покупок")
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
                                        logger.info(f"Найден токен: {buy['token_address']} ({symbol}) у получателя {to_addr}")
                            else:
                                logger.debug(f"У получателя {to_addr} нет исходящих переводов ETH/WETH, покупки не ищем")
                        except Exception as e:
                            logger.error(f"Ошибка при анализе получателя {to_addr}: {e}", exc_info=True)
                    else:
                        logger.debug(f"Получатель {to_addr} уже проверен ранее, пропускаем анализ покупок")

                    # 5. Добавляем получателя в очередь обхода (если не посещён)
                    if depth + 1 < MAX_DEPTH and to_addr not in self.visited:
                        self.visited.add(to_addr)
                        queue.append((to_addr, depth + 1))
                        self.total_addresses += 1
                        update_task_progress(self.request_id, self.total_addresses)

            update_request_status(self.request_id, 'done', finished=True)
            logger.info(f"Анализ завершён. Проверено адресов: {self.total_addresses}, найдено токенов: {len(self.found_tokens)}")
            return self.found_tokens

        except Exception as e:
            logger.exception("Критическая ошибка во время обхода")
            if self.request_id:
                update_request_status(self.request_id, 'error', str(e), finished=True)
            raise

    async def _get_outgoing_transfers_and_blocks(self, address: str) -> (List[Dict], Set[int]):
        """
        Возвращает список исходящих переводов (для агрегации получателей)
        и множество номеров блоков, в которых были исходящие переводы (для определения покупок).
        """
        # Обычные транзакции
        normal_txs = await EtherscanClient.get_normal_transactions(
            self.session, address, self.start_block, self.end_block
        )
        # Внутренние транзакции
        internal_txs = await EtherscanClient.get_internal_transactions(
            self.session, address, self.start_block, self.end_block
        )
        # WETH переводы
        weth_txs = await EtherscanClient.get_token_transfers(
            self.session, address, contract_address=WETH_ADDRESS,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="from"
        )

        transfers = []
        blocks = set()

        for tx in normal_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value']),
                'blockNumber': tx['blockNumber']
            })
            blocks.add(int(tx['blockNumber']))

        for tx in internal_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value']),
                'blockNumber': tx['blockNumber']
            })
            blocks.add(int(tx['blockNumber']))

        for tx in weth_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value']),
                'blockNumber': tx['blockNumber']
            })
            blocks.add(int(tx['blockNumber']))

        logger.debug(f"Адрес {address}: {len(transfers)} исходящих переводов в {len(blocks)} блоках")
        return transfers, blocks

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        agg = {}
        for t in transfers:
            to = t['to']
            agg[to] = agg.get(to, 0) + t['value_wei']
        min_wei = int(MIN_TRANSFER_VALUE_ETH * 10**18)
        filtered = {addr: val for addr, val in agg.items() if val >= min_wei}
        logger.debug(f"После фильтрации (минимум {MIN_TRANSFER_VALUE_ETH} ETH) осталось {len(filtered)} получателей")
        return filtered

    async def _find_buys(self, address: str, outgoing_blocks: Set[int]) -> List[Dict]:
        """
        Ищет входящие токены (не WETH) в блоках, где адрес отправлял ETH/WETH.
        """
        txs = await EtherscanClient.get_token_transfers(
            self.session, address,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="to"
        )
        buys = []
        for tx in txs:
            if tx['contractAddress'].lower() == WETH_ADDRESS.lower():
                continue
            if int(tx['blockNumber']) in outgoing_blocks:
                buys.append({
                    'token_address': tx['contractAddress'].lower(),
                    'tx_hash': tx['hash'],
                    'block_number': int(tx['blockNumber'])
                })
        # Помечаем адрес как проверенный (сохраняем start_block в кэш)
        set_visited_address_cache(address, self.start_block)
        return buys