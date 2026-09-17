from contextlib import asynccontextmanager
import asyncpg
import json
from fastapi import FastAPI

from ..core.config import settings

# Инициализация соединения — будет вызываться для каждого нового соединения из пула
async def init_connection(conn: asyncpg.Connection):
    """
    Автоматически настраивает jsonb, чтобы он возвращался как dict/list
    Иными словами:
        Без этой функции:
            из базы приходит JSON как строка ('{"a":1}')
        С этой функцией:
            из базы сразу приходит нормальный Python-словарь ({"a": 1})
    """
    await conn.set_type_codec(
        'jsonb',
        encoder=json.dumps,
        decoder=json.loads,
        schema='pg_catalog'
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    pool = await asyncpg.create_pool(
        user=settings.COURSES_POSTGRES_USER,
        password=settings.COURSES_POSTGRES_PASSWORD,
        database=settings.COURSES_POSTGRES_DB,
        host=settings.COURSES_POSTGRES_HOST,
        port=settings.COURSES_POSTGRES_PORT,
        min_size=5,
        max_size=20,
        timeout=60,
        command_timeout=60,
        init=init_connection,
    )

    app.state.db_pool = pool
    print("→ Database pool initialized")

    yield

    # Shutdown
    print("→ Closing database pool...")
    await pool.close()
    print("→ Database pool closed")
