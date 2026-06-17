"""
API clients.

В этой версии:
- BSCScan API не используется;
- BSC история идёт через публичный BSC RPC;
- цены ищутся через DexScreener -> GeckoTerminal;
- все HTTP-запросы имеют timeout;
- Solana балансы берутся напрямую через Helius RPC.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from web3 import Web3


logger = logging.getLogger(__name__)


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _hex_to_address(value: str) -> str:
    try:
        return "0x" + value.replace("0x", "")[-40:].lower()
    except Exception:
        return ""


def _hex_to_int(value: Any) -> int:
    try:
        return int(str(value), 16)
    except Exception:
        return 0


class TokenInfoService:
    @staticmethod
    async def get_symbol(session, contract_address: str, rpc_url: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": contract_address,
                    "data": "0x95d89b41",
                },
                "latest",
            ],
            "id": 1,
        }

        try:
            async with session.post(rpc_url, json=payload, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")

                    if result and result != "0x":
                        return Web3.to_text(result).strip("\x00")
        except Exception:
            pass

        return "?"

    @staticmethod
    async def get_name(session, contract_address: str, rpc_url: str) -> str:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": contract_address,
                    "data": "0x06fdde03",
                },
                "latest",
            ],
            "id": 1,
        }

        try:
            async with session.post(rpc_url, json=payload, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")

                    if result and result != "0x":
                        return Web3.to_text(result).strip("\x00")
        except Exception:
            pass

        return "?"

    @staticmethod
    async def get_decimals(session, contract_address: str, rpc_url: str) -> int:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [
                {
                    "to": contract_address,
                    "data": "0x313ce567",
                },
                "latest",
            ],
            "id": 1,
        }

        try:
            async with session.post(rpc_url, json=payload, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")

                    if result:
                        return int(result, 16)
        except Exception:
            pass

        return 18


class EVMWeb3Client:
    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        weth: str,
        router: str = "",
        stable: str = "",
        chunk_size: int = 1000,
    ):
        self.rpc_url = rpc_url
        self.chain_id = int(chain_id)
        self.weth = (weth or "").lower()
        self.router = router or ""
        self.stable = stable or ""
        self.chunk_size = 1000 if chain_id == 56 else 4_999

    async def _rpc_call(self, session, method: str, params: list) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }

        async with session.post(self.rpc_url, json=payload, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError(f"RPC HTTP {resp.status}")

            data = await resp.json()

            if "error" in data:
                raise RuntimeError(data["error"])

            return data.get("result")

    async def get_balance(self, session, address: str) -> float:
        result = await self._rpc_call(session, "eth_getBalance", [address, "latest"])
        return _hex_to_int(result) / 10**18

    async def get_current_block(self, session) -> int:
        result = await self._rpc_call(session, "eth_blockNumber", [])
        return _hex_to_int(result)

    async def get_block_by_timestamp_approx(self, session, timestamp: int) -> int:
        latest = await self.get_current_block(session)
        return max(0, latest - 500_000)

    async def get_token_transfers(
        self,
        session,
        address: str,
        direction: str,
        from_block: int,
        to_block: int,
    ) -> List[Dict[str, Any]]:
        address = address.lower()
        transfers = []

        for start in range(int(from_block), int(to_block) + 1, self.chunk_size):
            end = min(start + self.chunk_size - 1, int(to_block))

            topics = [TRANSFER_TOPIC]

            if direction == "to":
                topics.extend([None, address])
            elif direction == "from":
                topics.extend([address, None])

            params = [
                {
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": topics,
                }
            ]

            try:
                logs = await self._rpc_call(session, "eth_getLogs", params)
            except Exception as exc:
                logger.warning("eth_getLogs failed %s-%s: %s", start, end, exc)
                await asyncio.sleep(1)
                continue

            for log in logs or []:
                try:
                    if len(log.get("topics") or []) < 3:
                        continue

                    tx_hash = str(log.get("transactionHash") or "").lower()
                    contract_address = str(log.get("address") or "").lower()

                    from_addr = _hex_to_address(log["topics"][1])
                    to_addr = _hex_to_address(log["topics"][2])

                    if direction == "to" and to_addr != address:
                        continue

                    if direction == "from" and from_addr != address:
                        continue

                    amount = int.from_bytes(
                        bytes.fromhex(log.get("data", "0x").replace("0x", "")),
                        "big",
                    )

                    transfers.append(
                        {
                            "tx_hash": tx_hash,
                            "token_address": contract_address,
                            "from": from_addr,
                            "to": to_addr,
                            "amount_raw": amount,
                            "value_wei": amount,
                            "block_number": _hex_to_int(log.get("blockNumber")),
                            "blockNumber": _hex_to_int(log.get("blockNumber")),
                        }
                    )
                except Exception:
                    continue

        return transfers


class EVMExplorerClient:
    """
    Etherscan API V2 client.
    Работает для Ethereum chain_id=1 и BSC chain_id=56.
    """

    BASE_V2_URL = "https://api.etherscan.io/v2/api"

    def __init__(self, chain_id: int, weth: str, api_keys: List[str] = None):
        self.chain_id = int(chain_id)
        self.weth = (weth or "").lower()
        self.api_keys = list(api_keys or [])
        self.service_name = "etherscan" if chain_id == 1 else "bscscan"

    async def _request_page_raw(
        self,
        session,
        action: str,
        params: Dict[str, Any],
        page: int,
        offset: int,
    ) -> Any:
        if not self.api_keys:
            raise RuntimeError("Explorer API keys не заданы")

        for key_index, key in enumerate(self.api_keys):
            request_params = {
                "chainid": self.chain_id,
                "module": "account",
                "action": action,
                "page": page,
                "offset": offset,
                "sort": "asc",
                "apikey": key,
            }

            request_params.update(params)

            try:
                async with session.get(
                    self.BASE_V2_URL,
                    params=request_params,
                    timeout=20,
                ) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    result = data.get("result")

                    if isinstance(result, str):
                        lower = result.lower()

                        if "no transactions" in lower or "no records found" in lower:
                            return []

                        if "rate limit" in lower or "max rate limit" in lower:
                            await asyncio.sleep(2)
                            continue

                        raise RuntimeError(result)

                    from bot.database import increment_api_usage

                    increment_api_usage(self.service_name, key_index)

                    return result

            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue

            except Exception:
                await asyncio.sleep(1)
                continue

        raise RuntimeError(f"Explorer API не ответил: {self.service_name}/{action}")

    async def _request_page(
        self,
        session,
        action: str,
        params: Dict[str, Any],
        page: int,
        offset: int,
    ) -> List[Dict[str, Any]]:
        result = await self._request_page_raw(
            session,
            action,
            params,
            page,
            offset,
        )

        if isinstance(result, list):
            return result

        return []

    async def _request_all_pages(
        self,
        session,
        action: str,
        params: Dict[str, Any],
        offset: int = 10_000,
    ) -> List[Dict[str, Any]]:
        result = []
        page = 1

        while True:
            chunk = await self._request_page(
                session,
                action,
                params,
                page=page,
                offset=offset,
            )

            if not chunk:
                break

            result.extend(chunk)
            page += 1

            if len(chunk) < offset:
                break

            if len(result) >= 100_000:
                break

        return result

    async def get_block_by_timestamp(self, session, timestamp: int) -> int:
        result = await self._request_page_raw(
            session,
            "getblocknobytime",
            {
                "timestamp": int(timestamp),
                "closest": "before",
            },
            page=1,
            offset=1,
        )

        if isinstance(result, str):
            try:
                return int(result)
            except Exception:
                return 0

        if isinstance(result, list) and result:
            try:
                return int(result[0].get("blockNumber") or 0)
            except Exception:
                return 0

        return 0

    async def get_token_transfers(
        self,
        session,
        address: str,
        start_block: int,
        end_block: int,
        filter_by: str = None,
        contract_address: str = None,
    ) -> List[Dict[str, Any]]:
        params = {
            "address": address,
            "startblock": int(start_block),
            "endblock": int(end_block),
        }

        if contract_address:
            params["contractaddress"] = contract_address

        txs = await self._request_all_pages(
            session,
            "tokentx",
            params,
        )

        filtered = []

        for tx in txs:
            if filter_by == "to" and str(tx.get("to", "")).lower() != address.lower():
                continue

            if filter_by == "from" and str(tx.get("from", "")).lower() != address.lower():
                continue

            filtered.append(tx)

        return filtered

    async def get_normal_transactions(
        self,
        session,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict[str, Any]]:
        return await self._request_all_pages(
            session,
            "txlist",
            {
                "address": address,
                "startblock": int(start_block),
                "endblock": int(end_block),
            },
        )

    async def get_internal_transactions(
        self,
        session,
        address: str,
        start_block: int,
        end_block: int,
    ) -> List[Dict[str, Any]]:
        return await self._request_all_pages(
            session,
            "txlistinternal",
            {
                "address": address,
                "startblock": int(start_block),
                "endblock": int(end_block),
            },
        )

    async def get_account_balance(self, session, address: str) -> float:
        result = await self._request_page_raw(
            session,
            "balance",
            {
                "address": address,
            },
            page=1,
            offset=1,
        )

        if isinstance(result, str):
            try:
                return float(result) / 10**18
            except Exception:
                return 0.0

        return 0.0


class BscScanExplorerClient(EVMExplorerClient):
    def __init__(self, chain_id: int = 56, weth: str = None, api_keys: List[str] = None):
        super().__init__(
            chain_id=chain_id,
            weth=weth or "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
            api_keys=api_keys or [],
        )


class MoralisClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_balances(self, session, address: str, chain: str = "eth") -> List[Dict[str, Any]]:
        if not self.api_key:
            return []

        url = f"https://deep-index.moralis.io/api/v2/{address}/erc20"

        try:
            async with session.get(
                url,
                params={"chain": chain},
                headers={"X-API-Key": self.api_key},
                timeout=20,
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                result = []

                for item in data or []:
                    decimals = int(item.get("decimals") or item.get("tokenDecimal") or 0)
                    balance_raw = item.get("balance") or 0

                    try:
                        balance = int(balance_raw) / (10**decimals)
                    except Exception:
                        balance = 0.0

                    usd_price = float(item.get("usd_price") or item.get("usdPrice") or 0)

                    result.append(
                        {
                            "contract_address": str(
                                item.get("token_address") or item.get("address") or ""
                            ).lower(),
                            "symbol": item.get("symbol") or "?",
                            "balance_formatted": balance,
                            "decimals": decimals,
                            "tokenDecimal": decimals,
                            "balance": balance_raw,
                            "usd_value": balance * usd_price,
                        }
                    )

                return result

        except Exception as exc:
            logger.warning("Moralis balances error: %s", exc)
            return []


class AnkrClient:
    def __init__(self, api_url: str):
        self.api_url = api_url

    async def get_multichain_balances(
        self,
        session,
        address: str,
        chains: List[str],
    ) -> Dict[str, Any]:
        if not self.api_url:
            return {"assets": []}

        assets = []

        for chain in chains:
            payload = {
                "jsonrpc": "2.0",
                "method": "ankr_getAccountBalance",
                "params": {
                    "wallet": address,
                    "chain": chain,
                },
                "id": 1,
            }

            try:
                async with session.post(self.api_url, json=payload, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result") or {}

                        for item in result.get("assets", []) or []:
                            item["blockchain"] = chain
                            assets.append(item)

            except Exception as exc:
                logger.warning("Ankr balances error chain=%s: %s", chain, exc)

        return {"assets": assets}


class DexScreenerPrice:
    async def get_pairs(self, session, address: str) -> List[Dict[str, Any]]:
        try:
            async with session.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}",
                timeout=8,
            ) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                return data.get("pairs") or []

        except Exception as exc:
            logger.debug("DexScreener get_pairs error: %s", exc)
            return []


async def _dexscreener_pair(session, address: str, network: str):
    chain_map = {
        "ethereum": "ethereum",
        "eth": "ethereum",
        "bsc": "bsc",
        "bnb": "bsc",
        "binance-smart-chain": "bsc",
    }

    chain = chain_map.get((network or "").lower(), (network or "").lower())

    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}",
            timeout=8,
        ) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            pairs = data.get("pairs") or []

            if not pairs:
                return None

            if chain:
                filtered = [
                    pair
                    for pair in pairs
                    if str(pair.get("chainId", "")).lower() == chain
                ]

                if filtered:
                    pairs = filtered

            if not pairs:
                return None

            return max(
                pairs,
                key=lambda pair: float(
                    pair.get("liquidity", {}).get("usd", 0) or 0
                ),
            )

    except Exception as exc:
        logger.debug("DexScreener pair error for %s: %s", address, exc)
        return None


async def get_dexscreener_price(
    session,
    address: str,
    network: str,
) -> Optional[float]:
    pair = await _dexscreener_pair(session, address, network)

    if not pair:
        return None

    try:
        price = float(pair.get("priceUsd") or 0)
    except Exception:
        return None

    return price if price > 0 else None


async def get_geckoterminal_price(
    session,
    address: str,
    network: str,
) -> Optional[float]:
    network_map = {
        "ethereum": "ethereum",
        "eth": "ethereum",
        "bsc": "bsc",
        "bnb": "bsc",
        "binance-smart-chain": "bsc",
    }

    network_slug = network_map.get((network or "").lower(), (network or "").lower())

    try:
        async with session.get(
            f"https://api.geckoterminal.com/api/v2/networks/{network_slug}/tokens/{address}",
            timeout=8,
        ) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            attrs = data.get("data", {}).get("attributes", {})
            price = float(attrs.get("price_usd") or 0)

            return price if price > 0 else None

    except Exception as exc:
        logger.debug("GeckoTerminal price error for %s: %s", address, exc)
        return None


async def get_token_meta_cascade(
    session,
    address: str,
    network: str,
    rpc_url: str = None,
) -> Dict[str, Any]:
    result = {
        "symbol": "?",
        "name": "?",
        "price_usd": None,
        "source": None,
    }

    pair = await _dexscreener_pair(session, address, network)

    if pair:
        base = pair.get("baseToken", {}) or {}

        result.update(
            {
                "symbol": base.get("symbol") or "?",
                "name": base.get("name") or "?",
                "source": "dexscreener",
            }
        )

        try:
            price = float(pair.get("priceUsd") or 0)
            result["price_usd"] = price if price > 0 else None
        except Exception:
            pass

    if result["price_usd"] is None:
        result["price_usd"] = await get_geckoterminal_price(
            session,
            address,
            network,
        )

        if result["price_usd"] is not None:
            result["source"] = "geckoterminal"

    if rpc_url:
        try:
            symbol = await TokenInfoService.get_symbol(
                session,
                address,
                rpc_url,
            )

            if symbol:
                result["symbol"] = symbol
        except Exception:
            pass

        try:
            name = await TokenInfoService.get_name(
                session,
                address,
                rpc_url,
            )

            if name:
                result["name"] = name
        except Exception:
            pass

    return result


async def get_evm_token_symbols(
    session,
    tokens: List[str],
    network: str,
    rpc_url: str,
) -> Dict[str, Dict[str, Any]]:
    result = {}

    unique_tokens = list(
        dict.fromkeys(
            [
                str(token).lower()
                for token in tokens
                if str(token)
            ]
        )
    )[:100]

    for token in unique_tokens:
        result[token] = await get_token_meta_cascade(
            session,
            token,
            network,
            rpc_url,
        )

        await asyncio.sleep(0.02)

    return result


class EVMPriceCascade:
    def __init__(self, web3_client: EVMWeb3Client):
        self.web3_client = web3_client

    async def get_price(self, session, token_address: str, network: str) -> Optional[float]:
        price = await get_dexscreener_price(session, token_address, network)

        if price is not None:
            return price

        return await get_geckoterminal_price(session, token_address, network)


class CascadePriceFetcher:
    def __init__(self, helius: Any = None):
        self.helius = helius

    async def get_prices(
        self,
        session,
        addresses: List[str],
        network: str,
    ) -> Dict[str, float]:
        result = {}

        for address in addresses:
            price = await get_dexscreener_price(session, address, network)

            if price is None:
                price = await get_geckoterminal_price(session, address, network)

            if price:
                result[address] = price

            await asyncio.sleep(0.05)

        return result


class HeliusClient:
    BASE_URL = "https://api.helius.xyz/v1"
    RPC_URL = "https://mainnet.helius-rpc.com"
    NATIVE_SOL_MINT = "So11111111111111111111111111111111111111111"
    TOKEN_PROGRAMS = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFjc8R9fJzCPNQ6KTUu",
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key

        if api_key:
            self.rpc_url = f"{self.RPC_URL}/?api-key={api_key}"
        else:
            self.rpc_url = self.RPC_URL

        self._semaphore = asyncio.Semaphore(8)

    async def _do_request(
        self,
        session,
        method: str,
        params: list,
    ) -> Any:
        async with self._semaphore:
            await asyncio.sleep(0.15)

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }

            try:
                async with session.post(self.rpc_url, json=payload, timeout=20) as resp:
                    if resp.status == 200:
                        from bot.database import increment_api_usage

                        increment_api_usage("helius", 0)
                        return (await resp.json()).get("result")
            except Exception as exc:
                logger.debug("Helius RPC error method=%s: %s", method, exc)

            return None

    async def _get_wallet_balances_rest_metadata(
        self,
        session,
        address: str,
    ) -> Dict[str, Dict[str, Any]]:
        if not self.api_key:
            return {}

        url = f"{self.BASE_URL}/wallet/{address}/balances?api-key={self.api_key}"

        try:
            async with session.get(url, timeout=20) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    metadata: Dict[str, Dict[str, Any]] = {}

                    for token in data.get("balances", []) or []:
                        mint = token.get("mint")

                        if not mint:
                            continue

                        metadata[mint] = {
                            "symbol": token.get("symbol"),
                            "name": token.get("name"),
                            "usdValue": token.get("usdValue"),
                        }

                    return metadata
        except Exception as exc:
            logger.debug("Helius REST metadata error: %s", exc)

        return {}

    async def get_wallet_balances(
        self,
        session,
        address: str,
    ) -> Dict[str, Any]:
        if not address:
            return {"balances": []}

        balances_by_mint: Dict[str, Dict[str, Any]] = {}

        native_balance = await self._do_request(
            session,
            "getBalance",
            [
                address,
                "confirmed",
            ],
        )

        if isinstance(native_balance, dict):
            native_balance = native_balance.get("value")

        try:
            native_ui = float(native_balance or 0) / 10**9
        except Exception:
            native_ui = 0.0

        if native_ui > 0:
            balances_by_mint[self.NATIVE_SOL_MINT] = {
                "mint": self.NATIVE_SOL_MINT,
                "symbol": "SOL",
                "name": "Solana",
                "balance": native_ui,
                "rawAmount": int(native_ui * 10**9),
                "decimals": 9,
                "usdValue": None,
            }

        for program_id in self.TOKEN_PROGRAMS:
            try:
                result = await self._do_request(
                    session,
                    "getTokenAccountsByOwner",
                    [
                        address,
                        {
                            "programId": program_id,
                        },
                        {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                        },
                    ],
                )

                if not result:
                    continue

                for item in result.get("value", []) or []:
                    try:
                        parsed = item.get("account", {}).get("data", {}).get("parsed", {})

                        if parsed.get("type") != "account":
                            continue

                        info = parsed.get("info", {})
                        mint = info.get("mint")

                        if not mint:
                            continue

                        token_amount = info.get("tokenAmount", {}) or {}
                        raw_amount = int(token_amount.get("rawAmount") or 0)
                        decimals = int(token_amount.get("decimals") or 0)

                        if raw_amount <= 0:
                            continue

                        ui_amount = raw_amount / (10**decimals)

                        current = balances_by_mint.get(mint)

                        if current:
                            current["rawAmount"] = int(current.get("rawAmount") or 0) + raw_amount
                            current["balance"] = float(current.get("balance") or 0) + ui_amount
                            current["decimals"] = decimals
                        else:
                            balances_by_mint[mint] = {
                                "mint": mint,
                                "symbol": "?",
                                "name": "?",
                                "balance": ui_amount,
                                "rawAmount": raw_amount,
                                "decimals": decimals,
                                "usdValue": None,
                            }

                    except Exception as exc:
                        logger.debug("Helius token account parse error: %s", exc)

            except Exception as exc:
                logger.error("Helius getTokenAccountsByOwner error program=%s: %s", program_id, exc)

        metadata = await self._get_wallet_balances_rest_metadata(session, address)

        balances = []

        for mint, data in balances_by_mint.items():
            meta = metadata.get(mint, {})

            if meta.get("symbol"):
                data["symbol"] = meta["symbol"]

            if meta.get("name"):
                data["name"] = meta["name"]

            if meta.get("usdValue") is not None:
                data["usdValue"] = meta["usdValue"]

            balances.append(data)

        balances.sort(
            key=lambda item: (
                float(item.get("usdValue") or 0)
                if item.get("usdValue") is not None
                else -1
            ),
            reverse=True,
        )

        return {
            "balances": balances,
            "nativeBalance": native_ui,
        }

    async def get_signatures_for_address(
        self,
        session,
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
        session,
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