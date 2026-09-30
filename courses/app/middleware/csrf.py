import secrets
from fastapi import Request, HTTPException, Form
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"


def generate_csrf_token() -> str:
    """Генерирует безопасный CSRF-токен"""
    return secrets.token_urlsafe(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Если токена ещё нет — генерируем
        if CSRF_COOKIE_NAME not in request.cookies:
            token = generate_csrf_token()
            request.state.csrf_token = token
        else:
            request.state.csrf_token = request.cookies.get(CSRF_COOKIE_NAME)

        response = await call_next(request)

        # Устанавливаем cookie, если его ещё не было
        if CSRF_COOKIE_NAME not in request.cookies:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=request.state.csrf_token,
                httponly=True,
                samesite="lax",
                secure=False,           # В продакшене поставь True
                max_age=60 * 60 * 24 * 30,  # 30 дней
            )

        return response


def get_csrf_token(request: Request) -> str:
    """Зависимость для получения токена в шаблонах"""
    return getattr(request.state, "csrf_token", "")


def verify_csrf(request: Request, csrf_token: str = Form(None)):
    """
    Проверка CSRF-токена.
    Возвращает 403, если токен отсутствует или невалиден.
    """
    if not csrf_token:
        raise HTTPException(status_code=403, detail="CSRF token is missing")

    cookie_token = request.cookies.get("csrf_token")

    if not cookie_token or csrf_token != cookie_token:
        raise HTTPException(status_code=403, detail="CSRF token is missing or invalid")
