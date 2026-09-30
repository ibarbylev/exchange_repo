"""Страница коуча и совместный HTML-урок по WebSocket.

Подключение в приложении:

    from app.routers import coach
    app.include_router(coach.router)
"""

from __future__ import annotations

import json
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.config import DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE
from app.db.dependencies import CurrentUser, DBPoolDep, RequiredUser
from app.middleware.csrf import verify_csrf
from app.repositories import coach_session
from app.repositories.intensive import checkbox_ids, load_html_block
from app.routers.deps import LangDep, render_template


router = APIRouter(tags=["coach"])

WS_TICKET_TTL = timedelta(hours=4)


class LessonHub:
    def __init__(self) -> None:
        self.rooms: dict[int, set[WebSocket]] = defaultdict(set)

    async def join(self, lesson_id: int, websocket: WebSocket) -> None:
        self.rooms[lesson_id].add(websocket)

    def leave(self, lesson_id: int, websocket: WebSocket) -> None:
        peers = self.rooms.get(lesson_id)
        if not peers:
            return
        peers.discard(websocket)
        if not peers:
            self.rooms.pop(lesson_id, None)

    async def broadcast(self, lesson_id: int, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for peer in list(self.rooms.get(lesson_id) or []):
            try:
                await peer.send_json(payload)
            except Exception:
                dead.append(peer)
        for peer in dead:
            self.leave(lesson_id, peer)


hub = LessonHub()
_tickets: dict[str, dict[str, Any]] = {}


def _login_url() -> str:
    return f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/"


def _issue_ticket(user: dict, lesson_id: int, role: str) -> str:
    token = secrets.token_urlsafe(24)
    _tickets[token] = {
        "user_id": coach_session.user_pk(user),
        "role": role,
        "lesson_id": lesson_id,
        "exp": datetime.now(timezone.utc) + WS_TICKET_TTL,
    }
    return token


def _pop_expired_tickets() -> None:
    now = datetime.now(timezone.utc)
    for key in [key for key, item in _tickets.items() if item.get("exp") and item["exp"] < now]:
        _tickets.pop(key, None)


def _ticket_payload(token: str | None) -> dict[str, Any] | None:
    _pop_expired_tickets()
    if not token:
        return None
    return _tickets.get(token)


def viewer_role(user: dict | None, lesson: dict) -> str:
    if coach_session.can_run_lessons(user) and (
        user.get("is_superuser")
        or user.get("role") == "admin"
        or coach_session.user_pk(user) == lesson.get("coach_id")
    ):
        return "coach"
    return "student"


async def _load_projector(lesson: dict) -> tuple[str, list[str]]:
    file_name = lesson.get("file_name") or lesson.get("lesson_name")
    if not file_name:
        raise FileNotFoundError("У занятия нет file_name")
    html = load_html_block(file_name)
    return html, checkbox_ids(html)


@router.get("/{source_lang}/{ui_lang}/coach/", response_class=HTMLResponse)
async def coach_hub(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: RequiredUser,
    student_id: int | None = None,
):
    if not coach_session.can_run_lessons(current_user):
        raise HTTPException(status_code=403, detail="Страница доступна только коучу")

    coach_id = coach_session.user_pk(current_user)
    admin = bool(current_user.get("is_superuser") or current_user.get("role") == "admin")
    async with pool.acquire() as conn:
        students = await coach_session.list_coach_students(conn, coach_id, admin=admin)
        queue: list[dict] = []
        current = None
        if student_id:
            if not any(item["id"] == student_id for item in students):
                student_id = None
            else:
                queue = await coach_session.list_student_queue(
                    conn,
                    student_id,
                    coach_id=None if admin else coach_id,
                )
                current = coach_session.current_lesson(queue)

    return render_template(
        request=request,
        name="coach/hub.html",
        lang_pair=lang_pair,
        context={
            "current_page": "coach",
            "students": students,
            "student_id": student_id,
            "queue": queue,
            "current": current,
        },
    )


@router.get("/{source_lang}/{ui_lang}/coach/lesson/{lesson_id}", response_class=HTMLResponse)
async def coach_lesson_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: RequiredUser,
    lesson_id: int,
):
    if not coach_session.can_run_lessons(current_user):
        raise HTTPException(status_code=403, detail="Страница доступна только коучу")

    async with pool.acquire() as conn:
        lesson = await coach_session.get_lesson(conn, lesson_id)
        if not lesson:
            raise HTTPException(status_code=404, detail="Занятие не найдено")
        if not coach_session.lesson_accessible(lesson, current_user):
            raise HTTPException(status_code=403, detail="Это занятие другого коуча")
        queue = await coach_session.list_student_queue(
            conn,
            lesson["student_id"],
            coach_id=lesson.get("coach_id"),
        )
        current = coach_session.current_lesson(queue)
        if not lesson["is_completed"]:
            if not current or current["id"] != lesson["id"]:
                return RedirectResponse(
                    url=(
                        f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/coach/"
                        f"?student_id={lesson['student_id']}"
                    ),
                    status_code=303,
                )
            lesson = await coach_session.mark_in_progress(conn, lesson_id)

    try:
        projector_html, order = await _load_projector(lesson)
    except FileNotFoundError as exc:
        projector_html, order = f"<p class='text-danger'>{exc}</p>", []

    completed = bool(lesson.get("is_completed"))
    ticket = "" if completed else _issue_ticket(current_user, lesson_id, "coach")
    return render_template(
        request=request,
        name="coach/lesson.html",
        lang_pair=lang_pair,
        context={
            "current_page": "coach",
            "lesson": lesson,
            "queue": queue,
            "projector_html": projector_html,
            "checkbox_ids": order,
            "open_checkboxes": order if completed else (lesson.get("open_checkboxes") or []),
            "ws_ticket": ticket,
            "viewer_role": "coach",
        },
    )


@router.post("/{source_lang}/{ui_lang}/coach/lesson/{lesson_id}/complete")
async def complete_lesson(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: RequiredUser,
    lesson_id: int,
    _=Depends(verify_csrf),
):
    if not coach_session.can_run_lessons(current_user):
        raise HTTPException(status_code=403, detail="Только коуч может закрыть урок")

    async with pool.acquire() as conn:
        lesson = await coach_session.get_lesson(conn, lesson_id)
        if not lesson:
            raise HTTPException(status_code=404, detail="Занятие не найдено")
        if not coach_session.lesson_accessible(lesson, current_user):
            raise HTTPException(status_code=403, detail="Это занятие другого коуча")
        all_ids: list[str] = []
        try:
            _html, all_ids = await _load_projector(lesson)
        except FileNotFoundError:
            all_ids = list(lesson.get("open_checkboxes") or [])
        lesson = await coach_session.mark_completed(conn, lesson_id, all_ids)

    await hub.broadcast(lesson_id, {
        "type": "completed",
        "lesson_id": lesson_id,
        "status": lesson["status"],
        "all_open": True,
        "open_checkboxes": lesson.get("open_checkboxes") or all_ids,
    })
    return RedirectResponse(
        url=(
            f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/coach/"
            f"?student_id={lesson['student_id']}"
        ),
        status_code=303,
    )


@router.get("/api/classroom/coach/{block_name}")
async def student_coach_block(
    block_name: str,
    pool: DBPoolDep,
    current_user: CurrentUser,
):
    """Данные совместного урока для плеера студента."""
    if not current_user:
        return JSONResponse({"error": "auth required"}, status_code=401)

    if coach_session.can_run_lessons(current_user):
        return JSONResponse({
            "error": "open_coach_page",
            "message": "Коуч открывает урок со своей страницы.",
            "hub": f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/coach/",
        }, status_code=409)

    user_id = coach_session.user_pk(current_user)
    async with pool.acquire() as conn:
        lesson = await coach_session.find_student_lesson_for_block(conn, user_id, block_name)
        if not lesson:
            return JSONResponse(
                {"error": "no_lesson", "message": "Урок ещё не назначен"},
                status_code=404,
            )
        queue = await coach_session.list_student_queue(
            conn,
            user_id,
            coach_id=lesson.get("coach_id"),
        )
        if not coach_session.student_may_view(lesson, queue):
            return JSONResponse(
                {"error": "locked", "message": "Сначала нужно пройти предыдущий урок с коучем"},
                status_code=403,
            )

    try:
        projector_html, order = await _load_projector(lesson)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    role = viewer_role(current_user, lesson)
    completed = bool(lesson.get("is_completed"))
    ticket = "" if completed else _issue_ticket(current_user, lesson["id"], role)
    return {
        "lesson_id": lesson["id"],
        "lesson_name": lesson.get("lesson_name"),
        "title": lesson.get("lesson_title"),
        "file_name": lesson.get("file_name"),
        "status": lesson.get("status"),
        "open_checkboxes": (
            order if completed
            else (lesson.get("open_checkboxes") or [])
        ),
        "all_open": completed,
        "checkbox_ids": order,
        "html": projector_html,
        "role": role,
        "ws_ticket": ticket,
        "student_name": lesson.get("student_name"),
        "coach_name": lesson.get("coach_name"),
    }


@router.get("/api/coach/ws-ticket/{lesson_id}")
async def refresh_ws_ticket(
    lesson_id: int,
    pool: DBPoolDep,
    current_user: CurrentUser,
):
    """Новый RAM-ticket для переподключения сокета без перезагрузки страницы.

    Старый ticket после рестарта uvicorn в словаре процесса отсутствует.
    Клиент не должен долбить мёртвым билетом: сначала этот эндпоинт, потом WS.
    """
    if not current_user:
        return JSONResponse({"error": "auth required"}, status_code=401)

    async with pool.acquire() as conn:
        lesson = await coach_session.get_lesson(conn, lesson_id)
        if not lesson:
            return JSONResponse({"error": "no_lesson", "message": "Занятие не найдено"}, status_code=404)
        if not coach_session.lesson_accessible(lesson, current_user):
            return JSONResponse({"error": "forbidden", "message": "Нет доступа к занятию"}, status_code=403)
        if lesson.get("is_completed"):
            return JSONResponse(
                {"error": "completed", "message": "Урок уже закрыт"},
                status_code=409,
            )
        if not coach_session.can_run_lessons(current_user):
            queue = await coach_session.list_student_queue(
                conn,
                coach_session.user_pk(current_user),
                coach_id=lesson.get("coach_id"),
            )
            if not coach_session.student_may_view(lesson, queue):
                return JSONResponse(
                    {"error": "locked", "message": "Сначала нужно пройти предыдущий урок с коучем"},
                    status_code=403,
                )

    role = viewer_role(current_user, lesson)
    token = _issue_ticket(current_user, lesson_id, role)
    return {
        "ticket": token,
        "lesson_id": lesson_id,
        "role": role,
    }


def _ws_pool(websocket: WebSocket):
    """Пул БД нельзя брать через DBPoolDep: get_db_pool() требует Request.

    У WebSocket нет Request, поэтому FastAPI вызывает get_db_pool() без аргументов
    и падает. Берём тот же объект с app.state, не вызывая get_db_pool:
    отсутствие db_pool даёт AttributeError, а не TypeError, и хендлер
    молча умирает — тесты видят только таймаут ожидания сообщения.
    """
    app_obj = getattr(websocket, "app", None)
    state = getattr(app_obj, "state", None) if app_obj is not None else None
    if state is not None:
        for name in ("db_pool", "pool", "pg_pool"):
            pool = getattr(state, name, None)
            if pool is not None:
                return pool
    raise RuntimeError("Пул БД не найден на app.state (ожидались db_pool/pool/pg_pool)")


@router.websocket("/api/coach/ws/{lesson_id}")
async def coach_lesson_ws(websocket: WebSocket, lesson_id: int):
    ticket = websocket.query_params.get("ticket")
    payload = _ticket_payload(ticket)
    if not payload or payload.get("lesson_id") != lesson_id:
        await websocket.close(code=4401)
        return

    pool = _ws_pool(websocket)
    async with pool.acquire() as conn:
        lesson = await coach_session.get_lesson(conn, lesson_id)
    if not lesson:
        await websocket.close(code=4404)
        return
    if lesson.get("is_completed"):
        # Закрытый урок статичен: сокет не принимаем, в комнату не сажаем.
        await websocket.close(code=4403)
        return

    await websocket.accept()
    await hub.join(lesson_id, websocket)

    open_ids = lesson.get("open_checkboxes") or []
    await websocket.send_json({
        "type": "state",
        "lesson_id": lesson_id,
        "status": lesson.get("status"),
        "all_open": False,
        "open_checkboxes": open_ids,
        "role": payload.get("role"),
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if kind != "set_checkbox":
                continue
            if payload.get("role") != "coach":
                await websocket.send_json({"type": "error", "message": "Только коуч открывает ответы"})
                continue
            checkbox_id = str(message.get("id") or "").strip()
            opened = bool(message.get("open", True))
            if not checkbox_id:
                await websocket.send_json({"type": "error", "message": "Пустой id чекбокса"})
                continue
            async with pool.acquire() as conn:
                current = await coach_session.get_lesson(conn, lesson_id)
                if not current:
                    await websocket.send_json({"type": "error", "message": "Занятие не найдено"})
                    continue
                if current.get("is_completed"):
                    await websocket.send_json({"type": "error", "message": "Урок уже закрыт"})
                    continue
                try:
                    _html, order = await _load_projector(current)
                except FileNotFoundError:
                    order = []
                if order and checkbox_id not in order:
                    await websocket.send_json({"type": "error", "message": "Такого чекбокса нет в уроке"})
                    continue
                updated = await coach_session.set_checkbox(conn, lesson_id, checkbox_id, opened)
            await hub.broadcast(lesson_id, {
                "type": "state",
                "lesson_id": lesson_id,
                "status": updated.get("status"),
                "open_checkboxes": updated.get("open_checkboxes") or [],
            })
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(lesson_id, websocket)
