import pytest
from httpx import AsyncClient
from datetime import datetime, timedelta, timezone

from app.core.security import get_password_hash
from .conftest import post_form


# --- ТЕСТЫ КОРЗИНЫ --------------------------------------

@pytest.mark.asyncio
async def test_add_to_cart_success(client: AsyncClient, db_pool, user_data):
    """Успешное добавление товара в корзину авторизованным пользователем"""
    email = "cart_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "cart_user", email, hashed)

    # Логинимся
    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/login/", data=data)

    response = await post_form(
        client,
        "/bg/ru/cart/add/",
        data={"product_slug": "access-standard-monthly"},
        follow_redirects=False
    )

    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_add_same_product_creates_separate_items(auth_client, db_pool, user_data):
    """Повторное добавление того же товара создаёт отдельную позицию (по текущей бизнес-логике)"""
    client, user_id = auth_client
    data = {"product_slug": "access-standard-monthly"}
    # Добавляем один и тот же товар два раза
    await post_form(client, "/bg/ru/cart/add/", data=data)
    await post_form(client, "/bg/ru/cart/add/", data=data)

    # Проверяем, что создалось ДВЕ отдельные записи в order_items
    item_count = await db_pool.fetchval("""
        SELECT COUNT(*) 
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = $1
          AND o.status = 'cart'
    """, user_id)

    assert item_count == 2, f"Ожидалось 2 позиции в корзине, а получено {item_count}"


@pytest.mark.asyncio
async def test_remove_from_cart_success(client: AsyncClient, db_pool, user_data):
    """Успешное удаление товара из корзины"""
    email = "cart_test3@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "cart_user3", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/login/", data=data)

    # Добавляем товар
    await post_form(client,"/bg/ru/cart/add/", data={"product_slug": "access-standard-monthly"})

    # Получаем item_id
    item_id = await db_pool.fetchval("""
        SELECT oi.id 
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = (SELECT id FROM users WHERE email = $1)
    """, email)

    # Удаляем
    response = await post_form(
        client,
        "/bg/ru/cart/remove/",
        data={"item_id": item_id},
        follow_redirects=False
    )

    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")

    # Проверяем, что запись удалена
    remaining = await db_pool.fetchval("SELECT COUNT(*) FROM order_items WHERE id = $1", item_id)
    assert remaining == 0


@pytest.mark.asyncio
async def test_view_empty_cart(client: AsyncClient, db_pool, user_data):
    """Просмотр пустой корзины"""
    email = "empty_cart@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "empty_user", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/login/", data=data)

    response = await client.get("/bg/ru/cart/")

    assert response.status_code == 200
    assert "Ваша корзина пуста" in response.text


@pytest.mark.asyncio
async def test_view_cart_with_items(client: AsyncClient, db_pool, user_data):
    """Просмотр корзины с товарами"""
    email = "cart_with_items@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "full_user", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client,"/bg/ru/auth/login/", data=data)
    await post_form(client,"/bg/ru/cart/add/", data={"product_slug": "access-standard-monthly"})

    response = await client.get("/bg/ru/cart/")

    assert response.status_code == 200
    assert "access-standard-monthly" in response.text.lower() or "Стандарт" in response.text


@pytest.mark.asyncio
async def test_add_nonexistent_product(client: AsyncClient, db_pool, user_data):
    """Попытка добавить несуществующий продукт должна обработаться gracefully"""
    email = "nonexistent_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "nonexistent_user", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/login/", data=data)

    response = await post_form(client, "/bg/ru/cart/add/",
        data={"product_slug": "this-product-does-not-exist-999"},
        follow_redirects=False
    )

    # Сейчас у нас редирект на корзину даже при ошибке. Главное — не должно быть 500
    assert response.status_code == 303
    # Можно дополнительно проверить, что в корзине ничего не появилось
    cart_count = await db_pool.fetchval("""
        SELECT COUNT(*) FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = (SELECT id FROM users WHERE email = $1)
    """, email)
    assert cart_count == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_item_is_safe(client: AsyncClient, db_pool, user_data):
    """Удаление несуществующей позиции не должно ломать приложение"""
    email = "safe_remove_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "safe_user", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client,"/bg/ru/auth/login/", data=data)

    response = await post_form(
        client,
        "/bg/ru/cart/remove/",
        data={"item_id": 999999},   # несуществующий id
        follow_redirects=False
    )
    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_add_multiple_different_products(auth_client, db_pool):
    """Добавление нескольких разных товаров в корзину"""
    client, user_id = auth_client

    # Добавляем два разных товара
    response1 = await post_form(
        client,
        "/bg/ru/cart/add/",
        data={"product_slug": "access-standard-monthly"}
    )
    response2 = await post_form(
        client,
        "/bg/ru/cart/add/",
        data={"product_slug": "access-premium-monthly"}
    )

    # Опционально: проверяем, что запросы прошли успешно
    assert response1.status_code in (200, 303)
    assert response2.status_code in (200, 303)

    # Проверяем, что в корзине 2 товара
    items_count = await db_pool.fetchval("""
        SELECT COUNT(*)
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = $1
          AND o.status = 'cart'
    """, user_id)

    assert items_count == 2


@pytest.mark.asyncio
async def test_remove_last_item_cart_becomes_empty(client: AsyncClient, db_pool, user_data):
    """После удаления последнего товара корзина должна стать пустой"""
    email = "last_item_test@example.com"
    password = "StrongPass123!"
    hashed = get_password_hash(password)

    await db_pool.execute("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ($1, $2, $3, TRUE, TRUE)
    """, "last_item_user", email, hashed)

    data = user_data(email=email, password=password)
    await post_form(client, "/bg/ru/auth/login/", data=data)

    # Добавляем товар
    await client.post("/bg/ru/cart/add/", data={"product_slug": "access-standard-monthly"})

    # Получаем item_id
    item_id = await db_pool.fetchval("""
        SELECT oi.id FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        WHERE o.user_id = (SELECT id FROM users WHERE email = $1)
    """, email)

    # Удаляем последний товар
    await client.post("/bg/ru/cart/remove/", data={"item_id": item_id}, follow_redirects=False)

    # Проверяем, что корзина пустая
    response = await client.get("/bg/ru/cart/")
    assert response.status_code == 200
    assert "Ваша корзина пуста" in response.text


@pytest.mark.asyncio
async def test_remove_from_cart_without_auth(client: AsyncClient):
    """Попытка удалить товар без авторизации → редирект на логин"""
    response = await post_form(client, "/bg/ru/cart/remove/",
        data={"item_id": 1},
        follow_redirects=False
    )

    assert response.status_code == 303
    assert "/auth/login/" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_auto_pay_single_pending_order(db_pool, create_user, create_pending_order):
    """Проверяем, что при наличии одного pending-заказа он автоматически оплачивается"""
    user_id = await create_user()

    # Создаём один pending-заказ
    order_id = await create_pending_order(user_id, amount=50)

    # Пополняем баланс
    await db_pool.execute("""
        INSERT INTO payments (user_id, amount) 
        VALUES ($1, 100)
    """, user_id)

    # Проверяем, что заказ оплатился
    status = await db_pool.fetchval("SELECT status FROM orders WHERE id = $1", order_id)
    assert status == "paid"

    final_balance = await db_pool.fetchval("SELECT get_user_balance($1)", user_id)
    assert final_balance == 50


@pytest.mark.asyncio
async def test_auto_pay_zero_balance_does_not_pay(db_pool, create_user):
    """При нулевом балансе автоплатёж не должен оплачивать заказы"""
    user_id = await create_user()

    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'pending', 50)
    """, user_id)

    # Не пополняем баланс
    paid_count = await db_pool.fetchval("""
        SELECT COUNT(*) FROM orders WHERE user_id = $1 AND status = 'paid'
    """, user_id)

    assert paid_count == 0


@pytest.mark.asyncio
async def test_cart_to_pending_with_zero_balance(db_pool, create_user):
    """
    Проверка главного требования:
    Перевод корзины в pending не должен падать при нулевом балансе
    """
    user_id = await create_user()

    # Создаём корзину
    order_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 80)
        RETURNING id
    """, user_id)

    # Переводим в pending (должно пройти без ошибки)
    await db_pool.execute("""
        UPDATE orders SET status = 'pending' WHERE id = $1
    """, order_id)

    status = await db_pool.fetchval(
        "SELECT status FROM orders WHERE id = $1", order_id
    )
    assert status == "pending"


@pytest.mark.asyncio
async def test_clean_old_carts(db_pool, create_user):
    """Проверяем, что функция clean_old_carts удаляет старые корзины"""
    user_id = await create_user()

    # 1. Создаём "старую" корзину (более 14 дней назад)
    old_cart_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount, created_at)
        VALUES ($1, 'cart', 0, NOW() - INTERVAL '20 days')
        RETURNING id
    """, user_id)

    # 2. Вызываем очистку старых корзин
    deleted_count = await db_pool.fetchval(
        "SELECT clean_old_carts($1)",
        user_id
    )

    assert deleted_count == 1

    # 3. Проверяем, что старая корзина удалилась
    old_cart_exists = await db_pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM orders WHERE id = $1)",
        old_cart_id
    )
    assert old_cart_exists is False

    # 4. Теперь можно создать новую корзину (старая уже удалена)
    new_cart_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 0)
        RETURNING id
    """, user_id)

    assert new_cart_id is not None

    # Опционально: проверяем, что новая корзина действительно существует
    new_cart_exists = await db_pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM orders WHERE id = $1)",
        new_cart_id
    )
    assert new_cart_exists is True


@pytest.mark.asyncio
async def test_auto_pay_partial_payment_does_nothing(db_pool, create_user, create_pending_order):
    """
    Проверяем, что при частичной оплате заказа (40 из 50) ничего не происходит.
    Только после полной суммы (40 + 10) заказ оплачивается.
    """
    user_id = await create_user()

    # Создаём pending-заказ на 50
    order_id = await create_pending_order(user_id, amount=50)

    # === Шаг 1: Добавляем 40 ===
    await db_pool.execute("""
        INSERT INTO payments (user_id, amount)
        VALUES ($1, 40)
    """, user_id)

    # Проверяем, что заказ остался pending
    status_after_40 = await db_pool.fetchval(
        "SELECT status FROM orders WHERE id = $1", order_id
    )
    assert status_after_40 == "pending", "Заказ не должен был оплатиться при 40"

    # Проверяем баланс (должен быть 40)
    balance_after_40 = await db_pool.fetchval(
        "SELECT get_user_balance($1)", user_id
    )
    assert balance_after_40 == 40

    # === Шаг 2: Добавляем оставшиеся 10 ===
    await db_pool.execute("""
        INSERT INTO payments (user_id, amount)
        VALUES ($1, 10)
    """, user_id)

    # Теперь заказ должен стать paid
    status_after_50 = await db_pool.fetchval(
        "SELECT status FROM orders WHERE id = $1", order_id
    )
    assert status_after_50 == "paid", "Заказ должен был оплатиться после полной суммы"

    # Баланс должен стать 0 (40 + 10 - 50)
    final_balance = await db_pool.fetchval(
        "SELECT get_user_balance($1)", user_id
    )
    assert final_balance == 0


@pytest.mark.asyncio
async def test_access_history_and_trigger_flow(db_pool):
    """
    Полный цикл проверки добавления записей в историю:
    1. Добавляем запись в историю
    2. Проверяем, что триггер обновил users
    3. Добавляем вторую запись (продление)
    4. Проверяем, что old_* = new_* из предыдущей записи
    5. Вызываем сброс просроченного доступа
    6. Проверяем, что уровень сбросился и появилась запись в истории
    """

    # --- Подготовка: создаём пользователя ---
    user_id = await db_pool.fetchval("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active)
        VALUES ('history_test', 'history_test@example.com', 'dummy_hash', TRUE, TRUE)
        RETURNING id
    """)

    # Проверяем начальное состояние
    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 0
    assert user["access_until"] is None

    # ============================================
    # 1. Добавляем первую запись в историю (покупка Standard на 30 дней)
    # ============================================
    new_until_1 = datetime.now(timezone.utc) + timedelta(days=30)

    await db_pool.execute("""
        INSERT INTO user_access_history 
            (user_id, old_access_level, old_access_until, new_access_level, new_access_until, reason)
        VALUES ($1, 0, NULL, 1, $2, 'purchase')
    """, user_id, new_until_1)

    # ============================================
    # 2. Проверяем работу триггера — users должен обновиться
    # ============================================
    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 1
    assert user["access_until"] is not None
    # Дата примерно совпадает (с небольшой погрешностью)
    assert abs((user["access_until"] - new_until_1).total_seconds()) < 5

    # ============================================
    # 3. Добавляем вторую запись (продление ещё на 30 дней)
    # ============================================
    new_until_2 = new_until_1 + timedelta(days=30)

    await db_pool.execute("""
        INSERT INTO user_access_history 
            (user_id, old_access_level, old_access_until, new_access_level, new_access_until, reason)
        VALUES ($1, 1, $2, 1, $3, 'purchase')
    """, user_id, new_until_1, new_until_2)

    # ============================================
    # 4. Проверяем, что old_* второй записи = new_* первой записи
    # ============================================
    history = await db_pool.fetch("""
        SELECT old_access_level, old_access_until, new_access_level, new_access_until
        FROM user_access_history 
        WHERE user_id = $1 
        ORDER BY created_at
    """, user_id)

    assert len(history) == 2

    first_record = history[0]
    second_record = history[1]

    # old_* второй записи должны совпадать с new_* первой записи
    assert second_record["old_access_level"] == first_record["new_access_level"]
    assert abs((second_record["old_access_until"] - first_record["new_access_until"]).total_seconds()) < 5

    # Проверяем, что users обновился на вторую дату
    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 1
    assert abs((user["access_until"] - new_until_2).total_seconds()) < 5

    # ============================================
    # 5. Создаём пользователя с просроченным доступом и вызываем сброс
    # ============================================
    expired_user_id = await db_pool.fetchval("""
        INSERT INTO users (username, email, password_hash, is_confirmed, is_active,
                           access_level, access_until)
        VALUES ('expired_user', 'expired@example.com', 'hash', TRUE, TRUE, 2, NOW() - INTERVAL '10 days')
        RETURNING id
    """)

    # Вызываем функцию сброса просроченного доступа
    await db_pool.execute("SELECT reset_expired_access();")

    # ============================================
    # 6. Проверяем, что уровень сбросился и появилась запись в истории
    # ============================================
    expired_user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", expired_user_id
    )
    assert expired_user["access_level"] == 0

    # Проверяем, что в истории появилась запись с reason = 'expired'
    expired_history = await db_pool.fetchval("""
        SELECT COUNT(*) FROM user_access_history 
        WHERE user_id = $1 AND reason = 'expired'
    """, expired_user_id)

    assert expired_history == 1


# ============================================
#  7. Тесты для SQL-функции apply_purchase_access
# ============================================

@pytest.mark.asyncio
async def test_apply_purchase_access_new_user(db_pool, access_user):
    """Покупка первого доступа пользователем без подписки"""
    user_id = await access_user()     # новый пользователь без доступа

    # Вызываем функцию
    await db_pool.execute("""
        SELECT apply_purchase_access($1, 999, 'access-standard-monthly')
    """, user_id)

    # Проверяем users
    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 1
    assert user["access_until"] > datetime.now(timezone.utc) + timedelta(days=29)

    # Проверяем историю
    history = await db_pool.fetchrow("""
        SELECT old_access_level, new_access_level, reason 
        FROM user_access_history 
        WHERE user_id = $1
    """, user_id)

    assert history["old_access_level"] == 0
    assert history["new_access_level"] == 1
    assert history["reason"] == "purchase"


@pytest.mark.asyncio
async def test_apply_purchase_access_renewal(db_pool, access_user):
    """Продление активной подписки"""
    user_id = await access_user(access_level=1, days=10)   # уже есть доступ на 10 дней

    await db_pool.execute("""
        SELECT apply_purchase_access($1, 1000, 'access-standard-monthly')
    """, user_id)

    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    # Дата должна быть продлена (примерно +30 дней от предыдущей даты)
    assert user["access_level"] == 1
    assert user["access_until"] > datetime.now(timezone.utc) + timedelta(days=38)


@pytest.mark.asyncio
async def test_apply_purchase_access_block_downgrade(db_pool, access_user):
    """Попытка понизить уровень при активной подписке должна падать"""
    user_id = await access_user(access_level=2, days=20)   # Premium на 20 дней

    with pytest.raises(Exception) as exc:
        await db_pool.execute("""
            SELECT apply_purchase_access($1, 1001, 'access-standard-monthly')
        """, user_id)

    assert "Нельзя изменить действующий тип доступа" in str(exc.value)


@pytest.mark.asyncio
async def test_apply_purchase_access_after_expired(db_pool, access_user):
    """Покупка после истечения предыдущей подписки"""
    user_id = await access_user(access_level=1, days=-10)   # уже есть доступ на 10 дней

    await db_pool.execute("""
        SELECT apply_purchase_access($1, 1002, 'access-premium-monthly')
    """, user_id)

    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 2  # должен стать Premium


@pytest.mark.asyncio
async def test_apply_purchase_access_invalid_slug(db_pool, access_user):
    """Некорректный slug должен вызывать исключение"""
    user_id = await access_user()   # новый пользователь без доступа

    with pytest.raises(Exception) as exc:
        await db_pool.execute("""
            SELECT apply_purchase_access($1, 1003, 'some-random-product')
        """, user_id)

    assert "Не удалось определить срок подписки из slug" in str(exc.value)

# ============================================
#  8. Тест изменения доступа после оплаты заказа
# ============================================

@pytest.mark.asyncio
async def test_auto_pay_grants_access(db_pool, access_user):
    """После автооплаты access-продукта должен обновиться доступ и появиться запись в истории"""

    # Создаём пользователя
    user_id = await access_user()

    # Создаём pending заказ с access-продуктом
    order_id = await db_pool.fetchval("""
                                      INSERT INTO orders (user_id, status, amount)
                                      VALUES ($1, 'pending', 20.00) RETURNING id
                                      """, user_id)

    # Добавляем товар в заказ
    await db_pool.execute("""
                          INSERT INTO order_items (order_id, product_id, quantity, price)
                          VALUES ($1,
                                  (SELECT id FROM products WHERE slug = 'access-standard-monthly'),
                                  1,
                                  20.00)
                          """, order_id)

    # Добавляем платёж (должен сработать триггер автооплаты)
    await db_pool.execute("""
                          INSERT INTO payments (user_id, amount, comment)
                          VALUES ($1, 20.00, 'Тестовый платёж')
                          """, user_id)

    # Проверяем, что доступ выдали
    user = await db_pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    assert user["access_level"] == 1
    assert user["access_until"] is not None

    # Проверяем запись в истории
    history = await db_pool.fetchrow("""
                                     SELECT reason, note
                                     FROM user_access_history
                                     WHERE user_id = $1
                                     """, user_id)

    assert history["reason"] == "purchase"
    assert str(order_id) in history["note"]

# ============================================
#  9. Тесты на отображение страницы Checkout
# ============================================
@pytest.mark.asyncio
async def test_checkout_view_requires_auth(client):
    """Неавторизованный пользователь должен быть перенаправлен на логин"""
    response = await client.get("/bg/ru/cart/checkout/")
    assert response.status_code == 303
    assert "/auth/login/" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_checkout_view_shows_order(auth_client, db_pool):
    client, user_id = auth_client

    order_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 100.00) RETURNING id
    """, user_id)

    await db_pool.execute("""
        INSERT INTO order_items (order_id, product_id, quantity, price)
        VALUES ($1, (SELECT id FROM products LIMIT 1), 1, 100.00)
    """, order_id)

    response = await client.get("/bg/ru/cart/checkout/")
    assert response.status_code == 200


# ======================================================
#  10. Тесты на оформление заказа (POST /cart/checkout/)
# ======================================================
@pytest.mark.asyncio
async def test_checkout_post_changes_status_to_pending(auth_client, access_user, db_pool):
    """При оформлении заказа статус меняется с cart на pending"""
    client, user_id = auth_client

    order_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 50.00) RETURNING id
    """, user_id)

    response = await post_form(client,"/bg/ru/cart/select_payment/")

    assert response.status_code == 303
    assert "/cart/select_payment/" in response.headers.get("location", "")

    # Проверяем статус в БД
    status = await db_pool.fetchval(
        "SELECT status FROM orders WHERE id = $1", order_id
    )
    assert status == "pending"


@pytest.mark.asyncio
async def test_checkout_post_redirects_to_payment(auth_client, access_user, db_pool):
    """После оформления происходит редирект на страницу оплаты"""
    client, user_id = auth_client

    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'cart', 30.00)
    """, user_id)

    response = await post_form(client,"/bg/ru/cart/select_payment/")

    assert response.status_code == 303
    assert "/cart/select_payment/" in response.headers.get("location", "")

# ======================================================
#  11. Тесты на Middleware (PendingOrderMiddleware)
# ======================================================
@pytest.mark.asyncio
async def test_pending_order_middleware_returns_order(auth_client, db_pool):
    client, user_id = auth_client
    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'pending', 200.00)
    """, user_id)

    response = await client.get("/bg/ru/test/pending-order")
    assert response.status_code == 200
    assert response.json()["has_pending_order"] is True


@pytest.mark.asyncio
async def test_pending_order_middleware_returns_none_if_no_order(auth_client):
    client, user_id = auth_client
    response = await client.get("/bg/ru/test/pending-order")
    assert response.json()["has_pending_order"] is False


@pytest.mark.asyncio
async def test_pending_order_middleware_only_for_authenticated(client, access_user, db_pool):
    user_id = await access_user()
    await db_pool.execute("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'pending', 99.00)
    """, user_id)

    response = await client.get("/bg/ru/test/pending-order")
    assert response.status_code in (401, 403)


# ======================================================
#  12. Тесты на удаление неоплаченного заказа
# ======================================================
@pytest.mark.asyncio
async def test_cancel_pending_order(auth_client, db_pool):   # ← было admin_client
    """Удаление pending-заказа работает корректно"""
    client, user_id = auth_client

    order_id = await db_pool.fetchval("""
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'pending', 80.00) RETURNING id
    """, user_id)

    response = await post_form(client,"/bg/ru/cart/cancel-pending/")

    assert response.status_code == 303

    exists = await db_pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM orders WHERE id = $1)", order_id
    )
    assert exists is False


# ======================================================
#  13. Тесты на попытку работы с несуществующей корзиной
# ======================================================

@pytest.mark.asyncio
async def test_checkout_view_redirects_if_no_cart(auth_client, access_user):  # ← было admin_client
    """Если у пользователя нет корзины — редирект в корзину"""
    client, user_id = auth_client   # auth_client уже создаёт пользователя

    response = await client.get("/bg/ru/cart/checkout/")
    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_checkout_post_without_cart(auth_client):   # ← auth_client!
    """Попытка оформить несуществующую корзину (через правильный эндпоинт)"""
    client, user_id = auth_client

    # Пытаемся оформить заказ, когда корзины нет
    response = await post_form(client, "/bg/ru/cart/select_payment/")

    assert response.status_code == 303
    assert "/cart/" in response.headers.get("location", "")

