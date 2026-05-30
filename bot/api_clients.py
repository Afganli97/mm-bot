"""
Асинхронные клиенты для Etherscan, Alchemy, Infura, Coingecko.
Управляют лимитами, ротацией ключей, кэшированием.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS, ALCHEMY_URL, INFURA_URL,
    WETH_ADDRESS, DEX_ROUTERS, LOOKBACK_DAYS,
    MIN_TRANSFER_VALUE_ETH, MAX_BRANCHES_PER_ADDRESS
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
ALCHEMY_DAILY_LIMIT_CU = 300_000_000
INFURA_DAILY_LIMIT = 100_000
COINGECKO_LIMIT_PER_MINUTE = 10

class APIKeyRotator:
    """Ротация ключей с учётом использования."""
    def __init__(self, keys: List[str], service: str, daily_limit: int):
        self.keys = keys
        self.service = service
        self.daily_limit = daily_limit

    def _reset_old_if_needed(self, key_index: int):
        from datetime import date
        from bot.database import get_connection
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM api_usage WHERE service=? AND key_index=? AND usage_date != ?",
                (self.service, key_index, today)
            )
            conn.commit()
            logger.debug(f"Сброшены старые счётчики для {self.service} ключ {key_index}")

    def get_available_key(self) -> Optional[tuple]:
        for i, key in enumerate(self.keys):
            self._reset_old_if_needed(i)
            used = get_api_usage_today(self.service, i)
            logger.debug(f"{self.service} ключ {i}: использовано {used}/{self.daily_limit}")
            if used < self.daily_limit:
                return key, i
        return None

    async def make_request(self, session: aiohttp.ClientSession, url: str, params: dict = None) -> dict:
        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                logger.error(f"Все ключи сервиса {self.service} исчерпаны на сегодня")
                raise Exception(f"Дневной лимит API {self.service} исчерпан")
            key, idx = key_info

            if self.service == "etherscan":
                params = params or {}
                params["apikey"] = key
                params["chainid"] = 1

            logger.debug(f"Запрос к {self.service} (ключ {idx}): URL={url}, params={params}")
            try:
                async with session.get(url, params=params, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.debug(f"Ответ {self.service}: {data}")

                        if self.service == "etherscan":
                            # Пустой ответ — не ошибка
                            if data.get("message") == "No transactions found" or data.get("message") == "No records found":
                                increment_api_usage(self.service, idx)
                                return {"status": "1", "message": "OK", "result": []}
                            if data.get("status") == "1" or data.get("message") == "OK":
                                increment_api_usage(self.service, idx)
                                return data
                            elif data.get("message") == "NOTOK" and "limit" in data.get("result", "").lower():
                                logger.warning(f"Etherscan ключ {idx} исчерпал лимит")
                                continue
                            else:
                                logger.error(f"Etherscan ошибка: {data}")
                                raise Exception(f"Etherscan: {data.get('result', 'Неизвестная ошибка')}")
                        else:
                            increment_api_usage(self.service, idx)
                            return data
                    elif resp.status == 429:
                        logger.warning(f"429 от {self.service}, пробуем следующий ключ")
                        await asyncio.sleep(1)
                        continue
                    else:
                        text = await resp.text()
                        logger.error(f"HTTP {resp.status} от {self.service}: {text}")
                        raise Exception(f"HTTP {resp.status} от {self.service}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при запросе к {self.service}: {e}")
                raise
        raise Exception("Все попытки запроса исчерпаны")

etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)

class EtherscanClient:
    BASE_URL = "https://api.etherscan.io/v2/api"

    @staticmethod
    async def get_block_by_timestamp(session: aiohttp.ClientSession, timestamp: int) -> int:
        logger.info(f"Получение блока по timestamp {timestamp}")
        params = {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": timestamp,
            "closest": "before"
        }
        data = await etherscan_rotator.make_request(session, EtherscanClient.BASE_URL, params)
        block = int(data["result"])
        logger.info(f"Блок 30-дневной давности: {block}")
        return block

    @staticmethod
    async def get_normal_transactions(session: aiohttp.ClientSession, address: str,
                                      start_block: int, end_block: int) -> List[Dict]:
        """Обычные транзакции (не внутренние). Ищем исходящие переводы ETH."""
        logger.debug(f"Запрос обычных транзакций для {address} с блока {start_block} по {end_block}")
        all_txs = []
        page = 1
        while True:
            params = {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": start_block,
                "endblock": end_block,
                "page": page,
                "offset": 1000,
                "sort": "asc"
            }
            data = await etherscan_rotator.make_request(session, EtherscanClient.BASE_URL, params)
            txs = data.get("result", [])
            if not txs:
                break
            all_txs.extend(txs)
            if len(txs) < 1000:
                break
            page += 1
        # Фильтруем исходящие успешные с ненулевой value
        filtered = []
        for tx in all_txs:
            if (tx["from"].lower() == address.lower() and
                int(tx.get("isError", "0")) == 0 and
                int(tx["value"]) > 0):
                filtered.append(tx)
        logger.debug(f"Найдено {len(filtered)} исходящих обычных ETH-переводов для {address}")
        return filtered

    @staticmethod
    async def get_internal_transactions(session: aiohttp.ClientSession, address: str,
                                        start_block: int, end_block: int) -> List[Dict]:
        logger.debug(f"Запрос внутренних транзакций для {address} с блока {start_block} по {end_block}")
        all_txs = []
        page = 1
        while True:
            params = {
                "module": "account",
                "action": "txlistinternal",
                "address": address,
                "startblock": start_block,
                "endblock": end_block,
                "page": page,
                "offset": 1000,
                "sort": "asc"
            }
            data = await etherscan_rotator.make_request(session, EtherscanClient.BASE_URL, params)
            txs = data.get("result", [])
            if not txs:
                break
            all_txs.extend(txs)
            if len(txs) < 1000:
                break
            page += 1
        filtered = []
        for tx in all_txs:
            if (tx["from"].lower() == address.lower() and
                int(tx["isError"]) == 0 and
                int(tx["value"]) > 0):
                filtered.append(tx)
        logger.debug(f"Найдено {len(filtered)} исходящих внутренних ETH-переводов для {address}")
        return filtered

    @staticmethod
    async def get_token_transfers(session: aiohttp.ClientSession, address: str,
                                  contract_address: str = None,
                                  start_block: int = 0, end_block: int = 99999999,
                                  filter_by: str = None) -> List[Dict]:
        logger.debug(f"Запрос токен-транзакций для {address}, контракт={contract_address}, "
                     f"блоки {start_block}-{end_block}, фильтр={filter_by}")
        all_txs = []
        page = 1
        while True:
            params = {
                "module": "account",
                "action": "tokentx",
                "address": address,
                "startblock": start_block,
                "endblock": end_block,
                "page": page,
                "offset": 1000,
                "sort": "asc"
            }
            if contract_address:
                params["contractaddress"] = contract_address
            data = await etherscan_rotator.make_request(session, EtherscanClient.BASE_URL, params)
            txs = data.get("result", [])
            if not txs:
                break
            all_txs.extend(txs)
            if len(txs) < 1000:
                break
            page += 1
        if filter_by:
            all_txs = [tx for tx in all_txs if tx[filter_by].lower() == address.lower()]
        logger.debug(f"Найдено {len(all_txs)} токен-транзакций для {address} (фильтр: {filter_by})")
        return all_txs

class AlchemyClient:
    @staticmethod
    async def get_logs(session: aiohttp.ClientSession, params: dict) -> dict:
        logger.debug(f"Alchemy getLogs: {params}")
        url = ALCHEMY_URL
        async with session.post(url, json=params, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.debug(f"Alchemy ответ: {len(data.get('result', []))} логов")
                increment_api_usage("alchemy", 0)
                return data
            else:
                text = await resp.text()
                logger.error(f"Alchemy HTTP {resp.status}: {text}")
                raise Exception(f"Alchemy HTTP {resp.status}")

class InfuraClient:
    @staticmethod
    async def get_logs(session: aiohttp.ClientSession, params: dict) -> dict:
        logger.debug(f"Infura getLogs: {params}")
        url = INFURA_URL
        async with session.post(url, json=params, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.debug(f"Infura ответ: {len(data.get('result', []))} логов")
                increment_api_usage("infura", 0)
                return data
            else:
                text = await resp.text()
                logger.error(f"Infura HTTP {resp.status}: {text}")
                raise Exception(f"Infura HTTP {resp.status}")

class CoingeckoClient:
    BASE_URL = "https://api.coingecko.com/api/v3"

    @staticmethod
    async def get_top_100(session: aiohttp.ClientSession) -> List[Dict]:
        from bot.database import get_connection
        from datetime import timedelta
        import json
        logger.info("Загрузка топ-100 токенов с CoinGecko...")
        with get_connection() as conn:
            row = conn.execute("SELECT tokens_json, updated_at FROM top_tokens_cache WHERE id=1").fetchone()
            if row:
                updated = datetime.fromisoformat(row['updated_at'])
                if (datetime.utcnow() - updated) < timedelta(hours=1):
                    logger.debug("Использован кэш топ-100 токенов")
                    return json.loads(row['tokens_json'])

        url = f"{CoingeckoClient.BASE_URL}/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 100,
            "page": 1
        }
        async with session.get(url, params=params, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = []
                for coin in data:
                    eth_addr = coin.get("platforms", {}).get("ethereum", "")
                    if eth_addr:
                        tokens.append({
                            "id": coin["id"],
                            "symbol": coin["symbol"].upper(),
                            "address": eth_addr.lower()
                        })
                logger.info(f"Получено {len(tokens)} токенов из топ-100")
                with get_connection() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO top_tokens_cache (id, updated_at, tokens_json) VALUES (1, ?, ?)",
                        (datetime.utcnow().isoformat(), json.dumps(tokens))
                    )
                    conn.commit()
                return tokens
            else:
                text = await resp.text()
                logger.error(f"CoinGecko HTTP {resp.status}: {text}")
                raise Exception("Не удалось получить топ-100 с CoinGecko")