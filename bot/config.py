"""
Глобальные константы и настройки.
Загружает переменные окружения из файла .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Разрешённые пользователи (только им бот отвечает)
ALLOWED_USER_IDS = set()
raw_ids = os.getenv("ALLOWED_USER_IDS", "")
if raw_ids:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()}

# ---------- Ключи API ----------
# Etherscan (только для истории Ethereum)
ETHERSCAN_API_KEYS = [k.strip() for k in os.getenv("ETHERSCAN_API_KEYS", "").split(",") if k.strip()]
# Alchemy / Infura (резервный RPC)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
INFURA_API_KEY = os.getenv("INFURA_API_KEY", "")
# Ankr Advanced API – основной источник балансов EVM
ANKR_API_KEY = os.getenv("ANKR_API_KEY", "")
ANKR_API_URL = f"https://rpc.ankr.com/multichain/{ANKR_API_KEY}" if ANKR_API_KEY else "https://rpc.ankr.com/multichain"

# Solana
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
# Birdeye
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "")

# ---------- Параметры анализа ----------
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_BRANCHES = 50
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MIN_TRANSFER_VALUE_ETH = 0.001
DEFAULT_MAX_ADDRESSES = 2000
DEFAULT_MAX_FOUND_TOKENS = 100

# Генерация RPC URL для EVM (резерв)
def _evm_rpc_url(chain_id: int) -> str:
    if ALCHEMY_API_KEY:
        subdomains = {1: "eth-mainnet", 56: "bnb-mainnet"}
        sub = subdomains.get(chain_id, f"unknown-{chain_id}")
        return f"https://{sub}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    elif INFURA_API_KEY:
        domains = {1: "mainnet.infura.io", 56: "bsc-mainnet.infura.io"}
        domain = domains.get(chain_id, f"unknown-{chain_id}.infura.io")
        return f"https://{domain}/v3/{INFURA_API_KEY}"
    return "https://bsc-dataseed.binance.org/" if chain_id == 56 else "https://eth.llamarpc.com"

# ---------- Конфигурации сетей ----------
NETWORKS = {
    "ethereum": {
        "name": "Ethereum",
        "chain_id": 1,
        "native_symbol": "ETH",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "dex_routers": [
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2 Router
            "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  # Uniswap V3 Router 2
        ],
        "stablecoins": [
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
        ],
        "rpc_url": _evm_rpc_url(1),
        "min_transfer_value_native": 0.001,
    },
    "bsc": {
        "name": "BSC",
        "chain_id": 56,
        "native_symbol": "BNB",
        "weth": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB
        "dex_routers": [
            "0x10ED43C718714eb63d5aA57B78B54704E256024E",  # PancakeSwap Router v2
            "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",  # PancakeSwap Router v3
        ],
        "stablecoins": [
            "0x55d398326f99059fF775485246999027B3197955",  # USDT
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC
        ],
        "rpc_url": _evm_rpc_url(56),
        "min_transfer_value_native": 0.001,
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
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        ],
        "rpc_url": HELIUS_URL or "https://api.mainnet-beta.solana.com",
        "min_transfer_value_native": 0.001,
    }
}

# ---------- Прочее ----------
DB_PATH = "data/mm_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"
MIN_USD_VALUE = 0.10   # минимальная стоимость токена для отображения