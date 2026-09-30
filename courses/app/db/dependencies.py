import asyncpg
from decimal import Decimal
from datetime import datetime
from typing import Annotated, Optional
from fastapi import Depends, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from urllib.parse import quote

from app.core.security import get_current_user as verify_token

def get_db_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.db_pool


DBPoolDep = Annotated[asyncpg.Pool, Depends(get_db_pool)]


def _cached_auth_user(request: Request):
    """Пользователь, уже проверенный на этом запросе.

    Ellipsis — проверки ещё не было.
    None — проверяли, сессии нет.
    dict — проверяли, пользователь найден.
    """
    if not getattr(request.state, "auth_checked", False):
        return Ellipsis
    return getattr(request.state, "user", None)


def _remember_auth_user(request: Request, user: dict | None) -> None:
    """Фиксирует результат одной проверки JWT/сессии на весь запрос."""
    request.state.auth_checked = True
    request.state.user = user
    request.state.is_authenticated = bool(user)
    if user and isinstance(user, dict):
        request.state.role = user.get("role") or "student"
    elif not getattr(request.state, "role", None):
        request.state.role = "student"


# ============================================================
# 1. СТРОГАЯ АВТОРИЗАЦИЯ (для API → 401)
# ============================================================
async def get_current_active_user(
        request: Request,
        pool: DBPoolDep,
        token: str = None,
        access_token: str = None
):
    """Возвращает текущего авторизованного пользователя.
    При отсутствии токена выбрасывает HTTPException(401).

    Если AuthMiddleware (или предыдущая зависимость) уже проверил
    сессию, повторно JWT и БД не трогаем.
    """
    explicit_token = token or access_token
    if not explicit_token:
        cached = _cached_auth_user(request)
        if cached is not Ellipsis:
            if cached:
                return cached
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Не авторизован",
                headers={"WWW-Authenticate": "Bearer"},
            )

    if not explicit_token:
        explicit_token = request.cookies.get("access_token")

    if not explicit_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Проверяем токен + активную сессию
    user_data = await verify_token(token=explicit_token, pool=pool)
    if token is None and access_token is None:
        _remember_auth_user(request, user_data)
    return user_data


CurrentUser = Annotated[dict, Depends(get_current_active_user)]

# ============================================================
# 1b. СТРОГАЯ АВТОРИЗАЦИЯ ДЛЯ ВЕБ-СТРАНИЦ (редирект)
# ============================================================
async def get_current_user_required(
    request: Request,
    pool: DBPoolDep,
) -> dict:
    """
    Для HTML-страниц.
    При отсутствии/невалидном токене — сразу 303 Redirect на логин.
    Повторную проверку сессии не делает, если middleware уже отработал.
    """
    cached = _cached_auth_user(request)
    if cached is not Ellipsis:
        if cached:
            return cached
        raise _make_login_redirect_exception(request)

    token = request.cookies.get("access_token")

    if not token:
        raise _make_login_redirect_exception(request)

    try:
        user_data = await verify_token(token=token, pool=pool)
        _remember_auth_user(request, user_data)
        return user_data
    except Exception:
        raise _make_login_redirect_exception(request)


def _make_login_redirect_exception(request: Request) -> HTTPException:
    path_parts = request.url.path.strip("/").split("/")

    if len(path_parts) >= 2:
        source_lang, ui_lang = path_parts[0], path_parts[1]
        login_url = f"/{source_lang}/{ui_lang}/auth/login/"
    else:
        login_url = "/bg/ru/auth/login/"

    next_url = quote(
        str(request.url.path) + (("?" + request.url.query) if request.url.query else "")
    )

    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"{login_url}?next={next_url}"},
    )


RequiredUser = Annotated[dict, Depends(get_current_user_required)]


# ============================================================
# 2. ОПЦИОНАЛЬНАЯ АВТОРИЗАЦИЯ (не требует авторизации)
# ============================================================
async def get_current_active_user_optional(
    request: Request,
    pool: DBPoolDep,
) -> Optional[dict]:
    cached = _cached_auth_user(request)
    if cached is not Ellipsis:
        return cached

    try:
        token = request.cookies.get("access_token")
        if not token:
            _remember_auth_user(request, None)
            return None

        user = await verify_token(token=token, pool=pool)
        _remember_auth_user(request, user)
        return user
    except Exception as e:
        print(e)
        _remember_auth_user(request, None)
        return None


CurrentUserOptional = Annotated[Optional[dict], Depends(get_current_active_user_optional)]


# ============================================================
# 3. SUPERUSER (только для администраторов)
# ============================================================
async def get_current_superuser(
    current_user: CurrentUser,
    pool: DBPoolDep
) -> dict:
    """Проверяет, что пользователь — суперюзер"""
    if not current_user:
        raise HTTPException(status_code=401, detail="Не авторизован")

    # Проверяем наличие поля is_superuser
    user = await pool.fetchrow(
        "SELECT is_superuser FROM users WHERE id = $1",
        current_user["user_id"]
    )

    if not user or not user.get("is_superuser", False):
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещён. Требуются права суперпользователя."
        )

    return current_user


CurrentSuperUser = Annotated[dict, Depends(get_current_superuser)]


# ============================================================
# 4. PENDING ORDER
# ============================================================
async def get_current_pending_order(
    pool: DBPoolDep,
    current_user: CurrentUserOptional = None,
) -> dict | None:
    """Возвращает pending-заказ текущего пользователя (или None)."""
    if not current_user:
        return None

    order = await pool.fetchrow("""
        SELECT id, amount, created_at
        FROM orders
        WHERE user_id = $1 AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """, current_user["user_id"])

    if not order:
        return None

    order_dict = dict(order)
    if isinstance(order_dict.get("amount"), Decimal):
        order_dict["amount"] = float(order_dict["amount"])
    return order_dict
