"""
Роутер дневной активности и дневного задания.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request, Depends, Body
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db.dependencies import DBPoolDep, CurrentUserOptional
from app.middleware.csrf import verify_csrf
from app.routers.deps import LangDep, render_template
from app.repositories.daily_activity import (
    assign_or_get_daily_exercise,
    complete_daily_task_if_matches,
)

router = APIRouter(tags=["daily_activity"])


def _parse_groups(raw: str | None) -> list[list[str]]:
    """Парсит [a|b][c|d]. Для answers каждая группа — полная комбинация слотов."""
    if not raw:
        return []
    groups = []
    for inner in re.findall(r"\[([^\]]*)\]", raw):
        parts = [(part.strip() or " ") for part in inner.split("|")]
        groups.append(parts)
    return groups


@router.get("/{source_lang}/{ui_lang}/daily-task/", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/daily-task", response_class=HTMLResponse)
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
                SELECT name, title, question, variants, answers,
                       audio_question, audio_answers
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
                "audio_question": row["audio_question"] or "",
                "audio_answers": row["audio_answers"] or "",
            }

    return render_template(request, "daily_task.html", lang_pair, context)


@router.post("/{source_lang}/{ui_lang}/daily-task/complete/")
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
