"""
Роутер страницы реферала и публичной обработки реферальных ссылок.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from gettext import gettext as _

from app.db.dependencies import DBPoolDep, CurrentUser
from app.middleware.csrf import verify_csrf
from app.routers.deps import LangDep, render_template
from app.repositories.referral import (
    get_referral_by_user_id,
    get_referral_by_code,
    get_clients_info,
)
from app.core.config import DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE

router = APIRouter(tags=["referral"])


@router.get("/ref/{code}")
async def handle_referral_link(
    request: Request,
    code: str,
    pool: DBPoolDep,
):
    """
    Публичная обработка реферальной ссылки /ref/{code}.
    Ставит cookie и перенаправляет на главную.
    """
    referral = await get_referral_by_code(pool, code)

    response = RedirectResponse(
        url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/",
        status_code=303,
    )

    if referral:
        # Ставим cookie на 30 дней
        # secure=True только при HTTPS (на локальной разработке по HTTP cookie иначе не ставится)
        is_secure = request.url.scheme == "https"
        response.set_cookie(
            key="referral_code",
            value=code,
            max_age=30 * 24 * 60 * 60,  # 30 дней
            httponly=False,             # можно читать из JS при необходимости
            samesite="lax",
            secure=is_secure,
            path="/",
        )

    return response


@router.get("/{source_lang}/{ui_lang}/referral", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/referral/", response_class=HTMLResponse)
async def referral_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUser,
):
    """Страница реферала. Закрыта для всех, кроме роли referral."""
    role = current_user.get("role") if isinstance(current_user, dict) else None

    if role != "referral":
        # Перенаправляем на главную или логин
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/",
            status_code=303,
        )

    user_id = current_user["user_id"]
    referral = await get_referral_by_user_id(pool, user_id)

    if not referral:
        # Если роль есть, но записи нет — можно создать, но лучше это делает админ
        # Пока просто показываем сообщение
        return render_template(
            request,
            "referral.html",
            lang_pair,
            {
                "referral": None,
                "clients": [],
                "error": _("Referral record not found. Please contact the administrator."),
            },
        )

    clients = await get_clients_info(pool, referral.get("client_ids") or [])

    # Подготавливаем payments для отображения (уже JSONB)
    payments = referral.get("payments") or []

    # Формируем ссылку динамически из текущего запроса (протокол + домен + порт)
    base_url = str(request.base_url).rstrip("/")
    referral_link = f"{base_url}/ref/{referral['code']}"

    return render_template(
        request,
        "referral.html",
        lang_pair,
        {
            "referral": referral,
            "clients": clients,
            "payments": payments,
            "referral_link": referral_link,
        },
    )


# Пример POST-эндпоинта (на будущее, с CSRF):
# @router.post("/{source_lang}/{ui_lang}/referral/regenerate/")
# async def regenerate_code(
#     request: Request,
#     lang_pair: LangDep,
#     pool: DBPoolDep,
#     current_user: CurrentUser,
#     csrf_token_=Depends(verify_csrf),
# ):
#     ...
