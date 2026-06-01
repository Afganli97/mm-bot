"""
Асинхронные клиенты для Etherscan, BscScan, Solscan, RPC.
Управляют лимитами, ротацией ключей, кэшированием.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS, ALCHEMY_URL, INFURA_URL,
    SOLSCAN_API_KEY, HELIUS_URL
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
BSCSCAN_DAILY_LIMIT = 100_000
SOLSCAN_DAILY_LIMIT = 100_000

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

    async def make_request(self, session: aiohttp.ClientSession, url: str, params: dict = None,
                           headers: dict = None, delay: float = 0.4) -> dict:
        if self.service in ("etherscan", "bscscan"):
            await asyncio.sleep(delay)

        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                logger.error(f"Все ключи сервиса {self.service} исчерпаны на сегодня")
                raise Exception(f"Дневной лимит API {self.service} исчерпан")
            key, idx = key_info

            if self.service in ("etherscan", "bscscan"):
                params = params or {}
                params["apikey"] = key
                if self.service == "bscscan":
                    params["chainid"] = 56  # BSC
                else:
                    params["chainid"] = 1

            logger.debug(f"Запрос к {self.service} (ключ {idx}): URL={url}, params={params}")
            try:
                if self.service == "solscan":
                    headers = headers or {}
                    headers["Authorization"] = f"Bearer {key}"
                    async with session.get(url, headers=headers, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            increment_api_usage(self.service, idx)
                            return data
                        elif resp.status == 429:
                            logger.warning("429 от Solscan, пробуем следующий ключ")
                            continue
                        else:
                            raise Exception(f"Solscan HTTP {resp.status}")
                else:
                    async with session.get(url, params=params, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            logger.debug(f"Ответ {self.service}: {data}")
                            if self.service in ("etherscan", "bscscan"):
                                if data.get("message") == "No transactions found" or data.get("message") == "No records found":
                                    increment_api_usage(self.service, idx)
                                    return {"status": "1", "message": "OK", "result": []}
                                if data.get("status") == "1" or data.get("message") == "OK":
                                    increment_api_usage(self.service, idx)
                                    return data
                                elif data.get("message") == "NOTOK" and "limit" in data.get("result", "").lower():
                                    logger.warning(f"{self.service} ключ {idx} исчерпал лимит")
                                    continue
                                else:
                                    logger.error(f"{self.service} ошибка: {data}")
                                    raise Exception(f"{self.service}: {data.get('result', 'Неизвестная ошибка')}")
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

# Ротаторы для сервисов
etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)
bscscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "bscscan", BSCSCAN_DAILY_LIMIT)  # используем те же ключи
solscan_rotator = APIKeyRotator([SOLSCAN_API_KEY], "solscan", SOLSCAN_DAILY_LIMIT) if SOLSCAN_API_KEY else None

class EVMExplorerClient:
    """Общий клиент для Etherscan-подобных API (Ethereum, BSC)."""
    def __init__(self, base_url: str, rotator: APIKeyRotator, chain_id: int, weth_address: str, delay: float = 0.4):
        self.base_url = base_url
        self.rotator = rotator
        self.chain_id = chain_id
        self.weth_address = weth_address.lower()
        self.delay = delay

    async def get_block_by_timestamp(self, session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {"module": "block", "action": "getblocknobytime", "timestamp": timestamp, "closest": "before"}
        data = await self.rotator.make_request(session, self.base_url, params, delay=self.delay)
        return int(data["result"])

    async def get_normal_transactions(self, session: aiohttp.ClientSession, address: str,
                                      start_block: int, end_block: int) -> List[Dict]:
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "txlist", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.base_url, params, delay=self.delay)
            txs = data.get("result", [])
            if not txs:
                break
            all_txs.extend(txs)
            if len(txs) < 1000:
                break
            page += 1
        filtered = [tx for tx in all_txs if tx["from"].lower() == address.lower() and int(tx.get("isError", "0")) == 0 and int(tx["value"]) > 0]
        return filtered

    async def get_internal_transactions(self, session: aiohttp.ClientSession, address: str,
                                        start_block: int, end_block: int) -> List[Dict]:
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "txlistinternal", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.base_url, params, delay=self.delay)
            txs = data.get("result", [])
            if not txs:
                break
            all_txs.extend(txs)
            if len(txs) < 1000:
                break
            page += 1
        filtered = [tx for tx in all_txs if tx["from"].lower() == address.lower() and int(tx.get("isError", "0")) == 0 and int(tx["value"]) > 0]
        return filtered

    async def get_token_transfers(self, session: aiohttp.ClientSession, address: str,
                                  contract_address: str = None, start_block: int = 0, end_block: int = 99999999,
                                  filter_by: str = None) -> List[Dict]:
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "tokentx", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            if contract_address:
                params["contractaddress"] = contract_address
            data = await self.rotator.make_request(session, self.base_url, params, delay=self.delay)
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

    async def get_account_balance(self, session: aiohttp.ClientSession, address: str) -> float:
        params = {"module": "account", "action": "balance", "address": address, "tag": "latest"}
        data = await self.rotator.make_request(session, self.base_url, params, delay=self.delay)
        return int(data["result"]) / 10**18

class TokenInfoService:
    """Получение символа токена через RPC."""
    @staticmethod
    async def get_symbol(session: aiohttp.ClientSession, token_address: str, rpc_url: str) -> str:
        payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to": token_address, "data": "0x95d89b41"}, "latest"],"id":1}
        try:
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if 'result' in result and result['result'] != '0x':
                        hex_str = result['result'][2:]
                        try:
                            symbol = bytes.fromhex(hex_str).decode('utf-8').rstrip('\x00')
                            if symbol:
                                return symbol
                        except Exception:
                            pass
        except Exception:
            pass
        return "?"

class SolscanClient:
    """Клиент для Solscan API."""
    BASE_URL = "https://api.solscan.io/v1"

    def __init__(self):
        self.rotator = solscan_rotator

    async def get_token_balances(self, session: aiohttp.ClientSession, address: str) -> List[Dict]:
        if not self.rotator:
            return []
        url = f"{self.BASE_URL}/account/tokens?address={address}"
        data = await self.rotator.make_request(session, url, headers={}, delay=0.3)
        tokens = []
        if data.get("success") and data.get("data"):
            for item in data["data"]:
                tokens.append({
                    "address": item["tokenAddress"],
                    "symbol": item.get("tokenSymbol", "?"),
                    "balance": float(item["amount"]) / 10**item["decimals"],
                    "decimals": item["decimals"]
                })
        return tokens

    async def get_transactions(self, session: aiohttp.ClientSession, address: str, limit: int = 50) -> List[Dict]:
        if not self.rotator:
            return []
        url = f"{self.BASE_URL}/account/transactions?address={address}&limit={limit}"
        data = await self.rotator.make_request(session, url, headers={}, delay=0.3)
        return data.get("data", [])