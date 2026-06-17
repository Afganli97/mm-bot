"""
Глобальные настройки проекта.
"""

import os
from typing import Dict, List, Set

from dotenv import load_dotenv


load_dotenv()


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

_ALLOWED_user_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS: Set[int] = {
    int(item.strip())
    for item in _Allowed_user_ids.split(",")
    if item.strip().isdigit()
}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _env_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------

ETHERSCAN_API_KEYS = _env_list("ETHERSCAN_API_KEYS")
BSCSCAN_API_KEYS = _env_list("BSCSCAN_API_KEYS")

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
INFURA_API_KEY = os.getenv("INFURA_API_KEY", "")

ANKR_API_KEY = os.getenv("ANKR_API_KEY", "")
ANKR_API_URL = f"https://rpc.ankr.com/multichain/{ANKR_API_KEY}" if ANKR_API_KEY else ""

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")

SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")


# ---------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------

BSC_RPC_URL = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org/")
ETH_RPC_URL = os.getenv("ETH_RPC_URL", "https://eth.llamarpc.com")


def _evm_rpc_url(chain_id: int) -> str:
    if ALCHEMY_API_KEY:
        subdomains = {
            1: "eth-mainnet",
            56: "bnb-mainnet",
        }
        subdomain = subdomains.get(chain_id, f"unknown-{chain_id}")
        return f"https://{subdomain}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

    if INFURA_API_KEY:
        domains = {
            1: "mainnet.infura.io",
            56: "bsc-mainnet.infura.io",
        }
        domain = domains.get(chain_id, f"unknown-{chain_id}.infura.io")
        return f"https://{domain}/v3/{INFURA_API_KEY}"

    if chain_id == 56:
        return BSC_RPC_URL

    return ETH_RPC_URL


# ---------------------------------------------------------------------
# API limits
# ---------------------------------------------------------------------

API_LIMITS: Dict[str, int] = {
    "etherscan": 100_000,
    "bscscan": 0,
    "ankr": 100_000,
    "moralis": 1_500,
    "helius": 100_000,
    "birdeye": 100_000,
    "goplus": 0,
    "dexscreener": 0,
    "geckoterminal": 0,
    "jupiter": 0,
}


# ---------------------------------------------------------------------
# Анализ по умолчанию и ограничения
# ---------------------------------------------------------------------

DEFAULT_MAX_DEPTH = 3
HARD_MAX_DEPTH = 5

DEFAULT_LOOKBACK_DAYS = 30
HARD_MAX_LOOKBACK_DAYS = 90

DEFAULT_MAX_FOUND_TOKENS = 100
HARD_MAX_FOUND_TOKENS = 500

DEFAULT_MAX_ADDRESSES = 2_000
HARD_MAX_ADDRESSES = 2_000

DEFAULT_MAX_BRANCHES_PER_ADDRESS = 50
HARD_MAX_BRANCHES_PER_ADDRESS = 100


# ---------------------------------------------------------------------
# EVM history stability
# ---------------------------------------------------------------------

EVM_HISTORY_TIMEOUT_SECONDS = int(
    os.getenv("EVM_HISTORY_TIMEOUT_SECONDS", "600")
)

EVM_NETWORK_CALL_TIMEOUT_SECONDS = int(
    os.getenv("EVM_NETWORK_CALL_TIMEOUT_SECONDS", "12")
)

MAX_EVM_HISTORY_SYMBOL_LOOKUPS = int(
    os.getenv("MAX_EVM_HISTORY_SYMBOL_LOOKUPS", "100")
)


# ---------------------------------------------------------------------
# Spam / risk check
# ---------------------------------------------------------------------

ENABLE_GOPLUS_SECURITY = os.getenv("ENABLE_GOPLUS_SECURITY", "1") != "0"

SPAM_CHECK_TIMEOUT_SECONDS = int(
    os.getenv("SPAM_CHECK_TIMEOUT_SECONDS", "8")
)

MAX_BALANCE_SPAM_CHECKS = int(
    os.getenv("MAX_BALANCE_SPAM_CHECKS", "80")
)

BALANCE_SPAM_TOTAL_TIMEOUT_SECONDS = int(
    os.getenv("BALANCE_SPAM_TOTAL_TIMEOUT_SECONDS", "90")
)

MAX_HISTORY_SPAM_CHECKS = int(
    os.getenv("MAX_HISTORY_SPAM_CHECKS", "100")
)

HISTORY_SPAM_TOTAL_TIMEOUT_SECONDS = int(
    os.getenv("HISTORY_SPAM_TOTAL_TIMEOUT_SECONDS", "120")
)

MAX_HISTORY_BUY_TAX_PERCENT = float(os.getenv("MAX_HISTORY_BUY_TAX_PERCENT", "30"))
MAX_HISTORY_SELL_TAX_PERCENT = float(os.getenv("MAX_HISTORY_SELL_TAX_PERCENT", "30"))
MIN_HISTORY_HOLDER_COUNT = int(os.getenv("MIN_HISTORY_HOLDER_COUNT", "0"))


# ---------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------

SOLANA_BALANCE_TIMEOUT_SECONDS = int(
    os.getenv("SOLANA_BALANCE_TIMEOUT_SECONDS", "45")
)

SOLANA_PRICE_LOOKUP_TIMEOUT_SECONDS = int(
    os.getenv("SOLANA_PRICE_LOOKUP_TIMEOUT_SECONDS", "5")
)

MAX_SOLANA_PRICE_LOOKUPS_PER_BALANCE = int(
    os.getenv("MAX_SOLANA_PRICE_LOOKUPS_PER_BALANCE", "0")
)

SOLANA_BALANCE_SECURITY_CHECK = os.getenv(
    "SOLANA_BALANCE_SECURITY_CHECK",
    "0",
) != "0"

SOLANA_HISTORY_SECURITY_CHECK = os.getenv(
    "SOLANA_HISTORY_SECURITY_CHECK",
    "0",
) != "0"

SOLANA_HISTORY_NAME_LOOKUP_TIMEOUT_SECONDS = int(
    os.getenv("SOLANA_HISTORY_NAME_LOOKUP_TIMEOUT_SECONDS", "30")
)

MAX_SOLANA_HISTORY_NAME_LOOKUPS = int(
    os.getenv("MAX_SOLANA_HISTORY_NAME_LOOKUPS", "20")
)

DEFAULT_SOLANA_MAX_SIGNATURES_PER_ADDRESS = int(
    os.getenv("DEFAULT_SOLANA_MAX_SIGNATURES_PER_ADDRESS", "30")
)

HARD_MAX_SOLANA_MAX_SIGNATURES_PER_ADDRESS = int(
    os.getenv("HARD_MAX_SOLANA_MAX_SIGNATURES_PER_ADDRESS", "100")
)

DEFAULT_SOLANA_HISTORY_TIMEOUT_SECONDS = int(
    os.getenv("DEFAULT_SOLANA_HISTORY_TIMEOUT_SECONDS", "600")
)

HARD_MAX_SOLANA_HISTORY_TIMEOUT_SECONDS = int(
    os.getenv("HARD_MAX_SOLANA_HISTORY_TIMEOUT_SECONDS", "900")
)


# ---------------------------------------------------------------------
# Сети
# ---------------------------------------------------------------------

NETWORKS: Dict[str, dict] = {
    "ethereum": {
        "name": "Ethereum",
        "chain_id": 1,
        "native_symbol": "ETH",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "dex_routers": [
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
            "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        ],
        "stablecoins": [
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        ],
        "rpc_url": _evm_rpc_url(1),
    },
    "bsc": {
        "name": "BSC",
        "chain_id": 56,
        "native_symbol": "BNB",
        "weth": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "dex_routers": [
            "0x10ED43C718714eb63d5aA57B78B54704E256024E",
            "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        ],
        "stablecoins": [
            "0x55d398326f99059fF775485246999027B3197955",
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        ],
        "rpc_url": _evm_rpc_url(56),
    },
    "solana": {
        "name": "Solana",
        "chain_id": None,
        "native_symbol": "SOL",
        "weth": None,
        "dex_programs": [
            "JUP6LbhbzKjY1YJGgBX2RqHGrWFnQHk9mvQLyXZ9iH7",
            "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
            "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grHm7Fjkh",
        ],
        "stablecoins": [
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ],
        "rpc_url": HELIUS_URL or "https://api.mainnet-beta.solana.com",
    },
}


# ---------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------

DB_PATH = "data/mm_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"

MIN_USD_VALUE = 0.10
TELEGRAM_MAX_MESSAGE_LENGTH = 4000