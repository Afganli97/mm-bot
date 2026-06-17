"""
Spam/risk check.

Принципы:
1. Названия токенов НЕ используются для бана.
2. DexScreener НЕ используется как обязательный фильтр.
3. Если сервис завис или ошибся — токен НЕ скрывается автоматически.
4. Для баланса есть heuristic: ровно 1 токен считается подозрительным.
5. Для истории проверяются только уже найденные уникальные токены.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from bot.config import (
    ENABLE_GOPLUS_SECURITY,
    MAX_HISTORY_BUY_TAX_PERCENT,
    MAX_HISTORY_SELL_TAX_PERCENT,
    MIN_HISTORY_HOLDER_COUNT,
    SOLANA_BALANCE_SECURITY_CHECK,
    SOLANA_HISTORY_SECURITY_CHECK,
    SPAM_CHECK_TIMEOUT_SECONDS,
)
from bot.database import increment_api_usage
from bot.token_filter import is_excluded


logger = logging.getLogger(__name__)


GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1"

GOPLUS_CHAIN_IDS = {
    "ethereum": "1",
    "eth": "1",
    "bsc": "56",
    "bnb": "56",
    "binance-smart-chain": "56",
}


class TokenReputationService:
    def __init__(self):
        self._goplus_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if value is None:
            return False

        if isinstance(value, bool):
            return value

        return str(value).strip().lower() in ("1", "true", "yes", "y")

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        try:
            return float(str(value).strip().replace("%", ""))
        except Exception:
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except Exception:
            return 0

    @staticmethod
    def is_exact_one(
        raw_balance: Optional[int],
        decimals: Optional[int],
    ) -> bool:
        if raw_balance is None or decimals is None:
            return False

        try:
            raw_balance = int(raw_balance)
            decimals = int(decimals)
        except Exception:
            return False

        if raw_balance <= 0 or decimals < 0:
            return False

        return raw_balance == 10**decimals

    @staticmethod
    def _goplus_chain_id(network: str) -> Optional[str]:
        return GOPLUS_CHAIN_IDS.get((network or "").lower())

    async def get_goplus_info(
        self,
        session,
        address: str,
        network: str,
    ) -> Optional[Dict[str, Any]]:
        if not ENABLE_GOPLUS_SECURITY:
            return None

        chain_id = self._goplus_chain_id(network)

        if not chain_id or not address:
            return None

        address = address.lower()
        cache_key = (address, network)

        if cache_key in self._goplus_cache:
            return self._goplus_cache[cache_key]

        url = f"{GOPLUS_BASE_URL}/token_security/{chain_id}"

        try:
            async with session.get(
                url,
                params={"contract_addresses": address},
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    self._goplus_cache[cache_key] = None
                    return None

                data = await resp.json()
                info = (data.get("result") or {}).get(address)

                if not info:
                    self._goplus_cache[cache_key] = None
                    return None

                parsed = {
                    "source": "goplus",
                    "is_honeypot": self._to_bool(info.get("is_honeypot")),
                    "cannot_buy": self._to_bool(info.get("cannot_buy")),
                    "cannot_sell": self._to_bool(info.get("cannot_sell")),
                    "buy_tax": self._to_float(info.get("buy_tax")),
                    "sell_tax": self._to_float(info.get("sell_tax")),
                    "holder_count": self._to_int(info.get("holder_count")),
                    "raw": info,
                }

                increment_api_usage("goplus", 0)
                self._goplus_cache[cache_key] = parsed
                return parsed

        except Exception as exc:
            logger.debug("GoPlus error for %s: %s", address, exc)
            self._goplus_cache[cache_key] = None
            return None

    async def get_security_info(
        self,
        session,
        address: str,
        network: str,
    ) -> Optional[Dict[str, Any]]:
        network = (network or "").lower()

        if network in ("ethereum", "eth", "bsc", "bnb", "binance-smart-chain"):
            return await self.get_goplus_info(session, address, network)

        if network in ("solana", "sol"):
            if SOLANA_HISTORY_SECURITY_CHECK or SOLANA_BALANCE_SECURITY_CHECK:
                logger.warning("Solana security requested, но Birdeye сейчас не используется.")
            return None

        return None

    @staticmethod
    def is_hard_security_risk(info: Optional[Dict[str, Any]]) -> bool:
        if not info:
            return False

        return bool(
            info.get("is_honeypot")
            or info.get("cannot_buy")
            or info.get("cannot_sell")
        )

    async def check_token(
        self,
        session,
        address: str,
        network: str,
        symbol: Optional[str] = None,
        raw_balance: Optional[int] = None,
        decimals: Optional[int] = None,
        is_native: bool = False,
        require_liquidity: bool = False,
    ) -> Dict[str, Any]:
        address = str(address or "").lower()
        network = (network or "").lower()

        if not address:
            return {"is_spam": True, "checked": True, "reason": "empty_address"}

        if is_excluded(address):
            return {"is_spam": True, "checked": True, "reason": "excluded"}

        if not is_native and self.is_exact_one(raw_balance, decimals):
            return {
                "is_spam": True,
                "checked": True,
                "reason": "exact_one",
                "service": "heuristic",
            }

        if network in ("solana", "sol"):
            if require_liquidity and not SOLANA_HISTORY_SECURITY_CHECK:
                return {"is_spam": False, "checked": False, "reason": "service_disabled"}

            if not require_liquidity and not SOLANA_BALANCE_SECURITY_CHECK:
                return {"is_spam": False, "checked": False, "reason": "service_disabled"}

        try:
            info = await asyncio.wait_for(
                self.get_security_info(session, address, network),
                timeout=SPAM_CHECK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Spam check timeout: %s/%s", network, address)
            return {"is_spam": False, "checked": False, "reason": "timeout"}
        except Exception as exc:
            logger.debug("Spam check error: %s/%s: %s", network, address, exc)
            return {"is_spam": False, "checked": False, "reason": "error"}

        if self.is_hard_security_risk(info):
            return {
                "is_spam": True,
                "checked": True,
                "reason": "hard_security_risk",
                "service": info.get("source") if info else None,
            }

        if require_liquidity and info:
            holder_count = int(info.get("holder_count") or 0)

            if holder_count > 0 and holder_count < MIN_HISTORY_HOLDER_COUNT:
                return {
                    "is_spam": True,
                    "checked": True,
                    "reason": "low_holder_count",
                    "service": info.get("source"),
                }

            buy_tax = float(info.get("buy_tax") or 0)
            sell_tax = float(info.get("sell_tax") or 0)

            if buy_tax > MAX_HISTORY_BUY_TAX_PERCENT:
                return {
                    "is_spam": True,
                    "checked": True,
                    "reason": "high_buy_tax",
                    "service": info.get("source"),
                }

            if sell_tax > MAX_HISTORY_SELL_TAX_PERCENT:
                return {
                    "is_spam": True,
                    "checked": True,
                    "reason": "high_sell_tax",
                    "service": info.get("source"),
                }

        return {
            "is_spam": False,
            "checked": bool(info),
            "reason": "ok" if info else "no_data",
            "service": info.get("source") if info else None,
        }