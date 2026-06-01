"""
Глобальные константы и настройки.
Загружает переменные окружения из файла .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Разрешённые пользователи
ALLOWED_USER_IDS = set()
raw_ids = os.getenv("ALLOWED_USER_IDS", "")
if raw_ids:
    ALLOWED_USER_IDS = {int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()}

# ---------- Ключи API ----------
# Etherscan (только для Ethereum)
ETHERSCAN_API_KEYS = [k.strip() for k in os.getenv("ETHERSCAN_API_KEYS", "").split(",") if k.strip()]
# Alchemy (основной RPC для всех EVM, включая BSC)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
# Infura (резервный RPC)
INFURA_API_KEY = os.getenv("INFURA_API_KEY", "")

# Solana
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

# ---------- Параметры анализа ----------
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_BRANCHES = 50
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MIN_TRANSFER_VALUE_ETH = 0.001
DEFAULT_MAX_ADDRESSES = 2000
DEFAULT_MAX_FOUND_TOKENS = 100

# ---------- Генерация RPC URL для EVM (Alchemy / Infura) ----------
def _evm_rpc_url(chain_id: int) -> str:
    """Возвращает RPC URL для указанного chain_id, используя Alchemy или Infura."""
    if ALCHEMY_API_KEY:
        subdomains = {
            1: "eth-mainnet",
            56: "bsc-mainnet",
            137: "polygon-mainnet",
            42161: "arb-mainnet",
            10: "opt-mainnet",
            8453: "base-mainnet",
        }
        sub = subdomains.get(chain_id, f"unknown-{chain_id}")
        return f"https://{sub}.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    elif INFURA_API_KEY:
        domains = {
            1: "mainnet.infura.io",
            56: "bsc-mainnet.infura.io",
            137: "polygon-mainnet.infura.io",
            42161: "arbitrum-mainnet.infura.io",
            10: "optimism-mainnet.infura.io",
            8453: "base-mainnet.infura.io",
        }
        domain = domains.get(chain_id, f"unknown-{chain_id}.infura.io")
        return f"https://{domain}/v3/{INFURA_API_KEY}"
    else:
        public = {
            1: "https://eth.llamarpc.com",
            56: "https://bsc-dataseed.binance.org/",
        }
        return public.get(chain_id, "")

# ---------- Конфигурации сетей ----------
NETWORKS = {
    "ethereum": {
        "name": "Ethereum",
        "chain_id": 1,
        "native_symbol": "ETH",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "dex_routers": [
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
            "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
            "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        ],
        "stablecoins": [
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        ],
        "rpc_url": _evm_rpc_url(1),
        "min_transfer_value_native": 0.001,
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
            "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",
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
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ],
        "rpc_url": HELIUS_URL or "https://api.mainnet-beta.solana.com",
        "min_transfer_value_native": 0.001,
    }
}

# ---------- Прочее ----------
DB_PATH = "data/mm_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"