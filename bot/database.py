"""
SQLite storage.
WAL, busy timeout, migrations, usage counters, cache, settings.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path

from bot.config import DB_PATH

logger = logging.getLogger(__name__)

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                network TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                params_json TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                error_message TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visited_addresses (
                network TEXT NOT NULL,
                address TEXT NOT NULL,
                last_checked_identifier INTEGER,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, address)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS found_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                network TEXT NOT NULL,
                token_address TEXT NOT NULL,
                token_symbol TEXT,
                token_name TEXT,
                decimals INTEGER,
                amount_raw TEXT,
                amount TEXT,
                buyer_address TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                block_number INTEGER,
                tx_timestamp INTEGER,
                is_buy_confirmed INTEGER DEFAULT 1,
                is_spam INTEGER DEFAULT 0,
                spam_source TEXT,
                price_usd REAL,
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
            CREATE TABLE IF NOT EXISTS token_cache (
                network TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT,
                name TEXT,
                decimals INTEGER,
                is_native INTEGER DEFAULT 0,
                is_spam INTEGER DEFAULT 0,
                spam_source TEXT,
                spam_reason TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, token_address)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_cache (
                network TEXT NOT NULL,
                token_address TEXT NOT NULL,
                price_usd REAL,
                source TEXT,
                liquidity_usd REAL,
                volume_24h REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (network, token_address)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_progress (
                request_id INTEGER PRIMARY KEY,
                processed_addresses INTEGER DEFAULT 0,
                max_addresses INTEGER,
                processed_transactions INTEGER DEFAULT 0,
                max_transactions INTEGER,
                status TEXT DEFAULT 'running',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (request_id) REFERENCES requests(id)
            )
            """
        )

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

        conn.commit()
        logger.info("Database initialized")


def migrate_db():
    migrations = {
        "requests": [
            ("network", "TEXT NOT NULL DEFAULT ''"),
            ("params_json", "TEXT"),
        ],
        "visited_addresses": [
            ("network", "TEXT NOT NULL DEFAULT ''"),
            ("last_checked_identifier", "INTEGER"),
        ],
        "found_tokens": [
            ("network", "TEXT NOT NULL DEFAULT ''"),
            ("token_name", "TEXT"),
            ("decimals", "INTEGER"),
            ("amount_raw", "TEXT"),
            ("amount", "TEXT"),
            ("tx_timestamp", "INTEGER"),
            ("is_buy_confirmed", "INTEGER DEFAULT 1"),
            ("is_spam", "INTEGER DEFAULT 0"),
            ("spam_source", "TEXT"),
            ("price_usd", "REAL"),
        ],
        "token_cache": [
            ("is_native", "INTEGER DEFAULT 0"),
            ("is_spam", "INTEGER DEFAULT 0"),
            ("spam_source", "TEXT"),
            ("spam_reason", "TEXT"),
        ],
        "price_cache": [
            ("source", "TEXT"),
            ("liquidity_usd", "REAL"),
            ("volume_24h", "REAL"),
        ],
        "task_progress": [
            ("processed_transactions", "INTEGER DEFAULT 0"),
            ("max_transactions", "INTEGER"),
            ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ],
    }

    with get_connection() as conn:
        for table, columns in migrations.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns:
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    logger.info("Migration: added %s.%s", table, column)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_user_id ON requests(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_found_tokens_request_id ON found_tokens(request_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_found_tokens_network ON found_tokens(network)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_found_tokens_token ON found_tokens(token_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_date ON api_usage(usage_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_cache_network_address ON token_cache(network, token_address)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_cache_network_address ON price_cache(network, token_address)")

        conn.commit()
        logger.info("Database migrated")


def create_request(
    user_id: int,
    chat_id: int,
    address: str,
    network: str,
    depth: int,
    max_addresses: int,
    lookback_days: int,
    max_tokens: int,
    max_branches: int,
) -> int:
    params = {
        "depth": depth,
        "max_addresses": max_addresses,
        "lookback_days": lookback_days,
        "max_tokens": max_tokens,
        "max_branches": max_branches,
    }

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO requests
            (user_id, chat_id, address, network, status, params_json, started_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                user_id,
                chat_id,
                address,
                network,
                json.dumps(params, ensure_ascii=False),
                datetime.utcnow(),
            ),
        )
        request_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO task_progress
            (request_id, processed_addresses, max_addresses, processed_transactions, max_transactions, status)
            VALUES (?, 0, ?, 0, NULL, 'running')
            """,
            (request_id, max_addresses),
        )

        conn.commit()
        logger.debug("Created request %s for %s/%s", request_id, network, address)
        return request_id


def update_request_status(request_id: int, status: str, error_message: str = None, finished: bool = False):
    with get_connection() as conn:
        if finished:
            conn.execute(
                """
                UPDATE requests
                SET status=?, finished_at=?, error_message=?
                WHERE id=?
                """,
                (status, datetime.utcnow(), error_message, request_id),
            )
            conn.execute(
                """
                UPDATE task_progress
                SET status=?, updated_at=?
                WHERE request_id=?
                """,
                (status, datetime.utcnow(), request_id),
            )
        else:
            conn.execute(
                """
                UPDATE requests
                SET status=?, error_message=?
                WHERE id=?
                """,
                (status, error_message, request_id),
            )
        conn.commit()


def add_found_token(
    request_id: int,
    network: str,
    token_address: str,
    token_symbol: str,
    token_name: str,
    decimals: int,
    amount_raw: str,
    amount: str,
    buyer_address: str,
    tx_hash: str,
    block_number: int,
    tx_timestamp: int = None,
    is_buy_confirmed: bool = True,
    is_spam: bool = False,
    spam_source: str = None,
    price_usd: float = None,
):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO found_tokens
            (
                request_id,
                network,
                token_address,
                token_symbol,
                token_name,
                decimals,
                amount_raw,
                amount,
                buyer_address,
                tx_hash,
                block_number,
                tx_timestamp,
                is_buy_confirmed,
                is_spam,
                spam_source,
                price_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                network,
                token_address,
                token_symbol,
                token_name,
                decimals,
                str(amount_raw),
                str(amount),
                buyer_address,
                tx_hash,
                block_number,
                tx_timestamp,
                1 if is_buy_confirmed else 0,
                1 if is_spam else 0,
                spam_source,
                price_usd,
            ),
        )
        conn.commit()


def get_found_tokens(request_id: int):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM found_tokens WHERE request_id=? ORDER BY id ASC",
            (request_id,),
        ).fetchall()


def get_visited_address_cache(network: str, address: str, min_identifier: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT last_checked_identifier
            FROM visited_addresses
            WHERE network=? AND address=?
            """,
            (network, address),
        ).fetchone()

        if row is None:
            return False

        last = row["last_checked_identifier"]
        return last is not None and last >= min_identifier


def set_visited_address_cache(network: str, address: str, identifier: int):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO visited_addresses
            (network, address, last_checked_identifier, checked_at)
            VALUES (?, ?, ?, ?)
            """,
            (network, address, identifier, datetime.utcnow()),
        )
        conn.commit()


def get_api_usage_today(service: str, key_index: int = 0) -> int:
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count
            FROM api_usage
            WHERE service=? AND key_index=? AND usage_date=?
            """,
            (service, key_index, today),
        ).fetchone()
        return row["count"] if row else 0


def get_all_api_usage() -> dict:
    today = date.today().isoformat()
    usage = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT service, key_index, count
            FROM api_usage
            WHERE usage_date=?
            """,
            (today,),
        ).fetchall()

        for row in rows:
            usage[f"{row['service']}_{row['key_index']}"] = row["count"]

    return usage


def reset_api_usage(service: str = None, key_index: int = None):
    today = date.today().isoformat()
    with get_connection() as conn:
        if service:
            if key_index is None:
                conn.execute(
                    "DELETE FROM api_usage WHERE service=? AND usage_date=?",
                    (service, today),
                )
            else:
                conn.execute(
                    "DELETE FROM api_usage WHERE service=? AND key_index=? AND usage_date=?",
                    (service, key_index, today),
                )
        else:
            conn.execute("DELETE FROM api_usage WHERE usage_date=?", (today,))
        conn.commit()


def update_task_progress(
    request_id: int,
    processed_addresses: int = None,
    processed_transactions: int = None,
    status: str = None,
):
    with get_connection() as conn:
        if processed_addresses is not None:
            conn.execute(
                "UPDATE task_progress SET processed_addresses=?, updated_at=? WHERE request_id=?",
                (processed_addresses, datetime.utcnow(), request_id),
            )
        if processed_transactions is not None:
            conn.execute(
                "UPDATE task_progress SET processed_transactions=?, updated_at=? WHERE request_id=?",
                (processed_transactions, datetime.utcnow(), request_id),
            )
        if status:
            conn.execute(
                "UPDATE task_progress SET status=?, updated_at=? WHERE request_id=?",
                (status, datetime.utcnow(), request_id),
            )
        conn.commit()


def get_user_setting(user_id: int, setting: str, default: str = None) -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND setting=?",
            (user_id, setting),
        ).fetchone()
        return row["value"] if row else default


def set_user_setting(user_id: int, setting: str, value: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_settings (user_id, setting, value) VALUES (?, ?, ?)",
            (user_id, setting, value),
        )
        conn.commit()


def reset_user_settings(user_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        conn.commit()


def get_user_settings_dict(user_id: int) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT setting, value FROM user_settings WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {row["setting"]: row["value"] for row in rows}


def get_token_cache(network: str, token_address: str):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM token_cache
            WHERE network=? AND token_address=?
            """,
            (network, token_address),
        ).fetchone()


def set_token_cache(
    network: str,
    token_address: str,
    symbol: str = None,
    name: str = None,
    decimals: int = None,
    is_native: bool = False,
    is_spam: bool = False,
    spam_source: str = None,
    spam_reason: str = None,
):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO token_cache
            (
                network,
                token_address,
                symbol,
                name,
                decimals,
                is_native,
                is_spam,
                spam_source,
                spam_reason,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                network,
                token_address,
                symbol,
                name,
                decimals,
                1 if is_native else 0,
                1 if is_spam else 0,
                spam_source,
                spam_reason,
                datetime.utcnow(),
            ),
        )
        conn.commit()


def get_price_cache(network: str, token_address: str):
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM price_cache
            WHERE network=? AND token_address=?
            """,
            (network, token_address),
        ).fetchone()


def set_price_cache(
    network: str,
    token_address: str,
    price_usd: float = None,
    source: str = None,
    liquidity_usd: float = None,
    volume_24h: float = None,
):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO price_cache
            (
                network,
                token_address,
                price_usd,
                source,
                liquidity_usd,
                volume_24h,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                network,
                token_address,
                price_usd,
                source,
                liquidity_usd,
                volume_24h,
                datetime.utcnow(),
            ),
        )
        conn.commit()