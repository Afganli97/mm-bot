# bot/services/price_service.py
"""
Free price service cascade.
"""
import logging
from typing import Optional

import aiohttp

from bot.api_clients import (
    DexScreenerPrice,
    EVMPriceCascade,
    EVMWeb3Client,
    GeckoTerminalPrice,
    JupiterPrice,
)
from bot.database import get_price_cache, set_price_cache

logger = logging.getLogger(__name__)


class PriceService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.dexscreener = DexScreenerPrice()
        self.gecko = GeckoTerminalPrice()
        self.jupiter = JupiterPrice()

    async def get_price(
        self,
        network: str,
        token_address: str,
        web3: Optional[EVMWeb3Client] = None,
        weth_price_usd: float = 0.0,
    ) -> Optional[float]:
        if network == "solana":
            return await self._get_solana_price(token_address)

        return await self._get_evm_price(network, token_address, web3, weth_price_usd)

    async def _get_evm_price(
        self,
        network: str,
        token_address: str,
        web3: Optional[EVMWeb3Client],
        weth_price_usd: float,
    ) -> Optional[float]:
        token_address = token_address.lower()

        cached = get_price_cache(network, token_address)
        if cached and cached["price_usd"] is not None:
            return float(cached["price_usd"])

        if web3 and web3.weth_address and token_address == web3.weth_address.lower():
            price = await self._get_evm_price_without_router(network, token_address, web3)
            if price:
                set_price_cache(
                    network,
                    token_address,
                    price_usd=price,
                    source="dex_gecko_native_wrapper",
                )
                return price

        cascade = EVMPriceCascade(web3)

        if web3 and web3.weth_address and not weth_price_usd:
            weth_price_usd = (
                await self._get_evm_price_without_router(
                    network,
                    web3.weth_address.lower(),
                    web3,
                )
                or 0.0
            )

        price = await cascade.get_price(
            self.session,
            token_address,
            network_name=network,
            weth_price_usd=weth_price_usd,
        )

        if price:
            set_price_cache(
                network,
                token_address,
                price_usd=price,
                source="evm_cascade",
            )
            return price

        return None

    async def _get_evm_price_without_router(
        self,
        network: str,
        token_address: str,
        web3: Optional[EVMWeb3Client],
    ) -> Optional[float]:
        token_address = token_address.lower()

        dex = await self.dexscreener.get_price(self.session, token_address)
        if dex and dex.get("price_usd"):
            return float(dex["price_usd"])

        gecko_net = {"ethereum": "eth", "bsc": "bsc"}.get(network, "eth")
        gecko = await self.gecko.get_price(self.session, token_address, gecko_net)
        if gecko and gecko.get("price_usd"):
            return float(gecko["price_usd"])

        return None

    async def _get_solana_price(self, mint: str) -> Optional[float]:
        cached = get_price_cache("solana", mint)
        if cached and cached["price_usd"] is not None:
            return float(cached["price_usd"])

        jupiter = await self.jupiter.get_price(self.session, mint)
        if jupiter and jupiter.get("price_usd"):
            set_price_cache(
                "solana",
                mint,
                price_usd=jupiter["price_usd"],
                source=jupiter.get("source"),
                liquidity_usd=jupiter.get("liquidity_usd"),
                volume_24h=jupiter.get("volume_24h"),
            )
            return jupiter["price_usd"]

        dex = await self.dexscreener.get_price(self.session, mint)
        if dex and dex.get("price_usd"):
            set_price_cache(
                "solana",
                mint,
                price_usd=dex["price_usd"],
                source=dex.get("source"),
                liquidity_usd=dex.get("liquidity_usd"),
                volume_24h=dex.get("volume_24h"),
            )
            return dex["price_usd"]

        gecko = await self.gecko.get_price(self.session, mint, "solana")
        if gecko and gecko.get("price_usd"):
            set_price_cache(
                "solana",
                mint,
                price_usd=gecko["price_usd"],
                source=gecko.get("source"),
                liquidity_usd=gecko.get("liquidity_usd"),
                volume_24h=gecko.get("volume_24h"),
            )
            return gecko["price_usd"]

        return None