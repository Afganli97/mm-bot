"""
Проверка качества токенов.

Принцип:
- спам-проверка делается в конце;
- проверяются только уникальные найденные токены;
- один и тот же токен не проверяется дважды;
- если сервис завис/ошибся, токен не блокируется автоматически;
- fallback: баланс ровно 1 токен считается подозрительным;
- названия токенов НЕ используются для бана.
"""

import logging
from typing import Any, Dict, Optional

import aiohttp

from bot.config import (
    BIRDEYE_API_KEY,
    ENABLE_BIRDEYE_SECURITY,
    ENABLE_EXACT_ONE_SPAM_FILTER,
    ENABLE_GOPLUS_SECURITY,
    EXACT_ONE_SPAM_FILTER_FOR_BALANCE,
    EXACT_ONE_SPAM_FILTER_FOR_HISTORY,
    MAX_HISTORY_BUY_TAX_PERCENT,
    MAX_HISTORY_SELL_TAX_PERCENT,
    MIN_HISTORY_HOLDER_COUNT,
    SOLANA_BALANCE_SECURITY_CHECK,
    SOLANA_HISTORY_SECURITY_CHECK,
)
from bot.database import increment_api_usage
from bot.token_filter import is_excluded


logger = logging.getLogger(__name__)


GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1"
BIRDEYE_SECURITY_URL = "https://public-api.birdeye.so/defi/token_security"

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
        self._birdeye_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if value is None:
            return False

        if isinstance(value, bool):
            return value

        text = str(value).strip().lower()

        return text in ("1", "true", "yes", "y")

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip().replace("%", "")

        try:
            return float(text)
        except Exception:
            return 0.0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(float(value))
        except Exception:
            return 0

    @staticmethod
    def _goplus_chain_id(network: str) -> Optional[str]:
        return GOPLUS_CHAIN_IDS.get((network or "").lower())

    @staticmethod
    def is_exact_one(raw_balance: Optional[int], decimals: Optional[int]) -> bool:
        if not ENABLE_EXACT_ONE_SPAM_FILTER:
            return False

        if raw_balance is None:
            return False

        if decimals is None:
            return False

        try:
            raw_balance = int(raw_balance)
            decimals = int(decimals)
        except Exception:
            return False

        if raw_balance <= 0:
            return False

        if decimals < 0:
            return False

        return raw_balance == 10**decimals

    # -----------------------------------------------------------------
    # GoPlus Security API для EVM
    # -----------------------------------------------------------------

    async def get_goplus_info(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
    ) -> Optional[Dict[str, Any]]:
        if not ENABLE_GOPLUS_SECURITY:
            return None

        chain_id = self._goplus_chain_id(network)

        if not chain_id:
            return None

        if not address:
            return None

        address = address.lower()
        cache_key = (address, network)

        if cache_key in self._goplus_cache:
            return self._goplus_cache[cache_key]

        url = f"{GOPLUS_BASE_URL}/token_security/{chain_id}"

        try:
            async with session.get(
                url,
                params={
                    "contract_addresses": address,
                },
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    logger.debug("GoPlus HTTP %s for %s", resp.status, address)
                    self._goplus_cache[cache_key] = None
                    return None

                data = await resp.json()
                result = data.get("result", {}) or {}
                info = result.get(address)

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
                    "owner_balance": self._to_float(info.get("owner_balance")),
                    "is_mintable": self._to_bool(info.get("is_mintable")),
                    "is_proxy": self._to_bool(info.get("is_proxy")),
                    "is_open_source": self._to_bool(info.get("is_open_source")),
                    "raw": info,
                }

                increment_api_usage("goplus", 0)
                self._goplus_cache[cache_key] = parsed
                return parsed

        except Exception as exc:
            logger.debug("GoPlus error for %s: %s", address, exc)
            self._goplus_cache[cache_key] = None
            return None

    # -----------------------------------------------------------------
    # Birdeye Security API для Solana
    # -----------------------------------------------------------------

    async def get_birdeye_security_info(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str = "solana",
    ) -> Optional[Dict[str, Any]]:
        if (network or "").lower() != "solana":
            return None

        if not ENABLE_BIRDEYE_SECURITY:
            return None

        if not SOLANA_HISTORY_SECURITY_CHECK and not SOLANA_BALANCE_SECURITY_CHECK:
            return None

        if not BIRDEYE_API_KEY:
            return None

        if not address:
            return None

        cache_key = (address, network)

        if cache_key in self._birdeye_cache:
            return self._birdeye_cache[cache_key]

        try:
            async with session.get(
                BIRDEYE_SECURITY_URL,
                params={
                    "address": address,
                    "x-chain": "solana",
                },
                headers={
                    "X-API-KEY": BIRDEYE_API_KEY,
                },
                timeout=15,
            ) as resp:
                if resp.status != 200:
                    logger.debug("Birdeye security HTTP %s for %s", resp.status, address)
                    self._birdeye_cache[cache_key] = None
                    return None

                data = await resp.json()
                info = data.get("data", {}) or {}

                if not info:
                    self._birdeye_cache[cache_key] = None
                    return None

                parsed = {
                    "source": "birdeye",
                    "is_honeypot": self._to_bool(
                        info.get("is_honeypot")
                        or info.get("isHoneypot")
                        or info.get("honeypot")
                    ),
                    "cannot_buy": self._to_bool(
                        info.get("cannot_buy")
                        or info.get("cannotBuy")
                    ),
                    "cannot_sell": self._to_bool(
                        info.get("cannot_sell")
                        or info.get("cannotSell")
                    ),
                    "buy_tax": self._to_float(
                        info.get("buy_tax")
                        or info.get("buyTax")
                    ),
                    "sell_tax": self._to_float(
                        info.get("sell_tax")
                        or info.get("sellTax")
                    ),
                    "holder_count": self._to_int(
                        info.get("holder_count")
                        or info.get("holderCount")
                        or info.get("holder")
                    ),
                    "raw": info,
                }

                increment_api_usage("birdeye", 0)
                self._birdeye_cache[cache_key] = parsed
                return parsed

        except Exception as exc:
            logger.debug("Birdeye security error for %s: %s", address, exc)
            self._birdeye_cache[cache_key] = None
            return None

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def get_security_info(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
    ) -> Optional[Dict[str, Any]]:
        network = (network or "").lower()

        if network in ("ethereum", "eth", "bsc", "bnb", "binance-smart-chain"):
            return await self.get_goplus_info(session, address, network)

        if network in ("solana", "sol"):
            return await self.get_birdeye_security_info(session, address, network)

        return None

    def is_hard_security_risk(self, security_info: Optional[Dict[str, Any]]) -> bool:
        if not security_info:
            return False

        if security_info.get("is_honeypot"):
            return True

        if security_info.get("cannot_buy"):
            return True

        if security_info.get("cannot_sell"):
            return True

        buy_tax = float(security_info.get("buy_tax") or 0)
        sell_tax = float(security_info.get("sell_tax") or 0)

        if buy_tax > MAX_HISTORY_BUY_TAX_PERCENT:
            return True

        if sell_tax > MAX_HISTORY_SELL_TAX_PERCENT:
            return True

        return False

    async def check_token(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
        symbol: Optional[str] = None,
        raw_balance: Optional[int] = None,
        decimals: Optional[int] = None,
        is_native: bool = False,
    ) -> Dict[str, Any]:
        """
        Возвращает:
        {
            "is_spam": bool,
            "checked": bool,
            "reason": str,
            "service": str|None,
        }
        """

        if is_excluded(address or ""):
            return {
                "is_spam": True,
                "checked": True,
                "reason": "excluded",
                "service": "local",
            }

        if not is_native and self.is_exact_one(raw_balance, decimals):
            return {
                "is_spam": True,
                "checked": True,
                "reason": "exact_one_balance",
                "service": "fallback",
            }

        network = (network or "").lower()

        if network in ("solana", "sol") and not SOLANA_HISTORY_SECURITY_CHECK and not SOLANA_BALANCE_SECURITY_CHECK:
            return {
                "is_spam": False,
                "checked": False,
                "reason": "solana_security_disabled",
                "service": None,
            }

        if network in ("ethereum", "eth", "bsc", "bnb", "binance-smart-chain") and not ENABLE_GOPLUS_SECURITY:
            return {
                "is_spam": False,
                "checked": False,
                "reason": "goplus_disabled",
                "service": None,
            }

        try:
            security_info = await self.get_security_info(session, address, network)
        except Exception as exc:
            logger.debug("Security service error for %s/%s: %s", network, address, exc)

            return {
                "is_spam": False,
                "checked": False,
                "reason": "security_service_error",
                "service": None,
            }

        if self.is_hard_security_risk(security_info):
            service = security_info.get("source") if security_info else None

            return {
                "is_spam": True,
                "checked": True,
                "reason": "hard_security_risk",
                "service": service,
            }

        return {
            "is_spam": False,
            "checked": bool(security_info),
            "reason": "ok" if security_info else "no_security_data",
            "service": security_info.get("source") if security_info else None,
        }