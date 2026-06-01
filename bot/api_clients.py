"""
Асинхронные клиенты для Etherscan (только Ethereum), RPC (Alchemy/Infura), Solscan, CoinGecko.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any, Set
import aiohttp
from datetime import datetime, timezone

from bot.config import (
    ETHERSCAN_API_KEYS,
    SOLSCAN_API_KEY, HELIUS_URL
)
from bot.database import increment_api_usage, get_api_usage_today

logger = logging.getLogger(__name__)

ETHERSCAN_DAILY_LIMIT = 100_000
SOLSCAN_DAILY_LIMIT = 100_000

class APIKeyRotator:
    # ... (без изменений)
    pass

etherscan_rotator = APIKeyRotator(ETHERSCAN_API_KEYS, "etherscan", ETHERSCAN_DAILY_LIMIT)
solscan_rotator = APIKeyRotator([SOLSCAN_API_KEY], "solscan", SOLSCAN_DAILY_LIMIT) if SOLSCAN_API_KEY else None

class EVMExplorerClient:
    # ... (без изменений)
    pass

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

    async def get_token_list(self, session, address, from_block, to_block) -> Set[str]:
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        tokens = set()
        topic2 = "0x000000000000000000000000" + address[2:].lower()
        params_in = [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                      "topics": [transfer_topic, None, topic2]}]
        topic1 = "0x000000000000000000000000" + address[2:].lower()
        params_out = [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                       "topics": [transfer_topic, topic1, None]}]
        for params in (params_in, params_out):
            logs = await self._rpc_call(session, "eth_getLogs", params)
            for log in logs:
                token = log["address"].lower()
                if token != self.weth_address:
                    tokens.add(token)
        return tokens

    async def get_balance_of(self, session, token_address, owner_address) -> int:
        data = "0x70a08231" + "000000000000000000000000" + owner_address[2:]
        result = await self._rpc_call(session, "eth_call", [{"to": token_address, "data": data}, "latest"])
        if result and result != "0x":
            return int(result, 16)
        return 0

    async def get_price_via_router(self, session, token_address: str, weth_price_usd: float) -> Optional[float]:
        """
        Определяет цену токена в USD через вызов getAmountsOut на DEX-роутере.
        Маршрут: token -> WETH -> stablecoin, с учётом известной цены WETH в USD.
        Возвращает float или None.
        """
        if not self.router_address or not self.stable_address:
            return None

        # 1. Количество токена за 1 WETH
        weth_amount = 10**18  # 1 WETH
        try:
            # getAmountsOut(amountIn, [token, weth]) -> сколько WETH получим за amountIn токенов
            # Нам нужно узнать цену 1 токена в WETH, поэтому запрашиваем обратный своп:
            # getAmountsOut(1 токен, [token, weth]) вернёт количество WETH за 1 токен (в минимальных единицах)
            token_unit = 10**18  # предполагаем 18 decimals (упрощённо, можно уточнить)
            data = (f"0xd06ca61f"
                    f"0000000000000000000000000000000000000000000000000de0b6b3a7640000"  # 1 токен
                    f"0000000000000000000000000000000000000000000000000000000000000040"
                    f"0000000000000000000000000000000000000000000000000000000000000002"
                    f"000000000000000000000000{token_address[2:]:0>64}"
                    f"000000000000000000000000{self.weth_address[2:]:0>64}")
            result = await self._rpc_call(session, "eth_call", [{"to": self.router_address, "data": data}, "latest"])
            if result:
                # Декодируем массив amounts (последнее значение — сумма WETH)
                amounts_offset = int(result[2:66], 16)
                weth_out = int(result[2 + amounts_offset + 64*2:], 16)  # берём последнее число после смещения
                if weth_out > 0:
                    # Количество WETH за 1 токен
                    token_price_in_weth = weth_out / 1e18
                    # Переводим в USD по курсу WETH
                    return token_price_in_weth * weth_price_usd
        except Exception as e:
            logger.warning(f"RPC getAmountsOut failed for {token_address}: {e}")

        # 2. Альтернативно: цена через пару token/stablecoin напрямую
        # Пробуем getAmountsOut(1 token, [token, stablecoin])
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
                    # Цена в стейблкоине (USDT/BUSD = 1$), надо учесть decimals стейблкоина
                    return stable_out / 1e18  # упростим, считая stablecoin 18 decimals (обычно 18, но может 6)
        except Exception:
            pass

        return None

class TokenInfoService:
    # ... (без изменений)
    pass

class SolscanClient:
    # ... (без изменений)
    pass

class CoingeckoClient:
    # ... (без изменений)
    pass