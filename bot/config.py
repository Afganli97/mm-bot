"""
Глобальные настройки проекта.
Загружает переменные окружения из .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Разрешённые пользователи
ALLOWED_USER_IDS = set()
raw_ids = os.getenv("ALLOWED_USER_IDS", "")
if raw_ids:
    ALLOWED_USER_IDS = {
        int(uid.strip())
        for uid in raw_ids.split(",")
        if uid.strip().isdigit()
    }

# ---------- API keys ----------
ETHERSCAN_API_KEYS = [
    k.strip()
    for k in os.getenv("ETHERSCAN_API_KEYS", "").split(",")
    if k.strip()
]

BSCSCAN_API_KEYS = [
    k.strip()
    for k in os.getenv("BSCSCAN_API_KEYS", "").split(",")
    if k.strip()
]

ANKR_API_KEY = os.getenv("ANKR_API_KEY", "")
ANKR_API_URL = f"https://rpc.ankr.com/multichain/{ANKR_API_KEY}" if ANKR_API_KEY else ""

ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
INFURA_API_KEY = os.getenv("INFURA_API_KEY", "")

MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")

# ---------- Настройки поиска ----------
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_BRANCHES = 50
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MAX_ADDRESSES = 2000
DEFAULT_MAX_FOUND_TOKENS = 100

SETTING_CAPS = {
    "max_depth": 6,
    "max_branches": 200,
    "lookback_days": 180,
    "max_addresses": 5000,
    "max_tokens": 1000,
}

# ---------- Лимиты бесплатных сервисов ----------
RATE_LIMITS = {
    "etherscan": 100_000,
    "bscscan": 100_000,
    "ankr": 100_000,
    "moralis": 1_500,
    "alchemy": 100_000,
    "helius": 100_000,
    "dexscreener": 5_000,
    "geckoterminal": 5_000,
    "jupiter": 5_000,
    "birdeye": 5_000,
    "public_rpc": 20_000,
}

# ---------- Прочее ----------
DB_PATH = "data/mm_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"

MIN_USD_VALUE = 0.10

SPAM_LIQUIDITY_USD = float(os.getenv("SPAM_LIQUIDITY_USD", "50"))
SPAM_VOLUME_24H_USD = float(os.getenv("SPAM_VOLUME_24H_USD", "1"))


def _evm_rpc_url(chain_id: int) -> str:
    if ALCHEMY_API_KEY:
        subdomains = {
            1: "eth-mainnet",
            56: "bnb-mainnet",
        }
        sub = subdomains.get(chain_id, f"unknown-{chain_id}")
        return f"https://{sub}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    elif INFURA_API_KEY:
        domains = {
            1: "mainnet.infura.io",
            56: "bsc-mainnet.infura.io",
        }
        domain = domains.get(chain_id, f"unknown-{chain_id}.infura.io")
        return f"https://{domain}/v3/{INFURA_API_KEY}"

    if chain_id == 56:
        return "https://bsc-dataseed.binance.org/"
    return "https://eth.llamarpc.com"


# ---------- Конфигурации сетей ----------
NETWORKS = {
    "ethereum": {
        "key": "ethereum",
        "name": "Ethereum",
        "chain_id": 1,
        "native_symbol": "ETH",
        "native_decimals": 18,
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "moralis_chain": "eth",
        "ankr_chain": "eth",
        "dex_routers": [
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
            "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
            "0x1111111254EEB25477B68fb85Ed929f73A960582",
            "0x1111111254fb6c44bAC0beD2854e76F90643097d",
            "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
            "0x881D40237659C251811CEC9c364ef91dC08D300C",
            "0x986A52Df8E857f69C17a4a26C3917D77621896E7",
        ],
        "aggregators": [
            "0x1111111254EEB25477B68fb85Ed929f73A960582",
            "0x1111111254fb6c44bAC0beD2854e76F90643097d",
            "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
            "0x881D40237659C251811CEC9c364ef91dC08D300C",
            "0x986A52Df8E857f69C17a4a26C3917D77621896E7",
        ],
        "stablecoins": [
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        ],
        "rpc_url": _evm_rpc_url(1),
    },
    "bsc": {
        "key": "bsc",
        "name": "BSC",
        "chain_id": 56,
        "native_symbol": "BNB",
        "native_decimals": 18,
        "weth": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "moralis_chain": "bsc",
        "ankr_chain": "bsc",
        "dex_routers": [
            "0x10ED43C718714eb63d5aA57B78B54704E256024E",
            "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
            "0x1b81D678ffb9C0263b24A97847620C99d213eB14",
            "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8",
            "0xcF101d43F1b4676B91C6f68B26F366B2a5C8f8c8",
            "0x8317c460C22A9958c27b4B6403b78fDf7809C9C4",
            "0x1111111254EEB25477B68fb85Ed929f73A960582",
            "0x986A52Df8E857f69C17a4a26C3917D77621896E7",
        ],
        "aggregators": [
            "0x1111111254EEB25477B68fb85Ed929f73A960582",
            "0x986A52Df8E857f69C17a4a26C3917D77621896E7",
        ],
        "stablecoins": [
            "0x55d398326f99059fF775485246999027B3197955",
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
        ],
        "rpc_url": _evm_rpc_url(56),
    },
    "solana": {
        "key": "solana",
        "name": "Solana",
        "chain_id": None,
        "native_symbol": "SOL",
        "native_decimals": 9,
        "weth": None,
        "dex_programs": [
            "JUP6LbhbzKjY1YJGgBX2RqHGrWFnQHk9mvQLyXZ9iH7",
            "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
            "675kPX9MHTjS2zt1qfr1NYCn9RSm3MrH8e8m8j1xtMp",
            "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
            "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grHm7Fjkh",
            "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
            "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",
        ],
        "stablecoins": [
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ],
        "rpc_url": HELIUS_URL or "https://api.mainnet-beta.solana.com",
    },
}

EVM_NETWORKS = {
    key: conf
    for key, conf in NETWORKS.items()
    if conf.get("chain_id") is not None
}

SOLANA_NETWORK = NETWORKS["solana"]