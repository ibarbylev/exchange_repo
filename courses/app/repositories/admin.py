from asyncpg import Pool
from typing import Any


async def get_all_referrers_with_stats(pool: Pool) -> list[dict[str, Any]]:
    """
    Возвращает всех рефереров вместе с данными пользователя и простой статистикой.
    """
    rows = await pool.fetch(
        """
        SELECT
            r.id,
            r.user_id,
            r.code,
            r.referral_ids,
            r.payments,
            r.commission_percent,
            r.created_at,
            u.email,
            u.username,
            u.first_name,
            u.last_name,
            u.role
        FROM referrers r
        JOIN users u ON u.id = r.user_id
        ORDER BY r.created_at DESC
        """
    )

    result = []
    for row in rows:
        d = dict(row)
        referral_ids = d.get("referral_ids") or []
        payments = d.get("payments") or []
        d["referrals_count"] = len(referral_ids) if isinstance(referral_ids, list) else 0
        d["payments_count"] = len(payments) if isinstance(payments, list) else 0
        result.append(d)
    return result


async def get_users_with_referrer_role_without_record(pool: Pool) -> list[dict[str, Any]]:
    """
    Пользователи с ролью referrer, у которых ещё нет записи в таблице referrers.
    """
    rows = await pool.fetch(
        """
        SELECT
            u.id,
            u.email,
            u.username,
            u.first_name,
            u.last_name,
            u.role,
            u.created_at
        FROM users u
        LEFT JOIN referrers r ON r.user_id = u.id
        WHERE u.role = 'referrer'
          AND r.id IS NULL
        ORDER BY u.created_at DESC
        """
    )
    return [dict(r) for r in rows]
