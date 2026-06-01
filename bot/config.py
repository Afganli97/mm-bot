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
# Etherscan (общий для Ethereum, BSC и других EVM)
ETHERSCAN_API_KEYS = [k.strip() for k in os.getenv("ETHERSCAN_API_KEYS", "").split(",") if k.strip()]
# Alchemy (Ethereum RPC)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
ALCHEMY_URL = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}" if ALCHEMY_API_KEY else ""
# Infura (Ethereum RPC)
INFURA_API_KEY = os.getenv("INFURA_API_KEY")
INFURA_URL = f"https://mainnet.infura.io/v3/{INFURA_API_KEY}" if INFURA_API_KEY else ""

# Solana
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

# ---------- Параметры анализа (значения по умолчанию) ----------
DEFAULT_MAX_DEPTH = 3                      # максимальная глубина обхода
DEFAULT_MAX_BRANCHES = 50                 # макс. число получателей с одного адреса
DEFAULT_LOOKBACK_DAYS = 30                # анализируемый период
DEFAULT_MIN_TRANSFER_VALUE_ETH = 0.001    # минимальная сумма перевода ETH/WETH
DEFAULT_MAX_ADDRESSES = 2000              # предельное число адресов за одну задачу
DEFAULT_MAX_FOUND_TOKENS = 100            # максимальное количество найденных токенов

# ---------- Адреса контрактов и настройки сетей ----------
NETWORKS = {
    "ethereum": {
        "name": "Ethereum",
        "chain_id": 1,
        "native_symbol": "ETH",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "explorer_api_url": "https://api.etherscan.io/v2/api",
        "explorer_name": "etherscan",
        "dex_routers": [
            "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2 Router
            "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  # Uniswap V3 Router 2
            "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",  # SushiSwap Router
        ],
        "stablecoins": [
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
            "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
        ],
        "rpc_url": ALCHEMY_URL or INFURA_URL,
        "min_transfer_value_native": 0.001,  # ETH
    },
    "bsc": {
        "name": "BSC",
        "chain_id": 56,
        "native_symbol": "BNB",
        "weth": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB
        "explorer_api_url": "https://api.bscscan.com/api",  # BscScan V1
        "explorer_name": "bscscan",
        "dex_routers": [
            "0x10ED43C718714eb63d5aA57B78B54704E256024E",  # PancakeSwap Router v2
            "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",  # PancakeSwap Router v3
        ],
        "stablecoins": [
            "0x55d398326f99059fF775485246999027B3197955",  # USDT
            "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",  # USDC
            "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3",  # DAI
        ],
        "rpc_url": "https://bsc-dataseed.binance.org/",
        "min_transfer_value_native": 0.001,  # BNB
    },
    "solana": {
        "name": "Solana",
        "chain_id": None,
        "native_symbol": "SOL",
        "weth": None,  # не используется
        "explorer_api_url": "https://api.solscan.io/v1",
        "explorer_name": "solscan",
        "dex_programs": [
            "JUP6LbhbzKjY1YJGgBX2RqHGrWFnQHk9mvQLyXZ9iH7",  # Jupiter Aggregator v6
            "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
            "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grHm7Fjkh",  # Raydium
        ],
        "stablecoins": [
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        ],
        "rpc_url": HELIUS_URL or "https://api.mainnet-beta.solana.com",
        "min_transfer_value_native": 0.001,  # SOL
    }
}

# ---------- Прочее ----------
DB_PATH = "data/mm_bot.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"