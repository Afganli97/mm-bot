"""
Основной алгоритм обхода адресов и поиска покупок.
Покупка определяется как получение любого токена (кроме WETH) в блоке,
в котором адрес отправил ETH или WETH.
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

            # Получаем блок 30 дней назад
            now_ts = int(time.time())
            thirty_days_ago_ts = now_ts - LOOKBACK_DAYS * 86400
            self.start_block = await EtherscanClient.get_block_by_timestamp(self.session, thirty_days_ago_ts)
            logger.info(f"Период анализа: блоки {self.start_block} - текущий")

            # BFS
            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < MAX_ADDRESSES_PER_TASK:
                addr, depth = queue.popleft()
                logger.debug(f"Обработка адреса {addr} (глубина {depth}, всего обработано {self.total_addresses})")

                # Получаем исходящие переводы ETH/WETH
                try:
                    transfers = await self._get_outgoing_transfers(addr)
                except Exception as e:
                    logger.error(f"Ошибка получения переводов для {addr}: {e}", exc_info=True)
                    continue

                # Собираем блоки, в которых были исходящие переводы (для определения покупок)
                outgoing_blocks = set()
                for t in transfers:
                    # В транзакциях может не быть blockNumber, но они всегда есть в ответе Etherscan
                    if 'blockNumber' in t:
                        outgoing_blocks.add(int(t['blockNumber']))

                recipients = self._aggregate_recipients(transfers)
                sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:MAX_BRANCHES_PER_ADDRESS]
                logger.debug(f"Для {addr} отобрано {len(sorted_recs)} получателей")

                # Анализируем покупки самого addr (исходный адрес или получатель?)
                # Важно: мы ищем покупки как для текущего addr, так и для получателей.
                # Сначала проверим покупки самого addr (он мог купить токены, а потом перевести ETH дальше)
                if outgoing_blocks:
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

                # Теперь для каждого получателя ищем покупки
                for to_addr, _ in sorted_recs:
                    # Получатели тоже могли покупать токены — проверим после получения ими средств
                    # Но для простоты ищем покупки у всех получателей без анализа исходящих блоков,
                    # потому что мы не знаем их историю переводов. Используем тот же принцип:
                    # любое входящее поступление токена (кроме WETH) считается потенциальной покупкой.
                    # Но чтобы избежать ложных срабатываний, мы можем просто искать входящие токены
                    # и считать их покупками (это агрессивный подход). Для чистоты можно применить
                    # ту же логику, что и для addr: получить их исходящие переводы, но это увеличит число запросов.
                    # Пока оставим упрощённый вариант: ищем входящие токены без привязки к исходящим блокам.
                    try:
                        buys = await self._find_buys_any(to_addr)
                        logger.debug(f"У {to_addr} найдено {len(buys)} входящих токенов (потенциальные покупки)")
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
                    except Exception as e:
                        logger.error(f"Ошибка при поиске покупок для получателя {to_addr}: {e}", exc_info=True)

                    # Добавляем в очередь, если глубина позволяет и адрес новый
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

    async def _get_outgoing_transfers(self, address: str) -> List[Dict]:
        eth_txs = await EtherscanClient.get_internal_transactions(
            self.session, address, self.start_block, self.end_block
        )
        weth_txs = await EtherscanClient.get_token_transfers(
            self.session, address, contract_address=WETH_ADDRESS,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="from"
        )
        transfers = []
        for tx in eth_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value']),
                'blockNumber': tx['blockNumber']
            })
        for tx in weth_txs:
            transfers.append({
                'to': tx['to'].lower(),
                'value_wei': int(tx['value']),
                'blockNumber': tx['blockNumber']
            })
        logger.debug(f"Всего исходящих переводов (ETH+WETH) для {address}: {len(transfers)}")
        return transfers

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
        Поиск покупок: все входящие токены (не WETH) в тех же блоках, где адрес отправлял ETH/WETH.
        """
        if get_visited_address_cache(address, self.start_block):
            logger.debug(f"Адрес {address} уже проверялся после блока {self.start_block}, пропускаем запрос покупок")
            return []

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
        set_visited_address_cache(address, self.start_block)
        return buys

    async def _find_buys_any(self, address: str) -> List[Dict]:
        """
        Поиск любых входящих токенов (кроме WETH) — для получателей, у которых мы не знаем исходящие блоки.
        Считаем, что любое поступление токена после получения ETH — потенциальная покупка.
        """
        if get_visited_address_cache(address, self.start_block):
            logger.debug(f"Адрес {address} уже проверялся после блока {self.start_block}, пропускаем запрос покупок")
            return []

        txs = await EtherscanClient.get_token_transfers(
            self.session, address,
            start_block=self.start_block, end_block=self.end_block,
            filter_by="to"
        )
        buys = []
        for tx in txs:
            if tx['contractAddress'].lower() == WETH_ADDRESS.lower():
                continue
            # Принимаем все входящие токены как покупки
            buys.append({
                'token_address': tx['contractAddress'].lower(),
                'tx_hash': tx['hash'],
                'block_number': int(tx['blockNumber'])
            })
        set_visited_address_cache(address, self.start_block)
        return buys