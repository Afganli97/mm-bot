"""
Клиенты внешних API и RPC.

Здесь находятся:
- Etherscan V2;
- BscScan V2;
- Ankr;
- Moralis;
- Helius;
- DexScreener;
- GeckoTerminal;
- Jupiter;
- Birdeye;
- EVM RPC;
- TokenInfoService.

Все успешные API-запросы пишутся в api_usage.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

from bot.config import (
    ALCHEMY_API_KEY,
    ANKR_API_URL,
    BIRDEYE_API_KEY,
    BSCSCAN_API_KEYS,
    BSCSCAN_DAILY_LIMIT,
    ETHERSCAN_API_KEYS,
    ETHERSCAN_DAILY_LIMIT,
    HELIUS_API_KEY,
    HELIUS_URL,
    MORALIS_API_KEY,
    MORALIS_DAILY_LIMIT,
)
from bot.database import (
    get_api_usage_today,
    get_connection,
    increment_api_usage,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# API key rotator
# ---------------------------------------------------------------------

class APIKeyRotator:
    def __init__(
        self,
        keys: List[str],
        service: str,
        daily_limit: int,
        chain_id: Optional[int] = None,
    ):
        self.keys = [key.strip() for key in keys if key.strip()]
        self.service = service
        self.daily_limit = int(daily_limit)
        self.chain_id = chain_id

    def _reset_old_if_needed(self, idx: int) -> None:
        today = time.strftime("%Y-%m-%d")

        with get_connection() as conn:
            conn.execute(
                """
                DELETE FROM api_usage
                WHERE service = ?
                  AND key_index = ?
                  AND usage_date != ?
                """,
                (
                    self.service,
                    idx,
                    today,
                ),
            )
            conn.commit()

    def get_available_key(self):
        if not self.keys:
            return None

        for idx, key in enumerate(self.keys):
            self._reset_old_if_needed(idx)
            used = get_api_usage_today(self.service, idx)

            if self.daily_limit <= 0:
                return key, idx

            if used < self.daily_limit:
                return key, idx

        return None

    async def make_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        delay: float = 0.4,
    ) -> Dict[str, Any]:
        if not self.keys:
            raise Exception(f"API key не задан для сервиса {self.service}")

        if self.service in ("etherscan", "bscscan", "moralis"):
            await asyncio.sleep(delay)

        for _attempt in range(max(1, len(self.keys))):
            key_info = self.get_available_key()

            if not key_info:
                raise Exception(f"Лимит {self.service} исчерпан")

            key, idx = key_info

            request_params = dict(params or {})
            request_headers = dict(headers) if headers else None

            if self.service in ("etherscan", "bscscan"):
                request_params["apikey"] = key
                request_params["chainid"] = str(self.chain_id)

            elif self.service == "moralis":
                request_headers = dict(headers or {})
                request_headers["X-API-Key"] = key

            try:
                async with session.get(
                    url,
                    params=request_params,
                    headers=request_headers,
                    timeout=30,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        if self.service in ("etherscan", "bscscan"):
                            message = str(data.get("message", "")).lower()

                            if message in ("no transactions found", "no records found"):
                                increment_api_usage(self.service, idx)
                                return {
                                    "status": "1",
                                    "message": "OK",
                                    "result": [],
                                }

                            if data.get("status") == "1" or data.get("message") == "OK":
                                increment_api_usage(self.service, idx)
                                return data

                            result_text = str(data.get("result", "")).lower()

                            if data.get("message") == "NOTOK" and "limit" in result_text:
                                logger.warning("Лимит %s на ключе %s", self.service, idx)
                                await asyncio.sleep(1)
                                continue

                            raise Exception(
                                f"{self.service} Error: {data.get('result', 'Unknown')}"
                            )

                        if self.service == "moralis":
                            increment_api_usage(self.service, idx)
                            return data

                    if resp.status == 429:
                        logger.warning("Rate limit %s", self.service)
                        await asyncio.sleep(1)
                        continue

                    raise Exception(f"HTTP {resp.status} от {self.service}")

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.error("Сетевая ошибка API %s: %s", self.service, exc)
                await asyncio.sleep(1)

        raise Exception(f"Все попытки запроса к {self.service} исчерпаны")


etherscan_rotator = APIKeyRotator(
    keys=ETHERSCAN_API_KEYS,
    service="etherscan",
    daily_limit=ETHERSCAN_DAILY_LIMIT,
    chain_id=1,
)

bscscan_rotator = APIKeyRotator(
    keys=BSCSCAN_API_KEYS,
    service="bscscan",
    daily_limit=BSCSCAN_DAILY_LIMIT,
    chain_id=56,
)


# ---------------------------------------------------------------------
# Etherscan / BscScan
# ---------------------------------------------------------------------

class EVMExplorerClient:
    """
    Etherscan V2 API.
    Используется для Ethereum.
    """

    BASE_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, chain_id: int, weth: str, delay: float = 0.4):
        self.chain_id = int(chain_id)
        self.weth_address = weth.lower()
        self.delay = delay
        self.rotator = etherscan_rotator

    async def get_block_by_timestamp(self, session: aiohttp.ClientSession, timestamp: int) -> int:
        params = {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": int(timestamp),
            "closest": "before",
        }

        data = await self.rotator.make_request(
            session,
            self.BASE_URL,
            params,
            delay=self.delay,
        )

        return int(data["result"])

    async def get_account_balance(
        self,
        session: aiohttp.ClientSession,
        address: str,
    ) -> float:
        params = {
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
        }

        data = await self.rotator.make_request(
            session,
            self.BASE_URL,
            params,
            delay=self.delay,
        )

        return int(data.get("result", "0")) / 10**18

    async def get_normal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict[str, Any]]:
        all_txs: List[Dict[str, Any]] = []
        page = 1

        while True:
            params = {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": int(start_block),
                "endblock": int(end_block),
                "page": page,
                "offset": 1000,
                "sort": "asc",
            }

            data = await self.rotator.make_request(
                session,
                self.BASE_URL,
                params,
                delay=self.delay,
            )

            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        return [
            tx
            for tx in all_txs
            if tx.get("from", "").lower() == address.lower()
            and int(tx.get("isError", "0")) == 0
            and int(tx.get("value", "0")) > 0
        ]

    async def get_internal_transactions(
        self,
        session: aiohttp.ClientSession,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict[str, Any]]:
        all_txs: List[Dict[str, Any]] = []
        page = 1

        while True:
            params = {
                "module": "account",
                "action": "txlistinternal",
                "address": address,
                "startblock": int(start_block),
                "endblock": int(end_block),
                "page": page,
                "offset": 1000,
                "sort": "asc",
            }

            data = await self.rotator.make_request(
                session,
                self.BASE_URL,
                params,
                delay=self.delay,
            )

            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        return [
            tx
            for tx in all_txs
            if tx.get("from", "").lower() == address.lower()
            and int(tx.get("isError", "0")) == 0
            and int(tx.get("value", "0")) > 0
        ]

    async def get_token_transfers(
        self,
        session: aiohttp.ClientSession,
        address: str,
        contract_address: Optional[str] = None,
        start_block: int = 0,
        end_block: int = 99999999,
        filter_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        all_txs: List[Dict[str, Any]] = []
        page = 1

        while True:
            params = {
                "module": "account",
                "action": "tokentx",
                "address": address,
                "startblock": int(start_block),
                "endblock": int(end_block),
                "page": page,
                "offset": 1000,
                "sort": "asc",
            }

            if contract_address:
                params["contractaddress"] = contract_address

            data = await self.rotator.make_request(
                session,
                self.BASE_URL,
                params,
                delay=self.delay,
            )

            txs = data.get("result", [])

            if not txs:
                break

            all_txs.extend(txs)

            if len(txs) < 1000:
                break

            page += 1

        if filter_by in ("from", "to"):
            all_txs = [
                tx
                for tx in all_txs
                if tx.get(filter_by, "").lower() == address.lower()
            ]

        return all_txs


class BscScanExplorerClient(EVMExplorerClient):
    """
    BscScan V2 API.
    Используется для BSC, если задан BSCSCAN_API_KEYS.
    """

    def __init__(self, chain_id: int = 56, weth: Optional[str] = None, delay: float = 0.4):
        super().__init__(chain_id=chain_id, weth=weth or "", delay=delay)
        self.rotator = bscscan_rotator


# ---------------------------------------------------------------------
# Ankr / Moralis / Helius
# ---------------------------------------------------------------------

class AnkrClient:
    def __init__(self, api_url: str):
        self.api_url = api_url

    async def get_multichain_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chains: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
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
            async with session.post(self.api_url, json=payload, timeout=15) as resp:
                if resp.status == 200:
                    increment_api_usage("ankr", 0)
                    return (await resp.json()).get("result", {})
        except Exception as exc:
            logger.error("Ankr API error: %s", exc)

        return {}


class MoralisClient:
    BASE_URL = "https://deep-index.moralis.io/api/v2.2"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-API-Key": api_key}

    async def get_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
        chain: str = "eth",
    ) -> List[Dict[str, Any]]:
        if not self.api_key:
            return []

        try:
            async with session.get(
                f"{self.BASE_URL}/wallets/{address}/tokens?chain={chain}&exclude_spam=true",
                headers=self.headers,
                timeout=30,
            ) as resp:
                if resp.status == 200:
                    increment_api_usage("moralis", 0)
                    return (await resp.json()).get("result", [])
        except Exception as exc:
            logger.error("Moralis API error: %s", exc)

        return []


class HeliusClient:
    BASE_URL = "https://api.helius.xyz/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rpc_url = HELIUS_URL or "https://api.mainnet-beta.solana.com"
        self._semaphore = asyncio.Semaphore(5)

    async def _do_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        params: list,
    ) -> Any:
        async with self._semaphore:
            await asyncio.sleep(0.35)

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }

            try:
                async with session.post(self.rpc_url, json=payload, timeout=15) as resp:
                    if resp.status == 200:
                        increment_api_usage("helius", 0)
                        return (await resp.json()).get("result")
            except Exception as exc:
                logger.debug("Helius RPC error method=%s: %s", method, exc)

            return None

    async def get_wallet_balances(
        self,
        session: aiohttp.ClientSession,
        address: str,
    ) -> Dict[str, Any]:
        if not self.api_key:
            return {}

        url = f"{self.BASE_URL}/wallet/{address}/balances"

        if self.api_key:
            url += f"?api-key={self.api_key}"

        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    increment_api_usage("helius", 0)
                    return await resp.json()
        except Exception as exc:
            logger.error("Helius wallet balances error: %s", exc)

        return {}

    async def get_signatures_for_address(
        self,
        session: aiohttp.ClientSession,
        address: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return await self._do_request(
            session,
            "getSignaturesForAddress",
            [
                address,
                {
                    "limit": int(limit),
                },
            ],
        ) or []

    async def get_transaction(
        self,
        session: aiohttp.ClientSession,
        signature: str,
    ) -> Dict[str, Any]:
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


# ---------------------------------------------------------------------
# Price / token metadata
# ---------------------------------------------------------------------

class DexScreenerPrice:
    async def get_pairs(
        self,
        session: aiohttp.ClientSession,
        address: str,
    ) -> List[Dict[str, Any]]:
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                timeout=10,
            ) as resp:
                increment_api_usage("dexscreener", 0)

                if resp.status == 200:
                    return (await resp.json()).get("pairs", [])
        except Exception as exc:
            logger.debug("DexScreener error for %s: %s", address, exc)

        return []

    async def get_price(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: Optional[str] = None,
    ) -> Optional[float]:
        pairs = await self.get_pairs(session, address)

        if not pairs:
            return None

        if network:
            network_pairs = [
                pair
                for pair in pairs
                if str(pair.get("chainId", "")).lower() == str(network).lower()
            ]

            if network_pairs:
                pairs = network_pairs

        best = max(
            pairs,
            key=lambda pair: float(pair.get("liquidity", {}).get("usd", 0) or 0),
        )

        price = best.get("priceUsd")

        if price:
            return float(price)

        return None


class GeckoTerminalPrice:
    async def get_price(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str = "solana",
    ) -> Optional[float]:
        try:
            async with session.get(
                f"https://api.geckoterminal.com/api/v1/networks/{network}/tokens/{address}",
                timeout=10,
            ) as resp:
                increment_api_usage("geckoterminal", 0)

                if resp.status == 200:
                    price = (
                        (await resp.json())
                        .get("data", {})
                        .get("attributes", {})
                        .get("price_usd")
                    )

                    if price:
                        return float(price)
        except Exception as exc:
            logger.debug("GeckoTerminal error for %s: %s", address, exc)

        return None


class JupiterMassPrice:
    async def get_prices(
        self,
        session: aiohttp.ClientSession,
        mints: List[str],
        batch_size: int = 100,
    ) -> Dict[str, float]:
        prices: Dict[str, float] = {}

        for start in range(0, len(mints), batch_size):
            batch = mints[start:start + batch_size]

            if not batch:
                continue

            ids = ",".join(batch)
            url = f"https://price.jup.ag/v6/price?ids={ids}"

            try:
                async with session.get(url, timeout=15) as resp:
                    increment_api_usage("jupiter", 0)

                    if resp.status == 200:
                        data = (await resp.json()).get("data", {})

                        for mint, info in data.items():
                            if isinstance(info, dict) and info.get("price") is not None:
                                prices[mint] = float(info["price"])
            except Exception as exc:
                logger.debug("Jupiter price error: %s", exc)

            await asyncio.sleep(0.1)

        return prices


class BirdeyePrice:
    async def get_token_overview(
        self,
        session: aiohttp.ClientSession,
        mint: str,
    ) -> Dict[str, Any]:
        if not BIRDEYE_API_KEY:
            return {}

        url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}&x-chain=solana"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}

        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                increment_api_usage("birdeye", 0)

                if resp.status == 200:
                    return (await resp.json()).get("data", {})
        except Exception as exc:
            logger.debug("Birdeye token overview error for %s: %s", mint, exc)

        return {}

    async def get_prices(
        self,
        session: aiohttp.ClientSession,
        mints: List[str],
    ) -> Dict[str, float]:
        prices: Dict[str, float] = {}

        for mint in mints:
            data = await self.get_token_overview(session, mint)

            price = data.get("priceUsd") or data.get("price")

            if price is not None:
                try:
                    prices[mint] = float(price)
                except ValueError:
                    pass

            await asyncio.sleep(0.2)

        return prices


class CascadePriceFetcher:
    """
    Универсальный каскад получения цен.

    Для Solana:
    1. Jupiter
    2. Birdeye
    3. DexScreener
    4. GeckoTerminal

    Для EVM:
    1. DexScreener
    2. GeckoTerminal
    3. EVMWeb3Client router
    """

    def __init__(self, helius: Optional[HeliusClient] = None):
        self.helius = helius
        self.jupiter = JupiterMassPrice()
        self.birdeye = BirdeyePrice()
        self.dexscreener = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()

    async def get_prices(
        self,
        session: aiohttp.ClientSession,
        addresses: List[str],
        network: Optional[str] = None,
    ) -> Dict[str, float]:
        unique_addresses = list(dict.fromkeys([addr for addr in addresses if addr]))
        prices: Dict[str, float] = {}

        if not unique_addresses:
            return prices

        if network == "solana":
            jupiter_prices = await self.jupiter.get_prices(session, unique_addresses)
            prices.update(jupiter_prices)

            remaining = [addr for addr in unique_addresses if addr not in prices]

            if BIRDEYE_API_KEY:
                birdeye_prices = await self.birdeye.get_prices(session, remaining)
                prices.update(birdeye_prices)

            remaining = [addr for addr in remaining if addr not in prices]

            for address in remaining:
                price = await self.dexscreener.get_price(session, address, network="solana")

                if price is not None:
                    prices[address] = price
                    continue

                await asyncio.sleep(0.1)

                price = await self.gecko.get_price(session, address, network="solana")

                if price is not None:
                    prices[address] = price

            return prices

        for address in unique_addresses:
            price = await self.dexscreener.get_price(session, address, network=network)

            if price is not None:
                prices[address] = price
                continue

            await asyncio.sleep(0.1)

            price = await self.gecko.get_price(session, address, network=network or "solana")

            if price is not None:
                prices[address] = price

        return prices


class EVMPriceCascade:
    def __init__(self, web3_client):
        self.dexscreener = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()
        self.web3 = web3_client

    async def get_price(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        network_name: str = "ethereum",
        weth_price: float = 0.0,
    ) -> Optional[float]:
        price = await self.dexscreener.get_price(session, token_address, network=network_name)

        if price is not None:
            return price

        await asyncio.sleep(0.1)

        gecko_network = {
            "ethereum": "eth",
            "eth": "eth",
            "bsc": "bsc",
        }.get(network_name, "eth")

        price = await self.gecko.get_price(session, token_address, network=gecko_network)

        if price is not None:
            return price

        await asyncio.sleep(0.1)

        if self.web3 and weth_price > 0:
            try:
                return await self.web3.get_price_via_router(
                    session,
                    token_address,
                    weth_price,
                )
            except Exception as exc:
                logger.debug("Router price error: %s", exc)

        return None


# ---------------------------------------------------------------------
# EVM RPC
# ---------------------------------------------------------------------

class EVMWeb3Client:
    """
    RPC-клиент для EVM-сетей.

    Используется для:
    - нативного баланса;
    - eth_getLogs ERC20 Transfer;
    - получения текущего блока;
    - price через router.
    """

    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        weth: str,
        router: Optional[str] = None,
        stable: Optional[str] = None,
    ):
        self.rpc_url = rpc_url
        self.chain_id = int(chain_id)
        self.weth_address = weth.lower()
        self.router_address = router.lower() if router else None
        self.stable_address = stable.lower() if stable else None

    async def _rpc_call(
        self,
        session: aiohttp.ClientSession,
        method: str,
        params: List[Any],
    ) -> Any:
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

            if data.get("error"):
                error = data["error"]

                if isinstance(error, dict):
                    error = error.get("message", error)

                raise Exception(error)

            return data.get("result")

    async def get_current_block(self, session: aiohttp.ClientSession) -> int:
        return int(await self._rpc_call(session, "eth_blockNumber", []), 16)

    async def get_balance(self, session: aiohttp.ClientSession, address: str) -> float:
        result = await self._rpc_call(
            session,
            "eth_getBalance",
            [
                address,
                "latest",
            ],
        )

        return int(result, 16) / 10**18

    async def get_price_via_router(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        weth_price_usd: float,
    ) -> Optional[float]:
        """
        Получает цену через getAmountsOut(token -> WETH/WBNB).

        Подходит для Uniswap V2 / PancakeSwap V2-совместимых router.
        """

        if not self.router_address or not self.stable_address:
            return None

        if not token_address or token_address.lower() == self.ZERO_ADDRESS:
            return None

        try:
            data = (
                "0xd06ca61f"
                "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
                "0000000000000000000000000000000000000000000000000000000000000040"
                "0000000000000000000000000000000000000000000000000000000000000002"
                f"{token_address[2:]:0>64}"
                f"{self.weth_address[2:]:0>64}"
            )

            result = await self._rpc_call(
                session,
                "eth_call",
                [
                    {
                        "to": self.router_address,
                        "data": data,
                    },
                    "latest",
                ],
            )

            if not result:
                return None

            amounts_offset = int(result[2:66], 16)
            weth_out = int(result[2 + amounts_offset + 64 * 2:], 16)

            if weth_out > 0:
                return (weth_out / 10**18) * weth_price_usd

        except Exception as exc:
            logger.debug("get_price_via_router error: %s", exc)

        return None

    async def get_block_by_timestamp_approx(
        self,
        session: aiohttp.ClientSession,
        target_timestamp: int,
    ) -> int:
        """
        Приблизительное вычисление блока по времени.

        Точного eth_getBlockByTimestamp в обычном JSON-RPC нет.
        Для BSC берём ~3 сек/блок, для Ethereum ~12 сек/блок.
        """

        current_block = await self.get_current_block(session)
        current_time = int(time.time())

        diff_time = max(0, current_time - int(target_timestamp))

        if self.chain_id == 56:
            block_time = 3
        else:
            block_time = 12

        diff_blocks = diff_time // block_time

        return max(0, current_block - diff_blocks)

    async def get_token_transfers(
        self,
        session: aiohttp.ClientSession,
        address: str,
        direction: str = "to",
        from_block: int = 0,
        to_block: Any = "latest",
    ) -> List[Dict[str, Any]]:
        """
        Сбор ERC20 Transfer через eth_getLogs.

        direction:
        - "to"   = токены, пришедшие на address;
        - "from" = токены, ушедшие из address.
        """

        if direction not in ("to", "from"):
            raise ValueError("direction must be 'to' or 'from'")

        if to_block == "latest":
            to_block = await self.get_current_block(session)

        from_block = int(from_block)
        to_block = int(to_block)

        if from_block > to_block:
            return []

        address = address.lower()
        padded_addr = "0x000000000000000000000000" + address[2:]

        if direction == "to":
            topics = [
                self.TRANSFER_TOPIC,
                None,
                padded_addr,
            ]
        else:
            topics = [
                self.TRANSFER_TOPIC,
                padded_addr,
                None,
            ]

        results: List[Dict[str, Any]] = []
        chunk_size = 4_999

        logger.info(
            "RPC getLogs start: address=%s direction=%s blocks=%s-%s",
            address,
            direction,
            from_block,
            to_block,
        )

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

                for log in logs or []:
                    token_addr = (log.get("address") or "").lower()

                    if not token_addr or token_addr == self.ZERO_ADDRESS:
                        continue

                    block_num = int(log.get("blockNumber", "0x0"), 16)
                    tx_hash = log.get("transactionHash") or log.get("transaction_hash") or ""
                    value_hex = log.get("data") or "0x0"
                    value_wei = int(value_hex, 16) if value_hex != "0x" else 0

                    log_topics = log.get("topics") or []

                    from_addr = ""
                    to_addr = ""

                    if len(log_topics) >= 2:
                        from_addr = "0x" + log_topics[1][26:]

                    if len(log_topics) >= 3:
                        to_addr = "0x" + log_topics[2][26:]

                    if direction == "from":
                        if not to_addr or to_addr == self.ZERO_ADDRESS:
                            continue

                        results.append(
                            {
                                "token_address": token_addr,
                                "tx_hash": tx_hash,
                                "block_number": block_num,
                                "blockNumber": block_num,
                                "from": from_addr.lower(),
                                "to": to_addr.lower(),
                                "value_wei": value_wei,
                            }
                        )

                    else:
                        results.append(
                            {
                                "token_address": token_addr,
                                "tx_hash": tx_hash,
                                "block_number": block_num,
                                "blockNumber": block_num,
                                "from": from_addr.lower(),
                                "to": to_addr.lower(),
                                "value_wei": value_wei,
                            }
                        )

            except Exception as exc:
                logger.debug("RPC getLogs chunk failed %s-%s: %s", start_b, end_b, exc)

            await asyncio.sleep(0.05)

        return results


# ---------------------------------------------------------------------
# Token metadata
# ---------------------------------------------------------------------

class TokenInfoService:
    @staticmethod
    async def get_symbol(
        session: aiohttp.ClientSession,
        token_address: str,
        rpc_url: str,
    ) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": token_address,
                    "data": "0x95d89b41",
                },
                "latest",
            ],
            "id": 1,
        }

        try:
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    res_str = (await resp.json()).get("result", "")

                    if not res_str or res_str == "0x":
                        return "?"

                    raw_hex = res_str[2:] if res_str.startswith("0x") else res_str

                    try:
                        if len(raw_hex) >= 128:
                            length = int(raw_hex[64:128], 16)

                            if 0 < length < 64:
                                data = raw_hex[128:128 + (length * 2)]
                                return (
                                    bytes
                                    .fromhex(data)
                                    .decode("utf-8", errors="ignore")
                                    .strip()
                                )
                    except Exception:
                        pass

                    try:
                        symbol = (
                            bytes
                            .fromhex(raw_hex)
                            .decode("utf-8", errors="ignore")
                            .replace("\x00", "")
                            .strip()
                        )

                        symbol = re.sub(r"[^A-Za-z0-9_$-]", "", symbol)

                        if symbol:
                            return symbol
                    except Exception:
                        pass

        except Exception as exc:
            logger.debug("Token symbol error for %s: %s", token_address, exc)

        return "?"

    @staticmethod
    async def get_decimals(
        session: aiohttp.ClientSession,
        token_address: str,
        rpc_url: str,
    ) -> int:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": token_address,
                    "data": "0x313ce567",
                },
                "latest",
            ],
            "id": 1,
        }

        try:
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    result = (await resp.json()).get("result")

                    if result and result != "0x":
                        return int(result, 16)
        except Exception as exc:
            logger.debug("Token decimals error for %s: %s", token_address, exc)

        return 18