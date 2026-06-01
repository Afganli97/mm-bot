"""
Асинхронные клиенты для Etherscan (только Ethereum), RPC (Alchemy/Infura), Solscan, CoinGecko.
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
                           headers: dict = None, delay: float = 0.4, chain_id: int = None) -> dict:
        if self.service == "etherscan":
            await asyncio.sleep(delay)

        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                logger.error(f"Все ключи сервиса {self.service} исчерпаны")
                raise Exception(f"Лимит {self.service} исчерпан")
            key, idx = key_info

            if self.service == "etherscan":
                params = params or {}
                params["apikey"] = key
                if chain_id is not None:
                    params["chainid"] = chain_id

            logger.debug(f"Запрос к {self.service}: URL={url}, params={params}")
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
                            continue
                        else:
                            raise Exception(f"Solscan HTTP {resp.status}")
                else:
                    async with session.get(url, params=params, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if self.service == "etherscan":
                                if data.get("message") in ("No transactions found", "No records found"):
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
                            await asyncio.sleep(1)
                            continue
                        else:
                            raise Exception(f"HTTP {resp.status} от {self.service}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка: {e}")
                raise
        raise Exception("Все попытки запроса исчерпаны")

# Ротаторы
etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)
solscan_rotator = APIKeyRotator([SOLSCAN_API_KEY], "solscan", SOLSCAN_DAILY_LIMIT) if SOLSCAN_API_KEY else None

class EVMExplorerClient:
    """Клиент для Etherscan V2 API (используется только для Ethereum)."""
    BASE_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, chain_id: int, weth_address: str, delay: float = 0.4):
        self.chain_id = chain_id
        self.weth_address = weth_address.lower()
        self.delay = delay
        self.rotator = etherscan_rotator

    async def get_block_by_timestamp(self, session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {"module": "block", "action": "getblocknobytime", "timestamp": timestamp, "closest": "before"}
        data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay, chain_id=self.chain_id)
        return int(data["result"])

    async def get_normal_transactions(self, session, address, start_block, end_block):
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "txlist", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay, chain_id=self.chain_id)
            txs = data.get("result", [])
            if not txs: break
            all_txs.extend(txs)
            if len(txs) < 1000: break
            page += 1
        return [tx for tx in all_txs if tx["from"].lower() == address.lower() and int(tx.get("isError", "0")) == 0 and int(tx["value"]) > 0]

    async def get_internal_transactions(self, session, address, start_block, end_block):
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "txlistinternal", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay, chain_id=self.chain_id)
            txs = data.get("result", [])
            if not txs: break
            all_txs.extend(txs)
            if len(txs) < 1000: break
            page += 1
        return [tx for tx in all_txs if tx["from"].lower() == address.lower() and int(tx.get("isError", "0")) == 0 and int(tx["value"]) > 0]

    async def get_token_transfers(self, session, address, contract_address=None, start_block=0, end_block=99999999, filter_by=None):
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "tokentx", "address": address,
                      "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            if contract_address: params["contractaddress"] = contract_address
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay, chain_id=self.chain_id)
            txs = data.get("result", [])
            if not txs: break
            all_txs.extend(txs)
            if len(txs) < 1000: break
            page += 1
        if filter_by:
            all_txs = [tx for tx in all_txs if tx[filter_by].lower() == address.lower()]
        return all_txs

    async def get_account_balance(self, session, address):
        params = {"module": "account", "action": "balance", "address": address, "tag": "latest"}
        data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay, chain_id=self.chain_id)
        return int(data["result"]) / 10**18

class EVMWeb3Client:
    """Клиент для EVM‑сетей через JSON‑RPC (Alchemy/Infura). Используется для BSC."""
    def __init__(self, rpc_url: str, chain_id: int, weth_address: str):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.weth_address = weth_address.lower()

    async def _rpc_call(self, session, method, params):
        payload = {"jsonrpc":"2.0","method":method,"params":params,"id":1}
        async with session.post(self.rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data["result"]

    async def get_balance(self, session, address: str) -> float:
        result = await self._rpc_call(session, "eth_getBalance", [address, "latest"])
        return int(result, 16) / 10**18

    async def get_token_transfers(self, session, address, direction="to", from_block=0, to_block=99999999) -> List[Dict]:
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        if direction == "to":
            topic2 = "0x000000000000000000000000" + address[2:].lower()
            params = [{"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": [transfer_topic, None, topic2]}]
        else:
            topic1 = "0x000000000000000000000000" + address[2:].lower()
            params = [{"fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": [transfer_topic, topic1, None]}]
        logs = await self._rpc_call(session, "eth_getLogs", params)
        transfers = []
        for log in logs:
            token = log["address"]
            if token.lower() == self.weth_address:
                continue
            value = int(log["data"][2:], 16)
            block_num = int(log["blockNumber"], 16)
            tx_hash = log["transactionHash"]
            from_addr = "0x" + log["topics"][1][-40:]
            to_addr = "0x" + log["topics"][2][-40:]
            transfers.append({
                "token_address": token, "tx_hash": tx_hash, "block_number": block_num,
                "from": from_addr, "to": to_addr, "value": value
            })
        return transfers

class TokenInfoService:
    @staticmethod
    async def get_symbol(session, token_address: str, rpc_url: str) -> str:
        payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to": token_address, "data": "0x95d89b41"}, "latest"],"id":1}
        try:
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if 'result' in result and result['result'] != '0x':
                        try:
                            symbol = bytes.fromhex(result['result'][2:]).decode('utf-8').rstrip('\x00')
                            if symbol: return symbol
                        except: pass
        except: pass
        return "?"

class SolscanClient:
    BASE_URL = "https://api.solscan.io/v1"
    def __init__(self):
        self.rotator = solscan_rotator

    async def get_token_balances(self, session, address):
        if not self.rotator: return []
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

    async def get_transactions(self, session, address, limit=50):
        if not self.rotator: return []
        url = f"{self.BASE_URL}/account/transactions?address={address}&limit={limit}"
        data = await self.rotator.make_request(session, url, headers={}, delay=0.3)
        return data.get("data", [])

class CoingeckoClient:
    BASE_URL = "https://api.coingecko.com/api/v3"
    @staticmethod
    async def get_top_100(session, network_name="ethereum") -> List[Dict]:
        from bot.database import get_connection
        from datetime import timedelta
        import json
        platform_map = {"ethereum": "ethereum", "bsc": "binance-smart-chain", "solana": "solana"}
        platform = platform_map.get(network_name)
        if not platform: return []
        with get_connection() as conn:
            row = conn.execute("SELECT tokens_json, updated_at FROM top_tokens_cache WHERE network=?",
                               (network_name,)).fetchone()
            if row and (datetime.utcnow() - datetime.fromisoformat(row['updated_at'])) < timedelta(hours=1):
                return json.loads(row['tokens_json'])
        url = f"{CoingeckoClient.BASE_URL}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&sparkline=false"
        if network_name == "bsc": url += "&category=binance-smart-chain"
        async with session.get(url, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = []
                for coin in data:
                    addr = coin.get("platforms", {}).get(platform, "")
                    if addr: tokens.append({"id": coin["id"], "symbol": coin["symbol"].upper(), "address": addr.lower()})
                with get_connection() as conn:
                    conn.execute("INSERT OR REPLACE INTO top_tokens_cache (network, updated_at, tokens_json) VALUES (?, ?, ?)",
                                 (network_name, datetime.utcnow().isoformat(), json.dumps(tokens)))
                    conn.commit()
                return tokens
            else:
                raise Exception("CoinGecko API error")