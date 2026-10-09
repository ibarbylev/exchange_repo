"""
Роутер страницы программы лояльности.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.dependencies import DBPoolDep, CurrentUserOptional
from app.middleware.csrf import verify_csrf
from app.routers.deps import LangDep, render_template
from app.repositories.loyalty import (
    get_loyalty_balance,
    get_loyalty_transactions,
    insert_into_loyalty,
)

router = APIRouter(tags=["loyalty"])


@router.get("/{source_lang}/{ui_lang}/loyalty", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/loyalty/", response_class=HTMLResponse)
async def loyalty_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUserOptional,
):
    points = 0
    transactions = []
    has_freeze = False

    if current_user and isinstance(current_user, dict):
        user_id = current_user["user_id"]

        # Берём из current_user, если уже есть
        if "loyalty_points" in current_user:
            points = int(current_user.get("loyalty_points") or 0)
        else:
            points = await get_loyalty_balance(pool, user_id)

        has_freeze = bool(current_user.get("has_freeze", False))

        transactions = await get_loyalty_transactions(pool, user_id)

    return render_template(
        request,
        "loyalty.html",
        lang_pair,
        {
            "points": points,
            "transactions": transactions,
            "freeze_days": 1 if has_freeze else 0,
        },
    )


@router.post("/{source_lang}/{ui_lang}/loyalty/buy-freeze/")
async def buy_freeze(
    request: Request,
    source_lang: str,
    ui_lang: str,
    pool: DBPoolDep,
    current_user: CurrentUserOptional,
    _=Depends(verify_csrf),
):
    if not current_user or not isinstance(current_user, dict):
        return RedirectResponse(
            url=f"/{source_lang}/{ui_lang}/auth/login/",
            status_code=303,
        )

    user_id = current_user["user_id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Проверяем, не куплена ли уже заморозка
            has_freeze = await conn.fetchval(
                "SELECT has_freeze FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            if has_freeze:
                return RedirectResponse(
                    url=f"/{source_lang}/{ui_lang}/loyalty/",
                    status_code=303,
                )

            # Списываем 25 баллов
            try:
                await conn.fetchrow(
                    "SELECT * FROM insert_into_loyalty($1, $2, $3)",
                    user_id,
                    -25,
                    "Покупка 1-дневной заморозки серии",
                )
            except Exception:
                return RedirectResponse(
                    url=f"/{source_lang}/{ui_lang}/loyalty/?error=insufficient",
                    status_code=303,
                )

            # Ставим флаг
            await conn.execute(
                "UPDATE users SET has_freeze = true WHERE id = $1",
                user_id,
            )

    return RedirectResponse(
        url=f"/{source_lang}/{ui_lang}/loyalty/",
        status_code=303,
    )
