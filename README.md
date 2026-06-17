# MM Coin Scanner Bot

Приватный Telegram-бот для анализа кошельков Ethereum, BSC и Solana.

## Возможности

- Балансы EVM: Ethereum + BSC.
- Балансы Solana.
- История покупок EVM с обходом связанных адресов.
- История покупок Solana с pagination, `blockTime`, `innerInstructions`.
- Проверка spam через бесплатные источники.
- Исключение non-native токенов, которые находятся ровно в количестве `1` единица.
- Нативные ETH/BNB/SOL не исключаются по правилу `1` единицы.
- `/dashboard` показывает usage/free limits.
- `/settings` управляет глубиной, периодом, лимитами адресов/веток/токенов.
- Поддержка только бесплатных сервисов.

## Запуск

```bash
python -m pip install -r requirements.txt
python bot/main.py