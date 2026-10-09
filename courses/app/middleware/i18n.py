from babel.support import Translations
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import templates, DEFAULT_UI_LANGUAGE, SUPPORTED_UI_LANGUAGES
from app.core.config import locale_dir

def get_locale(request: Request) -> str:
    path = request.url.path.lstrip("/")
    parts = path.split("/")

    # Приоритет 1: Язык из URL (второй сегмент)
    if len(parts) >= 2 and parts[1] in SUPPORTED_UI_LANGUAGES:
        return parts[1]

    # Приоритет 2: Cookie
    if request.cookies.get("lang") in SUPPORTED_UI_LANGUAGES:
        return request.cookies.get("lang")

    # Приоритет 3: Query параметр
    if request.query_params.get("lang") in SUPPORTED_UI_LANGUAGES:
        return request.query_params.get("lang")

    # Приоритет 4: Accept-Language
    accept_lang = request.headers.get("accept-language", "")
    if accept_lang:
        lang = accept_lang.split(",")[0].split("-")[0].lower()
        if lang in SUPPORTED_UI_LANGUAGES:
            return lang

    # Дефолт
    return DEFAULT_UI_LANGUAGE


class I18nMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Определяем язык
        lang = get_locale(request)

        # Загружаем переводы
        try:
            translations = Translations.load(locale_dir, [lang], domain="messages")
        except FileNotFoundError:
            translations = Translations.load(
                locale_dir, [DEFAULT_UI_LANGUAGE], domain="messages"
            )

        # Устанавливаем переводы в Jinja2
        templates.env.install_gettext_translations(translations, newstyle=True)

        # Сохраняем язык в request.state
        request.state.lang = lang

        # Выполняем запрос
        response = await call_next(request)

        # Устанавливаем cookie языка, если был параметр ?lang=...
        if request.query_params.get("lang"):
            response.set_cookie(
                key="lang",
                value=lang,
                max_age=31536000,      # 1 год
                httponly=True,
                samesite="lax"
            )

        return response