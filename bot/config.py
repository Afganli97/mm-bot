"""
Глобальные константы и настройки.
Загружает переменные окружения из файла .env.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Etherscan API ключи (список)
ETHERSCAN_API_KEYS = [k.strip() for k in os.getenv("ETHERSCAN_API_KEYS", "").split(",") if k.strip()]
# Alchemy
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
ALCHEMY_URL = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}" if ALCHEMY_API_KEY else ""
# Infura
INFURA_API_KEY = os.getenv("INFURA_API_KEY")
INFURA_URL = f"https://mainnet.infura.io/v3/{INFURA_API_KEY}" if INFURA_API_KEY else ""

# ---------- Параметры анализа ----------
MAX_DEPTH = 3                      # максимальная глубина обхода
MAX_BRANCHES_PER_ADDRESS = 50      # макс. число получателей с одного адреса
LOOKBACK_DAYS = 30                 # анализируемый период
MIN_TRANSFER_VALUE_ETH = 0.001     # минимальная сумма перевода ETH/WETH для включения
MAX_ADDRESSES_PER_TASK = 2000      # предельное число адресов за одну задачу

# ---------- Адреса контрактов (Ethereum Mainnet) ----------
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# Роутеры DEX (используются для определения покупок)
DEX_ROUTERS = [
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2 Router
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",  # Uniswap V3 Router 2
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",  # SushiSwap Router
]

# Стейблкоины (адреса контрактов)
STABLECOINS = {
    "0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
    "0x4Fabb145d64652a948d72533023f6E7A623C7C53",  # BUSD
    "0x0000000000085d4780B73119b644AE5ecd22b376",  # TUSD
    "0x8E870D67F660D95d5be530380D0eC0bd388289E1",  # USDP
    "0x853d955aCEf822Db058eb8505911ED77F175b99e",  # FRAX
    "0x5f98805A4E8be255e2e6F31a4D5f6d7C7b5f29B0",  # LUSD
    "0x99D8a9C45b2ecA8864373A26D1459e3Dff1e17F3",  # MIM
}

# ---------- Прочее ----------
# База данных SQLite
DB_PATH = "data/mm_bot.db"

# Логирование
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = "data/bot.log"
