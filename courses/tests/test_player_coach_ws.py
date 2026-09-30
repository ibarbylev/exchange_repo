"""WebSocket совместного урока коуча: /api/coach/ws/{lesson_id}.

Не используем Starlette TestClient.websocket_connect — у тестов уже
async-цикл pytest-asyncio + httpx ASGITransport, а TestClient поднимает
свой loop и ломает фикстуры БД.

Вместо этого крутим тот же handler `coach_lesson_ws` на фейковом сокете:
тикет берём из `_issue_ticket`, пул — с app.state.db_pool (как в проде).
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from fastapi import WebSocketDisconnect

from app.main import app
from app.routers import coach as coach_mod
from app.routers.coach import _issue_ticket, coach_lesson_ws, hub

from .conftest import paid_private_lesson_item
from .test_player_coach import FAKE_HTML, _add_lesson, _make_c_course


class FakeWebSocket:
    """Минимальный WS, которого хватает handler'у coach_lesson_ws."""

    def __init__(self, ticket: str | None):
        self.app = app
        self.query_params = {"ticket": ticket or ""}
        self.sent: list[dict] = []
        self.closed: int | None = None
        self.accepted = False
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return item

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        await self._incoming.put(None)

    async def push(self, payload: dict | None) -> None:
        if payload is None:
            await self._incoming.put(None)
            return
        await self._incoming.put(json.dumps(payload, ensure_ascii=False))


async def _wait_until(predicate, timeout: float = 1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("истекло ожидание сообщения WebSocket")


async def _run_ws(ws: FakeWebSocket, lesson_id: int) -> asyncio.Task:
    return asyncio.create_task(coach_lesson_ws(ws, lesson_id))


@pytest.fixture(autouse=True)
def _isolate_ws_state():
    hub.rooms.clear()
    coach_mod._tickets.clear()
    yield
    hub.rooms.clear()
    coach_mod._tickets.clear()


@pytest.fixture
async def live_lesson(db_pool, create_user):
    course = await _make_c_course(db_pool, n_blocks=1)
    student_id = await create_user(role="student")
    coach_id = await create_user(role="coach")
    item_id = await paid_private_lesson_item(db_pool, student_id)
    lesson_id = await _add_lesson(
        db_pool,
        student_id=student_id,
        coach_id=coach_id,
        order_item_id=item_id,
        lesson_name=course["first"],
        status="in_progress",
        open_checkboxes=["intro"],
    )
    return {
        "course": course,
        "student_id": student_id,
        "coach_id": coach_id,
        "lesson_id": lesson_id,
    }


def _ticket(user_id: int, lesson_id: int, role: str) -> str:
    return _issue_ticket({"id": user_id, "role": role}, lesson_id, role)


class TestCoachWebsocketAuth:
    @pytest.mark.asyncio
    async def test_missing_ticket_closes_4401(self, live_lesson):
        ws = FakeWebSocket(None)
        task = await _run_ws(ws, live_lesson["lesson_id"])
        await _wait_until(lambda: ws.closed == 4401)
        assert ws.accepted is False
        await task

    @pytest.mark.asyncio
    async def test_ticket_from_another_lesson_closes_4401(self, live_lesson):
        ticket = _ticket(live_lesson["coach_id"], live_lesson["lesson_id"] + 999, "coach")
        ws = FakeWebSocket(ticket)
        task = await _run_ws(ws, live_lesson["lesson_id"])
        await _wait_until(lambda: ws.closed == 4401)
        await task


class TestCoachWebsocketProtocol:
    @pytest.mark.asyncio
    async def test_connect_sends_state(self, live_lesson):
        lesson_id = live_lesson["lesson_id"]
        ticket = _ticket(live_lesson["student_id"], lesson_id, "student")
        ws = FakeWebSocket(ticket)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            await ws.push(None)
            await task
        assert ws.accepted is True
        first = ws.sent[0]
        assert first["type"] == "state"
        assert first["lesson_id"] == lesson_id
        assert first["role"] == "student"
        assert first["all_open"] is False
        assert first["open_checkboxes"] == ["intro"]

    @pytest.mark.asyncio
    async def test_ping_pong(self, live_lesson):
        lesson_id = live_lesson["lesson_id"]
        ticket = _ticket(live_lesson["student_id"], lesson_id, "student")
        ws = FakeWebSocket(ticket)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            await ws.push({"type": "ping"})
            await _wait_until(lambda: any(item.get("type") == "pong" for item in ws.sent))
            await ws.push(None)
            await task
        assert {"type": "pong"} in ws.sent

    @pytest.mark.asyncio
    async def test_student_cannot_open_checkbox(self, live_lesson, db_pool):
        lesson_id = live_lesson["lesson_id"]
        ticket = _ticket(live_lesson["student_id"], lesson_id, "student")
        ws = FakeWebSocket(ticket)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            await ws.push({"type": "set_checkbox", "id": "q1", "open": True})
            await _wait_until(lambda: any(item.get("type") == "error" for item in ws.sent))
            await ws.push(None)
            await task
        err = next(item for item in ws.sent if item.get("type") == "error")
        assert "Только коуч" in err["message"]
        raw = await db_pool.fetchval(
            "SELECT open_checkboxes FROM coach_lessons WHERE id = $1", lesson_id
        )
        ids = raw if isinstance(raw, list) else json.loads(raw)
        assert ids == ["intro"]

    @pytest.mark.asyncio
    async def test_coach_set_checkbox_broadcasts_to_student(self, live_lesson, db_pool):
        lesson_id = live_lesson["lesson_id"]
        coach_ws = FakeWebSocket(_ticket(live_lesson["coach_id"], lesson_id, "coach"))
        student_ws = FakeWebSocket(_ticket(live_lesson["student_id"], lesson_id, "student"))
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            coach_task = await _run_ws(coach_ws, lesson_id)
            student_task = await _run_ws(student_ws, lesson_id)
            await _wait_until(lambda: coach_ws.sent and student_ws.sent)
            await coach_ws.push({"type": "set_checkbox", "id": "q1", "open": True})
            await _wait_until(
                lambda: any(
                    item.get("type") == "state" and "q1" in (item.get("open_checkboxes") or [])
                    for item in student_ws.sent
                )
            )
            await coach_ws.push(None)
            await student_ws.push(None)
            await coach_task
            await student_task

        raw = await db_pool.fetchval(
            "SELECT open_checkboxes FROM coach_lessons WHERE id = $1", lesson_id
        )
        ids = raw if isinstance(raw, list) else json.loads(raw)
        assert ids == ["intro", "q1"]
        student_states = [item for item in student_ws.sent if item.get("type") == "state"]
        assert student_states[-1]["open_checkboxes"] == ["intro", "q1"]

    @pytest.mark.asyncio
    async def test_unknown_checkbox_is_rejected(self, live_lesson):
        lesson_id = live_lesson["lesson_id"]
        ws = FakeWebSocket(_ticket(live_lesson["coach_id"], lesson_id, "coach"))
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            await ws.push({"type": "set_checkbox", "id": "no-such", "open": True})
            await _wait_until(lambda: any(item.get("type") == "error" for item in ws.sent))
            await ws.push(None)
            await task
        err = next(item for item in ws.sent if item.get("type") == "error")
        assert "чекбокса" in err["message"]

    @pytest.mark.asyncio
    async def test_completed_lesson_rejects_checkbox(self, db_pool, create_user):
        course = await _make_c_course(db_pool, n_blocks=1)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await paid_private_lesson_item(db_pool, student_id)
        lesson_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="completed",
            open_checkboxes=["intro", "q1", "q2"],
        )
        ws = FakeWebSocket(_ticket(coach_id, lesson_id, "coach"))
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            assert ws.sent[0]["all_open"] is True
            await ws.push({"type": "set_checkbox", "id": "q1", "open": False})
            await _wait_until(lambda: any(item.get("type") == "error" for item in ws.sent))
            await ws.push(None)
            await task
        err = next(item for item in ws.sent if item.get("type") == "error")
        assert "закрыт" in err["message"]
