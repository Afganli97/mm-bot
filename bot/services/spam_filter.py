# bot/services/spam_filter.py
"""
Spam filter.

Для баланса strict=False:
- не режем lowcaps только из-за отсутствия DexScreener пары.

Для истории strict=True:
- если нет DexScreener пары — считаем токен подозрительным/spam.
- если liquidity и volume почти нулевые — spam.
- домены .cc/.pro/.xyz/.top, gift/claim/airdrop/scam и т.п. — spam.
"""
import logging
from typing import Optional

import aiohttp

from bot.api_clients import BirdeyeTokenOverview, DexScreenerPrice
from bot.config import BIRDEYE_API_KEY, SPAM_LIQUIDITY_USD, SPAM_VOLUME_24H_USD
from bot.token_filter import BLACKLIST_TOKENS, is_exactly_one_unit

logger = logging.getLogger(__name__)


SPAM_KEYWORDS = (
    "scam",
    "gift",
    "airdrop",
    "claim",
    "freemint",
    "freeuse",
    "worldcup",
    ".cc",
    ".pro",
    ".xyz",
    ".top",
    "giveaway",
    "reward",
    "usdgift",
)


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
        name: str = "?",
        decimals: Optional[int] = None,
        raw_balance: Optional[int] = None,
        is_native: bool = False,
        strict: bool = False,
    ) -> dict:
        token_address_lower = token_address.lower() if network != "solana" else token_address
        symbol_lower = (symbol or "").lower()
        name_lower = (name or "").lower()

        if is_native:
            return {
                "is_spam": False,
                "source": "native",
                "reason": "native coin",
            }

        if token_address_lower in BLACKLIST_TOKENS:
            return {
                "is_spam": True,
                "source": "blacklist",
                "reason": "token blacklist",
            }

        combined_text = f"{token_address_lower} {symbol_lower} {name_lower}"

        for keyword in SPAM_KEYWORDS:
            if keyword in combined_text:
                return {
                    "is_spam": True,
                    "source": "keyword",
                    "reason": f"spam keyword: {keyword}",
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
                "source": "dexscreener",
                "reason": "dex pair exists",
            }

        if strict:
            return {
                "is_spam": True,
                "source": "dexscreener",
                "reason": "no dex pair found in strict history mode",
            }

        return {
            "is_spam": False,
            "source": "none",
            "reason": "no spam proof",
        }