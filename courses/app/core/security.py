import asyncpg
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import HTTPException, status

from app.core.config import settings
from app.repositories.user import get_current_user_session

pwd_context = CryptContext(
    schemes=["argon2", "django_pbkdf2_sha256", "pbkdf2_sha256", "bcrypt"],   # поддерживает все 3 формата
    deprecated=["django_pbkdf2_sha256", "pbkdf2_sha256", "bcrypt"],          # Но новые сохраняет только в новом argon2
)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, jti: str = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc)+ timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)

    if jti is None:
        jti = str(uuid.uuid4())

    to_encode.update({
        "exp": expire,
        "jti": jti,
        "type": "access"
    })
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def check_password_for_email(pool, email: str, password: str) -> bool:
    """Проверяет пароль и обновляет только старые хэши (чей формат не argon2)"""

    pass_hashed = await pool.fetchval(
        "SELECT password_hash FROM users WHERE email = $1",
        email
    )

    if not pass_hashed:
        print(f"Пользователь {email} в таблице пользователей не найден!")
        return False

    try:
        is_valid, new_hash = pwd_context.verify_and_update(password, pass_hashed)

        if is_valid and new_hash:
            await pool.execute(
                "UPDATE users SET password_hash = $1 WHERE email = $2",
                new_hash, email
            )
            print(f"✅ Пароль пользователя {email} обновлён до Argon2")
        return is_valid

    except Exception as e:
        print(f"Ошибка при проверке пароля для {email}: {e}")
        return False


def streak_view(daily_activity: dict) -> dict:
    """Поля индикатора серии. Новый день — с 07:00 времени сервера, как в daily_activity."""
    raw = daily_activity or {}
    try:
        streak = int(raw.get("streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    try:
        freeze_days = int(raw.get("freeze") or 0)
    except (TypeError, ValueError):
        freeze_days = 0
    last_on = raw.get("last_on") or ""
    activity_day = (datetime.now() - timedelta(hours=7)).date()
    last_date = None
    if last_on:
        try:
            last_date = datetime.strptime(str(last_on)[:10], "%Y-%m-%d").date()
        except ValueError:
            last_date = None
    weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    show = min(max(streak, 0), 7)
    marks = []
    if last_date and show:
        start = last_date - timedelta(days=show - 1)
        for offset in range(show):
            day = start + timedelta(days=offset)
            marks.append({
                "label": weekdays[day.weekday()],
                "is_today": day == activity_day,
                "done": True,
            })
    active_today = last_date == activity_day if last_date else False
    if not active_today:
        if len(marks) >= 7:
            marks = marks[-6:]
        marks.append({
            "label": weekdays[activity_day.weekday()],
            "is_today": True,
            "done": False,
        })
    return {
        "streak_days": streak,
        "active_today": active_today,
        "freeze_days": freeze_days,
        "marks": marks,
    }


def _as_daily_activity(raw) -> dict:
    """Ячейка стрика как словарь. Чтение не выдаёт номер Q и ничего не пишет."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return raw


async def get_current_user(token: str, pool):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Сессия недействительна. Войдите заново.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: int = int(payload.get("sub"))
        jti: str = payload.get("jti")

        print(f"JWT decoded → user_id: {user_id}, jti: {jti}")  # ← отладка

        if user_id is None or jti is None:
            raise credentials_exception

        # Проверяем, что это текущая активная сессия пользователя
        session = await get_current_user_session(pool, user_id)
        print(f"Session from DB: {dict(session) if session else None}")  # ← отладка

        if not session:
            print("Сессия не найдена в БД")
            raise HTTPException(status_code=401, detail="Сессия была завершена с другого устройства.")

        if session["jti"] != jti:
            print(f"JTI mismatch! DB: {session['jti']}, Token: {jti}")
            raise HTTPException(status_code=401, detail="Сессия была завершена с другого устройства.")

        print("Сессия успешно проверена")

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT access_level, current_exercise, access_until, role, 
                          daily_activity, loyalty_points, has_freeze
                   FROM users WHERE id = $1""",
                user_id
            )
            access_level = row["access_level"] if row else 0
            current_exercise = row["current_exercise"] if row else 0
            access_until = row["access_until"].strftime("%Y-%m-%d %H:%M") if row and row.get("access_until") else None
            role = row["role"] if row and row.get("role") else "student"
            daily_activity = _as_daily_activity(row["daily_activity"] if row else None)
            loyalty_points = int(row["loyalty_points"] or 0) if row else 0
            has_freeze = bool(row["has_freeze"]) if row else False

        return {
            "user_id": user_id,
            "email": payload.get("email"),
            "access_level": access_level,
            "access_until": access_until,
            "current_exercise": current_exercise,
            "daily_activity": daily_activity,
            "streak_view": streak_view(daily_activity),
            "role": role,
            "jti": jti,
            "loyalty_points": loyalty_points,
            "has_freeze": has_freeze,
        }

    except JWTError as e:
        print(f"JWTError: {e}")
        raise credentials_exception
    except HTTPException:  # ← важно: НЕ ловим наши 401
        raise  # пробрасываем дальше как есть
    except Exception as e:
        print(f"Unexpected error in get_current_user: {e}")
        raise credentials_exception


def create_email_verification_token(email: str) -> str:
    """Создаёт JWT-токен для подтверждения почты"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.EMAIL_VERIFY_TOKEN_EXPIRE_MINUTES)
    to_encode = {
        "sub": email,
        "type": "email_verification",
        "exp": expire,
        "jti": secrets.token_urlsafe(16)   # защита от replay-атак
    }
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_email_token(token: str) -> str | None:
    """Проверяет токен и возвращает email, если валиден"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") != "email_verification":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def create_password_reset_token(email: str) -> str:
    """Токен для сброса пароля (действует 30 минут)"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=30)
    to_encode = {
        "sub": email,
        "type": "password_reset",
        "exp": expire,
        "jti": secrets.token_urlsafe(16)
    }
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_password_reset_token(token: str) -> str | None:
    """Проверяет токен сброса пароля"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") != "password_reset":
            return None
        return payload.get("sub")
    except JWTError:
        return None
