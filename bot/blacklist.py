"""
Черный список адресов (Tier-1/2/3 CEX, мосты, роутеры DEX).
Предотвращает зацикливание алгоритма обхода на адресах с миллионами транзакций.
"""

# EVM адреса всегда приводим к нижнему регистру!
EVM_BLACKLIST = {
    # --- Binance Hot Wallets ---
    "0x28C6c06298d514Db089934071355E5743bf21d60".lower(),
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d".lower(),
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f".lower(),
    # --- OKX Hot Wallets ---
    "0x5041ed759Cb4bCC011F50Ca8E72806e1414CE91A".lower(),
    "0x6cc5F688a315f3dC28A7781717a9A798a59fDA7b".lower(),
    # --- MEXC, Gate, KuCoin, Bybit (Примеры) ---
    "0x75e89d5979E4f6Fba9F97c104c2F0AFB3F1dcB88".lower(), # MEXC
    "0x0A59649758aa4d66E25f08Dd01271e891fe52199".lower(), # Gate.io
    "0xf16E9B0D03470827A95CDfd0Cb8a8A3b46969B91".lower(), # Bybit
    # --- Bridges (Мосты) ---
    "0x8315177aB297bA92A06054cE80a67Ed4DBd7ed3a".lower(), # Arbitrum ERC20 Gateway
    "0x3ee18B2214AFF97000D974cf647E7C347E8fa585".lower(), # LayerZero
    "0x40ec5B33f54e0E8A33A975908C5BA1c14e5BbbDf".lower(), # Polygon ERC20 Bridge
    # --- DEX Routers (Uniswap, 1inch, Pancake) ---
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D".lower(), # Uniswap V2 Router
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45".lower(), # Uniswap V3 Router
    "0x1111111254fb6c44bac0bed2854e76f90643097d".lower(), # 1inch Router
    "0x10ED43C718714eb63d5aA57B78B54704E256024E".lower(), # PancakeSwap Router
}

# Solana адреса чувствительны к регистру (Base58)!
SOLANA_BLACKLIST = {
    # --- Binance Hot Wallets ---
    "5tzFkiKscXHK5ZXCGbXZcmAzEteEwEebn2x35v222a7w",
    "28xcLWgcYosG1E5y5gqG7P8T7Jg8Ff84z2L9nJ6Khy6p",
    "9WzDXwBbmcg8Zc8snZAKeKzzjXpLS71N1YFq7Z7zQ2Pj", # OKX
    "7MBLg6oV5phip11YBbJPxq216aPZ6GmyJ2tB9pG6FpY4", # MEXC
    "4B4e1yEn9E2kC9V8Eih2q3zT1W3jEeejV5fXJ2aE1mP6", # Gate.io
    "5Z9QJdG2jL7B4zZp1b9wL3zVv3B9d9L8y4C2b1Q5z9N7", # KuCoin
    # --- DEX & Bridges Authorities ---
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", # Raydium Authority
    "JUP6LbhbzKjY1YJGgBX2RqHGrWFnQHk9mvQLyXZ9iH7",  # Jupiter Aggregator
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpools
}

def is_blacklisted(address: str, is_solana: bool = False) -> bool:
    """
    Проверяет, находится ли адрес в черном списке бирж/мостов.
    """
    if is_solana:
        return address in SOLANA_BLACKLIST
    return address.lower() in EVM_BLACKLIST
