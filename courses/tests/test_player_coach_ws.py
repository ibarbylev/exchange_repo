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

from .conftest import login_as, paid_private_lesson_item
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


@pytest.fixture(autouse=True)
async def _bind_db_pool(db_pool):
    """Хендлер берёт пул с app.state, как в проде. Фикстура client не нужна."""
    previous = getattr(app.state, "db_pool", None)
    app.state.db_pool = db_pool
    try:
        yield
    finally:
        app.state.db_pool = previous


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
    async def test_completed_lesson_closes_without_accept(self, db_pool, create_user):
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
        task = await _run_ws(ws, lesson_id)
        await _wait_until(lambda: ws.closed == 4403)
        await task
        assert ws.accepted is False
        assert ws.sent == []
        assert lesson_id not in hub.rooms or not hub.rooms[lesson_id]


class TestRefreshWsTicket:
    """GET /api/coach/ws-ticket/{lesson_id} — свежий RAM-ticket без перезагрузки."""

    @pytest.mark.asyncio
    async def test_anonymous_gets_401(self, client, live_lesson):
        res = await client.get(f"/api/coach/ws-ticket/{live_lesson['lesson_id']}")
        assert res.status_code == 401

    @pytest.mark.asyncio
    async def test_other_coach_gets_403(self, client, db_pool, live_lesson, create_user):
        stranger = await create_user(role="coach")
        await login_as(client, db_pool, stranger)
        res = await client.get(f"/api/coach/ws-ticket/{live_lesson['lesson_id']}")
        assert res.status_code == 403
        assert res.json()["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_coach_receives_ticket_that_opens_ws(self, client, db_pool, live_lesson):
        lesson_id = live_lesson["lesson_id"]
        await login_as(client, db_pool, live_lesson["coach_id"])
        res = await client.get(f"/api/coach/ws-ticket/{lesson_id}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["lesson_id"] == lesson_id
        assert body["role"] == "coach"
        assert body["ticket"]
        assert body["ticket"] in coach_mod._tickets

        ws = FakeWebSocket(body["ticket"])
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task = await _run_ws(ws, lesson_id)
            await _wait_until(lambda: ws.sent)
            await ws.push(None)
            await task
        assert ws.accepted is True
        assert ws.sent[0]["role"] == "coach"

    @pytest.mark.asyncio
    async def test_student_receives_ticket_after_ram_flush(self, client, db_pool, live_lesson):
        lesson_id = live_lesson["lesson_id"]
        await login_as(client, db_pool, live_lesson["student_id"])
        first = await client.get(f"/api/coach/ws-ticket/{lesson_id}")
        assert first.status_code == 200
        old_ticket = first.json()["ticket"]
        coach_mod._tickets.clear()
        second = await client.get(f"/api/coach/ws-ticket/{lesson_id}")
        assert second.status_code == 200
        new_ticket = second.json()["ticket"]
        assert new_ticket
        assert new_ticket != old_ticket
        assert old_ticket not in coach_mod._tickets
        assert new_ticket in coach_mod._tickets

        ws = FakeWebSocket(old_ticket)
        task = await _run_ws(ws, lesson_id)
        await _wait_until(lambda: ws.closed == 4401)
        await task
        assert ws.accepted is False

        ws2 = FakeWebSocket(new_ticket)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            task2 = await _run_ws(ws2, lesson_id)
            await _wait_until(lambda: ws2.sent)
            await ws2.push(None)
            await task2
        assert ws2.accepted is True
        assert ws2.sent[0]["role"] == "student"

    @pytest.mark.asyncio
    async def test_completed_lesson_returns_409(self, client, db_pool, create_user):
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
        )
        await login_as(client, db_pool, coach_id)
        res = await client.get(f"/api/coach/ws-ticket/{lesson_id}")
        assert res.status_code == 409
        assert res.json()["error"] == "completed"
