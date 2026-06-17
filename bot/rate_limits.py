"""
Unified free-tier rate limiter.
Считает usage по UTC и резервирует запрос до выполнения.
"""
import logging
from datetime import datetime, timezone

from bot.config import RATE_LIMITS
from bot.database import get_connection

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    pass


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_configured_key_indexes(service: str):
    """
    Для multi-key сервисов показываем все ключи.
    Для остальных — один индекс 0.
    """
    if service == "etherscan":
        from bot.config import ETHERSCAN_API_KEYS

        return list(range(max(1, len(ETHERSCAN_API_KEYS))))

    if service == "bscscan":
        from bot.config import BSCSCAN_API_KEYS

        return list(range(max(1, len(BSCSCAN_API_KEYS))))

    return [0]


class RateLimitTracker:
    @staticmethod
    def get_limit(service: str, key_index: int = 0) -> int:
        return int(RATE_LIMITS.get(service, 10_000))

    @staticmethod
    def get_usage_today(service: str, key_index: int = 0) -> int:
        today = utc_today()
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

    @staticmethod
    def is_available(service: str, key_index: int = 0) -> bool:
        limit = RateLimitTracker.get_limit(service, key_index)
        used = RateLimitTracker.get_usage_today(service, key_index)
        return used < limit

    @staticmethod
    def reserve(service: str, key_index: int = 0) -> bool:
        today = utc_today()
        limit = RateLimitTracker.get_limit(service, key_index)

        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT count
                    FROM api_usage
                    WHERE service=? AND key_index=? AND usage_date=?
                    """,
                    (service, key_index, today),
                ).fetchone()

                used = row["count"] if row else 0
                if used >= limit:
                    conn.execute("COMMIT")
                    logger.warning("Rate limit exhausted: %s key=%s used=%s limit=%s", service, key_index, used, limit)
                    return False

                conn.execute(
                    """
                    INSERT INTO api_usage (service, key_index, usage_date, count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(service, key_index, usage_date)
                    DO UPDATE SET count = count + 1
                    """,
                    (service, key_index, today),
                )
                conn.execute("COMMIT")
                return True

            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def require(service: str, key_index: int = 0):
        if not RateLimitTracker.reserve(service, key_index):
            raise RateLimitExceeded(f"Лимит {service}#{key_index} исчерпан")

    @staticmethod
    def reset(service: str = None, key_index: int = None):
        today = utc_today()
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

    @staticmethod
    def get_dashboard_rows() -> list[dict]:
        rows = []
        services = list(RATE_LIMITS.keys())

        for service in services:
            indexes = get_configured_key_indexes(service)
            for idx in indexes:
                limit = RateLimitTracker.get_limit(service, idx)
                used = RateLimitTracker.get_usage_today(service, idx)
                rows.append(
                    {
                        "service": service,
                        "key_index": idx,
                        "used": used,
                        "limit": limit,
                        "remaining": max(0, limit - used),
                    }
                )

        return rows