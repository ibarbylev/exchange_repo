"""
Тесты реферальной программы.

Покрывают:
- репозиторий referrer (код, создание, привязка рефералов);
- SQL-функцию accrue_referral_commission (основной фокус);
- интеграцию через auto_payment_pending_order;
- HTTP-эндпоинты /ref/{code}, invite, страница реферера;
- регистрацию с cookie referrer_code (привязка + бонус 10 000).

Фикстуры переиспользуются из courses/tests/conftest.py:
  db_pool, create_user, create_pending_order, client, auth_client, post_form.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token
from app.repositories.referrer import (
    add_referral_to_referrer,
    create_referrer,
    generate_referral_code,
    get_referrals_info,
    get_referrer_by_code,
    get_referrer_by_user_id,
)


def _post_form(client: AsyncClient, url: str, data: dict | None = None, follow_redirects: bool = False, **kwargs):
    """Локальная копия helper'а post_form из conftest (CSRF из cookie)."""
    if data is None:
        data = {}
    csrf_token = client.cookies.get("csrf_token")
    if csrf_token:
        data = dict(data)
        data.setdefault("csrf_token", csrf_token)
    return client.post(url, data=data, follow_redirects=follow_redirects, **kwargs)


# ---------------------------------------------------------------------------
# Вспомогательные хелперы
# ---------------------------------------------------------------------------

async def _make_referrer(
    db_pool,
    create_user,
    *,
    commission_percent: float = 10.0,
    role: str = "referrer",
) -> dict:
    """Создаёт пользователя с ролью referrer и запись в referrers."""
    user_id = await create_user(role=role, username=f"ref_{uuid.uuid4().hex[:10]}")
    referrer = await create_referrer(db_pool, user_id, commission_percent)
    return referrer


async def _make_paid_order(
    db_pool,
    user_id: int,
    amount: float = 20.00,
    product_slug: str = "access-standard-monthly",
) -> int:
    """Создаёт заказ в статусе paid + order_item (без вызова auto_payment)."""
    product_id = await db_pool.fetchval(
        "SELECT id FROM products WHERE slug = $1", product_slug
    )
    assert product_id, f"Продукт {product_slug} не найден в тестовой БД"

    order_id = await db_pool.fetchval(
        """
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'paid', $2)
        RETURNING id
        """,
        user_id,
        amount,
    )
    await db_pool.execute(
        """
        INSERT INTO order_items (order_id, product_id, quantity, price, discount)
        VALUES ($1, $2, 1, $3, 0)
        """,
        order_id,
        product_id,
        amount,
    )
    return order_id


def _payments_list(payments) -> list:
    """asyncpg может вернуть JSONB как str или list."""
    if payments is None:
        return []
    if isinstance(payments, str):
        return json.loads(payments)
    if isinstance(payments, list):
        return payments
    return list(payments)


# ===========================================================================
# 1. Репозиторий
# ===========================================================================

class TestGenerateReferralCode:
    def test_length_and_digits_only(self):
        code = generate_referral_code(16)
        assert len(code) == 16
        assert code.isdigit()

    def test_custom_length(self):
        code = generate_referral_code(8)
        assert len(code) == 8
        assert code.isdigit()

    def test_uniqueness_sample(self):
        codes = {generate_referral_code() for _ in range(50)}
        assert len(codes) == 50


class TestCreateReferrer:
    @pytest.mark.asyncio
    async def test_creates_with_default_percent(self, db_pool, create_user):
        user_id = await create_user(role="referrer")
        ref = await create_referrer(db_pool, user_id)
        assert ref["user_id"] == user_id
        assert len(ref["code"]) == 16
        assert ref["code"].isdigit()
        assert Decimal(str(ref["commission_percent"])) == Decimal("10.00")
        assert ref["referral_ids"] == [] or ref["referral_ids"] is None or list(ref["referral_ids"]) == []
        assert _payments_list(ref["payments"]) == []

    @pytest.mark.asyncio
    async def test_creates_with_custom_percent(self, db_pool, create_user):
        user_id = await create_user(role="referrer")
        ref = await create_referrer(db_pool, user_id, commission_percent=15.5)
        assert Decimal(str(ref["commission_percent"])) == Decimal("15.50")

    @pytest.mark.asyncio
    async def test_idempotent_returns_existing(self, db_pool, create_user):
        user_id = await create_user(role="referrer")
        first = await create_referrer(db_pool, user_id)
        second = await create_referrer(db_pool, user_id, commission_percent=99.0)
        assert first["id"] == second["id"]
        assert first["code"] == second["code"]
        # Процент не меняется при повторном вызове
        assert Decimal(str(second["commission_percent"])) == Decimal("10.00")


class TestGetReferrer:
    @pytest.mark.asyncio
    async def test_by_user_id_and_code(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        by_user = await get_referrer_by_user_id(db_pool, ref["user_id"])
        by_code = await get_referrer_by_code(db_pool, ref["code"])
        assert by_user is not None
        assert by_code is not None
        assert by_user["id"] == by_code["id"] == ref["id"]

    @pytest.mark.asyncio
    async def test_missing_returns_none(self, db_pool):
        assert await get_referrer_by_user_id(db_pool, 999_999) is None
        assert await get_referrer_by_code(db_pool, "0000000000000000") is None


class TestAddReferralToReferrer:
    @pytest.mark.asyncio
    async def test_adds_new_referral(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")

        ok = await add_referral_to_referrer(db_pool, ref["code"], referral_id)
        assert ok is True

        updated = await get_referrer_by_code(db_pool, ref["code"])
        ids = list(updated["referral_ids"] or [])
        assert referral_id in ids

    @pytest.mark.asyncio
    async def test_no_duplicate(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")

        assert await add_referral_to_referrer(db_pool, ref["code"], referral_id) is True
        assert await add_referral_to_referrer(db_pool, ref["code"], referral_id) is False

        updated = await get_referrer_by_code(db_pool, ref["code"])
        ids = list(updated["referral_ids"] or [])
        assert ids.count(referral_id) == 1

    @pytest.mark.asyncio
    async def test_cannot_add_self(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        ok = await add_referral_to_referrer(db_pool, ref["code"], ref["user_id"])
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_code(self, db_pool, create_user):
        referral_id = await create_user()
        ok = await add_referral_to_referrer(db_pool, "9999999999999999", referral_id)
        assert ok is False


class TestGetReferralsInfo:
    @pytest.mark.asyncio
    async def test_empty_list(self, db_pool):
        assert await get_referrals_info(db_pool, []) == []

    @pytest.mark.asyncio
    async def test_returns_users(self, db_pool, create_user):
        u1 = await create_user(username=f"a_{uuid.uuid4().hex[:6]}")
        u2 = await create_user(username=f"b_{uuid.uuid4().hex[:6]}")
        info = await get_referrals_info(db_pool, [u1, u2])
        ids = {row["id"] for row in info}
        assert ids == {u1, u2}


# ===========================================================================
# 2. SQL-функция accrue_referral_commission  (главный фокус)
# ===========================================================================

class TestAccrueReferralCommission:
    """Прямые вызовы SELECT accrue_referral_commission($1)."""

    @pytest.mark.asyncio
    async def test_no_referrer_returns_false(self, db_pool, create_user):
        user_id = await create_user()
        order_id = await _make_paid_order(db_pool, user_id, amount=20.00)

        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_order_not_paid_returns_false(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        # pending, не paid
        order_id = await db_pool.fetchval(
            """
            INSERT INTO orders (user_id, status, amount)
            VALUES ($1, 'pending', 20.00)
            RETURNING id
            """,
            referral_id,
        )
        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is False

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert payments == []

    @pytest.mark.asyncio
    async def test_success_writes_payment_and_calculates_commission(
        self, db_pool, create_user
    ):
        ref = await _make_referrer(db_pool, create_user, commission_percent=10.0)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        amount = Decimal("20.00")
        order_id = await _make_paid_order(db_pool, referral_id, amount=float(amount))

        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is True

        updated = await get_referrer_by_code(db_pool, ref["code"])
        payments = _payments_list(updated["payments"])
        assert len(payments) == 1

        entry = payments[0]
        assert entry["order_id"] == order_id
        assert entry["user_id"] == referral_id
        assert Decimal(str(entry["amount"])) == amount
        # 20 * 10% = 2.00
        assert Decimal(str(entry["commission"])) == Decimal("2.00")
        assert "date" in entry
        # дата в формате YYYY-MM-DD
        datetime.strptime(entry["date"], "%Y-%m-%d")

    @pytest.mark.asyncio
    async def test_custom_commission_percent(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user, commission_percent=15.0)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        order_id = await _make_paid_order(db_pool, referral_id, amount=100.00)
        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is True

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert Decimal(str(payments[0]["commission"])) == Decimal("15.00")

    @pytest.mark.asyncio
    async def test_rounding_to_two_decimals(self, db_pool, create_user):
        """ROUND(amount * percent / 100, 2)."""
        ref = await _make_referrer(db_pool, create_user, commission_percent=10.0)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        # 33.33 * 10% = 3.333 → 3.33
        order_id = await _make_paid_order(db_pool, referral_id, amount=33.33)
        await db_pool.fetchval("SELECT accrue_referral_commission($1)", order_id)

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert Decimal(str(payments[0]["commission"])) == Decimal("3.33")

    @pytest.mark.asyncio
    async def test_idempotent_second_call_false(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        order_id = await _make_paid_order(db_pool, referral_id, amount=50.00)

        first = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        second = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert first is True
        assert second is False

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert len(payments) == 1

    @pytest.mark.asyncio
    async def test_zero_amount_returns_false(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        order_id = await _make_paid_order(db_pool, referral_id, amount=0.00)
        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_order_returns_false(self, db_pool):
        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", 999_999
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_multiple_orders_accumulate_payments(self, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user, commission_percent=10.0)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        o1 = await _make_paid_order(db_pool, referral_id, amount=20.00)
        o2 = await _make_paid_order(db_pool, referral_id, amount=30.00)

        assert await db_pool.fetchval("SELECT accrue_referral_commission($1)", o1) is True
        assert await db_pool.fetchval("SELECT accrue_referral_commission($1)", o2) is True

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert len(payments) == 2
        commissions = sorted(Decimal(str(p["commission"])) for p in payments)
        assert commissions == [Decimal("2.00"), Decimal("3.00")]

    @pytest.mark.asyncio
    async def test_self_not_in_referral_ids_no_commission(self, db_pool, create_user):
        """Даже если user_id случайно в referral_ids, условие r.user_id <> v_user_id."""
        ref = await _make_referrer(db_pool, create_user)
        # Пытаемся добавить себя — add_referral_to_referrer уже блокирует,
        # но проверим функцию напрямую на «чужом» заказе без привязки.
        order_id = await _make_paid_order(db_pool, ref["user_id"], amount=20.00)
        result = await db_pool.fetchval(
            "SELECT accrue_referral_commission($1)", order_id
        )
        assert result is False


# ===========================================================================
# 3. Интеграция через auto_payment_pending_order
# ===========================================================================

class TestAccrueViaAutoPayment:
    """
    auto_payment_pending_order после успешной оплаты вызывает
    PERFORM accrue_referral_commission(v_order_id).
    """

    @pytest.mark.asyncio
    async def test_commission_on_auto_payment(
        self, db_pool, create_user, create_pending_order
    ):
        ref = await _make_referrer(db_pool, create_user, commission_percent=10.0)
        referral_id = await create_user(username=f"refd_{uuid.uuid4().hex[:8]}")
        await add_referral_to_referrer(db_pool, ref["code"], referral_id)

        amount = 20.00
        order_id = await create_pending_order(referral_id, amount=amount)

        # Пополняем баланс (триггер tr_payments_auto_pay → auto_payment_pending_order)
        await db_pool.execute(
            """
            INSERT INTO payments (user_id, amount, comment)
            VALUES ($1, $2, 'test top-up')
            """,
            referral_id,
            amount,
        )

        order_status = await db_pool.fetchval(
            "SELECT status FROM orders WHERE id = $1", order_id
        )
        assert order_status == "paid"

        payments = _payments_list(
            (await get_referrer_by_code(db_pool, ref["code"]))["payments"]
        )
        assert len(payments) == 1
        assert payments[0]["order_id"] == order_id
        assert Decimal(str(payments[0]["commission"])) == Decimal("2.00")

    @pytest.mark.asyncio
    async def test_no_commission_without_referrer(
        self, db_pool, create_user, create_pending_order
    ):
        user_id = await create_user()
        amount = 20.00
        order_id = await create_pending_order(user_id, amount=amount)

        await db_pool.execute(
            "INSERT INTO payments (user_id, amount, comment) VALUES ($1, $2, 'top-up')",
            user_id,
            amount,
        )

        order_status = await db_pool.fetchval(
            "SELECT status FROM orders WHERE id = $1", order_id
        )
        assert order_status == "paid"

        # Ни у кого не должно появиться payments
        rows = await db_pool.fetch("SELECT payments FROM referrers")
        for row in rows:
            assert _payments_list(row["payments"]) == []


# ===========================================================================
# 4. HTTP: реферальные ссылки и страница реферера
# ===========================================================================

class TestReferralHTTP:
    @pytest.mark.asyncio
    async def test_ref_link_sets_cookie_and_redirects(self, client, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)

        response = await client.get(f"/ref/{ref['code']}", follow_redirects=False)
        assert response.status_code == 303
        assert "invite" in response.headers["location"]
        assert ref["code"] in response.headers["location"]
        assert response.cookies.get("referrer_code") == ref["code"]

    @pytest.mark.asyncio
    async def test_ref_link_invalid_code_still_redirects_without_cookie(
        self, client
    ):
        response = await client.get("/ref/0000000000000000", follow_redirects=False)
        assert response.status_code == 303
        # cookie не ставим для невалидного кода
        assert response.cookies.get("referrer_code") is None

    @pytest.mark.asyncio
    async def test_invite_page_valid_code_guest(self, client, db_pool, create_user):
        ref = await _make_referrer(db_pool, create_user)
        response = await client.get(f"/bg/ru/invite/{ref['code']}")
        assert response.status_code == 200
        body = response.text
        # state=invite: предложение зарегистрироваться
        assert "register" in body.lower() or "регистр" in body.lower() or ref["code"] in body

    @pytest.mark.asyncio
    async def test_invite_page_invalid_code_404(self, client):
        response = await client.get("/bg/ru/invite/0000000000000000")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_invite_page_logged_in_user(
        self, client, db_pool, create_user
    ):
        ref = await _make_referrer(db_pool, create_user)
        # авторизуем другого пользователя
        user_id = await create_user()
        jti = str(uuid.uuid4())
        token = create_access_token(data={"sub": str(user_id)}, jti=jti)
        await db_pool.execute(
            """
            INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
            VALUES ($1, $2, '127.0.0.1', 'test', NOW())
            """,
            user_id,
            jti,
        )
        client.cookies.set("access_token", token)

        response = await client.get(f"/bg/ru/invite/{ref['code']}")
        assert response.status_code == 200
        # already_logged_in — программа только для новых
        body = response.text.lower()
        assert "already" in body or "уже" in body or "home" in body or "главн" in body

    @pytest.mark.asyncio
    async def test_referrer_page_requires_role(self, auth_client, db_pool):
        """Обычный student → редирект с /referrer."""
        client, _ = auth_client
        response = await client.get("/bg/ru/referrer", follow_redirects=False)
        assert response.status_code == 303

    @pytest.mark.asyncio
    async def test_referrer_page_ok_for_referrer_role(
        self, client, db_pool, create_user
    ):
        ref = await _make_referrer(db_pool, create_user, role="referrer")
        user_id = ref["user_id"]
        jti = str(uuid.uuid4())
        token = create_access_token(data={"sub": str(user_id)}, jti=jti)
        await db_pool.execute(
            """
            INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
            VALUES ($1, $2, '127.0.0.1', 'test', NOW())
            """,
            user_id,
            jti,
        )
        client.cookies.set("access_token", token)

        response = await client.get("/bg/ru/referrer")
        assert response.status_code == 200
        assert ref["code"] in response.text


# ===========================================================================
# 5. Регистрация с referrer_code cookie
# ===========================================================================

class TestRegistrationWithReferral:
    @pytest.mark.asyncio
    async def test_register_with_valid_cookie_attaches_and_bonus(
        self, client, db_pool, create_user, user_data
    ):
        ref = await _make_referrer(db_pool, create_user)
        client.cookies.set("referrer_code", ref["code"])

        data = user_data(
            email=f"ref_reg_{uuid.uuid4().hex[:8]}@example.com",
            password="StrongPass123!",
            first_name="Реф",
            last_name="Тест",
        )
        response = await _post_form(
            client,
            "/bg/ru/auth/register/",
            data={
                "email": data["email"],
                "password": data["password"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
            },
            follow_redirects=False,
        )
        # успешная регистрация → 200 (success page) или редирект
        assert response.status_code in (200, 303, 302)

        new_user = await db_pool.fetchrow(
            "SELECT id, email FROM users WHERE email = $1",
            data["email"].lower(),
        )
        assert new_user is not None

        updated = await get_referrer_by_code(db_pool, ref["code"])
        ids = list(updated["referral_ids"] or [])
        assert new_user["id"] in ids

        reasons = await db_pool.fetch(
            "SELECT points, reason FROM loyalty WHERE user_id = $1 ORDER BY points DESC",
            new_user["id"],
        )
        points_list = [r["points"] for r in reasons]
        assert 10000 in points_list  # реферальный бонус

    @pytest.mark.asyncio
    async def test_register_without_cookie_no_referral_bonus(
        self, client, db_pool, user_data
    ):
        data = user_data(
            email=f"plain_{uuid.uuid4().hex[:8]}@example.com",
            password="StrongPass123!",
            first_name="Обычный",
            last_name="Юзер",
        )
        response = await _post_form(
            client,
            "/bg/ru/auth/register/",
            data={
                "email": data["email"],
                "password": data["password"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
            },
            follow_redirects=False,
        )
        assert response.status_code in (200, 303, 302)

        new_user = await db_pool.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            data["email"].lower(),
        )
        assert new_user is not None

        reasons = await db_pool.fetch(
            "SELECT points, reason FROM loyalty WHERE user_id = $1",
            new_user["id"],
        )
        points_list = [r["points"] for r in reasons]
        assert 10000 not in points_list

    @pytest.mark.asyncio
    async def test_register_with_invalid_cookie_no_crash(
        self, client, db_pool, user_data
    ):
        client.cookies.set("referrer_code", "0000000000000000")
        data = user_data(
            email=f"badref_{uuid.uuid4().hex[:8]}@example.com",
            password="StrongPass123!",
            first_name="Bad",
            last_name="Ref",
        )
        response = await _post_form(
            client,
            "/bg/ru/auth/register/",
            data={
                "email": data["email"],
                "password": data["password"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
            },
            follow_redirects=False,
        )
        assert response.status_code in (200, 303, 302)
        new_user = await db_pool.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            data["email"].lower(),
        )
        assert new_user is not None
