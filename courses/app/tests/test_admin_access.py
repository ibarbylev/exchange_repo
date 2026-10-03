import pytest
from datetime import datetime, timezone, timedelta
from .conftest import post_form


# ====================== ПОИСК ПОЛЬЗОВАТЕЛЯ ======================

@pytest.mark.asyncio
async def test_search_user_by_email(admin_client, access_user, db_pool):
    """Поиск пользователя по email (классический GET)"""
    user_id = await access_user(access_level=1, days=30)
    user = await db_pool.fetchrow("SELECT email FROM users WHERE id = $1", user_id)

    # Теперь используем классический GET-поиск (как в access.html)
    response = await admin_client.get(f"/admin/access/?q={user['email']}")
    assert response.status_code == 200
    assert user["email"] in response.text
    assert str(user_id) in response.text


# ====================== ИЗМЕНЕНИЕ ДОСТУПА ======================

@pytest.mark.asyncio
async def test_cannot_change_access_if_level_is_zero(admin_client, access_user):
    """Нельзя менять доступ, если у пользователя access_level = 0"""
    user_id = await access_user(access_level=0)
    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 1}
    )
    assert response.status_code == 303
    assert "error=level_zero" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_cannot_change_access_if_subscription_expired(admin_client, access_user):
    """Нельзя менять доступ, если подписка уже истекла"""
    user_id = await access_user(access_level=1, days=-5)
    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 2}
    )
    assert response.status_code == 303
    assert "error=expired" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_cannot_set_same_level(admin_client, access_user):
    """Попытка поставить тот же уровень"""
    user_id = await access_user(access_level=1, days=30)
    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 1}
    )
    assert response.status_code == 303
    assert "error=same_level" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_gift_access_different_level_fails(admin_client, access_user, db_pool):
    """Нельзя подарить другой уровень, если у пользователя уже есть активный доступ"""
    user_id = await access_user(access_level=1, days=30)
    response = await post_form(
        admin_client,
        "/admin/gift-access/",
        data={"user_id": user_id, "access_level": 2, "duration_months": 1}
    )
    assert response.status_code == 303
    # Проверяем код ошибки (а не текст сообщения)
    assert "error=different_level" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_set_access_to_zero(admin_client, access_user, db_pool):
    """Успешное обнуление доступа"""
    user_id = await access_user(access_level=2, days=30)
    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 0}
    )
    assert response.status_code == 303

    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 0
    assert user["access_until"] is None


@pytest.mark.asyncio
async def test_upgrade_standard_to_premium(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=30)
    before = await db_pool.fetchrow("SELECT access_until FROM users WHERE id = $1", user_id)
    old_until = before["access_until"]

    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 2},
    )
    assert response.status_code == 303

    after = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert after["access_level"] == 2

    now = datetime.now(timezone.utc)
    expected_until = now + (old_until - now) / 1.5
    assert abs((after["access_until"] - expected_until).total_seconds()) < 10


@pytest.mark.asyncio
async def test_downgrade_premium_to_standard(admin_client, access_user, db_pool):
    """Понижение с Premium (2) на Standard (1)"""
    user_id = await access_user(access_level=2, days=30)
    before = await db_pool.fetchrow("SELECT access_until FROM users WHERE id = $1", user_id)
    old_until = before["access_until"]

    response = await post_form(
        admin_client,
        "/admin/access/update/",
        data={"user_id": user_id, "new_access": 1}
    )
    assert response.status_code == 303

    after = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert after["access_level"] == 1

    now = datetime.now(timezone.utc)
    old_delta = (old_until - now).total_seconds()
    new_delta = (after["access_until"] - now).total_seconds()
    expected_new_delta = old_delta * 1.5
    assert abs(new_delta - expected_new_delta) < 10


# ====================== ПОДАРОК ДОСТУПА ======================

@pytest.mark.asyncio
async def test_gift_access_to_user_without_access(admin_client, access_user, db_pool):
    """Подарок доступа пользователю без доступа"""
    user_id = await access_user(access_level=0)
    response = await post_form(
        admin_client,
        "/admin/gift-access/",
        data={"user_id": user_id, "access_level": 1, "duration_months": 1}
    )
    assert response.status_code == 303

    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 1
    assert user["access_until"] is not None


@pytest.mark.asyncio
async def test_gift_access_to_user_with_expired_access(admin_client, access_user, db_pool):
    """Подарок доступа пользователю с истёкшей подпиской"""
    user_id = await access_user(access_level=2, days=-10)
    response = await post_form(
        admin_client,
        "/admin/gift-access/",
        data={"user_id": user_id, "access_level": 2, "duration_months": 12}
    )
    assert response.status_code == 303

    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 2
    assert user["access_until"] > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_gift_access_renew_same_level(admin_client, access_user, db_pool):
    """Продление подарком существующего доступа того же уровня"""
    user_id = await access_user(access_level=1, days=10)
    before = await db_pool.fetchrow("SELECT access_until FROM users WHERE id = $1", user_id)

    response = await post_form(
        admin_client,
        "/admin/gift-access/",
        data={"user_id": user_id, "access_level": 1, "duration_months": 1}
    )
    assert response.status_code == 303

    after = await db_pool.fetchrow("SELECT access_until FROM users WHERE id = $1", user_id)
    delta = (after["access_until"] - before["access_until"]).days
    assert 29 <= delta <= 31



@pytest.mark.asyncio
async def test_gift_access_creates_history_record(admin_client, access_user, db_pool):
    """При подарке доступа создаётся запись в истории"""
    user_id = await access_user(access_level=0)
    await post_form(
        admin_client,
        "/admin/gift-access/",
        data={"user_id": user_id, "access_level": 2, "duration_months": 12}
    )
    history = await db_pool.fetchrow("""
        SELECT reason, new_access_level
        FROM user_access_history
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 1
    """, user_id)
    assert history["reason"] == "gift"
    assert history["new_access_level"] == 2