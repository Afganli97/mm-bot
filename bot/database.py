"""
SQLite-хранилище проекта.

Хранит:
- задачи анализа;
- кэш посещённых адресов;
- найденные токены;
- счётчики API-запросов;
- пользовательские настройки;
- прогресс обхода графа.

Важно:
- api_usage чистится по дате;
- visited_addresses учитывает chain_id;
- requests хранит user_id/chat_id/network/chain_id.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

from bot.config import API_LIMITS, DB_PATH


logger = logging.getLogger(__name__)

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        yield conn
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def _migrate_requests(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            network TEXT,
            chain_id TEXT,
            depth_used INTEGER,
            max_addresses INTEGER,
            status TEXT DEFAULT 'pending',
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error_message TEXT
        )
        """
    )

    if not _column_exists(conn, "requests", "network"):
        conn.execute("ALTER TABLE requests ADD COLUMN network TEXT")

    if not _column_exists(conn, "requests", "chain_id"):
        conn.execute("ALTER TABLE requests ADD COLUMN chain_id TEXT")

    if not _column_exists(conn, "requests", "max_addresses"):
        conn.execute("ALTER TABLE requests ADD COLUMN max_addresses INTEGER")


def _migrate_visited_addresses(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visited_addresses (
            address TEXT NOT NULL,
            chain_id TEXT NOT NULL DEFAULT '0',
            last_checked_block INTEGER,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (address, chain_id)
        )
        """
    )

    if not _column_exists(conn, "visited_addresses", "chain_id"):
        conn.execute("ALTER TABLE visited_addresses ADD COLUMN chain_id TEXT")
        conn.execute("UPDATE visited_addresses SET chain_id = '0' WHERE chain_id IS NULL")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_visited_addresses_unique
        ON visited_addresses(address, chain_id)
        """
    )


def _migrate_task_progress(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_progress (
            request_id INTEGER PRIMARY KEY,
            processed_addresses INTEGER DEFAULT 0,
            max_addresses INTEGER,
            status TEXT DEFAULT 'running',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (request_id) REFERENCES requests(id)
        )
        """
    )

    if not _column_exists(conn, "task_progress", "updated_at"):
        conn.execute("ALTER TABLE task_progress ADD COLUMN updated_at TIMESTAMP")


def cleanup_old_api_usage(today: Optional[str] = None) -> None:
    """
    Удаляет старые записи api_usage, оставляя только сегодняшние.
    """

    today = today or date.today().isoformat()

    with get_connection() as conn:
        conn.execute(
            "DELETE FROM api_usage WHERE usage_date != ?",
            (today,),
        )
        conn.commit()


def init_db() -> None:
    with get_connection() as conn:
        _migrate_requests(conn)

        _migrate_visited_addresses(conn)

        conn.execute(
            """
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
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                service TEXT NOT NULL,
                key_index INTEGER NOT NULL,
                usage_date DATE NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (service, key_index, usage_date)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS top_tokens_cache (
                network TEXT PRIMARY KEY,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_json TEXT NOT NULL
            )
            """
        )

        _migrate_task_progress(conn)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER NOT NULL,
                setting TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY (user_id, setting)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_found_tokens_request
            ON found_tokens(request_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_found_tokens_token
            ON found_tokens(token_address)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_usage_date
            ON api_usage(usage_date)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_settings_user
            ON user_settings(user_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_requests_user_started
            ON requests(user_id, started_at)
            """
        )

        conn.commit()

    cleanup_old_api_usage()
    logger.info("База данных инициализирована")


def create_request(
    user_id: int,
    chat_id: int,
    address: str,
    depth: int,
    network: Optional[str] = None,
    chain_id: Optional[str] = None,
    max_addresses: int = 2000,
) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO requests (
                user_id,
                chat_id,
                address,
                network,
                chain_id,
                depth_used,
                max_addresses,
                status,
                started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                user_id,
                chat_id,
                address,
                network,
                chain_id,
                depth,
                max_addresses,
                datetime.utcnow(),
            ),
        )

        request_id = int(cursor.lastrowid)

        conn.execute(
            """
            INSERT INTO task_progress (
                request_id,
                processed_addresses,
                max_addresses,
                status,
                updated_at
            )
            VALUES (?, 0, ?, 'running', ?)
            """,
            (
                request_id,
                max_addresses,
                datetime.utcnow(),
            ),
        )

        conn.commit()

    logger.debug("Создана задача %s для адреса %s", request_id, address)
    return request_id


def update_request_status(
    request_id: int,
    status: str,
    error_message: Optional[str] = None,
    finished: bool = False,
) -> None:
    with get_connection() as conn:
        if finished:
            conn.execute(
                """
                UPDATE requests
                SET status = ?,
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    datetime.utcnow(),
                    error_message,
                    request_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE requests
                SET status = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    status,
                    error_message,
                    request_id,
                ),
            )

        conn.commit()

    logger.debug("Задача %s: статус=%s, ошибка=%s", request_id, status, error_message)


def add_found_token(
    request_id: int,
    token_address: str,
    token_symbol: str,
    buyer_address: str,
    tx_hash: str,
    block_number: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO found_tokens (
                request_id,
                token_address,
                token_symbol,
                buyer_address,
                tx_hash,
                block_number
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                token_address,
                token_symbol,
                buyer_address,
                tx_hash,
                block_number,
            ),
        )

        conn.commit()

    logger.debug("Добавлен токен %s в задачу %s", token_address, request_id)


def get_visited_address_cache(
    address: str,
    min_block: int,
    chain_id: Optional[str] = None,
) -> bool:
    chain_id = chain_id or "0"

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT last_checked_block
            FROM visited_addresses
            WHERE address = ? AND chain_id = ?
            """,
            (
                address.lower(),
                chain_id,
            ),
        ).fetchone()

    return bool(
        row is not None
        and row["last_checked_block"] is not None
        and int(row["last_checked_block"]) >= int(min_block)
    )


def set_visited_address_cache(
    address: str,
    block_number: int,
    chain_id: Optional[str] = None,
) -> None:
    chain_id = chain_id or "0"

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO visited_addresses (
                address,
                chain_id,
                last_checked_block,
                checked_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                address.lower(),
                chain_id,
                int(block_number),
                datetime.utcnow(),
            ),
        )

        conn.commit()

    logger.debug("Кэш адреса обновлён: %s chain_id=%s block=%s", address, chain_id, block_number)


def increment_api_usage(service: str, key_index: int = 0) -> None:
    today = date.today().isoformat()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_usage (
                service,
                key_index,
                usage_date,
                count
            )
            VALUES (?, ?, ?, 1)
            ON CONFLICT(service, key_index, usage_date)
            DO UPDATE SET count = count + 1
            """,
            (
                service,
                int(key_index),
                today,
            ),
        )

        conn.commit()


def get_api_usage_today(service: str, key_index: int = 0) -> int:
    today = date.today().isoformat()

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count
            FROM api_usage
            WHERE service = ?
              AND key_index = ?
              AND usage_date = ?
            """,
            (
                service,
                int(key_index),
                today,
            ),
        ).fetchone()

    return int(row["count"]) if row else 0


def get_all_api_usage() -> Dict[str, dict]:
    """
    Возвращает агрегированную статистику API-лимитов.

    Формат:
    {
        "etherscan": {
            "used": 120,
            "limit": 100000,
            "keys": {
                0: 70,
                1: 50
            }
        }
    }
    """

    today = date.today().isoformat()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT service, key_index, count
            FROM api_usage
            WHERE usage_date = ?
            """,
            (today,),
        ).fetchall()

    usage: Dict[str, dict] = {}

    for service in sorted(set(API_LIMITS.keys()) | {row["service"] for row in rows}):
        keys: Dict[int, int] = {}

        for row in rows:
            if row["service"] == service:
                keys[int(row["key_index"])] = int(row["count"])

        usage[service] = {
            "used": sum(keys.values()),
            "limit": API_LIMITS.get(service, 0),
            "keys": keys,
        }

    return usage


def update_task_progress(
    request_id: Optional[int],
    processed: int,
    status: str = "running",
) -> None:
    if request_id is None:
        return

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE task_progress
            SET processed_addresses = ?,
                status = ?,
                updated_at = ?
            WHERE request_id = ?
            """,
            (
                int(processed),
                status,
                datetime.utcnow(),
                request_id,
            ),
        )

        conn.commit()


def get_user_setting(user_id: int, setting: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT value
            FROM user_settings
            WHERE user_id = ?
              AND setting = ?
            """,
            (
                user_id,
                setting,
            ),
        ).fetchone()

    return row["value"] if row else default


def set_user_setting(user_id: int, setting: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_settings (
                user_id,
                setting,
                value
            )
            VALUES (?, ?, ?)
            """,
            (
                user_id,
                setting,
                value,
            ),
        )

        conn.commit()

    logger.debug("Пользователь %s: %s = %s", user_id, setting, value)


def get_user_settings_dict(user_id: int) -> Dict[str, str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT setting, value
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

    return {
        row["setting"]: row["value"]
        for row in rows
    }


def delete_user_settings(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()

    logger.debug("Настройки пользователя %s удалены", user_id)