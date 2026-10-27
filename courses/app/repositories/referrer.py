"""
Репозиторий для работы с реферерами (владельцами реферальных ссылок).
"""
from __future__ import annotations

import json
import secrets
import string
from datetime import date
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


def _normalize_payments(payments_raw: Any) -> list[dict[str, Any]]:
    """Приводит payments (list / str / None) к списку словарей."""
    if payments_raw is None:
        return []
    if isinstance(payments_raw, str):
        try:
            payments = json.loads(payments_raw)
        except (json.JSONDecodeError, TypeError):
            return []
    elif isinstance(payments_raw, list):
        payments = payments_raw
    else:
        payments = list(payments_raw) if payments_raw else []
    return [p for p in payments if isinstance(p, dict)]


def _calc_balance(payments: list[dict[str, Any]]) -> tuple[float, float, float]:
    """
    Считает total_commission, total_payouts, balance по правилам:
    - amount > 0  → начисление (берём commission)
    - amount < 0  → выплата (|amount|)
    """

    total_commission = 0.0
    total_payouts = 0.0
    for p in payments:
        try:
            amount = float(p.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount > 0:
            try:
                total_commission += float(p.get("commission") or 0)
            except (TypeError, ValueError):
                pass
        elif amount < 0:
            total_payouts += abs(amount)
    total_commission = round(total_commission, 2)
    total_payouts = round(total_payouts, 2)
    balance = round(total_commission - total_payouts, 2)
    return total_commission, total_payouts, balance


async def add_referrer_payout(
    pool: Pool,
    user_id: int,
    amount: float,
    comment: str = "",
) -> dict[str, Any]:
    """
    Создаёт запись выплаты рефереру в referrers.payments (JSONB).

    Конвенция поля payments:
    - начисление: amount > 0, commission, order_id, user_id, date
    - выплата:    amount < 0, comment, date

    amount в аргументе — положительная сумма, которую админ хочет выплатить.
    В JSON пишется отрицательное значение.

    Проверки:
    - реферер существует
    - amount > 0
    - amount <= balance (начислено − уже выплачено)

    Возвращает обновлённую запись реферера.
    Raises ValueError с кодом ошибки (строка) при валидации.
    """
    referrer = await get_referrer_by_user_id(pool, user_id)
    if not referrer:
        raise ValueError("referrer_not_found")

    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("amount_must_be_positive") from None

    if amount <= 0:
        raise ValueError("amount_must_be_positive")

    payments = _normalize_payments(referrer.get("payments"))
    _, _, balance = _calc_balance(payments)

    if amount > balance + 1e-9:
        raise ValueError("amount_exceeds_available")

    payout_entry = {
        "date": date.today().isoformat(),
        "amount": -amount,  # отрицательное = выплата
        "comment": (comment or "").strip()[:500],
    }

    row = await pool.fetchrow(
        """
        UPDATE referrers
        SET payments = payments || $2::jsonb
        WHERE user_id = $1
        RETURNING id, user_id, code, referral_ids, payments, commission_percent, created_at
        """,
        user_id,
        json.dumps([payout_entry]),
    )
    if not row:
        raise ValueError("update_failed")
    return dict(row)
