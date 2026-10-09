import pytest
from httpx import AsyncClient
from unittest.mock import patch

from app.core.security import get_password_hash, create_email_verification_token
from .conftest import post_form

# --- registration -------------------------------------------------
@pytest.mark.asyncio
async def test_register_new_user(client: AsyncClient, db_pool, user_data):
    """Регистрация нового пользователя + начисление 1000 приветственных баллов"""
    data = user_data()
    response = await post_form(client, "/bg/ru/auth/register/", data=data)

    assert response.status_code == 200
    assert "Проверьте почту" in response.text or "успешно" in response.text.lower()

    # Пользователь должен появиться в БД
    user = await db_pool.fetchrow(
        "SELECT id, loyalty_points FROM users WHERE LOWER(email) = LOWER($1)",
        data["email"],
    )
    assert user is not None

    # Приветственные баллы начислены
    assert user["loyalty_points"] == 1000

    # Запись в истории лояльности
    tx = await db_pool.fetchrow(
        "SELECT points, reason FROM loyalty WHERE user_id = $1",
        user["id"],
    )
    assert tx is not None
    assert tx["points"] == 1000
    assert "Приветственные" in (tx["reason"] or "")


@pytest.mark.asyncio
async def test_register_existing_unconfirmed_user(client, db_pool, user_data):
    email = "unconfirmed_test@example.com"

    # Первая регистрация
    data = user_data(email=email)
    await post_form(client, "/bg/ru/auth/register/", data=data)

    # Вторая регистрация
    response = await post_form(client, "/bg/ru/auth/register/", data=data)

    assert response.status_code == 200
    html_lower = response.text.lower()
    assert "registration success" in html_lower or "already_exists" in response.text

    # 1. Проверяем title страницы — самый стабильный маркер
    assert "registration success" in html_lower

    # 2. Проверяем наличие флага already_exists (мы его явно передаём из кода)
    assert "already_exists" in response.text


# --- login ---------------------------------------------------
@pytest.mark.asyncio
async def test_login_confirmed_user(client, db_pool, user_data):
    email = "confirmed_user_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "confirmed_test", email, hashed)

    # === ОТЛАДКА ===
    print("Cookies после создания клиента:", dict(client.cookies))
    csrf_from_cookie = client.cookies.get("csrf_token")
    print("CSRF token из cookie:", csrf_from_cookie)

    data = user_data(email=email, password=password)
    response = await post_form(client, "/bg/ru/auth/login/", data=data)

    print("Status code:", response.status_code)
    print("Response text (первые 300 символов):", response.text[:300])

    assert response.status_code == 303


@pytest.mark.asyncio
async def test_login_unconfirmed_user(client: AsyncClient, db_pool, user_data):
    """Попытка логина НЕподтверждённого пользователя"""
    email = "unconfirmed_login_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, FALSE, TRUE)
    """, "unconfirmed_test", email, hashed)

    data = user_data(email=email,  password = "StrongPass123!")
    response = await post_form(client, "/bg/ru/auth/login/", data=data)

    assert response.status_code == 200

    # === НАДЁЖНЫЕ ПРОВЕРКИ (независимые от языка) ===
    html = response.text.lower()

    # 1. Проверяем title страницы (самый стабильный маркер)
    assert "регистрация успешна" in html or "registration success" in html

    # 2. Проверяем наличие флага, который мы передаём из кода
    assert "not_confirmed" in response.text


@pytest.mark.asyncio
async def test_confirm_email_invalid_token(client: AsyncClient):
    response = await client.get("/bg/ru/auth/confirm-email/invalid-token-12345/")
    assert response.status_code in (400, 200)
    assert any(x in response.text for x in ["Ссылка недействительна", "истекла", "недействительна"])

# --- logout ------------------------------------------------------
@pytest.mark.asyncio
async def test_logout_success(client: AsyncClient, db_pool, user_data):
    """Успешный logout"""
    email = "logout_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    # Создаём и логиним пользователя
    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "logout_user", email, hashed)

    # Логинимся
    data = user_data(email=email, password=password)
    login_response = await post_form(client, "/bg/ru/auth/login/", data=data)

    assert login_response.status_code == 303

    # Делаем logout
    logout_response = await client.get("/bg/ru/logout/")

    assert logout_response.status_code == 303
    assert "/bg/ru/" in logout_response.headers.get("location", "")


# --- confirm email -----------------------------------------------
async def test_confirm_email_success(client: AsyncClient, db_pool):
    """Успешное подтверждение email"""
    email = "confirm_success_test@example.com"

    # ДОБАВЛЕНО: генерируем РЕАЛЬНЫЙ токен вместо фейкового
    token = create_email_verification_token(email)

    await db_pool.execute("""
                          INSERT INTO users (username, email, password_hash, is_confirmed)
                          VALUES ($1, $2, $3, FALSE)
                          """, "confirm_test", email, "dummy_hash")

    response = await client.get(f"/bg/ru/auth/confirm-email/{token}/")

    assert response.status_code == 200
    assert any(phrase in response.text.lower() for phrase in [
        "успешно подтверждён", "success", "активирован"
    ])


# --- reset password -------------------------------------------------------
@pytest.mark.asyncio
async def test_forgot_password_page(client: AsyncClient):
    """Страница «Забыли пароль?» открывается"""
    response = await client.get("/bg/ru/auth/forgot-password/")
    assert response.status_code == 200
    assert "Забыли пароль" in response.text or "forgot" in response.text.lower()


@pytest.mark.asyncio
async def test_forgot_password_post(client: AsyncClient, db_pool, user_data):
    """POST-запрос на восстановление пароля"""
    email = "forgot_test@example.com"

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed)
        VALUES ($1, $2, $3, TRUE)
    """, "forgot_test", email, "dummy_hash")

    data = user_data(email=email)
    response = await post_form(client, "/bg/ru/auth/forgot-password/", data=data)
    assert response.status_code == 200
    assert any(phrase in response.text for phrase in [
        "Ссылка отправлена",
        "Проверьте почту",
        "инструкцию"
    ])


@pytest.mark.asyncio
async def test_reset_password_success(client: AsyncClient, db_pool):
    """Успешный сброс пароля"""
    email = "reset_success@example.com"

    # 1. Создаём реальный токен
    from app.core.security import create_password_reset_token
    token = create_password_reset_token(email)

    # 2. Создаём пользователя
    await db_pool.execute("""
                          INSERT INTO users (username, email, password_hash, is_confirmed, created_at)
                          VALUES ($1, $2, $3, TRUE, NOW())
                          """, "reset_user", email, "old_hash")

    # 3. Отправляем запрос с настоящим токеном
    response = await post_form(
        client,
        f"/bg/ru/auth/reset-password/{token}/",
        data={
            "password": "NewStrongPass123!",
            "password_confirm": "NewStrongPass123!"
        }
    )

    assert response.status_code == 200
    assert "Пароль успешно изменён" in response.text or "успешно" in response.text.lower()


@pytest.mark.asyncio
async def test_reset_password_invalid_token(client: AsyncClient):
    """Невалидный токен сброса пароля"""
    response = await post_form(
        client,
        "/bg/ru/auth/reset-password/invalid-token-abc/",
        data={
            "password": "NewPass123!",
            "password_confirm": "NewPass123!"
        }
    )
    assert response.status_code in (400, 200)
    assert any(x in response.text for x in ["Ссылка недействительна", "истекла", "недействительна"])


@pytest.mark.asyncio
async def test_reset_password_password_mismatch(client: AsyncClient):
    """Пароли не совпадают"""
    response = await post_form(
        client,
        "/bg/ru/auth/reset-password/some-token/",
        data={
            "password": "NewPass123!",
            "password_confirm": "DifferentPass123!"
        }
    )
    assert response.status_code == 400
    assert "Пароли не совпадают" in response.text

# --- Очистка не подтвердивших регистрация более 24 часов ------------
@pytest.mark.asyncio
async def test_delete_expired_unconfirmed_users(client: AsyncClient, db_pool, user_data):
    """При любой регистрации должна запускаться очистка просроченных аккаунтов"""
    # Создаём старый неподтверждённый аккаунт (более 24 часов)
    old_email = "expired_test@example.com"
    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, created_at)
        VALUES ($1, $2, $3, FALSE, NOW() - INTERVAL '25 hours')
    """, "expired_user", old_email, "dummy_hash")

    # Выполняем любую регистрацию — должна сработать очистка
    data = user_data()
    response = await post_form(client, "/bg/ru/auth/register/", data=data)

    assert response.status_code == 200

    # Проверяем, что старый пользователь был удалён
    remaining = await db_pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE email = $1", old_email
    )
    assert remaining == 0, "Просроченный неподтверждённый пользователь не был удалён"


# --- Повторная регистрация на неподтверждённый email до времени удаления неподтверждённого пользователя -----------------
@pytest.mark.asyncio
async def test_verification_email_cooldown(client: AsyncClient, db_pool, user_data):
    """Повторная регистрация на неподтверждённый email в течение часа"""
    email = "cooldown_test@example.com"
    password = "StrongPass123!"

    # Первая регистрация
    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/register/", data=data)

    # Сразу повторная попытка
    response = await post_form(client, "/bg/ru/auth/register/", data=data)

    assert response.status_code == 200
    html_lower = response.text.lower()

    # Сейчас логика разрешает повторную отправку (ограничение только по created_at)
    assert "регистрация успешна" in html_lower or "registration success" in html_lower


# --- ПОВТОРНАЯ ОТПРАВКА ПИСЬМА ----------------------------------------------------------
@pytest.mark.asyncio
async def test_resend_verification_page(client: AsyncClient):
    """Страница повторной отправки письма открывается"""
    response = await client.get("/bg/ru/auth/resend-verification/")
    assert response.status_code == 200
    assert "Повторная отправка" in response.text or "resend" in response.text.lower()


@pytest.mark.asyncio
async def test_resend_verification_post_success(client: AsyncClient, db_pool):
    """Успешная повторная отправка письма подтверждения"""
    email = "resend_test@example.com"

    # Создаём пользователя с "возрастом" больше 1 часа, чтобы пройти ограничение
    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, created_at)
        VALUES ($1, $2, $3, FALSE, NOW() - INTERVAL '2 hours')
    """, "resend_user", email, "dummy_hash")

    response = await post_form(
        client,
        "/bg/ru/auth/resend-verification/",
        data={"email": email}
    )

    assert response.status_code == 200
    assert any(phrase in response.text for phrase in [
        "Письмо отправлено",
        "отправлено повторно",
        "проверьте почту"
    ])

# --- Специальная обработка ввода username вместо email ----------------
@pytest.mark.asyncio
async def test_login_with_username_instead_of_email(client: AsyncClient, db_pool, user_data):
    """Пользователь по ошибке ввёл username вместо email"""
    username = "testuser123"
    email = "testuser123@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    # Создаём пользователя
    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, username, email, hashed)

    # Пытаемся войти, вводя username вместо email
    data = user_data(email=username, password=hashed)  # ← username вместо email
    response = await post_form(client, "/bg/ru/auth/login/", data=data)

    assert response.status_code == 401
    html = response.text.lower()

    # Проверяем, что система дала полезную подсказку
    assert response.status_code == 401
    assert "неверный email или пароль" in response.text.lower()


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: AsyncClient, user_data):
    """Попытка входа с несуществующим email"""
    data = user_data()
    response = await post_form(client, "/bg/ru/auth/login/", data=data)

    assert response.status_code == 401
    html = response.text.lower()

    # Должна быть общая ошибка (без раскрытия, существует ли такой email)
    assert "неверный email или пароль" in html or "invalid" in html


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, db_pool, user_data):
    """Правильный email, но неверный пароль"""
    email = "wrongpass_test@example.com"
    password = "CorrectPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "wrongpass_user", email, hashed)

    data = user_data(email=email, password="WrongPassword999!")
    response = await post_form(client, "/bg/ru/auth/login/", data=data)

    assert response.status_code == 401
    assert "неверный email или пароль" in response.text.lower()


# --- Одна проверка сессии на HTTP-запрос -----------------------------------
@pytest.mark.asyncio
async def test_session_verified_once_on_current_user_endpoint(auth_client):
    """Middleware + CurrentUser не должны дважды декодировать JWT и ходить в сессии."""
    from app.db import dependencies as deps

    client, _user_id = auth_client
    real_verify = deps.verify_token
    calls = {"n": 0}

    async def wrapped(*args, **kwargs):
        calls["n"] += 1
        return await real_verify(*args, **kwargs)

    with patch("app.db.dependencies.verify_token", side_effect=wrapped):
        response = await client.get("/api/user/access-level")

    assert response.status_code == 200
    assert "accessLevel" in response.json()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_session_verified_once_on_required_user_page(auth_client):
    """HTML-зависимость RequiredUser тоже берёт пользователя из request.state."""
    from app.db import dependencies as deps

    client, _user_id = auth_client
    real_verify = deps.verify_token
    calls = {"n": 0}

    async def wrapped(*args, **kwargs):
        calls["n"] += 1
        return await real_verify(*args, **kwargs)

    with patch("app.db.dependencies.verify_token", side_effect=wrapped):
        response = await client.get("/bg/ru/coach/")

    # У обычного ученика страница коуча закрыта, но сессия уже проверена.
    assert response.status_code in (403, 303)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_anonymous_current_user_does_not_hit_session_store(client):
    """Без cookie нечего проверять в БД — ни middleware, ни зависимость."""
    from app.db import dependencies as deps

    real_verify = deps.verify_token
    calls = {"n": 0}

    async def wrapped(*args, **kwargs):
        calls["n"] += 1
        return await real_verify(*args, **kwargs)

    with patch("app.db.dependencies.verify_token", side_effect=wrapped):
        response = await client.get("/api/user/access-level")

    assert response.status_code == 401
    assert calls["n"] == 0

# --- CurrentUser.daily_activity ---------------------------------------------
@pytest.mark.asyncio
async def test_current_user_includes_empty_daily_activity(db_pool, access_user):
    """Новый пользователь: словарь есть, номер Q не выдаётся и в базу не пишется."""
    import uuid
    from app.core.security import create_access_token, get_current_user

    user_id = await access_user()
    jti = str(uuid.uuid4())
    await db_pool.execute(
        """
        INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
        VALUES ($1, $2, '127.0.0.1', 'pytest-client', NOW())
        """,
        user_id,
        jti,
    )
    token = create_access_token(data={"sub": str(user_id)}, jti=jti)

    user = await get_current_user(token, db_pool)

    assert user["user_id"] == user_id
    assert user["daily_activity"] == {}
    stored = await db_pool.fetchval(
        "SELECT daily_activity FROM users WHERE id = $1",
        user_id,
    )
    assert stored in ({}, "{}", None)


@pytest.mark.asyncio
async def test_current_user_includes_stored_daily_activity(db_pool, access_user):
    """Уже записанная ячейка попадает в CurrentUser как есть, без выдачи нового Q."""
    import json
    import uuid
    from app.core.security import create_access_token, get_current_user

    user_id = await access_user()
    payload = {
        "streak": 4,
        "last_on": "2026-10-02",
        "exercise_current": "BGRUA1015_Q003",
    }
    await db_pool.execute(
        "UPDATE users SET daily_activity = $1::jsonb WHERE id = $2",
        json.dumps(payload),
        user_id,
    )
    jti = str(uuid.uuid4())
    await db_pool.execute(
        """
        INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
        VALUES ($1, $2, '127.0.0.1', 'pytest-client', NOW())
        """,
        user_id,
        jti,
    )
    token = create_access_token(data={"sub": str(user_id)}, jti=jti)

    user = await get_current_user(token, db_pool)

    assert user["daily_activity"] == payload
    stored = await db_pool.fetchval(
        "SELECT daily_activity FROM users WHERE id = $1",
        user_id,
    )
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == payload
