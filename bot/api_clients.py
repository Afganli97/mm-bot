# bot/api_clients.py
"""
API clients for free services:
Etherscan, BscScan, Ankr, Moralis, Alchemy, Helius,
DexScreener, GeckoTerminal, Jupiter, Birdeye, public RPC.
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

from bot.config import (
    BIRDEYE_API_KEY,
    BSCSCAN_API_KEYS,
    ETHERSCAN_API_KEYS,
    HELIUS_API_KEY,
)
from bot.rate_limits import RateLimitExceeded, RateLimitTracker

logger = logging.getLogger(__name__)


class APIKeyRotator:
    def __init__(self, service: str, keys: List[str], delay: float = 0.0):
        self.service = service
        self.keys = keys or [""]
        self.delay = delay

    def get_available_key(self):
        for i, key in enumerate(self.keys):
            if RateLimitTracker.is_available(self.service, i):
                return key, i
        return None

    async def make_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        delay: Optional[float] = None,
    ) -> Dict[str, Any]:
        delay = self.delay if delay is None else delay

        for _ in range(len(self.keys) * 2 + 1):
            key_info = self.get_available_key()
            if not key_info:
                raise RateLimitExceeded(f"Лимит {self.service} исчерпан")

            key, idx = key_info

            if not RateLimitTracker.reserve(self.service, idx):
                await asyncio.sleep(1)
                continue

            req_params = dict(params or {})
            req_headers = dict(headers or {})
            req_json = dict(json_body) if json_body is not None else None

            if self.service == "etherscan":
                req_params["apikey"] = key
                req_params["chainid"] = "1"
            elif self.service == "bscscan":
                if key:
                    req_params["apikey"] = key

            try:
                if delay:
                    await asyncio.sleep(delay)

                if method.upper() == "GET":
                    async with session.get(
                        url,
                        params=req_params,
                        headers=req_headers,
                        timeout=30,
                    ) as resp:
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep(1.5)
                            continue

                        resp.raise_for_status()
                        data = await resp.json()

                else:
                    async with session.post(
                        url,
                        json=req_json,
                        headers=req_headers,
                        timeout=30,
                    ) as resp:
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep(1.5)
                            continue

                        resp.raise_for_status()
                        data = await resp.json()

                if self.service in ("etherscan", "bscscan"):
                    if data.get("status") == "0":
                        message = str(data.get("message", "")).lower()
                        result = str(data.get("result", "")).lower()

                        if (
                            message in ("no transactions found", "no records found")
                            or "no records found" in result
                        ):
                            return {"status": "1", "message": "OK", "result": []}

                        if "limit" in message or "limit" in result or "rate limit" in result:
                            await asyncio.sleep(1)
                            continue

                        raise Exception(
                            f"{self.service} Error: {data.get('message')} {data.get('result')}"
                        )

                return data

            except RateLimitExceeded:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug("%s request error: %s", self.service, e)
                await asyncio.sleep(1)
                continue

        raise Exception(f"Все попытки запроса к {self.service} исчерпаны")


class EVMExplorerClient:
    """
    Etherscan V2 API.
    Используется для Ethereum history.
    """

    BASE_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, chain_id: int = 1, delay: float = 0.22):
        self.chain_id = chain_id
        self.delay = delay
        self.rotator = APIKeyRotator("etherscan", ETHERSCAN_API_KEYS, delay=delay)

    async def get_block_by_timestamp(self, session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": timestamp,
            "closest": "before",
        }
        data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
        return int(data["result"])

    async def get_normal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
        filter_by_from: bool = False,
    ) -> List[Dict]:
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
                "sort": "asc",
            }
            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by_from:
            all_txs = [tx for tx in all_txs if tx.get("from", "").lower() == address.lower()]

        return all_txs

    async def get_internal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
        filter_by_from: bool = False,
    ) -> List[Dict]:
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
                "sort": "asc",
            }
            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by_from:
            all_txs = [tx for tx in all_txs if tx.get("from", "").lower() == address.lower()]

        return all_txs

    async def get_token_transfers(
        self,
        session: aiohttp.ClientSession,
        address: str,
        contract_address: Optional[str] = None,
        start_block: int = 0,
        end_block: int = 99999999,
        filter_by: Optional[str] = None,
    ) -> List[Dict]:
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
                "sort": "asc",
            }

            if contract_address:
                params["contractaddress"] = contract_address

            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by:
            all_txs = [tx for tx in all_txs if tx.get(filter_by, "").lower() == address.lower()]

        return all_txs


class BscScanClient:
    """
    BscScan Free API.
    Используется для BSC history, native transfers, internal transfers, BEP20 transfers.
    """

    BASE_URL = "https://api.bscscan.com/api"

    def __init__(self, delay: float = 0.22):
        self.delay = delay
        self.rotator = APIKeyRotator("bscscan", BSCSCAN_API_KEYS, delay=delay)

    async def get_block_by_timestamp(self, session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": timestamp,
            "closest": "before",
        }
        data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
        return int(data["result"])

    async def get_normal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
        filter_by_from: bool = False,
    ) -> List[Dict]:
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
                "sort": "asc",
            }
            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by_from:
            all_txs = [tx for tx in all_txs if tx.get("from", "").lower() == address.lower()]

        return all_txs

    async def get_internal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
        filter_by_from: bool = False,
    ) -> List[Dict]:
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
                "sort": "asc",
            }
            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by_from:
            all_txs = [tx for tx in all_txs if tx.get("from", "").lower() == address.lower()]

        return all_txs

    async def get_token_transfers(
        self,
        session: aiohttp.ClientSession,
        address: str,
        contract_address: Optional[str] = None,
        start_block: int = 0,
        end_block: int = 99999999,
        filter_by: Optional[str] = None,
    ) -> List[Dict]:
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
                "sort": "asc",
            }

            if contract_address:
                params["contractaddress"] = contract_address

            data = await self.rotator.make_request(session, "GET", self.BASE_URL, params=params)
            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by:
            all_txs = [tx for tx in all_txs if tx.get(filter_by, "").lower() == address.lower()]

        return all_txs


class AnkrClient:
    def __init__(self, api_url: str):
        self.api_url = api_url

    async def get_multichain_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chains: Optional[List[str]] = None,
    ) -> Dict:
        if not self.api_url:
            return {}

        payload = {
            "jsonrpc": "2.0",
            "method": "ankr_getAccountBalance",
            "params": {
                "blockchain": chains or ["eth", "bsc"],
                "walletAddress": address,
            },
            "id": 1,
        }

        try:
            RateLimitTracker.require("ankr", 0)
            async with session.post(self.api_url, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    logger.warning("Ankr HTTP %s", resp.status)
                    return {}
                data = await resp.json()
                if "error" in data:
                    logger.warning("Ankr RPC error: %s", data.get("error"))
                    return {}
                return data.get("result", {})
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("Ankr API error: %s", e)
            return {}


class MoralisClient:
    BASE_URL = "https://deep-index.moralis.io/api/v2.2"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-API-Key": api_key} if api_key else {}

    async def get_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str = "eth",
    ) -> List[Dict]:
        if not self.api_key:
            return []

        result = []
        page = 1
        page_size = 100

        while True:
            try:
                RateLimitTracker.require("moralis", 0)
                url = f"{self.BASE_URL}/wallets/{address}/tokens"
                params = {
                    "chain": chain,
                    "exclude_spam": "true",
                    "page": page,
                    "page_size": page_size,
                }

                async with session.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=30,
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Moralis HTTP %s", resp.status)
                        break

                    data = await resp.json()
                    page_result = data.get("result", [])
                    result.extend(page_result)

                    total = data.get("total", 0) or 0
                    if len(result) >= total or len(page_result) < page_size:
                        break

                    page += 1

            except RateLimitExceeded:
                raise
            except Exception as e:
                logger.debug("Moralis balances error: %s", e)
                break

        return result


class AlchemyClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_token_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
    ) -> List[Dict]:
        if not self.api_key:
            return []

        url = f"https://eth-mainnet.g.alchemy.com/v2/{self.api_key}"
        payload = {
            "jsonrpc": "2.0",
            "method": "alchemy_getTokenBalances",
            "params": [address, "erc20"],
            "id": 1,
        }

        try:
            RateLimitTracker.require("alchemy", 0)
            async with session.post(url, json=payload, timeout=30) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                balances = data.get("result", {}).get("tokenBalances", [])
                return [
                    item
                    for item in balances
                    if item.get("tokenBalance")
                    and item.get("tokenBalance")
                    != "0x0000000000000000000000000000000000000000000000000000000000000000"
                ]
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("Alchemy token balances error: %s", e)
            return []


class HeliusClient:
    RPC_URL = "https://mainnet.helius-rpc.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._semaphore = asyncio.Semaphore(5)

    async def _do_request(self, session: aiohttp.ClientSession, method: str, params: list) -> Any:
        if not self.api_key:
            return None

        async with self._semaphore:
            await asyncio.sleep(0.25)
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }

            try:
                RateLimitTracker.require("helius", 0)
                async with session.post(
                    f"{self.RPC_URL}/?api-key={self.api_key}",
                    json=payload,
                    timeout=20,
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Helius HTTP %s", resp.status)
                        return None
                    data = await resp.json()
                    if "error" in data:
                        logger.debug("Helius RPC error: %s", data.get("error"))
                        return None
                    return data.get("result")
            except RateLimitExceeded:
                raise
            except Exception as e:
                logger.debug("Helius RPC error: %s", e)
                return None

    async def get_wallet_balances(self, session: aiohttp.ClientSession, address: str) -> Dict:
        if not self.api_key:
            return {}

        try:
            RateLimitTracker.require("helius", 0)
            async with session.get(
                f"https://api.helius.xyz/v1/wallets/{address}/balances?api-key={self.api_key}",
                timeout=20,
            ) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("Helius wallet balances error: %s", e)
            return {}

    async def get_signatures_for_address(
        self,
        session: aiohttp.ClientSession,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
    ) -> List[Dict]:
        params = [{"limit": min(limit, 1000)}]
        if before:
            params[0]["before"] = before
        return await self._do_request(session, "getSignaturesForAddress", params) or []

    async def get_transaction(self, session: aiohttp.ClientSession, signature: str) -> Dict:
        return await self._do_request(
            session,
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        ) or {}


class DexScreenerPrice:
    async def get_price(self, session: aiohttp.ClientSession, token_address: str) -> Optional[Dict]:
        try:
            RateLimitTracker.require("dexscreener", 0)
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                pairs = data.get("pairs") or []

                if not pairs:
                    return None

                best = max(
                    pairs,
                    key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
                )

                liquidity = float(best.get("liquidity", {}).get("usd", 0) or 0)
                volume = float(best.get("volume", {}).get("h24", 0) or 0)
                price = best.get("priceUsd")

                return {
                    "price_usd": float(price) if price else None,
                    "liquidity_usd": liquidity,
                    "volume_24h": volume,
                    "source": "dexscreener",
                }
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("DexScreener price error for %s: %s", token_address, e)
            return None


class GeckoTerminalPrice:
    async def get_price(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        network: str = "solana",
    ) -> Optional[Dict]:
        try:
            RateLimitTracker.require("geckoterminal", 0)
            async with session.get(
                f"https://api.geckoterminal.com/api/v1/networks/{network}/tokens/{token_address}",
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                attrs = data.get("data", {}).get("attributes", {})
                price = attrs.get("price_usd")

                return {
                    "price_usd": float(price) if price else None,
                    "liquidity_usd": None,
                    "volume_24h": None,
                    "source": "geckoterminal",
                }
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("GeckoTerminal price error for %s: %s", token_address, e)
            return None


class JupiterPrice:
    async def get_price(self, session: aiohttp.ClientSession, mint: str) -> Optional[Dict]:
        try:
            RateLimitTracker.require("jupiter", 0)
            async with session.get(
                f"https://price.jup.ag/v4/price?ids={mint}",
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                info = data.get("data", {}).get(mint)

                if not info:
                    return None

                return {
                    "price_usd": float(info.get("price", 0) or 0),
                    "liquidity_usd": None,
                    "volume_24h": None,
                    "source": "jupiter",
                }
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("Jupiter price error for %s: %s", mint, e)
            return None


class BirdeyeTokenOverview:
    async def get_overview(self, session: aiohttp.ClientSession, token_address: str) -> Optional[Dict]:
        if not BIRDEYE_API_KEY:
            return None

        try:
            RateLimitTracker.require("birdeye", 0)
            headers = {"X-API-KEY": BIRDEYE_API_KEY}
            async with session.get(
                f"https://public-api.birdeye.so/defi/token_overview?address={token_address}&x-chain=solana",
                headers=headers,
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json()).get("data", {})
        except RateLimitExceeded:
            raise
        except Exception as e:
            logger.debug("Birdeye overview error for %s: %s", token_address, e)
            return None


class EVMPriceCascade:
    def __init__(self, web3_client: Optional["EVMWeb3Client"] = None):
        self.dexscr = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()
        self.web3 = web3_client

    async def get_price(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        network_name: str = "ethereum",
        weth_price_usd: float = 0.0,
    ) -> Optional[float]:
        price_data = await self.dexscr.get_price(session, token_address)
        if price_data and price_data.get("price_usd"):
            return float(price_data["price_usd"])

        await asyncio.sleep(0.2)

        gecko_net = {"ethereum": "eth", "bsc": "bsc", "eth": "eth"}.get(network_name, "eth")
        price_data = await self.gecko.get_price(session, token_address, gecko_net)
        if price_data and price_data.get("price_usd"):
            return float(price_data["price_usd"])

        await asyncio.sleep(0.2)

        if self.web3:
            try:
                return await self.web3.get_price_via_router(
                    session,
                    token_address,
                    weth_price_usd,
                )
            except Exception:
                pass

        return None


class EVMWeb3Client:
    """
    Public/free EVM RPC client.
    """

    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        weth: Optional[str],
        router: Optional[str] = None,
        stable: Optional[str] = None,
    ):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.weth_address = weth.lower() if weth else None
        self.router_address = router.lower() if router else None
        self.stable_address = stable.lower() if stable else None

    async def _rpc_call(self, session: aiohttp.ClientSession, method: str, params: list) -> Any:
        RateLimitTracker.require("public_rpc", 0)
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }

        async with session.post(self.rpc_url, json=payload, timeout=15) as resp:
            if resp.status != 200:
                raise Exception(f"RPC HTTP {resp.status}")
            data = await resp.json()
            if "error" in data:
                raise Exception(data["error"])
            return data.get("result")

    async def get_current_block(self, session: aiohttp.ClientSession) -> int:
        return int(await self._rpc_call(session, "eth_blockNumber", []), 16)

    async def get_block_by_number(self, session: aiohttp.ClientSession, block_number: int) -> Dict:
        return await self._rpc_call(session, "eth_getBlockByNumber", [hex(block_number), False])

    async def get_block_timestamp(self, session: aiohttp.ClientSession, block_number: int) -> int:
        block = await self.get_block_by_number(session, block_number)
        return int(block.get("timestamp", "0x0"), 16)

    async def get_block_by_timestamp_binary_search(
        self,
        session: aiohttp.ClientSession,
        target_timestamp: int,
    ) -> int:
        high = await self.get_current_block(session)
        low = 0
        best = 0

        while low <= high:
            mid = (low + high) // 2
            ts = await self.get_block_timestamp(session, mid)

            if ts <= target_timestamp:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

            await asyncio.sleep(0.03)

        return best

    async def get_balance_raw(self, session: aiohttp.ClientSession, address: str) -> int:
        return int(await self._rpc_call(session, "eth_getBalance", [address, "latest"]), 16)

    async def get_balance(self, session: aiohttp.ClientSession, address: str) -> float:
        raw = await self.get_balance_raw(session, address)
        return raw / (10**18)

    async def get_balance_at_block_raw(
        self,
        session: aiohttp.ClientSession,
        address: str,
        block_number: int,
    ) -> int:
        return int(await self._rpc_call(session, "eth_getBalance", [address, hex(block_number)]), 16)

    async def get_token_balance_at_block_raw(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        holder_address: str,
        block_number: int,
    ) -> int:
        padded_holder = holder_address.lower()[2:].rjust(64, "0")
        data = "0x70a08231" + padded_holder
        result = await self._rpc_call(
            session,
            "eth_call",
            [
                {"to": token_address, "data": data},
                hex(block_number),
            ],
        )
        return int(result, 16) if result and result != "0x" else 0

    async def get_transaction(self, session: aiohttp.ClientSession, tx_hash: str) -> Dict:
        return await self._rpc_call(session, "eth_getTransactionByHash", [tx_hash]) or {}

    async def get_transaction_receipt(self, session: aiohttp.ClientSession, tx_hash: str) -> Dict:
        return await self._rpc_call(session, "eth_getTransactionReceipt", [tx_hash]) or {}

    async def get_token_transfers(
        self,
        session: aiohttp.ClientSession,
        address: str,
        direction: str = "to",
        from_block: int = 0,
        to_block: int = 99999999,
        chunk_size: int = 4999,
    ) -> List[Dict]:
        if to_block == "latest":
            to_block = await self.get_current_block(session)

        padded_addr = "0x000000000000000000000000" + address[2:].lower()

        if direction == "to":
            topics = [self.TRANSFER_TOPIC, None, padded_addr]
        else:
            topics = [self.TRANSFER_TOPIC, padded_addr]

        results = []

        for start_b in range(from_block, to_block + 1, chunk_size):
            end_b = min(start_b + chunk_size - 1, to_block)
            params = [
                {
                    "fromBlock": hex(start_b),
                    "toBlock": hex(end_b),
                    "topics": topics,
                }
            ]

            try:
                logs = await self._rpc_call(session, "eth_getLogs", params)

                for log in logs:
                    token_addr = log.get("address", "").lower()
                    block_num = int(log.get("blockNumber", "0x0"), 16)
                    tx_hash = log.get("transactionHash", "")
                    value_hex = log.get("data", "0x0")
                    value_wei = int(value_hex, 16) if value_hex and value_hex != "0x" else 0

                    topics = log.get("topics", [])
                    from_addr = ""
                    to_addr = ""

                    if len(topics) >= 2:
                        from_addr = "0x" + topics[1][26:].lower()
                    if len(topics) >= 3:
                        to_addr = "0x" + topics[2][26:].lower()

                    results.append(
                        {
                            "token_address": token_addr,
                            "tx_hash": tx_hash,
                            "block_number": block_num,
                            "from": from_addr,
                            "to": to_addr,
                            "value": str(value_wei),
                            "value_wei": value_wei,
                        }
                    )

            except Exception as e:
                logger.debug("RPC getLogs chunk failed %s-%s: %s", start_b, end_b, e)

            await asyncio.sleep(0.05)

        return results

    async def get_price_via_router(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        weth_price_usd: float,
    ) -> Optional[float]:
        if not self.router_address or not self.weth_address or not weth_price_usd:
            return None

        try:
            amount_in = "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
            offset = "0000000000000000000000000000000000000000000000000000000000000040"
            length = "0000000000000000000000000000000000000000000000000000000000000002"
            token = token_address.lower()[2:].rjust(64, "0")
            weth = self.weth_address[2:].rjust(64, "0")
            data = "0xd06ca61f" + amount_in + offset + length + token + weth

            result = await self._rpc_call(
                session,
                "eth_call",
                [
                    {"to": self.router_address, "data": data},
                    "latest",
                ],
            )

            if not result or result == "0x":
                return None

            amounts_offset = int(result[2:66], 16)
            weth_out = int(result[2 + amounts_offset + 64 * 2 :], 16)

            if weth_out > 0:
                return (weth_out / (10**18)) * weth_price_usd

        except Exception as e:
            logger.debug("Router price failed for %s: %s", token_address, e)

        return None


class TokenInfoService:
    @staticmethod
    async def get_symbol(session: aiohttp.ClientSession, token_address: str, rpc_url: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": token_address, "data": "0x95d89b41"}, "latest"],
            "id": 1,
        }

        try:
            RateLimitTracker.require("public_rpc", 0)
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    return "?"
                res_str = (await resp.json()).get("result", "")
                return TokenInfoService._decode_string(res_str)
        except Exception:
            return "?"

    @staticmethod
    async def get_name(session: aiohttp.ClientSession, token_address: str, rpc_url: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": token_address, "data": "0x06fdde03"}, "latest"],
            "id": 1,
        }

        try:
            RateLimitTracker.require("public_rpc", 0)
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    return "?"
                res_str = (await resp.json()).get("result", "")
                return TokenInfoService._decode_string(res_str)
        except Exception:
            return "?"

    @staticmethod
    async def get_decimals(session: aiohttp.ClientSession, token_address: str, rpc_url: str) -> int:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": token_address, "data": "0x313ce567"}, "latest"],
            "id": 1,
        }

        try:
            RateLimitTracker.require("public_rpc", 0)
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    return 18
                result = (await resp.json()).get("result")
                if result and result != "0x":
                    decimals = int(result, 16)
                    if 0 <= decimals <= 36:
                        return decimals
        except Exception:
            pass

        return 18

    @staticmethod
    def _decode_string(res_str: str) -> str:
        if not res_str or res_str == "0x":
            return "?"

        raw_hex = res_str[2:]

        try:
            if len(raw_hex) >= 128:
                length = int(raw_hex[64:128], 16)
                if 0 < length < 256:
                    data = raw_hex[128 : 128 + (length * 2)]
                    return bytes.fromhex(data).decode("utf-8", errors="ignore").strip()
        except Exception:
            pass

        try:
            symbol = bytes.fromhex(raw_hex).decode("utf-8", errors="ignore").replace("\x00", "").strip()
            symbol = re.sub(r"[^A-Za-z0-9_$.-]", "", symbol)
            return symbol or "?"
        except Exception:
            return "?"