"""
Роутер страницы программы лояльности.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request, Depends, Body
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.dependencies import DBPoolDep, CurrentUserOptional
from app.middleware.csrf import verify_csrf
from app.routers.deps import LangDep, render_template
from app.repositories.loyalty import (
    get_loyalty_balance,
    get_loyalty_transactions,
    insert_into_loyalty,
    assign_or_get_daily_exercise,
    complete_daily_task_if_matches,
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
            "has_freeze": has_freeze,
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


def _parse_groups(raw: str | None) -> list[list[str]]:
    """Парсит строку вида [a|b][c|d] в список групп."""
    if not raw:
        return []
    groups = []
    for inner in re.findall(r"\[([^\]]*)\]", raw):
        parts = [p if p else " " for p in inner.split("|")]
        groups.append(parts)
    return groups


@router.get("/{source_lang}/{ui_lang}/loyalty/daily-task/", response_class=HTMLResponse)
async def daily_task_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: CurrentUserOptional,
):
    """
    Страница дневного задания.
    Назначает Q (если ещё не назначено) и отдаёт его для локального прохождения.
    """
    source_lang, ui_lang = lang_pair.source_lang, lang_pair.ui_lang
    if not current_user or not isinstance(current_user, dict):
        return RedirectResponse(
            url=f"/{source_lang}/{ui_lang}/auth/login/",
            status_code=303,
        )

    user_id = current_user["user_id"]
    exercise_name = await assign_or_get_daily_exercise(pool, user_id)

    context = {"exercise": None, "error": None}
    if not exercise_name:
        context["error"] = "Сегодня дневное задание уже выполнено или подходящее упражнение не найдено."
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT name, title, question, variants, answers
                FROM exercises WHERE name = $1
                """,
                exercise_name,
            )
        if not row:
            context["error"] = "Упражнение не найдено."
        else:
            context["exercise"] = {
                "name": row["name"],
                "title": row["title"] or "",
                "question": row["question"] or "",
                "variants": _parse_groups(row["variants"]),
                "answers": _parse_groups(row["answers"]),
            }

    return render_template(request, "daily_task.html", lang_pair, context)


@router.post("/{source_lang}/{ui_lang}/loyalty/daily-task/complete/")
async def daily_task_complete(
    request: Request,
    source_lang: str,
    ui_lang: str,
    pool: DBPoolDep,
    current_user: CurrentUserOptional,
    exercise_name: str = Body(..., embed=True),
    _=Depends(verify_csrf),
):
    """
    Завершение дневного Q. Вызывается игроком, когда пройдено exercise_current.
    """
    if not current_user or not isinstance(current_user, dict):
        return {"ok": False, "error": "auth"}
    result = await complete_daily_task_if_matches(
        pool, current_user["user_id"], str(exercise_name or "")
    )
    if not result:
        return {"ok": False, "reason": "not_current_or_already_done"}
    award = result.get("award") or {}
    return {
        "ok": True,
        "points": award.get("points"),
        "streak": result["streak"],
        "last_on": result["last_on"],
    }
