"""
Роутер страницы программы лояльности.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db.dependencies import DBPoolDep, CurrentUserOptional
from app.routers.deps import LangDep, render_template
from app.repositories.loyalty import get_loyalty_balance, get_loyalty_transactions

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
    if current_user and isinstance(current_user, dict):
        user_id = current_user["user_id"]
        # Предпочтительно брать уже посчитанное в middleware / user dict,
        # но на случай отсутствия — считаем запросом.
        if "loyalty_points" in current_user:
            points = int(current_user.get("loyalty_points") or 0)
        else:
            points = await get_loyalty_balance(pool, user_id)
            # Можно закэшировать в request.state.user для шаблонов
            if hasattr(request.state, "user") and isinstance(request.state.user, dict):
                request.state.user["loyalty_points"] = points

        # Загружаем историю транзакций
        transactions = await get_loyalty_transactions(pool, user_id)

    return render_template(
        request,
        "loyalty.html",
        lang_pair,
        {"points": points, "transactions": transactions},
    )
