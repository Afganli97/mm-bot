"""
Основной алгоритм обхода адресов и поиска покупок.
Покупка определяется как получение любого токена (кроме нативного) в блоке,
в котором адрес отправил нативный токен или WETH/WBNB.
"""
import asyncio
import logging
import time
from collections import deque
from typing import List, Dict, Set, Optional
import aiohttp

from bot.database import create_request, update_request_status, add_found_token
from bot.database import get_visited_address_cache, set_visited_address_cache, update_task_progress
from bot.token_filter import is_excluded, get_token_symbol
from bot.api_clients import TokenInfoService

logger = logging.getLogger(__name__)

class GraphTraversal:
    def __init__(self, session: aiohttp.ClientSession, start_address: str, network,
                 max_tokens: int = 100, lookback_days: int = 30, max_depth: int = 3):
        self.session = session
        self.start_address = start_address.lower()
        self.network = network
        self.max_tokens = max_tokens
        self.lookback_days = lookback_days
        self.max_depth = max_depth
        self.request_id = None
        self.start_block = 0
        self.end_block = 99999999
        self.visited: Set[str] = set()
        self.total_addresses = 0
        self.found_tokens = []
        self.unique_token_addresses = set()
        self.token_limit_reached = False

    async def run(self) -> List[Dict]:
        try:
            logger.info(f"Начало обхода сети {self.network.name} для адреса {self.start_address}")
            self.request_id = create_request(0, 0, self.start_address, self.max_depth)  # user_id/chat_id не важны в этом контексте

            now_ts = int(time.time())
            thirty_days_ago_ts = now_ts - self.lookback_days * 86400
            self.start_block = await self.network.explorer.get_block_by_timestamp(self.session, thirty_days_ago_ts)
            logger.info(f"Период анализа: блоки {self.start_block} - текущий")

            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < 2000 and len(self.unique_token_addresses) < self.max_tokens:
                addr, depth = queue.popleft()
                logger.debug(f"Обработка адреса {addr} (глубина {depth}, всего обработано {self.total_addresses})")

                try:
                    transfers, outgoing_blocks = await self._get_outgoing_transfers_and_blocks(addr)
                except Exception as e:
                    logger.error(f"Ошибка получения переводов для {addr}: {e}", exc_info=True)
                    continue

                if outgoing_blocks and not get_visited_address_cache(addr, self.start_block):
                    try:
                        buys = await self._find_buys(addr, outgoing_blocks)
                        for buy in buys:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break
                            if not is_excluded(buy['token_address']):
                                if buy['token_address'] not in self.unique_token_addresses:
                                    symbol = await TokenInfoService.get_symbol(self.session, buy['token_address'], self.network.rpc_url)
                                    add_found_token(self.request_id, buy['token_address'], symbol, addr, buy['tx_hash'], buy['block_number'])
                                    self.found_tokens.append({'token': buy['token_address'], 'symbol': symbol, 'buyer': addr, 'tx': buy['tx_hash']})
                                    self.unique_token_addresses.add(buy['token_address'])
                                    logger.info(f"Найден токен: {buy['token_address']} ({symbol}) у покупателя {addr}")
                    except Exception as e:
                        logger.error(f"Ошибка при поиске покупок для {addr}: {e}", exc_info=True)

                if len(self.unique_token_addresses) >= self.max_tokens:
                    self.token_limit_reached = True
                    break

                recipients = self._aggregate_recipients(transfers)
                sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:50]
                logger.debug(f"Для {addr} отобрано {len(sorted_recs)} получателей")

                for to_addr, _ in sorted_recs:
                    if len(self.unique_token_addresses) >= self.max_tokens:
                        self.token_limit_reached = True
                        break
                    if not get_visited_address_cache(to_addr, self.start_block):
                        try:
                            _, recv_blocks = await self._get_outgoing_transfers_and_blocks(to_addr)
                            if recv_blocks:
                                buys = await self._find_buys(to_addr, recv_blocks)
                                for buy in buys:
                                    if len(self.unique_token_addresses) >= self.max_tokens:
                                        break
                                    if not is_excluded(buy['token_address']):
                                        if buy['token_address'] not in self.unique_token_addresses:
                                            symbol = await TokenInfoService.get_symbol(self.session, buy['token_address'], self.network.rpc_url)
                                            add_found_token(self.request_id, buy['token_address'], symbol, to_addr, buy['tx_hash'], buy['block_number'])
                                            self.found_tokens.append({'token': buy['token_address'], 'symbol': symbol, 'buyer': to_addr, 'tx': buy['tx_hash']})
                                            self.unique_token_addresses.add(buy['token_address'])
                                            logger.info(f"Найден токен: {buy['token_address']} ({symbol}) у получателя {to_addr}")
                        except Exception as e:
                            logger.error(f"Ошибка при анализе получателя {to_addr}: {e}", exc_info=True)

                    if depth + 1 < self.max_depth and to_addr not in self.visited and len(self.unique_token_addresses) < self.max_tokens:
                        self.visited.add(to_addr)
                        queue.append((to_addr, depth + 1))
                        self.total_addresses += 1
                        update_task_progress(self.request_id, self.total_addresses)

                if len(self.unique_token_addresses) >= self.max_tokens:
                    self.token_limit_reached = True
                    break

            update_request_status(self.request_id, 'done', finished=True)
            logger.info(f"Обход завершён. Проверено адресов: {self.total_addresses}, найдено токенов: {len(self.found_tokens)}")
            return self.found_tokens

        except Exception as e:
            logger.exception("Критическая ошибка во время обхода")
            if self.request_id:
                update_request_status(self.request_id, 'error', str(e), finished=True)
            raise

    async def _get_outgoing_transfers_and_blocks(self, address: str) -> (List[Dict], Set[int]):
        normal_txs = await self.network.explorer.get_normal_transactions(self.session, address, self.start_block, self.end_block)
        internal_txs = await self.network.explorer.get_internal_transactions(self.session, address, self.start_block, self.end_block)
        weth_txs = await self.network.explorer.get_token_transfers(self.session, address, contract_address=self.network.config["weth"], start_block=self.start_block, end_block=self.end_block, filter_by="from")

        transfers = []
        blocks = set()
        for tx in normal_txs:
            transfers.append({'to': tx['to'].lower(), 'value_wei': int(tx['value']), 'blockNumber': tx['blockNumber']})
            blocks.add(int(tx['blockNumber']))
        for tx in internal_txs:
            transfers.append({'to': tx['to'].lower(), 'value_wei': int(tx['value']), 'blockNumber': tx['blockNumber']})
            blocks.add(int(tx['blockNumber']))
        for tx in weth_txs:
            transfers.append({'to': tx['to'].lower(), 'value_wei': int(tx['value']), 'blockNumber': tx['blockNumber']})
            blocks.add(int(tx['blockNumber']))

        logger.debug(f"Адрес {address}: {len(transfers)} исходящих переводов в {len(blocks)} блоках")
        return transfers, blocks

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        agg = {}
        for t in transfers:
            to = t['to']
            agg[to] = agg.get(to, 0) + t['value_wei']
        min_wei = int(self.network.config["min_transfer_value_native"] * 10**18)
        filtered = {addr: val for addr, val in agg.items() if val >= min_wei}
        logger.debug(f"После фильтрации (минимум {self.network.config['min_transfer_value_native']} {self.network.native_symbol}) осталось {len(filtered)} получателей")
        return filtered

    async def _find_buys(self, address: str, outgoing_blocks: Set[int]) -> List[Dict]:
        txs = await self.network.explorer.get_token_transfers(self.session, address, start_block=self.start_block, end_block=self.end_block, filter_by="to")
        buys = []
        for tx in txs:
            if tx['contractAddress'].lower() == self.network.config["weth"].lower():
                continue
            if int(tx['blockNumber']) in outgoing_blocks:
                buys.append({'token_address': tx['contractAddress'].lower(), 'tx_hash': tx['hash'], 'block_number': int(tx['blockNumber'])})
        set_visited_address_cache(address, self.start_block)
        return buys