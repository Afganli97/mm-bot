"""
Инициализация и работа с SQLite.
Хранит кэш адресов, результаты задач, счётчики API, кэш топ-токенов.
"""
import sqlite3
import logging
from datetime import datetime, date
from contextlib import contextmanager
from pathlib import Path

from bot.config import DB_PATH

logger = logging.getLogger(__name__)

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                depth_used INTEGER,
                status TEXT DEFAULT 'pending',
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                error_message TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS visited_addresses (
                address TEXT PRIMARY KEY,
                last_checked_block INTEGER,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS found_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                token_address TEXT NOT NULL,
                token_symbol TEXT,
                buyer_address TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                block_number INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (request_id) REFERENCES requests(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                service TEXT NOT NULL,
                key_index INTEGER NOT NULL,
                usage_date DATE NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (service, key_index, usage_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS top_tokens_cache (
                id INTEGER PRIMARY KEY CHECK (id=1),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_progress (
                request_id INTEGER PRIMARY KEY,
                processed_addresses INTEGER DEFAULT 0,
                max_addresses INTEGER,
                status TEXT DEFAULT 'running',
                FOREIGN KEY (request_id) REFERENCES requests(id)
            )
        """)
        conn.commit()
    logger.info("База данных инициализирована")

@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def create_request(user_id: int, chat_id: int, address: str, depth: int) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO requests (user_id, chat_id, address, depth_used, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
            (user_id, chat_id, address, depth, datetime.utcnow())
        )
        request_id = cur.lastrowid
        conn.execute(
            "INSERT INTO task_progress (request_id, processed_addresses, max_addresses, status) VALUES (?, 0, ?, 'running')",
            (request_id, depth * 10)
        )
        conn.commit()
        logger.debug(f"Создана задача {request_id} для адреса {address}")
        return request_id

def update_request_status(request_id: int, status: str, error_message: str = None, finished: bool = False):
    with get_connection() as conn:
        if finished:
            conn.execute(
                "UPDATE requests SET status=?, finished_at=?, error_message=? WHERE id=?",
                (status, datetime.utcnow(), error_message, request_id)
            )
        else:
            conn.execute(
                "UPDATE requests SET status=?, error_message=? WHERE id=?",
                (status, error_message, request_id)
            )
        conn.commit()
        logger.debug(f"Задача {request_id}: статус={status}, ошибка={error_message}")

def add_found_token(request_id: int, token_address: str, token_symbol: str, buyer_address: str, tx_hash: str, block_number: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO found_tokens (request_id, token_address, token_symbol, buyer_address, tx_hash, block_number) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, token_address, token_symbol, buyer_address, tx_hash, block_number)
        )
        conn.commit()
        logger.debug(f"Добавлен токен {token_address} в задачу {request_id}")

def get_visited_address_cache(address: str, min_block: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_checked_block FROM visited_addresses WHERE address=?",
            (address,)
        ).fetchone()
        return row is not None and row['last_checked_block'] is not None and row['last_checked_block'] >= min_block

def set_visited_address_cache(address: str, block_number: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO visited_addresses (address, last_checked_block, checked_at) VALUES (?, ?, ?)",
            (address, block_number, datetime.utcnow())
        )
        conn.commit()
        logger.debug(f"Кэш посещённых адресов обновлён: {address} (блок {block_number})")

def increment_api_usage(service: str, key_index: int):
    today = date.today().isoformat()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO api_usage (service, key_index, usage_date, count) VALUES (?, ?, ?, 1) ON CONFLICT(service, key_index, usage_date) DO UPDATE SET count = count + 1",
            (service, key_index, today)
        )
        conn.commit()

def get_api_usage_today(service: str, key_index: int) -> int:
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT count FROM api_usage WHERE service=? AND key_index=? AND usage_date=?",
            (service, key_index, today)
        ).fetchone()
        return row['count'] if row else 0

def get_all_api_usage() -> dict:
    today = date.today().isoformat()
    usage = {}
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT service, key_index, count FROM api_usage WHERE usage_date=?",
            (today,)
        ).fetchall()
        for r in rows:
            service, idx, cnt = r['service'], r['key_index'], r['count']
            usage[f"{service}_{idx}"] = cnt
    return usage

def update_task_progress(request_id: int, processed: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE task_progress SET processed_addresses=? WHERE request_id=?",
            (processed, request_id)
        )
        conn.commit()