"""
Высокоуровневые функции начисления/списания баллов лояльности.
Все правила живут здесь.
Низкоуровневая вставка — через SQL-функцию insert_into_loyalty.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from asyncpg import Pool


async def insert_into_loyalty(
    pool: Pool,
    user_id: int,
    points: int,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """
    Низкоуровневый вызов SQL-функции insert_into_loyalty.
    Возвращает строку loyalty или None при ошибке.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM insert_into_loyalty($1, $2, $3)",
            user_id,
            points,
            reason,
        )
    return dict(row) if row else None


def weighted_daily_points() -> int:
    """1–5 баллов. Меньшие значения встречаются чаще."""
    # 1: 40%, 2: 30%, 3: 15%, 4: 10%, 5: 5%
    return random.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0]


async def award_daily_task(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """1–5 баллов за решение задания дня (меньшие чаще)."""
    points = weighted_daily_points()
    return await insert_into_loyalty(
        pool, user_id, points, "Решение задания дня"
    )


async def award_useful_feedback(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """100 баллов за полезный отзыв (question_quality = 1)."""
    return await insert_into_loyalty(
        pool, user_id, 100, "Полезный отзыв по улучшению сайта"
    )


async def award_cashback(
    pool: Pool, user_id: int, amount_euro: float
) -> dict[str, Any] | None:
    """
    Кэшбэк: 1000 * 1% от суммы покупки в евро.
    Пример: 50€ → 500 баллов.
    """
    points = int(amount_euro * 10)  # 1% от суммы * 1000
    if points <= 0:
        return None
    return await insert_into_loyalty(
        pool, user_id, points, f"Кэшбэк с покупки {amount_euro:.2f} €"
    )


async def award_welcome(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """1000 приветственных баллов при регистрации."""
    return await insert_into_loyalty(
        pool, user_id, 1000, "Приветственные баллы"
    )


async def get_loyalty_balance(pool: Pool, user_id: int) -> int:
    """Текущий баланс из users.loyalty_points (быстро) или fallback на SUM."""
    async with pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT loyalty_points FROM users WHERE id = $1",
            user_id,
        )
        if balance is None:
            balance = await conn.fetchval(
                "SELECT COALESCE(SUM(points), 0) FROM loyalty WHERE user_id = $1",
                user_id,
            )
    return int(balance or 0)


async def get_loyalty_transactions(pool: Pool, user_id: int) -> list[dict]:
    """Список транзакций (новые сверху)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT points, reason, created_at
            FROM loyalty
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )

    transactions = []
    for row in rows:
        created_at = row["created_at"]
        if isinstance(created_at, datetime):
            created_at_str = created_at.strftime("%d.%m.%Y %H:%M")
        else:
            created_at_str = str(created_at)[:16]
        transactions.append({
            "points": int(row["points"] or 0),
            "reason": row["reason"] or "Операция",
            "created_at_str": created_at_str,
        })
    return transactions


