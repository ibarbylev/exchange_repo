"""
Репозиторий для работы с реферерами (владельцами реферальных ссылок).
"""
from __future__ import annotations

import secrets
import string
from typing import Any

from asyncpg import Pool


def generate_referral_code(length: int = 16) -> str:
    """Генерирует случайный код из цифр (абстрактный хэш)."""
    alphabet = string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_referrer_by_user_id(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """Получает запись реферера по user_id."""
    row = await pool.fetchrow(
        """
        SELECT id, user_id, code, referral_ids, payments, commission_percent, created_at
        FROM referrers
        WHERE user_id = $1
        """,
        user_id,
    )
    return dict(row) if row else None


async def get_referrer_by_code(pool: Pool, code: str) -> dict[str, Any] | None:
    """Получает запись реферера по коду."""
    row = await pool.fetchrow(
        """
        SELECT id, user_id, code, referral_ids, payments, commission_percent, created_at
        FROM referrers
        WHERE code = $1
        """,
        code,
    )
    return dict(row) if row else None


async def create_referrer(pool: Pool, user_id: int, commission_percent: float = 10.0) -> dict[str, Any]:
    """Создаёт запись реферера с уникальным кодом. Вызывать после назначения роли."""
    # Проверяем, нет ли уже
    existing = await get_referrer_by_user_id(pool, user_id)
    if existing:
        return existing

    # Генерируем уникальный код
    for _ in range(10):
        code = generate_referral_code()
        try:
            row = await pool.fetchrow(
                """
                INSERT INTO referrers (user_id, code, commission_percent)
                VALUES ($1, $2, $3)
                RETURNING id, user_id, code, referral_ids, payments, commission_percent, created_at
                """,
                user_id,
                code,
                commission_percent,
            )
            return dict(row)
        except Exception:
            continue  # код занят, пробуем другой

    raise RuntimeError("Не удалось сгенерировать уникальный код реферера")


async def add_referral_to_referrer(pool: Pool, code: str, referral_user_id: int) -> bool:
    """
    Добавляет реферала (нового пользователя) в список реферера (без дублей).
    Возвращает True, если добавление произошло.
    """
    result = await pool.execute(
        """
        UPDATE referrers
        SET referral_ids = array_append(referral_ids, $2)
        WHERE code = $1
          AND NOT ($2 = ANY(referral_ids))
          AND user_id != $2
        """,
        code,
        referral_user_id,
    )
    # result looks like "UPDATE 1" or "UPDATE 0"
    return result.endswith("1")


async def get_referrals_info(pool: Pool, referral_ids: list[int]) -> list[dict[str, Any]]:
    """Получает краткую информацию о рефералах (пользователях, пришедших по ссылке)."""
    if not referral_ids:
        return []

    rows = await pool.fetch(
        """
        SELECT id, username, email, first_name, last_name, created_at
        FROM users
        WHERE id = ANY($1::int[])
        ORDER BY created_at DESC
        """,
        referral_ids,
    )
    return [dict(r) for r in rows]
