"""
Основной алгоритм обхода адресов и поиска покупок (EVM).
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
from bot.blacklist import is_blacklisted
from bot.api_clients import TokenInfoService, EVMWeb3Client, EVMExplorerClient

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
        self.is_rpc = hasattr(network, 'web3') and isinstance(network.web3, EVMWeb3Client)

    async def run(self) -> List[Dict]:
        try:
            logger.info(f"Начало обхода {self.network.name} для {self.start_address} | Глубина: {self.max_depth}, Дней: {self.lookback_days}")
            self.request_id = create_request(0, 0, self.start_address, self.max_depth)

            if not self.is_rpc:
                now_ts = int(time.time())
                days_ago_ts = now_ts - self.lookback_days * 86400
                self.start_block = await self.network.explorer.get_block_by_timestamp(self.session, days_ago_ts)
                logger.info(f"Период анализа: блоки {self.start_block} - текущий")
            else:
                days_ago_ts = int(time.time()) - self.lookback_days * 86400
                self.start_block = await self.network.web3.get_block_by_timestamp_approx(self.session, days_ago_ts)
                self.end_block = await self.network.web3.get_current_block(self.session)
                logger.info(f"RPC: приблизительный период блоков {self.start_block} - {self.end_block}")

            queue = deque([(self.start_address, 0)])
            self.visited.add(self.start_address)
            self.total_addresses = 1

            while queue and self.total_addresses < 2000 and len(self.unique_token_addresses) < self.max_tokens:
                addr, depth = queue.popleft()
                logger.debug(f"Обработка адреса {addr} (глубина {depth})")

                try:
                    if self.is_rpc:
                        txs = await self._get_incoming_tokens_rpc(addr)
                        for tx in txs:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break
                            token = tx['token_address'].lower()
                            if token in self.unique_token_addresses or is_excluded(token):
                                continue
                            symbol = await TokenInfoService.get_symbol(self.session, token, self.network.config["rpc_url"])
                            add_found_token(self.request_id, token, symbol, addr, tx['tx_hash'], tx['block_number'])
                            self.found_tokens.append({'token': token, 'symbol': symbol, 'buyer': addr, 'tx': tx['tx_hash']})
                            self.unique_token_addresses.add(token)
                    else:
                        buys = await self._find_buys_eth(addr)
                        for buy in buys:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break
                            token = buy['token_address']
                            if token in self.unique_token_addresses or is_excluded(token):
                                continue
                            symbol = await TokenInfoService.get_symbol(self.session, token, self.network.config["rpc_url"])
                            add_found_token(self.request_id, token, symbol, addr, buy['tx_hash'], buy['block_number'])
                            self.found_tokens.append({'token': token, 'symbol': symbol, 'buyer': addr, 'tx': buy['tx_hash']})
                            self.unique_token_addresses.add(token)
                except Exception as e:
                    logger.error(f"Ошибка при поиске покупок для {addr}: {e}", exc_info=True)

                if len(self.unique_token_addresses) >= self.max_tokens:
                    self.token_limit_reached = True
                    break

                # Обход получателей (связанные кошельки)
                if not self.is_rpc and depth + 1 <= self.max_depth:
                    try:
                        transfers, _ = await self._get_outgoing_transfers_and_blocks(addr)
                        recipients = self._aggregate_recipients(transfers)
                        sorted_recs = sorted(recipients.items(), key=lambda x: x[1], reverse=True)[:50]
                        
                        for to_addr, _ in sorted_recs:
                            if len(self.unique_token_addresses) >= self.max_tokens:
                                break
                            
                            # ЗАЩИТА: Проверка адреса по черному списку бирж и мостов!
                            if is_blacklisted(to_addr, is_solana=False):
                                logger.debug(f"Адрес {to_addr} проигнорирован (в черном списке CEX/Мосты)")
                                continue

                            if to_addr not in self.visited:
                                self.visited.add(to_addr)
                                queue.append((to_addr, depth + 1))
                                self.total_addresses += 1
                                update_task_progress(self.request_id, self.total_addresses)
                    except Exception as e:
                        logger.error(f"Ошибка при анализе получателей для {addr}: {e}")

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
        return transfers, blocks

    def _aggregate_recipients(self, transfers: List[Dict]) -> Dict[str, int]:
        """Агрегирует переводы по адресам. Игнорирует только строго нулевые переводы."""
        agg = {}
        for t in transfers:
            to = t['to']
            agg[to] = agg.get(to, 0) + t['value_wei']
        # Отсеиваем только нулевые спам-транзакции. Лимиты цены убраны.
        return {addr: val for addr, val in agg.items() if val > 0}

    async def _find_buys_eth(self, address: str) -> List[Dict]:
        if get_visited_address_cache(address, self.start_block):
            return []
        txs = await self.network.explorer.get_token_transfers(self.session, address, start_block=self.start_block, end_block=self.end_block, filter_by="to")
        buys = []
        for tx in txs:
            if tx['contractAddress'].lower() == self.network.config["weth"].lower():
                continue
            buys.append({'token_address': tx['contractAddress'].lower(), 'tx_hash': tx['hash'], 'block_number': int(tx['blockNumber'])})
        set_visited_address_cache(address, self.start_block)
        return buys

    async def _get_incoming_tokens_rpc(self, address: str) -> List[Dict]:
        return await self.network.web3.get_token_transfers(self.session, address, direction="to",
                                                           from_block=self.start_block, to_block=self.end_block)
