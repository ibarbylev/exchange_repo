from asyncpg import Pool
from typing import Any
# import json

from app.repositories.referrer import _normalize_payments, _calc_balance


async def get_all_referrers_with_stats(pool: Pool) -> list[dict[str, Any]]:
    """
    Возвращает всех рефереров вместе с данными пользователя и статистикой:
    - clients_count: количество клиентов (рефералов)
    - total_sales: сумма продаж (amount > 0) по всем клиентам реферера
    - total_commission: сумма начисленных вознаграждений (commission) по amount > 0
    - total_payouts: сумма выплаченного рефереру (|amount| при amount < 0)
    - balance: total_commission - total_payouts (к выплате)
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
        payments = _normalize_payments(d.get("payments"))

        d["clients_count"] = len(referral_ids) if isinstance(referral_ids, list) else 0

        total_sales = 0.0
        total_commission = 0.0
        total_payouts = 0.0
        for p in payments:
            if not isinstance(p, dict):
                continue
            try:
                amount = float(p.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0.0

            if amount > 0:
                total_sales += amount
                try:
                    total_commission += float(p.get("commission") or 0)
                except (TypeError, ValueError):
                    pass
            elif amount < 0:
                total_payouts += abs(amount)

        d["total_sales"] = round(total_sales, 2)
        d["total_commission"] = round(total_commission, 2)
        d["total_payouts"] = round(total_payouts, 2)
        d["balance"] = round(total_commission - total_payouts, 2)
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
