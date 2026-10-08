"""
Роутер страницы программы лояльности.
Подключение в main.py:
    from app.routers.loyalty import router as loyalty_router
    app.include_router(loyalty_router)
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db.dependencies import DBPoolDep, CurrentUserOptional
from app.routers.deps import LangDep, render_template

router = APIRouter(tags=["loyalty"])


async def get_loyalty_balance(pool, user_id: int) -> int:
    """Считает текущий баланс баллов как SUM(points) из таблицы loyalty."""
    async with pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT COALESCE(SUM(points), 0) FROM loyalty WHERE user_id = $1",
            user_id,
        )
    return int(balance or 0)


@router.get("/{source_lang}/{ui_lang}/loyalty", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/loyalty/", response_class=HTMLResponse)
async def loyalty_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUserOptional,
):
    points = 0
    if current_user and isinstance(current_user, dict):
        # Предпочтительно брать уже посчитанное в middleware / user dict,
        # но на случай отсутствия — считаем запросом.
        if "loyalty_points" in current_user:
            points = int(current_user.get("loyalty_points") or 0)
        else:
            points = await get_loyalty_balance(pool, current_user["user_id"])
            # Можно закэшировать в request.state.user для шаблонов
            if hasattr(request.state, "user") and isinstance(request.state.user, dict):
                request.state.user["loyalty_points"] = points

    return render_template(
        request,
        "loyalty.html",
        lang_pair,
        {"points": points},
    )
