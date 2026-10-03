from fastapi import Request, HTTPException, Path, Depends
from fastapi.responses import HTMLResponse
from typing import Any, Annotated

from app.core.config import templates, SUPPORTED_UI_LANGUAGES, DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE


class LangPair:
    def __init__(self, source_lang: str, ui_lang: str):
        self.source_lang = source_lang
        self.ui_lang = ui_lang
        self.lang_pair = (self.source_lang, self.ui_lang)
        self.slug = self.source_lang + '_' + self.ui_lang


def get_lang_params(
    request: Request,
    source_lang: Annotated[str, Path(pattern=r"^[a-z]{2,3}$")],
    ui_lang: Annotated[str, Path(pattern=r"^[a-z]{2,3}$")],
) -> LangPair:
    """Зависимость для получения и проверки языковых параметров"""
    if source_lang not in SUPPORTED_UI_LANGUAGES or ui_lang not in SUPPORTED_UI_LANGUAGES:
        raise HTTPException(status_code=400, detail="Unsupported language")

    return LangPair(source_lang, ui_lang)


def get_default_lang_pair():
    return LangPair(DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE)


# Удобные aliases
LangDep = Annotated[LangPair, Depends(get_lang_params)]
DefaultLangDep = Annotated[LangPair, Depends(get_default_lang_pair)]


def render_template(
    request: Request,
    name: str,
    lang_pair: LangPair,
    context: dict[str, Any] | None = None,
    status_code: int = 200
) -> HTMLResponse:
    """
    Универсальная функция для рендера шаблонов с языковыми параметрами.
    Убирает дублирование кода в роутерах.
    """

    ctx = {
        "request": request,
        "source_lang": lang_pair.source_lang,
        "ui_lang": lang_pair.ui_lang,
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }
    if context:
        ctx.update(context)

    return templates.TemplateResponse(
        name=name,
        request=request,
        context=ctx,
        status_code=status_code
    )

def render_template_string(
    request: Request,
    name: str,
    lang_pair: LangPair,
    context: dict[str, Any] | None = None,
) -> str:
    """
    Рендерит Jinja-шаблон в HTML-строку.

    Используется там, где шаблон является частью другого ответа,
    а не самостоятельным HTMLResponse.
    """

    ctx = {
        "request": request,
        "source_lang": lang_pair.source_lang,
        "ui_lang": lang_pair.ui_lang,
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }

    if context:
        ctx.update(context)

    template = templates.get_template(name)

    return template.render(**ctx)
