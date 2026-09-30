from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, RedirectResponse

from app.db.dependencies import get_current_active_user


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        user = None
        is_authenticated = False
        session_kicked = False

        try:
            user = await get_current_active_user(
                request=request,
                pool=request.app.state.db_pool
            )
            is_authenticated = True

        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 401:
                detail = str(exc.detail).lower()

                if "с другого устройства" in detail:
                    session_kicked = True
                # Обычный "Не авторизован" — просто продолжаем (главная страница публичная)
                else:
                    print(f"[AuthMiddleware] Неавторизованный пользователь: {exc.detail}")
            else:
                print(f"[AuthMiddleware] Неожиданная ошибка: {exc}")

        # Сохраняем в state для шаблонов и зависимостей этого же запроса.
        # auth_checked=True запрещает CurrentUser/RequiredUser ходить в JWT/БД повторно.
        request.state.user = user
        request.state.is_authenticated = is_authenticated
        request.state.session_kicked = session_kicked
        request.state.auth_checked = True
        if user and isinstance(user, dict):
            request.state.role = user.get("role") or "student"
        else:
            request.state.role = "student"

        # === Только специальный случай: выбросили с другого устройства ===
        if session_kicked:
            from app.core.config import (
                DEFAULT_SOURCE_LANGUAGE,
                SUPPORTED_UI_LANGUAGES,
                DEFAULT_UI_LANGUAGE,
            )
            # Правильно формируем URL с двумя языками
            path_parts = request.url.path.lstrip("/").split("/")
            source_lang = (
                path_parts[0]
                if len(path_parts) > 0 and path_parts[0] in SUPPORTED_UI_LANGUAGES
                else DEFAULT_SOURCE_LANGUAGE
            )
            ui_lang = getattr(request.state, "lang", DEFAULT_UI_LANGUAGE)

            redirect_url = f"/{source_lang}/{ui_lang}/auth/login/"

            response = RedirectResponse(url=redirect_url, status_code=303)
            response.delete_cookie(key="access_token", path="/")   # на всякий случай

            response.set_cookie(
                key="flash",
                value="session_kicked",
                max_age=15,          # 15 секунд — достаточно, чтобы показать один раз
                httponly=False,
                samesite="lax",
            )
            return response

        # Обычный путь (включая логаут → главная)
        response = await call_next(request)
        return response
