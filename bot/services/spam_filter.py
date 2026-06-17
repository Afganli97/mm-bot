"""
Conservative spam filter for free services.
Не режем lowcaps только из-за низкой ликвидности.
Spam ставим только при явном признаке от сервиса или blacklist.
"""
import logging
from typing import Optional

import aiohttp

from bot.api_clients import BirdeyeTokenOverview, DexScreenerPrice
from bot.config import BIRDEYE_API_KEY, SPAM_LIQUIDITY_USD, SPAM_VOLUME_24H_USD
from bot.token_filter import BLACKLIST_TOKENS, is_exactly_one_unit

logger = logging.getLogger(__name__)


class SpamFilterService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.dexscreener = DexScreenerPrice()
        self.birdeye = BirdeyeTokenOverview()

    async def is_spam(
        self,
        network: str,
        token_address: str,
        symbol: str = "?",
        decimals: Optional[int] = None,
        raw_balance: Optional[int] = None,
        is_native: bool = False,
    ) -> dict:
        token_address = token_address.lower() if network != "solana" else token_address

        if is_native:
            return {
                "is_spam": False,
                "source": "native",
                "reason": "native coin",
            }

        if token_address in BLACKLIST_TOKENS:
            return {
                "is_spam": True,
                "source": "blacklist",
                "reason": "token blacklist",
            }

        if raw_balance is not None and decimals is not None:
            if is_exactly_one_unit(raw_balance, decimals, is_native=False):
                return {
                    "is_spam": False,
                    "exclude_by_one_unit": True,
                    "source": "one_unit_rule",
                    "reason": "exactly 1 non-native unit",
                }

        if network == "solana" and BIRDEYE_API_KEY:
            overview = await self.birdeye.get_overview(self.session, token_address)
            if overview:
                if overview.get("isScam") or overview.get("isHoneypot"):
                    return {
                        "is_spam": True,
                        "source": "birdeye",
                        "reason": "birdeye scam/honeypot",
                    }

        dex = await self.dexscreener.get_price(self.session, token_address)
        if dex and dex.get("price_usd") is not None:
            liquidity = float(dex.get("liquidity_usd") or 0)
            volume = float(dex.get("volume_24h") or 0)

            if liquidity <= 0 and volume <= 0:
                return {
                    "is_spam": True,
                    "source": "dexscreener",
                    "reason": "zero liquidity and zero volume",
                }

            if liquidity < SPAM_LIQUIDITY_USD and volume < SPAM_VOLUME_24H_USD:
                return {
                    "is_spam": True,
                    "source": "dexscreener",
                    "reason": "very low liquidity and volume",
                }

        return {
            "is_spam": False,
            "source": "none",
            "reason": "no spam proof",
        }