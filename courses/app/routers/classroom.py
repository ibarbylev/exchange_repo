from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request, HTTPException, Path, Form, Depends, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db.dependencies import DBPoolDep, CurrentUser, RequiredUser
from app.middleware.csrf import verify_csrf
from app.repositories.classroom import get_classroom_tree
from app.routers.deps import LangPair, LangDep, render_template, render_template_string

PLAYERS = {
    "vqt": "classroom/player_vqt.html",
    "text_intensive": "classroom/player_text_intensive.html",
}

router = APIRouter(tags=["classroom"])


def theme_heading(theme_name: str) -> str:
    match = re.match(r"A(\d)(\d{2,3})_H(\d+)", theme_name or "")
    if not match:
        return theme_name
    return (
        f"Курс A{match.group(1)}, "
        f"Урок {match.group(2).zfill(2)}, "
        f"Тема {match.group(3).zfill(2)}:"
    )


def has_tariff_restrictions(exercise_counts: dict | None, access_level: int) -> bool:
    if not exercise_counts:
        return False
    for counts in exercise_counts.values():
        if not isinstance(counts, (list, tuple)) or len(counts) < 3:
            continue
        current = counts[access_level] if access_level < len(counts) else 0
        if current < max(counts):
            return True
    return False


def detect_player(theme: dict, requested: str | None = None) -> str:
    if requested in PLAYERS:
        return requested
    value = str(theme.get("player") or theme.get("kind") or "").lower()
    if value in PLAYERS:
        return value
    if value in {"listening", "textlab", "text_lab", "intensive"}:
        return "text_intensive"
    return "vqt"


def find_theme_in_tree(tree: dict | None, theme_name: str) -> dict | None:
    if not tree:
        return None
    for course in tree.get("courses") or []:
        for lesson in course.get("lessons") or []:
            for item in lesson.get("themes") or []:
                if item.get("name") == theme_name:
                    return item
    return None


def build_player_html(
    request: Request,
    lang_pair: LangPair,
    theme: dict,
    view: str,
    player: str,
    exercise: dict | None,
    access_level: int,
    courses_url: str,
) -> str:
    theme_name = theme.get("name") or ""
    context: dict[str, Any] = {
        "view": view,
        "theme_name": theme_name,
        "theme_title": theme.get("title") or "",
        "theme_heading": theme_heading(theme_name),
        "exercise_counts": theme.get("exercise_counts") or {},
        "is_available": theme.get("is_available", True),
        "is_completed": theme.get("is_completed", False),
        "is_current": theme.get("is_current", False),
        "has_tariff_restrictions": has_tariff_restrictions(
            theme.get("exercise_counts"), access_level
        ),
        "courses_url": courses_url,
        "exercise": exercise,
        "audio_url": (exercise or {}).get("audio_url") or theme.get("audio_url") or "",
        "task_type": (exercise or {}).get("task_type") or "",
        "prompt": (exercise or {}).get("prompt") or (exercise or {}).get("question") or "",
    }
    template_name = PLAYERS.get(player, PLAYERS["vqt"])
    return render_template_string(
        request=request,
        name=template_name,
        lang_pair=lang_pair,
        context=context,
    )


@router.get("/api/classroom/player")
async def classroom_player(
    request: Request,
    theme: str,
    pool: DBPoolDep,
    current_user: CurrentUser,
    view: str = "lobby",
    player: str | None = None,
    exercise: str | None = None,
    source_lang: str | None = None,
    ui_lang: str | None = None,
):
    access_level = current_user.get("access_level", 0) if current_user else 0

    lang_pair = (
        LangPair(source_lang, ui_lang)
        if source_lang and ui_lang
        else LangPair(theme[:2].lower(), theme[2:4].lower())
    )
    courses_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/courses/"

    tree_data = await get_classroom_tree(
        pool=pool,
        lang_prefix=f"{lang_pair.source_lang}{lang_pair.ui_lang}".upper(),
        user_access_level=access_level,
        target_exercise=current_user.get("current_exercise") if current_user else None,
    )
    theme_data = find_theme_in_tree(tree_data, theme)
    if not theme_data:
        return JSONResponse({"error": f"Тема не найдена: {theme}"}, status_code=404)

    theme_data.setdefault("name", theme)
    mode = detect_player(theme_data, player)

    html = build_player_html(
        request,
        lang_pair,
        theme_data,
        view,
        mode,
        None,
        access_level,
        courses_url,
    )
    return JSONResponse({
        "html": html,
        "meta": {
            "player": mode,
            "theme_name": theme,
            "view": view,
            "exercise_name": exercise,
        },
    })

def parse_variants(variants_str: str | None) -> list[list[str]]:
    """
    Парсит строку вида:
    [ |а|о|и][ |се|съм|си|е|сме|сте|са|ли][ |се|съм|си|е|сме|сте|са|ли]

    Возвращает:
    [
        [' ', 'а', 'о', 'и'],
        [' ', 'се', 'съм', 'си', 'е', 'сме', 'сте', 'са', 'ли'],
        ...
    ]
    """
    if not variants_str:
        return []

    result = []
    current = ""
    i = 0

    while i < len(variants_str):
        if variants_str[i] == '[':
            if current:
                # На случай, если перед первой скобкой что-то было
                result.append(current.strip().split('|'))
                current = ""
            i += 1
            continue

        if variants_str[i] == ']':
            if current:
                result.append(current.strip().split('|'))
                current = ""
            i += 1
            continue

        current += variants_str[i]
        i += 1

    # Если что-то осталось после последней скобки
    if current.strip():
        result.append(current.strip().split('|'))

    # Убираем пустые строки внутри групп
    cleaned = []
    for group in result:
        cleaned_group = [item for item in group if item != ""]
        if cleaned_group:
            cleaned.append(cleaned_group)

    return cleaned


@router.get("/{source_lang}/{ui_lang}/classroom/", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/classroom", response_class=HTMLResponse)
async def get_classroom(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: RequiredUser,
):
    lang_prefix = f"{lang_pair.source_lang}{lang_pair.ui_lang}".upper()

    user_access_level = current_user.get("access_level", 0) if current_user else 0

    # Определяем, какую тему показывать по умолчанию
    target_exercise = current_user.get("current_exercise") if current_user else None

    tree_data = await get_classroom_tree(
        pool=pool,
        lang_prefix=lang_prefix,
        user_access_level=user_access_level,
        target_exercise=target_exercise
    )

    access_names = {
        0: "Базовый",
        1: "Стандарт",
        2: "Премиум",
    }

    return render_template(
        name="classroom.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "classroom",
            "tree": tree_data,
            "current_exercise": target_exercise,
            "access_level": user_access_level,
            "access_level_name": access_names.get(user_access_level),
            "access_until": current_user.get("access_until"),
            "role": getattr(request.state, "role", "user"),
            "loyalty_points": current_user.get("loyalty_points", 0),
        }
    )


@router.get("/api/exercise/{exercise_name}")
async def get_exercise(
    exercise_name: str,
    pool: DBPoolDep,
    current_user: CurrentUser,
    show_correct: bool = Query(False, alias="show_correct")
):
    user_access_level = current_user.get("access_level", 0) if current_user else 0

    # --- Получаем row - строку упражнения из БД (таблица exercises) --------------
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                name, title, question, audio_question,
                variants, answers, audio_answers,
                exercise_type, theme_name,
                youtube_video, theory, duration, duration_limit
            FROM exercises
            WHERE name = $1
        """, exercise_name)

        if not row:
            return {"error": "Exercise not found"}

        # --- Получаем информацию о теме, уроке и курсе этого упражнения ------------
        theme_info = await conn.fetchrow(
            "SELECT name, title FROM themes WHERE name = $1",
            row["theme_name"]
        )
        theme_name = theme_info["name"]
        theme_title = theme_info["title"].capitalize()

        course_level = f" Уровень {theme_name[4:6]}"   # BGRUA1002_H003[4:6] = A1
        lesson_number = f"Урок {theme_name[7:9]}"      # BGRUA1002_H003[7:9] = 02
        theme_number = f"Тема {theme_name[12:]}"       # BGRUA1002_H003[7:9] = 03
        exercise_full_name = ", ".join([course_level, lesson_number, theme_number]) + ":"

        ex_type = row["exercise_type"]

        # --- Базовый результат (общий для всех типов) ---------------------
        result = {
            "name": row["name"],
            "title": row["title"],
            "question": row["question"],
            "audio_question": row["audio_question"],
            "audio_answers": row["audio_answers"],
            "type": ex_type,
            "themeName": theme_name,
            "theme_title": theme_title,
            "exercise_full_name": exercise_full_name,
            "showCorrectAnswers": show_correct,
        }

        # --- Упражнение V (ВИДЕО) -----------------------------------------
        if ex_type == "V":
            result.update({
                "youtube_video": row["youtube_video"],
                "theory": row["theory"],
                "duration": row["duration"],
                "duration_limit": row["duration_limit"],
            })

            # --- Первые упражнения Q и T для кнопки "Продолжить" ------------
            # --- Первое упражнение Q: ---------------------------------------
            first_q = await conn.fetchval("""
                SELECT name FROM exercises
                WHERE theme_name = $1 
                  AND exercise_type = 'Q'
                  AND $2 = ANY(visibility)
                ORDER BY pos LIMIT 1
            """, theme_name, user_access_level)

            # --- Первое упражнение T: ---------------------------------------
            first_test = await conn.fetchval("""
                SELECT name FROM exercises
                WHERE theme_name = $1 
                  AND exercise_type = 'T'
                  AND $2 = ANY(visibility)
                ORDER BY pos LIMIT 1
            """, theme_name, user_access_level)

            result.update({
                "firstQExercise": first_q,
                "firstTestExercise": first_test,
            })


        # --- Упражнение Q -----------------------------------------------
        if ex_type == "Q":
            q_exercises = await conn.fetch("""
                SELECT name, title, pos, exercise_type
                FROM exercises
                WHERE theme_name = $1
                  AND exercise_type = 'Q'
                  AND $2 = ANY(visibility)
                ORDER BY pos
            """, theme_name, user_access_level)

            current_index = next((i for i, ex in enumerate(q_exercises) if ex["name"] == exercise_name), 0)

            first_test = await conn.fetchval("""
                SELECT name FROM exercises
                WHERE theme_name = $1 
                  AND exercise_type = 'T'
                  AND $2 = ANY(visibility)
                ORDER BY pos LIMIT 1
            """, theme_name, user_access_level)

            result.update({
                "variants": parse_variants(row["variants"]) if row["variants"] else [],
                "correctCombinations": parse_variants(row["answers"]) if row["answers"] else [],
                "slots_count": len(parse_variants(row["variants"]) if row["variants"] else []),
                "themeQExercises": [dict(ex) for ex in q_exercises],
                "currentQIndex": current_index,
                "firstTestExercise": first_test
            })

        # --- Упражнение T --------------------------------------------------
        elif ex_type == "T":
            t_exercises = await conn.fetch("""
                SELECT name, title, pos, exercise_type
                FROM exercises
                WHERE theme_name = $1
                  AND exercise_type = 'T'
                  AND $2 = ANY(visibility)
                ORDER BY pos
            """, theme_name, user_access_level)

            current_t_index = next((i for i, ex in enumerate(t_exercises) if ex["name"] == exercise_name), 0)

            # Следующая тема → первое V (или первое упражнение)
            next_theme_row = await conn.fetchrow("""
                SELECT name FROM themes
                WHERE lesson_name = (
                    SELECT lesson_name FROM themes WHERE name = $1
                )
                  AND pos > (SELECT pos FROM themes WHERE name = $1)
                ORDER BY pos LIMIT 1
            """, theme_name)

            next_theme_first_v = None
            if next_theme_row:
                next_theme_first_v = await conn.fetchval("""
                    SELECT name FROM exercises
                    WHERE theme_name = $1
                      AND exercise_type = 'V'
                      AND $2 = ANY(visibility)
                    ORDER BY pos LIMIT 1
                """, next_theme_row["name"], user_access_level)

            # Первое упражнение текущей темы (для ретейка)
            current_theme_first = await conn.fetchval("""
                SELECT name FROM exercises
                WHERE theme_name = $1 AND $2 = ANY(visibility)
                ORDER BY 
                    CASE exercise_type WHEN 'V' THEN 1 WHEN 'Q' THEN 2 WHEN 'T' THEN 3 ELSE 4 END, pos
                LIMIT 1
            """, theme_name, user_access_level)

            result.update({
                "variants": parse_variants(row["variants"]) if row["variants"] else [],
                "correctCombinations": parse_variants(row["answers"]) if row["answers"] else [],
                "themeTExercises": [dict(ex) for ex in t_exercises],
                "currentTIndex": current_t_index,
                "nextThemeFirstExercise": next_theme_first_v,
                "currentThemeFirstExercise": current_theme_first,
                "mistakesAllowed": 3,
            })

        return result


@router.post("/api/user/current-exercise")
async def save_current_exercise(
    pool: DBPoolDep,
    current_user: CurrentUser,
    exercise_name: str = Body(..., embed=True),
):

    if not current_user:
        return {"success": False}

    try:
        # обновляем только если pos нового > pos старого упражнения
        await pool.execute("""
            UPDATE users
                SET current_exercise = $1
                WHERE id = $2
                  AND (
                      current_exercise IS NULL
                      OR (
                          SELECT pos FROM exercises WHERE name = $1
                      ) > (
                          SELECT pos FROM exercises WHERE name = users.current_exercise
                      )
                  )
        """, exercise_name, current_user["user_id"])
        return {"success": True}

    except Exception as e:
        print(f"[BACKEND] ОШИБКА при UPDATE: {e}")
        return {"success": False, "error": str(e)}


@router.get("/api/theme/{theme_name}/exercises")
async def get_theme_block_starters(
    theme_name: str,
    pool: DBPoolDep,
    current_user: CurrentUser,
):
    """
    Возвращает первые упражнения каждого блока (V, Q, T),
    которые доступны пользователю.
    """
    user_access_level = current_user.get("access_level", 0) if current_user else 0

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT name
            FROM (
                SELECT DISTINCT ON (exercise_type)
                       name,
                       pos
                FROM exercises
                WHERE theme_name = $1 
                  AND $2 = ANY(visibility)
                ORDER BY exercise_type, pos
            ) t
            ORDER BY pos;
            """, theme_name, user_access_level)
        print([row["name"] for row in rows])
        return [row["name"] for row in rows]  # ['BGRUA1002_V001', 'BGRUA1002_Q001', 'BGRUA1002_T001']


@router.get("/api/user/access-level")
async def get_user_access_level(current_user: CurrentUser):
    """
    Возвращает текущий уровень доступа пользователя.
    Используется для проверки, не истёк ли доступ.
    """
    if not current_user:
        return {"accessLevel": 0}

    return {
        "accessLevel": current_user.get("access_level", 0),
        # Можно также вернуть дату окончания подписки, если нужно
        # "subscriptionEnd": current_user.get("subscription_end")
    }


@router.get("/api/theme/{theme_name}/first-video")
async def get_first_video_of_theme(
    theme_name: str,
    pool: DBPoolDep,
    current_user: CurrentUser
):
    user_access_level = current_user.get("access_level", 0) if current_user else 0

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT name 
            FROM exercises 
            WHERE theme_name = $1 
              AND exercise_type = 'V'
              AND $2 = ANY(visibility)
            ORDER BY pos 
            LIMIT 1
        """, theme_name, user_access_level)

        if row:
            return {"exercise_name": row["name"]}
        return {"exercise_name": None}
