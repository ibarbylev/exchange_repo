import pytest
from httpx import AsyncClient

from .conftest import post_form

# ======================================================
# Публичные страницы (не требуют авторизации)
# ======================================================
PUBLIC_CSRF_PAGES = [
    "/bg/ru/auth/login/",  # Здесь находятся обе формы (login + register)
    "/bg/ru/auth/forgot-password/",
    "/bg/ru/auth/resend-verification/",
]

# ======================================================
# Авторизованные страницы (требуют логина)
# ======================================================
AUTH_CSRF_PAGES = [
    "/bg/ru/cart/",
    "/bg/ru/cart/checkout/",
]


# ------------------- Публичные формы -------------------

CSRF_FORM_PAGES = [
    "/bg/ru/auth/login/",
    "/bg/ru/auth/forgot-password/",
    "/bg/ru/auth/resend-verification/",
]


@pytest.mark.parametrize("url", CSRF_FORM_PAGES)
@pytest.mark.asyncio
async def test_csrf_token_present_in_form(client: AsyncClient, url: str):
    """Проверяем наличие скрытого поля CSRF-токена на страницах с формами"""
    response = await client.get(url)

    # Для авторизованных страниц может прийти 303, если корзина пустая.
    # Пока просто проверяем, что это не 404/500.
    assert response.status_code in (200, 303), f"Страница {url} вернула {response.status_code}"

    if response.status_code == 200:
        assert '<input type="hidden" name="csrf_token"' in response.text, \
            f"На странице {url} отсутствует поле CSRF-токена"


# --------- Формы, требующие авторизации -----------

@pytest.mark.asyncio
async def test_csrf_token_in_cart_page(auth_client):
    """Проверяем наличие CSRF-токена на странице корзины"""
    client, user_id = auth_client

    response = await client.get("/bg/ru/cart/")

    assert response.status_code == 200
    assert '<input type="hidden" name="csrf_token"' in response.text


@pytest.mark.asyncio
async def test_csrf_token_in_checkout_page(auth_client, db_pool):
    """Проверяем наличие CSRF-токена на странице checkout"""
    client, user_id = auth_client

    # Создаём корзину
    order_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 100.00) RETURNING id
    """, user_id)

    # Добавляем хотя бы один товар (обязательно!)
    await db_pool.execute("""
        INSERT INTO order_items (order_id, product_id, quantity, price)
        VALUES (
            $1, 
            (SELECT id FROM products LIMIT 1), 
            1, 
            100.00
        )
    """, order_id)

    response = await client.get("/bg/ru/cart/checkout/")

    assert response.status_code == 200
    assert '<input type="hidden" name="csrf_token"' in response.text


@pytest.mark.asyncio
async def test_checkout_redirects_without_cart(auth_client):
    """Проверка, что без корзины пользователь редиректится с /checkout/"""
    client, user_id = auth_client

    response = await client.get("/bg/ru/cart/checkout/")

    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")


import pytest
from httpx import AsyncClient

# ============================================================
# AUTH
# ============================================================

@pytest.mark.asyncio
async def test_csrf_protection_login(client: AsyncClient, db_pool, user_data):
    """Проверка CSRF на /auth/login/"""
    # Создаём пользователя
    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ('csrf_test', 'csrf@example.com', 'dummy', TRUE, TRUE)
    """)

    data = {"email": "csrf@example.com", "password": "wrong"}

    # Без токена
    response = await client.post("/bg/ru/auth/login/", data=data)
    assert response.status_code == 403

    # С токеном
    response = await post_form(client, "/bg/ru/auth/login/", data=data)
    assert response.status_code in (200, 401, 303)  # 401 — неверный пароль


@pytest.mark.asyncio
async def test_csrf_protection_register(client: AsyncClient):
    """Проверка CSRF на регистрацию"""
    data = {
        "email": "newuser@example.com",
        "username": "newuser",
        "password": "StrongPass123!"
    }

    response = await client.post("/bg/ru/auth/register/", data=data)
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/auth/register/", data=data)
    assert response.status_code in (200, 303, 422)


@pytest.mark.asyncio
async def test_csrf_protection_forgot_password(client: AsyncClient):
    """Проверка CSRF на восстановление пароля"""
    data = {"email": "test@example.com"}

    response = await client.post("/bg/ru/auth/forgot-password/", data=data)
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/auth/forgot-password/", data=data)
    assert response.status_code == 200


# ============================================================
# SHOP (Cart)
# ============================================================

@pytest.mark.asyncio
async def test_csrf_protection_cart_add(auth_client, db_pool):
    """Проверка CSRF при добавлении в корзину"""
    client, user_id = auth_client

    data = {"product_slug": "access-standard-monthly"}

    response = await client.post("/bg/ru/cart/add/", data=data)
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/cart/add/", data=data)
    assert response.status_code in (200, 303)


@pytest.mark.asyncio
async def test_csrf_protection_cart_remove(auth_client, db_pool):
    """Проверка CSRF при удалении из корзины"""
    client, user_id = auth_client

    # Создаём тестовый item (упрощённо)
    data = {"item_id": 999}  # в реальном тесте нужно создать реальный item

    response = await client.post("/bg/ru/cart/remove/", data=data)
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/cart/remove/", data=data)
    assert response.status_code in (200, 303, 404)


@pytest.mark.asyncio
async def test_csrf_protection_select_payment(auth_client, db_pool):
    """Проверка CSRF при выборе способа оплаты"""
    client, user_id = auth_client

    # Создаём корзину
    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 100)
    """, user_id)

    data = {"payment_method": "balance"}

    response = await client.post("/bg/ru/cart/select_payment/", data=data)
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/cart/select_payment/", data=data)
    assert response.status_code in (200, 303)


@pytest.mark.asyncio
async def test_csrf_protection_cancel_pending(auth_client, db_pool):
    """Проверка CSRF при отмене pending-заказа"""
    client, user_id = auth_client

    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'pending', 50)
    """, user_id)

    response = await client.post("/bg/ru/cart/cancel-pending/")
    assert response.status_code == 403

    response = await post_form(client, "/bg/ru/cart/cancel-pending/")
    assert response.status_code == 303


# ============================================================
# ADMIN
# ============================================================

@pytest.mark.asyncio
async def test_csrf_protection_admin_access_update(admin_client):
    """Проверка CSRF в админке"""
    data = {"user_id": 1, "access_level": 1}

    # Без токена → должен быть 403
    response = await admin_client.post("/admin/access/update/", data=data)
    assert response.status_code == 403

    # С токеном → не должно быть CSRF-ошибки (403)
    response = await post_form(admin_client, "/admin/access/update/", data=data)
    assert response.status_code != 403, "CSRF protection should not block valid request"



# ============================================================
# CLASSROOM JSON endpoints (header X-CSRF-Token)
# ============================================================

def _ensure_csrf(client):
    token = client.cookies.get("csrf_token")
    if not token:
        token = "test-csrf-token-value"
        client.cookies.set("csrf_token", token)
    return {"X-CSRF-Token": token}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,payload",
    [
        ("/api/user/current-exercise", {"exercise_name": "BGRUA1001_V001"}),
        ("/api/user/exercise-stars", {"exercise_name": "BGRUA1001_V001", "stars": 2}),
        ("/api/user/intensive-progress", {
            "exercise_name": "word_01_synonym_01",
            "series": "X",
            "block_name": "some_block",
            "lang_prefix": "BGRU",
        }),
    ],
)
async def test_classroom_json_csrf_required(auth_client, url, payload):
    """Без заголовка — 403, с X-CSRF-Token — не 403."""
    client, _ = auth_client

    # без токена
    response = await client.post(url, json=payload)
    assert response.status_code == 403, f"{url} must reject missing CSRF"

    # с заголовком (кука тоже ставится)
    headers = _ensure_csrf(client)
    response = await client.post(url, json=payload, headers=headers)
    assert response.status_code != 403, f"{url} must accept valid CSRF header"
