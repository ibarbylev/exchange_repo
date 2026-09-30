import asyncpg
from datetime import datetime, timezone


async def get_current_user_session(pool, user_id: int):
    query = """
        SELECT jti, ip_address, user_agent, created_at 
        FROM user_sessions 
        WHERE user_id = $1
    """
    return await pool.fetchrow(query, user_id)


async def delete_user_session(pool, user_id: int):
    query = "DELETE FROM user_sessions WHERE user_id = $1"
    await pool.execute(query, user_id)


async def create_user_session(pool, user_id: int, jti: str,
                             ip_address: str = None, user_agent: str = None):
    """Создаёт или обновляет сессию пользователя (одна активная сессия)"""

    # добавляет сессию, если её ещё нет и обновляет jti, если уже сессия есть
    query = """
        INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (user_id) 
        DO UPDATE SET 
            jti = EXCLUDED.jti,
            ip_address = EXCLUDED.ip_address,
            user_agent = EXCLUDED.user_agent,
            created_at = NOW()
    """
    await pool.execute(query, user_id, jti, ip_address, user_agent)


# Обновите существующие функции под pool
async def get_user_by_email(pool, email: str):
    query = """
        SELECT id, email, username, first_name, last_name, password_hash,
               is_active, created_at, last_login, is_confirmed
        FROM users 
        WHERE LOWER(email) = LOWER($1)
    """
    return await pool.fetchrow(query, email)


async def update_last_login(pool, user_id: int):
    query = "UPDATE users SET last_login = NOW() WHERE id = $1"
    await pool.execute(query, user_id)


async def delete_expired_unconfirmed_users(pool):
    query = """
        DELETE FROM users
        WHERE is_confirmed = FALSE
          AND created_at < NOW() - INTERVAL '24 hours'
        RETURNING id, email;
    """
    deleted = await pool.fetch(query)
    if deleted:
        print(f"🧹 Удалено {len(deleted)} неподтверждённых аккаунтов старше 24 часов")
    return len(deleted)


async def can_send_verification_email(pool, email: str) -> tuple[bool, str]:
    """
    Проверяет, можно ли отправить письмо подтверждения.
    Ограничения:
      - Не чаще 1 раза в час
      - Аккаунт не старше 24 часов (старше мы удаляем автоматически)
    """
    query = """
        SELECT created_at 
        FROM users 
        WHERE LOWER(email) = LOWER($1) 
          AND is_confirmed = FALSE
    """
    row = await pool.fetchrow(query, email)
    if not row:
        return True, ""

    created_at: datetime = row["created_at"]

    # Если аккаунту больше 24 часов — запрещаем совсем
    hours_since_creation = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    if hours_since_creation > 24:
        return False, "Срок подтверждения email истёк. Пожалуйста, зарегистрируйтесь заново."

    # Не чаще 1 раза в час
    if hours_since_creation < 1:
        minutes_left = int(60 - (hours_since_creation * 60))
        return False, f"Повторная отправка возможна через {minutes_left} минут."

    return True, ""
