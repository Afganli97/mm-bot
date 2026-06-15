"""
Основной алгоритм обхода адресов и поиска покупок (Мультичейн EVM).
Алгоритм абстрагирован: он получает стандартизированные списки от интерфейса BaseNetwork.
"""
import asyncio
import logging
import time
from collections import deque
from typing import List, Dict, Set

from bot.database import create_request, update_request_status, add_found_token
from bot.database import get_visited_address_cache, set_visited_address_cache, update_task_progress
from bot.token_filter import is_excluded
from bot.blacklist import is_blacklisted
from bot.api_clients import TokenInfoService

logger = logging.getLogger(__name__)

class GraphTraversal:
    def __init__(self, session, start_address: str, network, max_tokens: int = 100, lookback_days: int = 30, max_depth: int = 3):
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

    async def run(self) -> List[Dict]:
        try:
            logger.info(f"Начало обхода графа ({self.network.name}) для {self.start_address} | Глубина: {self.max_depth}")
            self.request_id = create_request(0, 0, self.start_address, self.max_depth)

            # 1. Определяем период блоков (Сеть сама решит, как это вычислить)
            days_ago_ts = int(time.time()) - (self.lookback_days * 86400)
            self.start_block = await self.network.get_block_by_timestamp(days_ago_ts)
            if hasattr(self.network, 'web3') and self.network.web3:
                self.end_block = await self.network.web3.get_current_block(self.session)
                
            logger.info(f"Период обхода: блоки {self.start_block} - {self.end_block}")

            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < 2000 and len(self.unique_token_addresses) < self.max_tokens:
                addr, depth = queue.popleft()
                logger.debug(f"Анализ связей адреса {addr} (Глубина {depth})")

                # 2. Ищем Входящие покупки (Скрытая реализация: ETH через V2, BSC через RPC)
                try:
                    if not get_visited_address_cache(addr, self.start_block):
                        buys = await self.network.get_incoming_buys(addr, self.start_block, self.end_block)
                        for buy in buys:
                            if len(self.unique_token_addresses) >= self.max_tokens: break
                            token = buy['token_address']
                            if token in self.unique_token_addresses or is_excluded(token): continue
                            
                            symbol = await TokenInfoService.get_symbol(self.session, token, self.network.rpc_url)
                            add_found_token(self.request_id, token, symbol, addr, buy.get('tx_hash', ''), buy.get('block_number', 0))
                            self.found_tokens.append({'token': token, 'symbol': symbol, 'buyer': addr})
                            self.unique_token_addresses.add(token)
                            
                        set_visited_address_cache(addr, self.start_block)
                except Exception as e:
                    logger.error(f"Ошибка при поиске покупок для {addr}: {e}")

                if len(self.unique_token_addresses) >= self.max_tokens: break

                # 3. Ищем Исходящие переводы для расширения Графа Связей
                if depth + 1 <= self.max_depth:
                    try:
                        transfers = await self.network.get_outgoing_transfers(addr, self.start_block, self.end_block)
                        recipients = self._aggregate_recipients(transfers)
                        sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:50]
                        
                        for to_addr, _ in sorted_recs:
                            if len(self.unique_token_addresses) >= self.max_tokens: break
                            
                            # Блокировка CEX и мостов!
                            if is_blacklisted(to_addr, is_solana=False):
                                continue

                            if to_addr not in self.visited:
                                self.visited.add(to_addr)
                                queue.append((to_addr, depth + 1))
                                self.total_addresses += 1
                                update_task_progress(self.request_id, self.total_addresses)
                    except Exception as e:
                        logger.error(f"Ошибка при анализе получателей для {addr}: {e}")

            update_request_status(self.request_id, 'done', finished=True)
            logger.info(f"Обход завершён. Проверено адресов: {self.total_addresses}, Найдено токенов: {len(self.found_tokens)}")
            return self.found_tokens

        except Exception as e:
            logger.exception("Критическая ошибка обхода графа")
            if self.request_id: update_request_status(self.request_id, 'error', str(e), finished=True)
            raise

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        agg = {}
        for t in transfers:
            to = t['to']
            agg[to] = agg.get(to, 0) + t.get('value_wei', 0)
        return {addr: val for addr, val in agg.items() if val > 0}
