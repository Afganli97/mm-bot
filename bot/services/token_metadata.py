"""
Token metadata service.
Кэш + RPC/Helius metadata.
"""
import logging
from typing import Optional

import aiohttp

from bot.api_clients import TokenInfoService
from bot.database import get_token_cache, set_token_cache

logger = logging.getLogger(__name__)


class TokenMetadataService:
    def __init__(self, session: aiohttp.ClientSession, helius=None):
        self.session = session
        self.helius = helius

    async def get_native_metadata(self, network: str, symbol: str, decimals: int):
        return {
            "symbol": symbol,
            "name": symbol,
            "decimals": decimals,
            "is_native": True,
        }

    async def get_evm_metadata(self, network: str, token_address: str, rpc_url: str) -> dict:
        token_address = token_address.lower()
        cached = get_token_cache(network, token_address)

        if cached:
            return {
                "symbol": cached["symbol"] or "?",
                "name": cached["name"] or "?",
                "decimals": cached["decimals"] if cached["decimals"] is not None else 18,
                "is_native": bool(cached["is_native"]),
            }

        symbol = await TokenInfoService.get_symbol(self.session, token_address, rpc_url)
        name = await TokenInfoService.get_name(self.session, token_address, rpc_url)
        decimals = await TokenInfoService.get_decimals(self.session, token_address, rpc_url)

        metadata = {
            "symbol": symbol or "?",
            "name": name or "?",
            "decimals": decimals,
            "is_native": False,
        }

        set_token_cache(
            network=network,
            token_address=token_address,
            symbol=metadata["symbol"],
            name=metadata["name"],
            decimals=metadata["decimals"],
            is_native=False,
        )

        return metadata

    async def get_solana_metadata(self, mint: str, hint: Optional[dict] = None) -> dict:
        cached = get_token_cache("solana", mint)

        if cached:
            return {
                "symbol": cached["symbol"] or "?",
                "name": cached["name"] or "?",
                "decimals": cached["decimals"] if cached["decimals"] is not None else 0,
                "is_native": bool(cached["is_native"]),
            }

        hint = hint or {}
        token_info = hint.get("token_info") if isinstance(hint, dict) else {}

        if cached:
            cached_symbol = cached["symbol"]
            cached_name = cached["name"]
        else:
            cached_symbol = None
            cached_name = None

        symbol = token_info.get("symbol") or hint.get("symbol") or cached_symbol or "?"
        name = token_info.get("name") or hint.get("name") or cached_name or "?"
        decimals = int(hint.get("decimals") or token_info.get("decimals") or 0)

        metadata = {
            "symbol": symbol or "?",
            "name": name or "?",
            "decimals": decimals,
            "is_native": False,
        }

        set_token_cache(
            network="solana",
            token_address=mint,
            symbol=metadata["symbol"],
            name=metadata["name"],
            decimals=metadata["decimals"],
            is_native=False,
        )

        return metadata