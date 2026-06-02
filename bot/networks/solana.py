"""
Сеть Solana.
"""
import logging
from typing import List, Dict, Optional
from ._base import BaseNetwork
from bot.api_clients import SolscanClient

logger = logging.getLogger(__name__)

class SolanaNetwork(BaseNetwork):
    def __init__(self, network_config: dict, session):
        super().__init__(network_config, session)
        self.solscan = SolscanClient()

    async def validate_address(self, address: str) -> bool:
        try:
            from solders.pubkey import Pubkey
            Pubkey.from_string(address)
            return True
        except Exception:
            return False

    async def get_balance(self, address: str) -> float:
        payload = {"jsonrpc":"2.0","id":1,"method":"getBalance","params":[address]}
        async with self.session.post(self.config["rpc_url"], json=payload, timeout=10) as resp:
            data = await resp.json()
            return data['result']['value'] / 1e9

    async def get_token_balances(self, address: str) -> List[Dict]:
        return await self.solscan.get_token_balances(self.session, address)

    async def get_swap_history(self, address: str, start_time: int, end_time: int,
                               min_amount_native: float, max_tokens: int) -> List[Dict]:
        # Пока не реализовано, но структура готова
        return []

    async def get_token_price_usd(self, session, mint_address: str) -> Optional[float]:
        """
        Возвращает цену токена Solana в USD через Jupiter Price API.
        """
        url = f"https://price.jup.ag/v4/price?ids={mint_address}"
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token_data = data.get("data", {}).get(mint_address, {})
                    price = token_data.get("price")
                    if price is not None:
                        return float(price)
        except Exception as e:
            logger.warning(f"Jupiter price request failed for {mint_address}: {e}")
        return None