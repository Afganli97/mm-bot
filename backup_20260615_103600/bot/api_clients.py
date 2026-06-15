"""
Клиенты для Etherscan (Только Ethereum), Ankr, RPC (Альтернатива BSC), Helius, Moralis.
"""
import asyncio
import logging
import re
import time
from typing import Optional, List, Dict, Any, Set
import aiohttp
from datetime import date

from bot.config import (
    ETHERSCAN_API_KEYS, ANKR_API_URL,
    HELIUS_API_KEY, HELIUS_URL, BIRDEYE_API_KEY, MORALIS_API_KEY
)
from bot.database import increment_api_usage, get_api_usage_today, get_connection

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
ANKR_DAILY_LIMIT = 100_000
MORALIS_DAILY_LIMIT = 1500

class APIKeyRotator:
    def __init__(self, keys, service, daily_limit):
        self.keys = keys
        self.service = service
        self.daily_limit = daily_limit
        
    def _reset_old_if_needed(self, idx):
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
        
    async def make_request(self, session, url, params=None, headers=None, delay=0.4):
        if self.service in ("etherscan", "moralis"):
            await asyncio.sleep(delay)
        for attempt in range(len(self.keys)):
            key_info = self.get_available_key()
            if not key_info:
                raise Exception(f"Лимит {self.service} исчерпан")
            key, idx = key_info
            
            if self.service == "etherscan":
                params = params or {}
                params["apikey"] = key
                params["chainid"] = "1" # Etherscan V2 требует указания чейна. Жестко фиксируем ETH (1)
            elif self.service == "moralis":
                headers = headers or {}
                headers["X-API-Key"] = key
                
            try:
                if self.service in ("etherscan", "moralis"):
                    async with session.get(url, params=params, headers=headers, timeout=30) as resp:
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
                                    raise Exception(f"Etherscan Error: {data.get('result', 'Unknown')}")
                            else:
                                increment_api_usage(self.service, idx)
                                return data
                        elif resp.status == 429:
                            await asyncio.sleep(1)
                            continue
                        else:
                            raise Exception(f"HTTP {resp.status} от {self.service}")
                else:
                    async with session.post(url, json=params, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            increment_api_usage(self.service, idx)
                            return data
                        elif resp.status == 429:
                            await asyncio.sleep(1)
                            continue
                        else:
                            raise Exception(f"HTTP {resp.status} от {self.service}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка API {self.service}: {e}")
                raise
        raise Exception(f"Все попытки запроса к {self.service} исчерпаны")

etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)

class EVMExplorerClient:
    """Клиент для Etherscan API V2 (только для Ethereum)"""
    BASE_URL = "https://api.etherscan.io/v2/api"
    def __init__(self, chain_id, weth, delay=0.4):
        self.chain_id = chain_id
        self.weth_address = weth.lower()
        self.delay = delay
        self.rotator = etherscan_rotator

    async def get_block_by_timestamp(self, session, timestamp):
        params = {"module": "block", "action": "getblocknobytime", "timestamp": timestamp, "closest": "before"}
        data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay)
        return int(data["result"])

    async def get_normal_transactions(self, session, address, start_block, end_block):
        all_txs = []
        page = 1
        while True:
            params = {"module": "account", "action": "txlist", "address": address, "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay)
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
            params = {"module": "account", "action": "txlistinternal", "address": address, "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay)
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
            params = {"module": "account", "action": "tokentx", "address": address, "startblock": start_block, "endblock": end_block, "page": page, "offset": 1000, "sort": "asc"}
            if contract_address: params["contractaddress"] = contract_address
            data = await self.rotator.make_request(session, self.BASE_URL, params, delay=self.delay)
            txs = data.get("result", [])
            if not txs: break
            all_txs.extend(txs)
            if len(txs) < 1000: break
            page += 1
        if filter_by:
            all_txs = [tx for tx in all_txs if tx[filter_by].lower() == address.lower()]
        return all_txs

class AnkrClient:
    def __init__(self, api_url): self.api_url = api_url
    async def get_multichain_balances(self, session, address, chains=None):
        payload = {"jsonrpc": "2.0", "method": "ankr_getAccountBalance", "params": {"blockchain": chains or ["eth", "bsc"], "walletAddress": address}, "id": 1}
        try:
            async with session.post(self.api_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    increment_api_usage("ankr", 0)
                    return (await resp.json()).get("result", {})
        except Exception as e: logger.error(f"Ankr API error: {e}")
        return {}

class MoralisClient:
    BASE_URL = "https://deep-index.moralis.io/api/v2.2"
    def __init__(self, api_key: str): self.headers = {"X-API-Key": api_key}
    async def get_balances(self, session, address: str, chain: str = "eth") -> List[Dict]:
        try:
            async with session.get(f"{self.BASE_URL}/wallets/{address}/tokens?chain={chain}&exclude_spam=true", headers=self.headers, timeout=30) as resp:
                if resp.status == 200:
                    increment_api_usage("moralis", 0)
                    return (await resp.json()).get("result", [])
        except: pass
        return []

class HeliusClient:
    BASE_URL = "https://api.helius.xyz/v1"
    RPC_URL = "https://mainnet.helius-rpc.com"
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._semaphore = asyncio.Semaphore(5)

    async def _do_request(self, session, method: str, params: list) -> Any:
        async with self._semaphore:
            await asyncio.sleep(0.35)
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                async with session.post(f"{self.RPC_URL}/?api-key={self.api_key}", json=payload, timeout=10) as resp:
                    if resp.status == 200: return (await resp.json()).get("result")
            except: pass
            return None

    async def get_wallet_balances(self, session, address: str) -> Dict:
        try:
            async with session.get(f"{self.BASE_URL}/wallet/{address}/balances?api-key={self.api_key}", timeout=10) as resp:
                if resp.status == 200: return await resp.json()
        except: pass
        return {}

    async def get_signatures_for_address(self, session, address: str, limit: int = 100) -> List[Dict]:
        return await self._do_request(session, "getSignaturesForAddress", [address, {"limit": limit}]) or []

    async def get_transaction(self, session, signature: str) -> Dict:
        return await self._do_request(session, "getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) or {}

class DexScreenerPrice:
    async def get_price(self, session, mint: str) -> Optional[float]:
        try:
            async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=5) as resp:
                if resp.status == 200:
                    pairs = (await resp.json()).get("pairs")
                    if pairs:
                        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                        price = best.get("priceUsd")
                        if price: return float(price)
        except: pass
        return None

class GeckoTerminalPrice:
    async def get_price(self, session, mint: str, network: str = "solana") -> Optional[float]:
        try:
            async with session.get(f"https://api.geckoterminal.com/api/v1/networks/{network}/tokens/{mint}", timeout=5) as resp:
                if resp.status == 200:
                    price = (await resp.json()).get("data", {}).get("attributes", {}).get("price_usd")
                    if price: return float(price)
        except: pass
        return None

class EVMPriceCascade:
    def __init__(self, web3_client):
        self.dexscr = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()
        self.web3 = web3_client

    async def get_price(self, session, token_address: str, network_name: str = "ethereum", weth_price: float = 0.0) -> Optional[float]:
        price = await self.dexscr.get_price(session, token_address)
        if price: return price
        await asyncio.sleep(0.2)
        gecko_net = {"ethereum": "eth", "bsc": "bsc", "eth": "eth"}.get(network_name, "eth")
        price = await self.gecko.get_price(session, token_address, gecko_net)
        if price: return price
        await asyncio.sleep(0.2)
        if self.web3:
            try: return await self.web3.get_price_via_router(session, token_address, weth_price)
            except: pass
        return None

class EVMWeb3Client:
    """Альтернативный RPC Клиент для сетей без бесплатного эксплорера (BSC)."""
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
            if "error" in data: raise Exception(data["error"])
            return data["result"]

    async def get_current_block(self, session):
        return int(await self._rpc_call(session, "eth_blockNumber", []), 16)

    async def get_balance(self, session, address: str) -> float:
        return int(await self._rpc_call(session, "eth_getBalance", [address, "latest"]), 16) / 10**18

    async def get_price_via_router(self, session, token_address: str, weth_price_usd: float) -> Optional[float]:
        if not self.router_address or not self.stable_address: return None
        try:
            data = (f"0xd06ca61f0000000000000000000000000000000000000000000000000de0b6b3a7640000"
                    f"00000000000000000000000000000000000000000000000000000000000000400000000000000000000000000000000000000000000000000000000000000002"
                    f"000000000000000000000000{token_address[2:]:0>64}000000000000000000000000{self.weth_address[2:]:0>64}")
            result = await self._rpc_call(session, "eth_call", [{"to": self.router_address, "data": data}, "latest"])
            if result:
                amounts_offset = int(result[2:66], 16)
                weth_out = int(result[2 + amounts_offset + 64*2:], 16)
                if weth_out > 0: return (weth_out / 1e18) * weth_price_usd
        except: pass
        return None

    # ---- АЛЬТЕРНАТИВА ДЛЯ BSC (РАБОТА ЧЕРЕЗ RPC) ----
    
    async def get_block_by_timestamp_approx(self, session, target_timestamp: int) -> int:
        """Математическое вычисление блока по времени (т.к. у RPC нет эндпоинта по времени)."""
        current_block = await self.get_current_block(session)
        current_time = int(time.time())
        diff_time = current_time - target_timestamp
        # BSC: ~3 сек на блок. Если сеть другая - ставим дефолт 12
        block_time = 3 if self.chain_id == 56 else 12 
        diff_blocks = diff_time // block_time
        return max(0, current_block - diff_blocks)

    async def get_token_transfers(self, session, address, direction="to", from_block=0, to_block="latest"):
        """Сбор трансферов токенов (ERC20) через eth_getLogs чанками по 4999 блоков."""
        if to_block == "latest":
            to_block = await self.get_current_block(session)
            
        TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        padded_addr = "0x000000000000000000000000" + address[2:].lower()
        
        # Направление: Если "to", то наш адрес должен быть получателем (topic2). Если "from" - отправителем (topic1)
        topics = [TRANSFER_TOPIC, None, padded_addr] if direction == "to" else [TRANSFER_TOPIC, padded_addr]
        
        results = []
        chunk_size = 4999 # Безопасный чанк для публичных RPC
        
        logger.info(f"RPC getLogs старт: адрес {address}, направление {direction}, блоки {from_block}-{to_block}")
        
        for start_b in range(from_block, to_block + 1, chunk_size):
            end_b = min(start_b + chunk_size - 1, to_block)
            params = [{
                "fromBlock": hex(start_b),
                "toBlock": hex(end_b),
                "topics": topics
            }]
            try:
                logs = await self._rpc_call(session, "eth_getLogs", params)
                for log in logs:
                    token_addr = log.get('address', '').lower()
                    block_num = int(log.get('blockNumber', '0x0'), 16)
                    
                    if direction == "from":
                        # Если мы ищем получателей, достаем их из Topic 2
                        if len(log.get('topics', [])) >= 3:
                            to_addr = "0x" + log['topics'][2][26:]
                            value_hex = log.get('data', '0x0')
                            value_wei = int(value_hex, 16) if value_hex != '0x' else 0
                            results.append({
                                'to': to_addr.lower(),
                                'value_wei': value_wei,
                                'blockNumber': block_num
                            })
                    else:
                        # Если мы ищем входящие покупки (какой токен куплен)
                        results.append({
                            'token_address': token_addr,
                            'tx_hash': log.get('transactionHash', ''),
                            'block_number': block_num
                        })
            except Exception as e:
                logger.debug(f"RPC getLogs chunk failed {start_b}-{end_b}: {e}")
                
            await asyncio.sleep(0.05) # Защита от Rate-Limit
            
        return results

class TokenInfoService:
    @staticmethod
    async def get_symbol(session, token_address, rpc_url):
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": token_address, "data": "0x95d89b41"}, "latest"], "id": 1}
        try:
            async with session.post(rpc_url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    res_str = (await resp.json()).get('result', '')
                    if res_str and res_str != '0x':
                        raw_hex = res_str[2:]
                        try:
                            if len(raw_hex) >= 128:
                                length = int(raw_hex[64:128], 16)
                                if 0 < length < 64: return bytes.fromhex(raw_hex[128:128+(length*2)]).decode('utf-8', errors='ignore').strip()
                        except: pass
                        try:
                            symbol = bytes.fromhex(raw_hex).decode('utf-8', errors='ignore').replace('\x00', '').strip()
                            symbol = re.sub(r'[^A-Za-z0-9_$-]', '', symbol)
                            if symbol: return symbol
                        except: pass
        except: pass
        return "?"
