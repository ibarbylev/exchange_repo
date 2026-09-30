import asyncpg
import pytest
import uuid
from datetime import datetime, timezone, timedelta
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock
from jose import jwt
from app.core.config import settings, SCHEMA_PATH, CURRICULUM_PATH, FUNCTIONS_PATH
from app.main import app
from app.core.security import create_access_token


# ====================== ТЕСТОВАЯ БД ======================
TEST_DB_NAME = f"{settings.COURSES_POSTGRES_DB}_test"


async def create_test_database():
    """Создаёт тестовую БД и применяет schema.sql"""
    # Подключаемся к postgres как суперпользователь
    conn = await asyncpg.connect(
        user=settings.COURSES_POSTGRES_USER,
        password=settings.COURSES_POSTGRES_PASSWORD,
        host=settings.COURSES_POSTGRES_HOST,
        port=settings.COURSES_POSTGRES_PORT,
        database="postgres",  # системная БД
    )
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME};")
        await conn.execute(f"CREATE DATABASE {TEST_DB_NAME};")
        print(f"✅ Тестовая БД '{TEST_DB_NAME}' создана")
    finally:
        await conn.close()


async def apply_schema():
    """Применяет schema.sql один раз на сессию"""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Файл схемы не найден: {SCHEMA_PATH}")

    conn = await asyncpg.connect(
        user=settings.COURSES_POSTGRES_USER,
        password=settings.COURSES_POSTGRES_PASSWORD,
        database=TEST_DB_NAME,
        host=settings.COURSES_POSTGRES_HOST,
        port=settings.COURSES_POSTGRES_PORT,
    )
    try:
        # 1. schema.sql (таблицы + триггеры)
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            await conn.execute(f.read())
        print("✅ Часть схемы `schema.sql` успешно применена")

        # 2. schema_curriculum.sql (таблицы + триггеры)
        with open(CURRICULUM_PATH, "r", encoding="utf-8") as f:
            await conn.execute(f.read())
        print("✅ Часть схемы `schema_curriculum.sql` успешно применена")

        # 3. functions.sql (функции)
        with open(FUNCTIONS_PATH, "r", encoding="utf-8") as f:
            functions_sql = f.read()
        await conn.execute(functions_sql)
        print("✅ functions.sql применён")

    finally:
        await conn.close()


async def post_form(
    client: AsyncClient,
    url: str,
    data: dict = None,
    follow_redirects: bool = False,
    **kwargs
):
    """
    Удобный POST для HTML-форм с автоматической подстановкой CSRF-токена.
    """
    if data is None:
        data = {}

    csrf_token = client.cookies.get("csrf_token")
    if csrf_token:
        data = dict(data)  # копируем, чтобы не мутировать оригинальный словарь
        data.setdefault("csrf_token", csrf_token)

    return await client.post(url, data=data, follow_redirects=follow_redirects, **kwargs)


@pytest.fixture
def user_data():
    """Фабрика для создания данных пользователя с возможностью переопределения полей"""
    def _factory(**overrides):
        data = {
            "email": "new_user_test@example.com",
            "password": "StrongPass123!",
            "first_name": "Тест",
            "last_name": "Пользователь",
        }
        data.update(overrides)
        return data

    return _factory


@pytest.fixture(scope="session")
async def test_db():
    """Создаёт тестовую БД один раз на всю сессию тестов"""
    await create_test_database()
    await apply_schema()
    yield
    print(f"🧹 Тестовая БД '{TEST_DB_NAME}' будет удалена после всех тестов")


@pytest.fixture
async def db_pool(test_db):
    """Создаёт новый пул для КАЖДОГО теста (решает все concurrency-проблемы)"""
    pool = await asyncpg.create_pool(
        user=settings.COURSES_POSTGRES_USER,
        password=settings.COURSES_POSTGRES_PASSWORD,
        database=TEST_DB_NAME,
        host=settings.COURSES_POSTGRES_HOST,
        port=settings.COURSES_POSTGRES_PORT,
        min_size=2,
        max_size=10,
    )

    # Очищаем таблицы перед каждым тестом
    async with pool.acquire() as conn:
        await conn.execute("""
                TRUNCATE users, user_sessions, user_access_history
                RESTART IDENTITY CASCADE;
            """)
        # C-блоки не каскадятся с users и копятся между тестами.
        # Дерево считает всю серию C курса BGRUA1 одной очередью.
        await conn.execute("DELETE FROM intensive_blocks WHERE block_type = 'C'")

    yield pool
    await pool.close()


@pytest.fixture
async def create_user(db_pool):
    """
    Универсальная фабрика создания тестового пользователя.
    Можно задавать balance, access_level и другие параметры.
    """
    async def _factory(
        access_level: int = 0,
        days: int = 0,
        is_confirmed: bool = True,
        **extra_fields
    ):
        timestamp = datetime.now(timezone.utc).timestamp()
        username = extra_fields.get("username") or f"user_{timestamp}"
        email = extra_fields.get("email") or f"{username}@example.com"

        if days > 0:
            access_until = datetime.now(timezone.utc) + timedelta(days=days)
        else:
            access_until = None

        defaults = {
            "username": username,
            "email": email,
            "password_hash": "dummy_hash",
            "is_confirmed": is_confirmed,
            "is_active": True,
            "access_level": access_level,
            "access_until": access_until,
        }
        defaults.update(extra_fields)

        columns = ", ".join(defaults.keys())
        placeholders = ", ".join([f"${i+1}" for i in range(len(defaults))])

        user_id = await db_pool.fetchval(
            f"""
            INSERT INTO users ({columns})
            VALUES ({placeholders})
            RETURNING id
            """,
            *defaults.values()
        )
        return user_id
    return _factory


@pytest.fixture
async def create_pending_order(db_pool):
    """
    Создаёт заказ в статусе 'pending' + одну позицию в order_items.
    Удобно использовать в тестах автоплатежа.
    """
    async def _factory(
        user_id: int,
        amount: float,
        days_ago: int = 0,
        product_slug: str = "access-standard-monthly"
    ) -> int:
        # Получаем product_id
        product_id = await db_pool.fetchval(
            "SELECT id FROM products WHERE slug = $1",
            product_slug
        )
        if not product_id:
            raise ValueError(f"Продукт с slug '{product_slug}' не найден")

        # Вычисляем дату создания на Python
        created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)

        # Создаём заказ
        order_id = await db_pool.fetchval("""
            INSERT INTO orders (user_id, status, amount, created_at)
            VALUES ($1, 'pending', $2, $3)
            RETURNING id
        """, user_id, amount, created_at)

        # Создаём позицию в заказе
        await db_pool.execute("""
            INSERT INTO order_items (order_id, product_id, quantity, price, discount)
            VALUES ($1, $2, 1, $3, 0)
        """, order_id, product_id, amount)

        return order_id
    return _factory


@pytest.fixture
async def client(db_pool):
    """Тестовый клиент с поддержкой CSRF"""
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    from fastapi.encoders import jsonable_encoder

    from app.routers.deps import LangDep
    from app.db.dependencies import CurrentUser

    temp_router = APIRouter()

    @temp_router.get("/{source_lang}/{ui_lang}/test/pending-order")
    async def get_pending_order_for_test(
        lang_pair: LangDep,
        current_user: CurrentUser,
        request: Request
    ):
        pending_order = getattr(request.state, "pending_order", None)
        return JSONResponse(
            content=jsonable_encoder({
                "has_pending_order": pending_order is not None,
                "pending_order": pending_order
            })
        )

    app.include_router(temp_router)
    app.state.db_pool = db_pool

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        # Делаем первичный GET, чтобы получить CSRF-токен
        await ac.get("/bg/ru/")

        yield ac


@pytest.fixture
async def access_user(db_pool):
    """
    Создаёт уникальных пользователя для тестов доступа
    (каждый раз нового, чтобы избежать влияния других тестов)
    Можно передавать параметры:
        access_level (int): 0, 1 или 2
        days (int): на сколько дней активна подписка (отрицательное — просрочена)
    """

    async def _create_user(access_level: int = 0, days: int = 0):
        # Делаем username и email уникальными
        timestamp = datetime.now(timezone.utc).timestamp()
        username = f"access_{access_level}_{days}_{timestamp}"
        email = f"access_user_{timestamp}@example.com"

        if days == 0:
            until = None
        else:
            until = datetime.now(timezone.utc) + timedelta(days=days)

        user_id = await db_pool.fetchval("""
             INSERT INTO users
             (username, email, password_hash, is_confirmed, is_active, access_level,
              access_until)
             VALUES ($1, $2, 'hash', TRUE, TRUE, $3, $4) RETURNING id
             """, username, email, access_level, until)
        return user_id
    return _create_user


@pytest.fixture
async def auth_client(client, access_user, db_pool):
    """Клиент с авторизованным пользователем + правильным jti"""
    user_id = await access_user()

    # 1. Генерируем jti заранее
    jti = str(uuid.uuid4())

    # 2. Создаём токен, явно передавая jti
    token = create_access_token(
        data={"sub": str(user_id)},
        jti=jti
    )

    # 3. Создаём сессию в БД с этим же jti
    await db_pool.execute("""
        INSERT INTO user_sessions 
            (user_id, jti, ip_address, user_agent, created_at)
        VALUES 
            ($1, $2, '127.0.0.1', 'pytest-client', NOW())
    """, user_id, jti)

    # 4. Устанавливаем cookie
    client.cookies.set("access_token", token)

    return client, user_id


@pytest.fixture
def admin_client(client):
    """Клиент с подменённой зависимостью суперпользователя"""
    from app.main import app
    from app.db.dependencies import get_current_superuser

    async def override_get_current_superuser():
        return {
            "user_id": 999,
            "email": "admin@test.com",
            "is_superuser": True
        }

    # Подменяем зависимость
    app.dependency_overrides[get_current_superuser] = override_get_current_superuser

    yield client

    # Обязательно очищаем после теста!
    app.dependency_overrides.clear()


# --- МОКИНГ EMAIL ---------------------------------------------------------
@pytest.fixture(autouse=True)
def mock_email_sending():
    """Глушит ВСЕ отправки email в тестах"""
    # Мокаем по месту использования (в роутерах) — самый надёжный способ
    with patch("app.routers.auth.send_verification_email", new_callable=AsyncMock) as mock_verify, \
         patch("app.routers.auth.send_password_reset_email", new_callable=AsyncMock) as mock_reset, \
         patch("app.core.email.send_verification_email", new_callable=AsyncMock) as mock_verify_core, \
         patch("app.core.email.send_password_reset_email", new_callable=AsyncMock) as mock_reset_core:

        mock_verify.return_value = True
        mock_reset.return_value = True
        mock_verify_core.return_value = True
        mock_reset_core.return_value = True

        yield {
            "send_verification_email": mock_verify,
            "send_password_reset_email": mock_reset
        }


async def login_as(client, db_pool, user_id: int):
    """Ставит cookie access_token и строку в user_sessions для данного user_id."""
    jti = str(uuid.uuid4())
    token = create_access_token(data={"sub": str(user_id)}, jti=jti)
    await db_pool.execute(
        """
        INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
        VALUES ($1, $2, '127.0.0.1', 'pytest-client', NOW())
        ON CONFLICT (user_id) DO UPDATE SET jti = EXCLUDED.jti
        """,
        user_id,
        jti,
    )
    client.cookies.set("access_token", token)
    return client


async def paid_private_lesson_item(db_pool, user_id: int, slug: str = "private-lesson-single") -> int:
    """Оплаченный заказ с позицией private-lesson-*."""
    product_id = await db_pool.fetchval("SELECT id FROM products WHERE slug = $1", slug)
    if not product_id:
        raise ValueError(f"В тестовой БД нет продукта {slug}")
    order_id = await db_pool.fetchval(
        """
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'paid', 25)
        RETURNING id
        """,
        user_id,
    )
    return await db_pool.fetchval(
        """
        INSERT INTO order_items (order_id, product_id, quantity, price, discount)
        VALUES ($1, $2, 1, 25, 0)
        RETURNING id
        """,
        order_id,
        product_id,
    )

# --- Фиктура для тестирования курсов / уроков / тем / упражнений ------------------
@pytest.fixture
async def minimal_theme(db_pool):
    """Создаёт минимальный курс + урок + тему для тестов"""
    theme_name = "BGRUA1001_H001"
    lesson_name = "BGRUA1001"
    course_name = "BGRUA1"

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO courses (name, title) VALUES ($1, 'Test Course')
            ON CONFLICT (name) DO NOTHING
        """, course_name)

        await conn.execute("""
            INSERT INTO lessons (name, course_name, title, permission, visibility)
            VALUES ($1, $2, 'Test Lesson', ARRAY[0,1,2], ARRAY[0,1,2])
            ON CONFLICT (name) DO NOTHING
        """, lesson_name, course_name)

        await conn.execute("""
            INSERT INTO themes (name, lesson_name, title, pos, permission, visibility)
            VALUES ($1, $2, 'Test Theme', 1, ARRAY[0,1,2], ARRAY[0,1,2])
            ON CONFLICT (name) DO NOTHING
        """, theme_name, lesson_name)

    return theme_name