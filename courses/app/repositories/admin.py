from asyncpg import Pool
from typing import Any

async def get_all_referrals_with_stats(pool: Pool) -> list[dict[str, Any]]:
    """
    Возвращает всех рефералов вместе с данными пользователя и простой статистикой.
    """
    rows = await pool.fetch(
        """
        SELECT
            r.id,
            r.user_id,
            r.code,
            r.client_ids,
            r.payments,
            r.commission_percent,
            r.created_at,
            u.email,
            u.username,
            u.first_name,
            u.last_name,
            u.role
        FROM referrals r
        JOIN users u ON u.id = r.user_id
        ORDER BY r.created_at DESC
        """
    )

    result = []
    for row in rows:
        d = dict(row)
        client_ids = d.get("client_ids") or []
        payments = d.get("payments") or []
        d["clients_count"] = len(client_ids) if isinstance(client_ids, list) else 0
        d["payments_count"] = len(payments) if isinstance(payments, list) else 0
        result.append(d)
    return result


async def get_users_with_referral_role_without_record(pool: Pool) -> list[dict[str, Any]]:
    """
    Пользователи с ролью referral, у которых ещё нет записи в таблице referrals.
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
        LEFT JOIN referrals r ON r.user_id = u.id
        WHERE u.role = 'referral'
          AND r.id IS NULL
        ORDER BY u.created_at DESC
        """
    )
    return [dict(r) for r in rows]
