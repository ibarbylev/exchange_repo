"""
Роутер classroom: страница, API упражнений и плеер после рефакторинга.

Новый контракт плеера
---------------------
GET /api/classroom/player
    theme, view=lobby|exercise, player=vqt|text_intensive, exercise?

Ответ: {html, meta}

Оболочка classroom.html вставляет html в #stickySlot / #contentSlot.
Плеер V/Q/T затем ходит в уже существующие:
    GET  /api/theme/{theme}/exercises
    GET  /api/exercise/{name}
    POST /api/user/current-exercise

Если эти три маршрута у Вас уже реализованы в этом файле —
оставьте свои функции и не дублируйте заглушки ниже.
Блок PLAYER использует те же хелперы.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


router = APIRouter()

PLAYERS = {
    "vqt": "classroom/player_vqt.html",
    "text_intensive": "classroom/player_text_intensive.html",
}


# ---------------------------------------------------------------------------
# Шаблоны / утилиты
# ---------------------------------------------------------------------------

def resolve_templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is not None:
        return templates
    return Jinja2Templates(directory="courses/app/templates")


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
    for key in ("player", "kind", "lab"):
        value = str(theme.get(key) or "").lower()
        if value in PLAYERS:
            return value
        if value in {"listening", "textlab", "text_lab", "intensive"}:
            return "text_intensive"
    return "vqt"


def courses_url_for(request: Request) -> str:
    source = (
        getattr(request.state, "source_lang", None)
        or request.path_params.get("source_lang")
        or "bg"
    )
    ui = (
        getattr(request.state, "ui_lang", None)
        or request.path_params.get("ui_lang")
        or "ru"
    )
    return f"/{source}/{ui}/courses/"


def current_user_meta(request: Request) -> tuple[int, str]:
    user = getattr(request.state, "user", None)
    if user is None:
        return 0, "student"
    level = getattr(user, "access_level", 0) or 0
    role = getattr(user, "role", "student") or "student"
    return int(level), str(role)


def find_theme_in_tree(tree: dict | None, theme_name: str) -> dict | None:
    if not tree:
        return None
    for course in tree.get("courses") or []:
        for lesson in course.get("lessons") or []:
            for theme in lesson.get("themes") or []:
                if theme.get("name") == theme_name:
                    return theme
    return None


# ---------------------------------------------------------------------------
# Доступ к данным.
# Сначала пробуем функции, которые уже могут быть в этом модуле
# (Ваш готовый classroom.py). Иначе — дерево со страницы / request.state.
# ---------------------------------------------------------------------------

def load_theme(theme_name: str, request: Request | None = None) -> dict | None:
    for name in ("get_theme", "get_theme_by_name", "fetch_theme"):
        fn = globals().get(name)
        if callable(fn) and fn is not load_theme:
            try:
                data = fn(theme_name)
                if data:
                    return dict(data)
            except TypeError:
                continue

    tree = None
    if request is not None:
        tree = getattr(request.state, "tree", None) or getattr(request.app.state, "tree", None)
    if tree:
        found = find_theme_in_tree(tree, theme_name)
        if found:
            found = dict(found)
            found.setdefault("name", theme_name)
            return found

    return {"name": theme_name, "title": theme_name, "exercise_counts": {}}


def load_exercise(exercise_name: str, show_correct: bool = False) -> dict | None:
    for name in ("get_exercise", "get_exercise_payload", "fetch_exercise"):
        fn = globals().get(name)
        if callable(fn) and fn is not load_exercise:
            try:
                data = fn(exercise_name, show_correct)
            except TypeError:
                try:
                    data = fn(exercise_name)
                except TypeError:
                    continue
            if data:
                payload = dict(data)
                payload.setdefault("name", exercise_name)
                return payload
    return None


def load_block_starters(theme_name: str) -> list[str]:
    for name in ("get_theme_exercises", "get_theme_block_starters", "fetch_theme_exercises"):
        fn = globals().get(name)
        if callable(fn) and fn is not load_block_starters:
            try:
                data = fn(theme_name)
                if data:
                    return list(data)
            except TypeError:
                continue
    return []


# ---------------------------------------------------------------------------
# Сборка HTML плеера
# ---------------------------------------------------------------------------

def build_player_html(
    request: Request,
    theme: dict,
    view: str,
    player: str,
    exercise: dict | None,
) -> str:
    access_level, _role = current_user_meta(request)
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
        "courses_url": courses_url_for(request),
        "exercise": exercise,
        "audio_url": (exercise or {}).get("audio_url") or theme.get("audio_url") or "",
        "task_type": (exercise or {}).get("task_type") or "",
        "prompt": (exercise or {}).get("prompt") or (exercise or {}).get("question") or "",
    }
    template_name = PLAYERS.get(player, PLAYERS["vqt"])
    return resolve_templates(request).get_template(template_name).render(**context)


# ---------------------------------------------------------------------------
# PLAYER
# ---------------------------------------------------------------------------

@router.get("/api/classroom/player")
async def classroom_player(
    request: Request,
    theme: str,
    view: str = "lobby",
    player: str | None = None,
    exercise: str | None = None,
):
    theme_data = load_theme(theme, request) or {"name": theme}
    theme_data.setdefault("name", theme)
    mode = detect_player(theme_data, player)

    exercise_data = None
    if exercise:
        exercise_data = load_exercise(exercise)
        if exercise_data:
            exercise_data.setdefault("name", exercise)

    html = build_player_html(request, theme_data, view, mode, exercise_data)
    return JSONResponse({
        "html": html,
        "meta": {
            "player": mode,
            "theme_name": theme,
            "view": view,
            "exercise_name": exercise,
            "exercise": exercise_data,
        },
    })


# ---------------------------------------------------------------------------
# API, которые вызывает плеер V/Q/T после входа в тему.
# Если одноимённые маршруты уже есть в Вашем classroom.py — удалите заглушки.
# ---------------------------------------------------------------------------

@router.get("/api/theme/{theme_name}/exercises")
async def theme_exercises(theme_name: str):
    starters = load_block_starters(theme_name)
    return JSONResponse(starters)


@router.get("/api/exercise/{exercise_name}")
async def exercise_payload(exercise_name: str, show_correct: int = 0):
    data = load_exercise(exercise_name, show_correct=bool(show_correct))
    if not data:
        return JSONResponse({"error": f"Упражнение не найдено: {exercise_name}"}, status_code=404)
    return JSONResponse(data)


@router.get("/api/theme/{theme_name}/text-intensive")
async def theme_text_intensive(theme_name: str):
    theme = load_theme(theme_name) or {"name": theme_name}
    return JSONResponse({
        "theme_name": theme_name,
        "task_type": theme.get("task_type") or "true_false",
        "prompt": theme.get("prompt") or theme.get("title") or "",
        "audio_url": theme.get("audio_url") or "",
        "choices": theme.get("choices") or [],
        "items": theme.get("items") or [],
    })


class CurrentExerciseBody(BaseModel):
    exercise_name: str


@router.post("/api/user/current-exercise")
async def save_current_exercise(body: CurrentExerciseBody, request: Request):
    saver = globals().get("save_user_current_exercise")
    if callable(saver):
        saver(request, body.exercise_name)
    return JSONResponse({"ok": True, "exercise_name": body.exercise_name})
