"""
Free price service cascade.
"""
import logging
from typing import Optional

import aiohttp

from bot.api_clients import (
    DexScreenerPrice,
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

        dex = await self.dexscreener.get_price(self.session, token_address)
        if dex and dex.get("price_usd"):
            set_price_cache(
                network,
                token_address,
                price_usd=dex["price_usd"],
                source=dex.get("source"),
                liquidity_usd=dex.get("liquidity_usd"),
                volume_24h=dex.get("volume_24h"),
            )
            return dex["price_usd"]

        gecko_net = {"ethereum": "eth", "bsc": "bsc"}.get(network, "eth")
        gecko = await self.gecko.get_price(self.session, token_address, gecko_net)
        if gecko and gecko.get("price_usd"):
            set_price_cache(
                network,
                token_address,
                price_usd=gecko["price_usd"],
                source=gecko.get("source"),
                liquidity_usd=gecko.get("liquidity_usd"),
                volume_24h=gecko.get("volume_24h"),
            )
            return gecko["price_usd"]

        if web3:
            try:
                router_price = await web3.get_price_via_router(self.session, token_address, weth_price_usd)
                if router_price:
                    set_price_cache(
                        network,
                        token_address,
                        price_usd=router_price,
                        source="router",
                    )
                    return router_price
            except Exception as e:
                logger.debug("Router price failed: %s", e)

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