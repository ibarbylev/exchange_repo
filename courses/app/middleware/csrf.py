import secrets
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAMES = ("x-csrf-token", "x-csrftoken")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _cookie_value(request: Request) -> str | None:
    value = request.cookies.get(CSRF_COOKIE_NAME)
    return value if value else None


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        existing = _cookie_value(request)
        if existing:
            request.state.csrf_token = existing
        else:
            request.state.csrf_token = generate_csrf_token()

        response = await call_next(request)

        # Ставим куку только если токен реально отдали клиенту
        # (через get_csrf_token в шаблоне или в JSON-эндпоинте)
        if not existing and getattr(request.state, "csrf_token_used", False):
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=request.state.csrf_token,
                httponly=True,
                samesite="lax",
                secure=False,  # settings.SECURE_COOKIES
                path="/",
                max_age=60 * 60 * 24 * 30,
            )
            response.headers["Cache-Control"] = "private, no-store"

        return response


def get_csrf_token(request: Request) -> str:
    """Вызывать там, где токен реально вставляется в ответ (шаблон или JSON)."""
    request.state.csrf_token_used = True
    return getattr(request.state, "csrf_token", "") or ""


async def verify_csrf(request: Request):
    if request.method in SAFE_METHODS:
        return

    token = None
    for header in CSRF_HEADER_NAMES:
        token = request.headers.get(header)
        if token:
            break

    if not token:
        content_type = (request.headers.get("content-type") or "").lower()
        if (
            "application/x-www-form-urlencoded" in content_type
            or "multipart/form-data" in content_type
        ):
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            token = value if isinstance(value, str) else None

    cookie_token = _cookie_value(request)

    if (
        not isinstance(token, str)
        or not token
        or not cookie_token
        or not secrets.compare_digest(
            token.encode("utf-8"), cookie_token.encode("utf-8")
        )
    ):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

