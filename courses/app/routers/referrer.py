"""
Роутер страницы реферера и публичной обработки реферальных ссылок.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from gettext import gettext as _

from app.db.dependencies import DBPoolDep, CurrentUser, CurrentUserOptional
from app.middleware.csrf import verify_csrf
from app.routers.deps import LangDep, render_template, get_default_lang_pair
from app.repositories.referrer import (
    get_referrer_by_user_id,
    get_referrer_by_code,
    get_referrals_info,
    _normalize_payments,
)
from app.core.config import DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE

router = APIRouter(tags=["referrer"])

REFERRER_COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 дней


def _set_referrer_cookie(response: Response, request: Request, code: str) -> None:
    """Ставит cookie referrer_code (secure только на HTTPS)."""
    is_secure = request.url.scheme == "https"
    response.set_cookie(
        key="referrer_code",
        value=code,
        max_age=REFERRER_COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        secure=is_secure,
        path="/",
    )


@router.get("/ref/{code}")
async def handle_referral_link(
    request: Request,
    code: str,
    pool: DBPoolDep,
):
    """
    Короткая реферальная ссылка /ref/{code}.

    Проверяет код, ставит cookie и редиректит на страницу приглашения
    с языковой парой по умолчанию — чтобы i18n работал как на всём сайте.
    """
    referrer = await get_referrer_by_code(pool, code)
    lang_pair = get_default_lang_pair()
    invite_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/invite/{code}"

    response = RedirectResponse(url=invite_url, status_code=303)
    if referrer:
        _set_referrer_cookie(response, request, code)
    return response


@router.get("/{source_lang}/{ui_lang}/invite/{code}", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/invite/{code}/", response_class=HTMLResponse)
async def invite_referral_page(
    request: Request,
    code: str,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUserOptional = None,
):
    """
    Страница приглашения по реферальной программе (с языковой парой в URL).

    - Неверный код → сообщение об ошибке.
    - Залогиненный пользователь → только для новых, предложение на главную.
    - Гость → приглашение зарегистрироваться.
    """
    referrer = await get_referrer_by_code(pool, code)
    home_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/"

    # Недействительная ссылка
    if not referrer:
        return render_template(
            request,
            "invite_referral.html",
            lang_pair,
            {
                "state": "invalid",
                "referrer_code": code,
                "home_url": home_url,
            },
            status_code=404,
        )

    # Уже залогинен — программа только для новых пользователей
    if current_user:
        home_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/"
        return render_template(
            request,
            "invite_referral.html",
            lang_pair,
            {
                "state": "already_logged_in",
                "referrer_code": code,
                "home_url": home_url,
            },
        )

    # Гость: ставим cookie и показываем приглашение зарегистрироваться
    register_url = (
        f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/#register"
    )
    home_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/"

    response = render_template(
        request,
        "invite_referral.html",
        lang_pair,
        {
            "state": "invite",
            "referrer_code": code,
            "register_url": register_url,
            "home_url": home_url,
            "bonus_welcome": 1000,
            "bonus_referral": 10000,
        },
    )
    _set_referrer_cookie(response, request, code)
    return response


@router.get("/{source_lang}/{ui_lang}/referrer", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/referrer/", response_class=HTMLResponse)
async def referrer_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUser,
):
    """Страница реферера. Закрыта для всех, кроме роли referrer."""
    role = current_user.get("role") if isinstance(current_user, dict) else None

    if role != "referrer":
        # Перенаправляем на главную или логин
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/",
            status_code=303,
        )

    user_id = current_user["user_id"]
    referrer = await get_referrer_by_user_id(pool, user_id)

    if not referrer:
        # Если роль есть, но записи нет — можно создать, но лучше это делает админ
        # Пока просто показываем сообщение
        return render_template(
            request,
            "referrer.html",
            lang_pair,
            {
                "referrer": None,
                "referrals": [],
                "error": _("Referrer record not found. Please contact the administrator."),
            },
        )

    referrals = await get_referrals_info(pool, referrer.get("referral_ids") or [])

    # Нормализуем payments: разворачиваем «битые» строковые элементы в dict
    payments = _normalize_payments(referrer.get("payments"))

    # Формируем ссылку динамически из текущего запроса (протокол + домен + порт)
    base_url = str(request.base_url).rstrip("/")
    # Короткая ссылка для шаринга — без языковой пары
    referrer_link = f"{base_url}/ref/{referrer['code']}"

    return render_template(
        request,
        "referrer.html",
        lang_pair,
        {
            "referrer": referrer,
            "referrals": referrals,
            "payments": payments,
            "referrer_link": referrer_link,
        },
    )


# Пример POST-эндпоинта (на будущее, с CSRF):
# @router.post("/{source_lang}/{ui_lang}/referrer/regenerate/")
# async def regenerate_code(
#     request: Request,
#     lang_pair: LangDep,
#     pool: DBPoolDep,
#     current_user: CurrentUser,
#     csrf_token_=Depends(verify_csrf),
# ):
#     ...
