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

# Лимиты запросов в сутки
ETHERSCAN_DAILY_LIMIT = 100_000
ALCHEMY_DAILY_LIMIT_CU = 300_000_000  # условно, будем считать 1 запрос = 1 CU
INFURA_DAILY_LIMIT = 100_000
COINGECKO_LIMIT_PER_MINUTE = 10

class APIKeyRotator:
    """Простая ротация ключей с подсчётом использования."""
    def __init__(self, keys: List[str], service: str, daily_limit: int):
        self.keys = keys
        self.service = service
        self.daily_limit = daily_limit
        # Сброс старых счётчиков перенесён в get_available_key, чтобы не требовать готовой БД при импорте

    def _reset_old_if_needed(self, key_index: int):
        """Удаляет устаревшие записи для указанного ключа (не сегодняшние)."""
        from datetime import date
        from bot.database import get_connection
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM api_usage WHERE service=? AND key_index=? AND usage_date != ?",
                (self.service, key_index, today)
            )
            conn.commit()

    def get_available_key(self) -> Optional[tuple]:
        """Возвращает (key, index) первый неисчерпанный ключ, или None."""
        for i, key in enumerate(self.keys):
            # Сбросим старые записи перед проверкой
            self._reset_old_if_needed(i)
            used = get_api_usage_today(self.service, i)
            if used < self.daily_limit:
                return key, i
        return None

    async def make_request(self, session: aiohttp.ClientSession, url: str, params: dict = None) -> dict:
        """Выполняет GET-запрос с учётом лимита и ротации."""
        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                logger.error(f"Все ключи сервиса {self.service} исчерпаны на сегодня")
                raise Exception(f"Дневной лимит API {self.service} исчерпан")
            key, idx = key_info
            # Для Etherscan ключ передаётся в params
            if self.service == "etherscan":
                params = params or {}
                params["apikey"] = key
                params["chainid"] = 1
            try:
                async with session.get(url, params=params, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if self.service == "etherscan":
                            logger.info(f"Etherscan raw response: {data}")
                            if data.get("status") == "1" or data.get("message") == "OK":
                                increment_api_usage(self.service, idx)
                                return data
                            elif data.get("message") == "NOTOK" and "limit" in data.get("result", ""):
                                logger.warning(f"Etherscan ключ {idx} достиг лимита")
                                continue
                            else:
                                logger.error(f"Etherscan ошибка: {data}")
                                raise Exception(data.get("message", "Ошибка Etherscan"))
                        else:
                            increment_api_usage(self.service, idx)
                            return data
                    elif resp.status == 429:
                        logger.warning(f"429 Too Many Requests, пробуем следующий ключ")
                        await asyncio.sleep(1)
                        continue
                    else:
                        text = await resp.text()
                        logger.error(f"HTTP {resp.status}: {text}")
                        raise Exception(f"HTTP {resp.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Ошибка сети: {e}")
                raise
        raise Exception("Все попытки запроса исчерпаны")

# Инициализация ротатора (теперь безопасно, БД не требуется)
etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)

class EtherscanClient:
    """Асинхронные методы для Etherscan API."""
    BASE_URL = "https://api.etherscan.io/v2/api"

    @staticmethod
    async def get_block_by_timestamp(session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": timestamp,
            "closest": "before"
        }
        data = await etherscan_rotator.make_request(session, EtherscanClient.BASE_URL, params)
        return int(data["result"])

    @staticmethod
    async def get_internal_transactions(session: aiohttp.ClientSession, address: str,
                                        start_block: int, end_block: int) -> List[Dict]:
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
        return filtered

    @staticmethod
    async def get_token_transfers(session: aiohttp.ClientSession, address: str,
                                  contract_address: str = None,
                                  start_block: int = 0, end_block: int = 99999999,
                                  filter_by: str = None) -> List[Dict]:
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
        return all_txs

class AlchemyClient:
    """Обёртка для Alchemy RPC."""
    @staticmethod
    async def get_logs(session: aiohttp.ClientSession, params: dict) -> dict:
        url = ALCHEMY_URL
        async with session.post(url, json=params, timeout=30) as resp:
            if resp.status == 200:
                increment_api_usage("alchemy", 0)
                return await resp.json()
            else:
                raise Exception(f"Alchemy HTTP {resp.status}")

class InfuraClient:
    """Резервный Infura RPC."""
    @staticmethod
    async def get_logs(session: aiohttp.ClientSession, params: dict) -> dict:
        url = INFURA_URL
        async with session.post(url, json=params, timeout=30) as resp:
            if resp.status == 200:
                increment_api_usage("infura", 0)
                return await resp.json()
            else:
                raise Exception(f"Infura HTTP {resp.status}")

class CoingeckoClient:
    """Получение списка топ-100 монет."""
    BASE_URL = "https://api.coingecko.com/api/v3"

    @staticmethod
    async def get_top_100(session: aiohttp.ClientSession) -> List[Dict]:
        from bot.database import get_connection
        from datetime import timedelta
        import json
        with get_connection() as conn:
            row = conn.execute("SELECT tokens_json, updated_at FROM top_tokens_cache WHERE id=1").fetchone()
            if row:
                updated = datetime.fromisoformat(row['updated_at'])
                if (datetime.utcnow() - updated) < timedelta(hours=1):
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
                with get_connection() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO top_tokens_cache (id, updated_at, tokens_json) VALUES (1, ?, ?)",
                        (datetime.utcnow().isoformat(), json.dumps(tokens))
                    )
                    conn.commit()
                return tokens
            else:
                logger.error(f"Coingecko HTTP {resp.status}")
                raise Exception("Не удалось получить топ-100 с Coingecko")
