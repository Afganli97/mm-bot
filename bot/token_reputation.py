"""
Token reputation through free APIs.
"""
import logging
from typing import Any, Dict, Optional

import aiohttp

from bot.api_clients import BirdeyeTokenOverview, DexScreenerPrice
from bot.config import BIRDEYE_API_KEY

logger = logging.getLogger(__name__)


class TokenReputationChecker:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.dexscreener = DexScreenerPrice()
        self.birdeye = BirdeyeTokenOverview()

    async def check_token(
        self,
        session: aiohttp.ClientSession,
        token_address: str,
        network: str = "ethereum",
    ) -> Dict[str, Any]:
        cache_key = f"{network}:{token_address}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        result = {
            "is_scam": False,
            "is_honeypot": False,
            "risk_level": "unknown",
            "source": "none",
        }

        if network == "solana" and BIRDEYE_API_KEY:
            birdeye_result = await self._check_birdeye(session, token_address)
            if birdeye_result:
                result.update(birdeye_result)
                result["source"] = "birdeye"

        if result["risk_level"] == "unknown":
            dexscreener_result = await self._check_dexscreener(session, token_address)
            if dexscreener_result:
                result.update(dexscreener_result)
                result["source"] = "dexscreener"

        self._cache[cache_key] = result
        return result

    async def _check_birdeye(self, session: aiohttp.ClientSession, token_address: str) -> Optional[Dict]:
        overview = await self.birdeye.get_overview(session, token_address)
        if not overview:
            return None

        is_scam = bool(overview.get("isScam"))
        is_honeypot = bool(overview.get("isHoneypot"))

        return {
            "is_scam": is_scam,
            "is_honeypot": is_honeypot,
            "risk_level": "high" if is_scam or is_honeypot else "low",
        }

    async def _check_dexscreener(self, session: aiohttp.ClientSession, token_address: str) -> Optional[Dict]:
        dex = await self.dexscreener.get_price(session, token_address)
        if not dex:
            return None

        liquidity = float(dex.get("liquidity_usd") or 0)
        volume = float(dex.get("volume_24h") or 0)

        if liquidity <= 0 and volume <= 0:
            return {
                "is_scam": False,
                "is_honeypot": False,
                "risk_level": "high",
            }

        if liquidity < 100 and volume < 10:
            return {
                "is_scam": False,
                "is_honeypot": False,
                "risk_level": "medium",
            }

        return {
            "is_scam": False,
            "is_honeypot": False,
            "risk_level": "low",
        }


reputation_checker = TokenReputationChecker()