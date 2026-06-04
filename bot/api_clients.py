"""
Клиенты для Etherscan, Ankr Multichain, RPC, Solscan, каскадного определения цен.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS, ANKR_API_URL,
    SOLSCAN_API_KEY, HELIUS_URL, NETWORKS
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
SOLSCAN_DAILY_LIMIT = 100_000
ANKR_DAILY_LIMIT = 100_000   # условно, лимитов нет, но ведём учёт

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

    def get_available_key(self) -> Optional[tuple]:
        for i, key in enumerate(self.keys):
            self._reset_old_if_needed(i)
            used = get_api_usage_today(self.service, i)
            if used < self.daily_limit:
                return key, i
        return None

    async def make_request(self, session, url, params=None, headers=None, delay=0.4, chain_id=None):
        if self.service in ("etherscan",):
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
solscan_rotator = APIKeyRotator([SOLSCAN_API_KEY], "solscan", SOLSCAN_DAILY_LIMIT) if SOLSCAN_API_KEY else None

class EVMExplorerClient:
    """Клиент для Etherscan V2 API (история Ethereum)."""
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

class AnkrClient:
    """Клиент для Ankr Advanced API (балансы всех EVM сетей)."""
    def __init__(self, api_url: str):
        self.api_url = api_url

    async def get_multichain_balances(self, session: aiohttp.ClientSession, address: str, chains: List[str] = None) -> Dict:
        """
        Возвращает балансы для указанных сетей (по умолчанию ['eth','bsc']).
        Ответ: { totalBalanceUsd, assets: [ { blockchain, tokenSymbol, balance, balanceUsd, tokenDecimals, tokenAddress } ] }
        """
        if chains is None:
            chains = ["eth", "bsc"]
        payload = {
            "jsonrpc": "2.0",
            "method": "ankr_getAccountBalance",
            "params": {
                "blockchain": chains,
                "walletAddress": address
            },
            "id": 1
        }
        try:
            async with session.post(self.api_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug(f"Ankr response: {data}")
                    increment_api_usage("ankr", 0)  # учёт
                    return data.get("result", {})
                else:
                    text = await resp.text()
                    logger.error(f"Ankr HTTP {resp.status}: {text}")
        except Exception as e:
            logger.error(f"Ankr request failed: {e}")
        return {}

class EVMWeb3Client:
    """RPC-клиент для истории покупок и прямого расчета цен."""
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
        """Прямой расчёт цены через Router (токен -> WETH -> stablecoin)."""
        if not self.router_address or not self.stable_address:
            return None
        try:
            # Узнаём decimals токена
            decimals = await self._get_decimals(session, token_address)
            amount_in = 10 ** decimals
            path = [token_address, self.weth_address, self.stable_address]
            data = ("0xd06ca61f"
                    f"{amount_in:064x}"
                    f"0000000000000000000000000000000000000000000000000000000000000040"
                    f"0000000000000000000000000000000000000000000000000000000000000003"
                    f"000000000000000000000000{token_address[2:].lower():0>64}"
                    f"000000000000000000000000{self.weth_address[2:]:0>64}"
                    f"000000000000000000000000{self.stable_address[2:]:0>64}")
            result = await self._rpc_call(session, "eth_call", [{"to": self.router_address, "data": data}, "latest"])
            if result:
                amounts_offset = int(result[2:66], 16)
                # Последнее значение в массиве – количество стейблкоина
                stable_out = int(result[2 + amounts_offset + 64*2:], 16)
                if stable_out > 0:
                    # Для USDT/BUSD decimals = 18 в Pancake, но для USDC 6? Будем считать 18 для простоты
                    return stable_out / 10**18
        except Exception as e:
            logger.debug(f"Router price failed for {token_address}: {e}")
        return None

    async def _get_decimals(self, session, token_address: str) -> int:
        payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to": token_address, "data": "0x313ce567"}, "latest"],"id":1}
        try:
            result = await self._rpc_call(session, "eth_call", [{"to": token_address, "data": "0x313ce567"}, "latest"])
            if result:
                return int(result, 16)
        except:
            pass
        return 18

class CascadePriceFetcher:
    """Каскадное определение цены: DexScreener -> GeckoTerminal -> RPC-роутер."""
    DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens"
    GECKO_URL = "https://api.geckoterminal.com/api/v1/networks/{network}/tokens/{address}"

    def __init__(self):
        self._semaphore = asyncio.Semaphore(10)

    async def get_price(self, session: aiohttp.ClientSession, chain: str, token_address: str, web3_client: EVMWeb3Client = None, weth_price: float = 0.0) -> Optional[float]:
        """Возвращает цену в USD или None."""
        async with self._semaphore:
            # 1. DexScreener
            try:
                url = f"{self.DEXSCREENER_URL}/{token_address}"
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs")
                        if pairs and pairs[0].get("priceUsd"):
                            return float(pairs[0]["priceUsd"])
            except Exception as e:
                logger.debug(f"DexScreener failed for {token_address}: {e}")

            await asyncio.sleep(0.2)

            # 2. GeckoTerminal
            gecko_network = {"ethereum": "eth", "bsc": "bsc", "eth": "eth"}.get(chain.lower(), chain.lower())
            try:
                url = self.GECKO_URL.format(network=gecko_network, address=token_address)
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = data.get("data", {}).get("attributes", {}).get("price_usd")
                        if price:
                            return float(price)
            except Exception as e:
                logger.debug(f"GeckoTerminal failed for {token_address}: {e}")

            await asyncio.sleep(0.2)

            # 3. Прямой RPC-роутер (если передан web3_client)
            if web3_client:
                try:
                    return await web3_client.get_price_via_router(session, token_address, weth_price)
                except Exception as e:
                    logger.debug(f"RPC router failed for {token_address}: {e}")

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