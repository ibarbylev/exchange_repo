"""Плеер classroom: отдаёт один HTML на два слота.

GET /api/classroom/player
    theme     код темы
    view      lobby | exercise
    player    vqt | text_intensive   (если не задан — из темы)
    exercise  имя упражнения (необязательно)

Ответ:
    {
      "html": "<div data-slot=sticky>...</div><div data-slot=content>...</div>",
      "meta": { "player": "vqt"|"text_intensive", "theme_name": "...", "view": "..." }
    }

Тема с player='text_intensive' (или kind='listening') открывает текстовый интенсив,
остальные — существующий контур V/Q/T.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()

PLAYERS = {
    "vqt": "classroom/player_vqt.html",
    "text_intensive": "classroom/player_text_intensive.html",
}


def resolve_templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is not None:
        return templates
    return Jinja2Templates(directory="courses/app/templates")


def theme_heading(theme_name: str) -> str:
    match = re.match(r"A(\d)(\d{2,3})_H(\d+)", theme_name or "")
    if not match:
        return theme_name
    return f"Курс A{match.group(1)}, Урок {match.group(2).zfill(2)}, Тема {match.group(3).zfill(2)}:"


def has_tariff_restrictions(exercise_counts: dict | None, access_level: int) -> bool:
    if not exercise_counts:
        return False
    for counts in exercise_counts.values():
        if not isinstance(counts, (list, tuple)) or len(counts) < 3:
            continue
        if (counts[access_level] if access_level < len(counts) else 0) < max(counts):
            return True
    return False


def detect_player(theme: dict, requested: str | None) -> str:
    if requested in PLAYERS:
        return requested
    for key in ("player", "kind", "lab"):
        value = str(theme.get(key) or "").lower()
        if value in PLAYERS:
            return value
        if value in {"listening", "textlab", "text_lab", "intensive"}:
            return "text_intensive"
    return "vqt"


# Замените на реальные сервисы проекта.
def load_theme(theme_name: str) -> dict | None:
    raise NotImplementedError("Подключите загрузку темы из дерева / БД")


def load_exercise(exercise_name: str) -> dict | None:
    return None


def current_access_level(request: Request) -> int:
    user = getattr(request.state, "user", None)
    return int(getattr(user, "access_level", 0) or 0) if user else 0


def courses_url_for(request: Request) -> str:
    source = getattr(request.state, "source_lang", "bg")
    ui = getattr(request.state, "ui_lang", "ru")
    return f"/{source}/{ui}/courses/"


@router.get("/api/classroom/player")
async def classroom_player(
    request: Request,
    theme: str,
    view: str = "lobby",
    player: str | None = None,
    exercise: str | None = None,
):
    try:
        theme_data = load_theme(theme) or {}
    except NotImplementedError as exc:
        return JSONResponse({"error": str(exc)}, status_code=501)

    theme_data.setdefault("name", theme)
    mode = detect_player(theme_data, player)
    exercise_data = load_exercise(exercise) if exercise else None
    access_level = current_access_level(request)

    context: dict[str, Any] = {
        "view": view,
        "theme_name": theme,
        "theme_title": theme_data.get("title") or "",
        "theme_heading": theme_heading(theme),
        "exercise_counts": theme_data.get("exercise_counts") or {},
        "is_available": theme_data.get("is_available", True),
        "is_completed": theme_data.get("is_completed", False),
        "is_current": theme_data.get("is_current", False),
        "has_tariff_restrictions": has_tariff_restrictions(
            theme_data.get("exercise_counts"), access_level
        ),
        "courses_url": courses_url_for(request),
        "exercise": exercise_data,
        "audio_url": (exercise_data or {}).get("audio_url") or theme_data.get("audio_url") or "",
        "task_type": (exercise_data or {}).get("task_type") or "",
        "prompt": (exercise_data or {}).get("prompt") or "",
    }

    html = resolve_templates(request).get_template(PLAYERS[mode]).render(**context)
    return JSONResponse({
        "html": html,
        "meta": {
            "player": mode,
            "theme_name": theme,
            "view": view,
            "exercise_name": exercise,
        },
    })
