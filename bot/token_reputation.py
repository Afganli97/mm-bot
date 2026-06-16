"""
Проверка качества токенов без использования названия.

Источники:
1. GoPlus Security API — бесплатно для EVM.
2. Birdeye Security API — для Solana, если задан BIRDEYE_API_KEY.
3. Exact-one heuristic — если баланс токена ровно 1.

DexScreener НЕ используется как обязательный фильтр,
потому что он может не показывать неактивные пары.
"""

import logging
from typing import Any, Dict, Optional

import aiohttp

from bot.api_clients import DexScreenerPrice
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

BIRDEYE_SECURITY_URL = "https://public-api.birdeye.so/defi/token_security"


class TokenReputationService:
    def __init__(self):
        self.dexscreener = DexScreenerPrice()
        self._goplus_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}
        self._birdeye_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}
        self._dex_cache: Dict[tuple, Optional[Dict[str, Any]]] = {}

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
    def _network_for_dexscreener(network: str) -> str:
        mapping = {
            "ethereum": "ethereum",
            "eth": "ethereum",
            "bsc": "bsc",
            "bnb": "bsc",
            "binance-smart-chain": "bsc",
            "solana": "solana",
            "sol": "solana",
        }

        return mapping.get((network or "").lower(), (network or "").lower())

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
        if not ENABLE_BIRDEYE_SECURITY:
            return None

        if not BIRDEYE_API_KEY:
            return None

        if (network or "").lower() != "solana":
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
    # DexScreener только для цены/метаданных
    # -----------------------------------------------------------------

    async def get_dex_info(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
    ) -> Optional[Dict[str, Any]]:
        """
        DexScreener НЕ используется для бана.
        Используется только чтобы получить цену/liquidity как дополнительную информацию.
        """

        if not address:
            return None

        address = address.lower()
        network = self._network_for_dexscreener(network)
        cache_key = (address, network)

        if cache_key in self._dex_cache:
            return self._dex_cache[cache_key]

        try:
            pairs = await self.dexscreener.get_pairs(session, address)

            if network:
                filtered_pairs = [
                    pair
                    for pair in pairs
                    if str(pair.get("chainId", "")).lower() == network
                ]

                if filtered_pairs:
                    pairs = filtered_pairs

            if not pairs:
                self._dex_cache[cache_key] = None
                return None

            best_pair = max(
                pairs,
                key=lambda pair: float(
                    pair.get("liquidity", {}).get("usd", 0) or 0
                ),
            )

            liquidity = best_pair.get("liquidity", {}) or {}

            info = {
                "pair_count": len(pairs),
                "liquidity_usd": float(liquidity.get("usd", 0) or 0),
                "fdv": self._to_float(best_pair.get("fdv")),
                "market_cap": self._to_float(best_pair.get("marketCap")),
                "price_usd": self._to_float(best_pair.get("priceUsd")),
                "dex_url": best_pair.get("url", ""),
                "base_token": best_pair.get("baseToken", {}),
                "best_pair": best_pair,
            }

            self._dex_cache[cache_key] = info
            return info

        except Exception as exc:
            logger.debug("DexScreener metadata error for %s: %s", address, exc)
            self._dex_cache[cache_key] = None
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

    async def should_hide_balance(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
        symbol: Optional[str] = None,
        raw_balance: Optional[int] = None,
        decimals: Optional[int] = None,
        is_native: bool = False,
    ) -> bool:
        """
        Мягкая проверка для баланса.

        Не скрываем токен из-за:
        - названия;
        - отсутствия DexScreener-пары;
        - отсутствия цены.

        Скрываем если:
        - security API говорит honeypot/cannot buy/cannot sell/high tax;
        - баланс ровно 1 токен, если включён exact-one heuristic.
        """

        if is_excluded(address or ""):
            return True

        if (network or "").lower() in ("solana", "sol") and not SOLANA_BALANCE_SECURITY_CHECK:
            security_info = None
        else:
            security_info = await self.get_security_info(session, address, network)

        if self.is_hard_security_risk(security_info):
            logger.info(
                "Balance skip: hard security risk address=%s network=%s source=%s",
                address,
                network,
                security_info.get("source") if security_info else None,
            )
            return True

        if not is_native and EXACT_ONE_SPAM_FILTER_FOR_BALANCE:
            if self.is_exact_one(raw_balance, decimals):
                logger.info(
                    "Balance skip: exact-one heuristic address=%s network=%s symbol=%s",
                    address,
                    network,
                    symbol,
                )
                return True

        return False

    async def should_hide_history_token(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
        symbol: Optional[str] = None,
    ) -> bool:
        """
        Проверка для истории.

        Для истории не требуем DexScreener-ликвидность,
        потому что неактивные пары могут отсутствовать в DexScreener.

        Скрываем только если security API явно показывает риск.
        """

        if is_excluded(address or ""):
            return True

        security_info = await self.get_security_info(session, address, network)

        if self.is_hard_security_risk(security_info):
            logger.info(
                "History skip: hard security risk address=%s network=%s source=%s",
                address,
                network,
                security_info.get("source") if security_info else None,
            )
            return True

        return False

    async def get_price_for_balance(
        self,
        session: aiohttp.ClientSession,
        address: str,
        network: str,
    ) -> Optional[float]:
        """
        DexScreener используется только для цены.
        Если пары нет — возвращаем None, но токен не скрываем.
        """

        info = await self.get_dex_info(session, address, network)

        if not info:
            return None

        price = float(info.get("price_usd") or 0)

        if price > 0:
            return price

        return None
