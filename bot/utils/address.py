"""
Address validators.
"""
from web3 import Web3


def is_evm_address(addr: str) -> bool:
    try:
        return Web3.is_address(addr.strip())
    except Exception:
        return False


def is_solana_address(addr: str) -> bool:
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(addr.strip())
        return True
    except Exception:
        return False


def detect_address_type(addr: str) -> str:
    addr = addr.strip()

    if is_solana_address(addr):
        return "solana"

    if is_evm_address(addr):
        return "evm"

    return "unknown"