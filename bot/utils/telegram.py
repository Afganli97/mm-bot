# bot/utils/telegram.py
"""
Telegram message helpers.
"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


async def send_long_message(bot, chat_id: int, text: str, parse_mode: str = None):
    text = str(text or "").strip()

    if not text:
        return

    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        await bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return

    chunk = ""

    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > TELEGRAM_MAX_MESSAGE_LENGTH:
            await bot.send_message(
                chat_id,
                chunk.strip(),
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            chunk = line + "\n"
        else:
            chunk += line + "\n"

    if chunk.strip():
        await bot.send_message(
            chat_id,
            chunk.strip(),
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )