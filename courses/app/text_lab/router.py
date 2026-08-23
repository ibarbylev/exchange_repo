"""
router.py — FastAPI-роутер лаборатории текстовых упражнений.

Подключение:

    from app.text_lab.router import router as text_lab_router
    # или
    from courses.app.text_lab.router import router as text_lab_router

    app.include_router(text_lab_router)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.routers.deps import LangDep, render_template
from . import storage

router = APIRouter(tags=["text-lab"])


# ---------------------------------------------------------------------------
# HTML-страницы
# ---------------------------------------------------------------------------

@router.get("/{source_lang}/{ui_lang}/text-lab/play", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/text-lab/play/", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/text-lab/", response_class=HTMLResponse)
async def text_lab_play(
    request: Request,
    lang_pair: LangDep,
):
    """
    Главная страница лаборатории + плеер.
    Слева — список упражнений, справа — плеер.
    """
    block = storage.get_block_info()
    exercises = storage.list_exercises()

    return render_template(
        name="text_lab/play.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "text_lab_play",
            "text_block": block,
            "exercises": exercises,
            "audio_url": block.get("audio_url"),
        },
    )


@router.get("/{source_lang}/{ui_lang}/text-lab/add", response_class=HTMLResponse)
@router.get("/{source_lang}/{ui_lang}/text-lab/add/", response_class=HTMLResponse)
async def text_lab_add(
    request: Request,
    lang_pair: LangDep,
):
    """Страница добавления нового упражнения."""
    block = storage.get_block_info()

    return render_template(
        name="text_lab/add.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "text_lab_add",
            "text_block": block,
        },
    )


# ---------------------------------------------------------------------------
# Минимальный API (только для сохранения из формы add)
# ---------------------------------------------------------------------------

@router.post("/{source_lang}/{ui_lang}/text-lab/api/exercises")
async def text_lab_add_exercise(
    request: Request,
    lang_pair: LangDep,
):
    """Добавление упражнения из формы / JS."""
    from fastapi import HTTPException

    payload = await request.json()
    required = {"type", "title"}
    if not required.issubset(payload.keys()):
        raise HTTPException(status_code=400, detail="Обязательные поля: type, title")

    try:
        created = storage.add_exercise(payload)
        return {"success": True, "exercise": created}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
