"""
Асинхронные клиенты для Etherscan, Blockscout, RPC (Alchemy/Infura), Solscan, CoinGecko.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS,
    BLOCKSCOUT_API_KEY,
    SOLSCAN_API_KEY, HELIUS_URL
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
BLOCKSCOUT_DAILY_LIMIT = 100_000   # кредитов
SOLSCAN_DAILY_LIMIT = 100_000

class APIKeyRotator:
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

    def get_available_key(self) -> Optional[tuple]:
        for i, key in enumerate(self.keys):
            self._reset_old_if_needed(i)
            used = get_api_usage_today(self.service, i)
            if used < self.daily_limit:
                return key, i
        return None

    async def make_request(self, session, url, params=None, headers=None, delay=0.4, chain_id=None):
        if self.service in ("etherscan", "blockscout"):
            await asyncio.sleep(delay)

        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                raise Exception(f"Лимит {self.service} исчерпан")
            key, idx = key_info
            if self.service == "etherscan":
                params = params or {}
                params["apikey"] = key
                if chain_id is not None:
                    params["chainid"] = chain_id
            elif self.service == "blockscout":
                params = params or {}
                params["apikey"] = key
                if chain_id is not None:
                    params["chain_id"] = chain_id

            try:
                if self.service == "solscan":
                    headers = headers or {}
                    headers["Authorization"] = f"Bearer {key}"
                    async with session.get(url, headers=headers, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            increment_api_usage(self.service, idx)
                            return data
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
                                    continue
                                else:
                                    raise Exception(f"Etherscan: {data.get('result', 'Unknown error')}")
                            elif self.service == "blockscout":
                                # Blockscout возвращает либо данные, либо сообщение об ошибке
                                if data.get("status") == "1" or "result" in data:
                                    increment_api_usage(self.service, idx)
                                    return data
                                else:
                                    raise Exception(f"Blockscout: {data}")
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

etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)
blockscout_rotator = APIKeyRotator([BLOCKSCOUT_API_KEY], "blockscout", BLOCKSCOUT_DAILY_LIMIT) if BLOCKSCOUT_API_KEY else None
solscan_rotator = APIKeyRotator([SOLSCAN_API_KEY], "solscan", SOLSCAN_DAILY_LIMIT) if SOLSCAN_API_KEY else None

class EVMExplorerClient:
    BASE_URL = "https://api.etherscan.io/v2/api"
    def __init__(self, chain_id: int, weth_address: str, delay=0.4):
        self.chain_id = chain_id
        self.weth_address = weth_address.lower()
        self.delay = delay
        self.rotator = etherscan_rotator

    async def get_block_by_timestamp(self, session, timestamp):
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

class BlockscoutClient:
    BASE_URL = "https://api.blockscout.com/v2/api"

    def __init__(self, rotator: APIKeyRotator):
        self.rotator = rotator

    async def get_native_balance(self, session, chain_id: int, address: str) -> float:
        params = {"module": "account", "action": "balance", "address": address, "chain_id": chain_id}
        data = await self.rotator.make_request(session, self.BASE_URL, params, delay=0.25, chain_id=chain_id)
        return int(data["result"]) / 10**18

    async def get_token_balances(self, session, chain_id: int, address: str) -> List[Dict]:
        """
        Возвращает список токенов с балансами через action=tokenlist.
        Каждый элемент: {address, symbol, balance (в минимальных единицах), decimals}
        """
        params = {"module": "account", "action": "tokenlist", "address": address, "chain_id": chain_id}
        data = await self.rotator.make_request(session, self.BASE_URL, params, delay=0.25, chain_id=chain_id)
        result = data.get("result", [])
        tokens = []
        for t in result:
            tokens.append({
                "address": t["contractAddress"],
                "symbol": t.get("tokenSymbol", "?"),
                "balance": int(t["balance"]),
                "decimals": int(t.get("decimals", 18))
            })
        return tokens

class EVMWeb3Client:
    def __init__(self, rpc_url: str, chain_id: int, weth_address: str, router_address: str = None, stable_address: str = None):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.weth_address = weth_address.lower()
        self.router_address = router_address.lower() if router_address else None
        self.stable_address = stable_address.lower() if stable_address else None

    async def _rpc_call(self, session, method, params):
        payload = {"jsonrpc":"2.0","method":method,"params":params,"id":1}
        async with session.post(self.rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data["result"]

    async def get_current_block(self, session) -> int:
        result = await self._rpc_call(session, "eth_blockNumber", [])
        return int(result, 16)

    async def get_balance(self, session, address: str) -> float:
        result = await self._rpc_call(session, "eth_getBalance", [address, "latest"])
        return int(result, 16) / 10**18

    async def get_balance_of(self, session, token_address, owner_address) -> int:
        data = "0x70a08231" + "000000000000000000000000" + owner_address[2:]
        result = await self._rpc_call(session, "eth_call", [{"to": token_address, "data": data}, "latest"])
        if result and result != "0x":
            return int(result, 16)
        return 0

    async def get_price_via_router(self, session, token_address: str, weth_price_usd: float) -> Optional[float]:
        if not self.router_address or not self.stable_address:
            return None
        # 1. token -> WETH
        try:
            data = (f"0xd06ca61f"
                    f"0000000000000000000000000000000000000000000000000de0b6b3a7640000"
                    f"0000000000000000000000000000000000000000000000000000000000000040"
                    f"0000000000000000000000000000000000000000000000000000000000000002"
                    f"000000000000000000000000{token_address[2:]:0>64}"
                    f"000000000000000000000000{self.weth_address[2:]:0>64}")
            result = await self._rpc_call(session, "eth_call", [{"to": self.router_address, "data": data}, "latest"])
            if result:
                amounts_offset = int(result[2:66], 16)
                weth_out = int(result[2 + amounts_offset + 64*2:], 16)
                if weth_out > 0:
                    return (weth_out / 1e18) * weth_price_usd
        except Exception:
            pass
        # 2. token -> stablecoin
        try:
            data = (f"0xd06ca61f"
                    f"0000000000000000000000000000000000000000000000000de0b6b3a7640000"
                    f"0000000000000000000000000000000000000000000000000000000000000040"
                    f"0000000000000000000000000000000000000000000000000000000000000002"
                    f"000000000000000000000000{token_address[2:]:0>64}"
                    f"000000000000000000000000{self.stable_address[2:]:0>64}")
            result = await self._rpc_call(session, "eth_call", [{"to": self.router_address, "data": data}, "latest"])
            if result:
                amounts_offset = int(result[2:66], 16)
                stable_out = int(result[2 + amounts_offset + 64*2:], 16)
                if stable_out > 0:
                    return stable_out / 1e18
        except Exception:
            pass
        return None

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