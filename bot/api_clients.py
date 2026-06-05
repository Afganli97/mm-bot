"""
Клиенты для Etherscan, Ankr Multichain, RPC, Helius, каскадного определения цен.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS, ANKR_API_URL,
    HELIUS_API_KEY, HELIUS_URL, BIRDEYE_API_KEY
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
ANKR_DAILY_LIMIT = 100_000

class APIKeyRotator:
    def __init__(self, keys, service, daily_limit):
        self.keys = keys
        self.service = service
        self.daily_limit = daily_limit
    def _reset_old_if_needed(self, idx):
        from datetime import date
        from bot.database import get_connection
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute("DELETE FROM api_usage WHERE service=? AND key_index=? AND usage_date != ?",
                         (self.service, idx, today))
            conn.commit()
    def get_available_key(self):
        for i, key in enumerate(self.keys):
            self._reset_old_if_needed(i)
            used = get_api_usage_today(self.service, i)
            if used < self.daily_limit:
                return key, i
        return None
    async def make_request(self, session, url, params=None, headers=None, delay=0.4, chain_id=None):
        if self.service == "etherscan":
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
            try:
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

class EVMExplorerClient:
    BASE_URL = "https://api.etherscan.io/v2/api"
    def __init__(self, chain_id, weth, delay=0.4):
        self.chain_id = chain_id
        self.weth_address = weth.lower()
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

class AnkrClient:
    def __init__(self, api_url):
        self.api_url = api_url

    async def get_multichain_balances(self, session, address, chains=None):
        if chains is None:
            chains = ["eth", "bsc"]
        payload = {
            "jsonrpc": "2.0",
            "method": "ankr_getAccountBalance",
            "params": {"blockchain": chains, "walletAddress": address},
            "id": 1
        }
        async with session.post(self.api_url, json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                increment_api_usage("ankr", 0)
                return data.get("result", {})
            else:
                logger.error(f"Ankr HTTP {resp.status}")
        return {}

class HeliusClient:
    """Клиент для Helius API (Solana баланс и история)."""
    BASE_URL = "https://api.helius.xyz/v1"
    RPC_URL = "https://mainnet.helius-rpc.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_wallet_balances(self, session, address: str) -> Dict:
        url = f"{self.BASE_URL}/wallet/{address}/balances?api-key={self.api_key}"
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.error(f"Helius balances HTTP {resp.status}")
                return {}

    async def get_signatures_for_address(self, session, address: str, limit: int = 100) -> List[Dict]:
        """Возвращает список транзакций адреса (подписи)."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": limit}]
        }
        async with session.post(f"{self.RPC_URL}/?api-key={self.api_key}", json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result", [])
            else:
                logger.error(f"Helius getSignatures HTTP {resp.status}")
        return []

    async def get_transaction(self, session, signature: str) -> Dict:
        """Получает детали транзакции по подписи."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }
        async with session.post(f"{self.RPC_URL}/?api-key={self.api_key}", json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result", {})
            else:
                logger.error(f"Helius getTransaction HTTP {resp.status}")
        return {}

    async def get_token_reserves(self, session, pool_address: str) -> Optional[tuple]:
        """Возвращает резервы (reserve0, reserve1) для пула Raydium/Orca. Упрощённо: получаем аккаунт пула."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [pool_address, {"encoding": "jsonParsed"}]
        }
        async with session.post(f"{self.RPC_URL}/?api-key={self.api_key}", json=payload, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                info = data.get("result", {}).get("value", {})
                # Здесь нужен парсинг структуры пула, но для примера возвращаем None
                return None
        return None

class JupiterMassPrice:
    """Получение цен через Jupiter (один запрос для многих токенов)."""
    BASE_URL = "https://price.jup.ag/v4/price"

    async def get_prices(self, session, mint_addresses: List[str]) -> Dict[str, float]:
        if not mint_addresses:
            return {}
        ids = ",".join(mint_addresses[:100])  # до 100 за раз
        url = f"{self.BASE_URL}?ids={ids}"
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = {mint: float(info["price"]) for mint, info in data.get("data", {}).items()}
                    logger.debug(f"Jupiter prices: {prices}")
                    return prices
        except Exception as e:
            logger.warning(f"Jupiter mass price failed: {e}")
        return {}

class BirdeyePrice:
    """Цена через Birdeye (требуется API-ключ)."""
    BASE_URL = "https://public-api.birdeye.so/defi/price"

    def __init__(self, api_key: str):
        self.headers = {"X-API-KEY": api_key}
        logger.info(f"BirdeyePrice initialized with key: {api_key[:8]}...")

    async def get_price(self, session, mint: str) -> Optional[float]:
        params = {"address": mint, "x-chain": "solana"}
        logger.debug(f"Birdeye request for {mint}")
        try:
            async with session.get(self.BASE_URL, params=params, headers=self.headers, timeout=5) as resp:
                data = await resp.json()
                logger.debug(f"Birdeye response: {resp.status} {data}")
                if resp.status == 200 and data.get("success") and data.get("data"):
                    price = float(data["data"]["value"])
                    logger.info(f"Birdeye price for {mint}: {price}")
                    return price
        except Exception as e:
            logger.warning(f"Birdeye failed for {mint}: {e}")
        return None

class DexScreenerPrice:
    """Цена через DexScreener."""
    BASE_URL = "https://api.dexscreener.com/latest/dex/tokens"

    async def get_price(self, session, mint: str) -> Optional[float]:
        url = f"{self.BASE_URL}/{mint}"
        logger.debug(f"DexScreener request for {mint}")
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs")
                    if pairs:
                        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                        price = best.get("priceUsd")
                        if price:
                            logger.info(f"DexScreener price for {mint}: {price}")
                            return float(price)
        except Exception as e:
            logger.warning(f"DexScreener failed for {mint}: {e}")
        return None

class GeckoTerminalPrice:
    """Цена через GeckoTerminal."""
    BASE_URL = "https://api.geckoterminal.com/api/v1/networks/solana/tokens"

    async def get_price(self, session, mint: str) -> Optional[float]:
        url = f"{self.BASE_URL}/{mint}"
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = data.get("data", {}).get("attributes", {}).get("price_usd")
                    if price:
                        return float(price)
        except Exception as e:
            logger.debug(f"GeckoTerminal failed for {mint}: {e}")
        return None

class DexPaprikaPrice:
    """Цена через DexPaprika (без ключа)."""
    BASE_URL = "https://api.dexpaprika.com/v1/tokens"

    async def get_price(self, session, mint: str) -> Optional[float]:
        url = f"{self.BASE_URL}/{mint}/price"
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
        except Exception as e:
            logger.debug(f"DexPaprika failed for {mint}: {e}")
        return None

class RPCReservesPrice:
    """Расчёт цены через резервы пула (через Helius RPC)."""
    def __init__(self, helius: HeliusClient):
        self.helius = helius

    async def get_price(self, session, mint: str) -> Optional[float]:
        return None

class CascadePriceFetcher:
    """Каскадный определитель цены для Solana токенов."""
    def __init__(self, helius: HeliusClient):
        self.jupiter = JupiterMassPrice()
        self.birdeye = BirdeyePrice(BIRDEYE_API_KEY) if BIRDEYE_API_KEY else None
        self.dexscr = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()
        self.dexpaprika = DexPaprikaPrice()
        if not BIRDEYE_API_KEY:
            logger.warning("BIRDEYE_API_KEY is empty, Birdeye step will be skipped")

    async def get_prices(self, session, mints: List[str]) -> Dict[str, float]:
        prices = {}
        logger.info(f"Cascade: getting prices for {len(mints)} tokens: {mints}")
        # 1. Jupiter
        if mints:
            prices.update(await self.jupiter.get_prices(session, mints))

        remaining = [m for m in mints if m not in prices]
        if not remaining:
            return prices

        # 2. Birdeye
        if self.birdeye:
            logger.info(f"Trying Birdeye for {len(remaining)} tokens")
            for mint in remaining[:]:
                price = await self.birdeye.get_price(session, mint)
                await asyncio.sleep(0.2)
                if price:
                    prices[mint] = price
                    remaining.remove(mint)
        else:
            logger.warning("Birdeye client not available")

        # 3. DexScreener
        for mint in remaining[:]:
            price = await self.dexscr.get_price(session, mint)
            await asyncio.sleep(0.2)
            if price:
                prices[mint] = price
                remaining.remove(mint)

        # 4. GeckoTerminal
        for mint in remaining[:]:
            price = await self.gecko.get_price(session, mint)
            await asyncio.sleep(0.2)
            if price:
                prices[mint] = price
                remaining.remove(mint)

        # 5. DexPaprika
        for mint in remaining[:]:
            price = await self.dexpaprika.get_price(session, mint)
            await asyncio.sleep(0.2)
            if price:
                prices[mint] = price
                remaining.remove(mint)

        logger.info(f"Cascade result: {prices}")
        return prices

class EVMWeb3Client:
    def __init__(self, rpc_url, chain_id, weth, router=None, stable=None):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.weth_address = weth.lower()
        self.router_address = router.lower() if router else None
        self.stable_address = stable.lower() if stable else None

    async def _rpc_call(self, session, method, params):
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with session.post(self.rpc_url, json=payload, timeout=10) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data["result"]

    async def get_current_block(self, session):
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

class TokenInfoService:
    @staticmethod
    async def get_symbol(session, token_address, rpc_url):
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": token_address, "data": "0x95d89b41"}, "latest"], "id": 1}
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