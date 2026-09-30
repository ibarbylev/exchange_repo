"""Coach Online (упражнения / блоки типа C): проектор, очередь, API плеера, дерево.

Админские сценарии расписания уже покрыты в test_admin_coach_and_roles.py.
Здесь — то, чего не было: плеер студента, статусы очереди и курсор ветки C.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.repositories import coach_session
from app.repositories.classroom import (
    PLAYER_BY_BLOCK_TYPE,
    TREE_STATUS_COMPLETED,
    TREE_STATUS_CURRENT,
    TREE_STATUS_LOCKED,
    _first_open_c_index,
    _insert_intensive_blocks,
)
from app.repositories.intensive import (
    HTML_DIR,
    checkbox_ids,
    html_file_path,
    load_html_block,
)
from app.routers.classroom import detect_player

from .conftest import login_as, paid_private_lesson_item, post_form


PROJECTOR_STEM = "BGRUA1_projector1_to_lesson07"
FAKE_HTML = (
    '<h1>Coach lesson</h1>'
    '<span data-checkbox="intro"></span>'
    '<span data-checkbox="q1"></span>'
    '<span data-checkbox="q1"></span>'
    '<span data-checkbox="q2"></span>'
)


def _unique(prefix: str = "C") -> str:
    return f"{prefix}_{int(datetime.now().timestamp() * 1000000)}_{uuid.uuid4().hex[:8]}"


_auth_as = login_as
_paid_item = paid_private_lesson_item


async def _make_c_course(db_pool, *, n_blocks: int = 2, file_stems: list[str] | None = None):
    """Курс BGRUA1 + урок + N блоков C.

    Дерево вешает intensive-блок на курс только если
    after_lesson.startswith(course.name). Поэтому якорь урока
    должен начинаться с «BGRUA1», а не с уникального имени курса.
    """
    unique = _unique("C")
    course_name = "BGRUA1"
    lesson_name = f"BGRUA1L_{unique}"
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM intensive_blocks WHERE block_type = 'C'")
        await conn.execute("""
            INSERT INTO courses (name, title) VALUES ($1, 'Курс болгарского языка уровень A1')
            ON CONFLICT (name) DO NOTHING
        """, course_name)
        await conn.execute(
            """
            INSERT INTO lessons (name, course_name, title, permission, visibility)
            VALUES ($1, $2, 'Coach lesson anchor', ARRAY[0,1,2], ARRAY[0,1,2])
            """,
            lesson_name,
            course_name,
        )
        blocks = []
        for index in range(1, n_blocks + 1):
            name = f"BGRUA1_C{index:02d}_{unique}"
            stem = (file_stems[index - 1] if file_stems and index - 1 < len(file_stems)
                    else f"{name}_file")
            await conn.execute(
                """
                INSERT INTO intensive_blocks
                    (name, title, block_type, after_lesson, sort_order, file_name,
                     permission, visibility)
                VALUES ($1, $2, 'C', $3, $4, $5, ARRAY[0,1,2], ARRAY[0,1,2])
                """,
                name,
                f"Coach Online {index:02d}",
                lesson_name,
                index,
                stem,
            )
            blocks.append({"name": name, "file_name": stem, "sort_order": index})
    return {
        "course_name": course_name,
        "lesson_name": lesson_name,
        "blocks": blocks,
        "first": blocks[0]["name"],
        "second": blocks[1]["name"] if len(blocks) > 1 else None,
    }


async def _add_lesson(
    db_pool,
    *,
    student_id: int,
    coach_id: int,
    order_item_id: int,
    lesson_name: str,
    status: str | None = None,
    open_checkboxes: list[str] | None = None,
) -> int:
    return await db_pool.fetchval(
        """
        INSERT INTO coach_lessons
            (student_id, coach_id, order_item_id, lesson_name, status, open_checkboxes)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        student_id,
        coach_id,
        order_item_id,
        lesson_name,
        status,
        json.dumps(open_checkboxes or []),
    )


# ---------------------------------------------------------------------------
# Юнит: HTML-проектор и чистые функции очереди
# ---------------------------------------------------------------------------

class TestCoachProjectorHtml:
    def test_html_dir_and_real_projector_file_exist(self):
        path = html_file_path(PROJECTOR_STEM)
        assert path == HTML_DIR / f"{PROJECTOR_STEM}.html"
        assert path.is_file(), (
            f"Нет HTML проектора {path}. "
            "Блоки C читают intensive_data/html/, не json/."
        )

    def test_load_html_block_accepts_stem_and_suffix(self):
        raw = load_html_block(PROJECTOR_STEM)
        also = load_html_block(f"{PROJECTOR_STEM}.html")
        assert raw == also
        assert "data-checkbox=" in raw

    def test_checkbox_ids_order_and_dedup(self):
        ids = checkbox_ids(FAKE_HTML)
        assert ids == ["intro", "q1", "q2"]

    def test_checkbox_ids_from_real_projector_start_with_known_markers(self):
        ids = checkbox_ids(load_html_block(PROJECTOR_STEM))
        assert ids[:4] == ["1", "11", "12", "13"]
        assert "1101" in ids

    def test_missing_html_raises(self):
        with pytest.raises(FileNotFoundError):
            load_html_block("no_such_coach_projector")


class TestCoachSessionHelpers:
    def test_can_run_lessons_roles(self):
        assert coach_session.can_run_lessons(None) is False
        assert coach_session.can_run_lessons({"role": "student"}) is False
        assert coach_session.can_run_lessons({"role": "coach"}) is True
        assert coach_session.can_run_lessons({"role": "admin"}) is True
        assert coach_session.can_run_lessons({"is_superuser": True, "role": "student"}) is True
        assert coach_session.can_run_lessons({"is_staff": True}) is True

    def test_user_pk_accepts_id_or_user_id(self):
        assert coach_session.user_pk({"id": "12"}) == 12
        assert coach_session.user_pk({"user_id": 7}) == 7
        assert coach_session.user_pk({"id": "x"}) is None

    def test_parse_open_checkboxes_filters_junk(self):
        assert coach_session.parse_open_checkboxes(None) == []
        assert coach_session.parse_open_checkboxes('["a","a","b"]') == ["a", "b"]
        assert coach_session.parse_open_checkboxes({"a": True}) == []
        assert coach_session.parse_open_checkboxes(["", " q1 ", "q1"]) == ["q1"]

    def test_student_may_view_current_or_completed_only(self):
        current = {"id": 1, "is_completed": False, "queue_state": "current"}
        locked = {"id": 2, "is_completed": False, "queue_state": "locked"}
        done = {"id": 3, "is_completed": True, "queue_state": "completed"}
        queue = [current, locked]
        assert coach_session.student_may_view(current, queue) is True
        assert coach_session.student_may_view(locked, queue) is False
        assert coach_session.student_may_view(done, queue) is True
        assert coach_session.student_may_view(None, queue) is False

    def test_viewer_role_only_assigned_coach(self):
        from app.routers.coach import viewer_role

        lesson = {"coach_id": 5}
        assert viewer_role({"role": "coach", "id": 5}, lesson) == "coach"
        assert viewer_role({"role": "coach", "id": 9}, lesson) == "student"
        assert viewer_role({"role": "student", "id": 1}, lesson) == "student"
        assert viewer_role({"is_superuser": True, "id": 1, "role": "admin"}, lesson) == "coach"


class TestDetectPlayerC:
    def test_detect_player_maps_c_aliases(self):
        assert detect_player({"player": "coach_online"}) == "coach_online"
        assert detect_player({"kind": "C"}) == "coach_online"
        assert detect_player({"player": "coach"}) == "coach_online"
        assert PLAYER_BY_BLOCK_TYPE["C"] == "coach_online"


class TestCTreeStatus:
    def test_first_open_c_index(self):
        blocks = [{"name": "C01"}, {"name": "C02"}, {"name": "C03"}]
        assert _first_open_c_index(blocks, None) is None
        assert _first_open_c_index(blocks, set()) == 0
        assert _first_open_c_index(blocks, {"C01"}) == 1
        assert _first_open_c_index(blocks, {"C01", "C02", "C03"}) == 3

    def test_insert_uses_completed_lessons_not_cursor_for_c(self):
        lessons = [{"name": "BGRUA1007", "title": "L07", "is_clickable": True}]
        blocks = [
            {
                "name": "BGRUA1_C01",
                "title": "Coach 1",
                "block_type": "C",
                "after_lesson": "BGRUA1007",
                "sort_order": 1,
                "file_name": "p1",
                "is_clickable": True,
                "permission": [0, 1, 2],
            },
            {
                "name": "BGRUA1_C02",
                "title": "Coach 2",
                "block_type": "C",
                "after_lesson": "BGRUA1007",
                "sort_order": 2,
                "file_name": "p2",
                "is_clickable": True,
                "permission": [0, 1, 2],
            },
        ]
        # Курсор врёт (стоит на втором), занятия говорят: закрыт только первый.
        tree = _insert_intensive_blocks(
            lessons,
            blocks,
            progress={"BGRU": {"C": "BGRUA1_C02"}},
            lang_prefix="BGRU",
            coach_completed_names={"BGRUA1_C01"},
        )
        c_nodes = [node for node in tree if node.get("block_type") == "C"]
        assert [node["tree_status"] for node in c_nodes] == [
            TREE_STATUS_COMPLETED,
            TREE_STATUS_CURRENT,
        ]
        assert c_nodes[0]["player"] == "coach_online"
        assert c_nodes[1]["is_clickable"] is True

    def test_without_coach_lessons_c_falls_back_to_cursor(self):
        lessons = [{"name": "BGRUA1007", "title": "L07", "is_clickable": True}]
        blocks = [
            {
                "name": "BGRUA1_C01",
                "title": "Coach 1",
                "block_type": "C",
                "after_lesson": "BGRUA1007",
                "sort_order": 1,
                "file_name": "p1",
                "is_clickable": True,
            },
            {
                "name": "BGRUA1_C02",
                "title": "Coach 2",
                "block_type": "C",
                "after_lesson": "BGRUA1007",
                "sort_order": 2,
                "file_name": "p2",
                "is_clickable": True,
            },
        ]
        tree = _insert_intensive_blocks(
            lessons,
            blocks,
            progress={"BGRU": {"C": "BGRUA1_C01"}},
            lang_prefix="BGRU",
            coach_completed_names=None,
        )
        c_nodes = [node for node in tree if node.get("block_type") == "C"]
        assert [node["tree_status"] for node in c_nodes] == [
            TREE_STATUS_CURRENT,
            TREE_STATUS_LOCKED,
        ]
        assert c_nodes[1]["lock_reason"] == "sequence"


# ---------------------------------------------------------------------------
# Репозиторий: очередь и курсор C
# ---------------------------------------------------------------------------

class TestCoachQueueAndCursor:
    @pytest.mark.asyncio
    async def test_queue_locks_everything_after_first_open(self, db_pool, create_user):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        first_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="scheduled",
        )
        second_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["second"],
            status="scheduled",
        )
        async with db_pool.acquire() as conn:
            queue = await coach_session.list_student_queue(conn, student_id, coach_id)
        assert [item["id"] for item in queue] == [first_id, second_id]
        assert queue[0]["queue_state"] == "current"
        assert queue[0]["can_open"] is True
        assert queue[1]["queue_state"] == "locked"
        assert queue[1]["can_open"] is False
        assert coach_session.current_lesson(queue)["id"] == first_id

    @pytest.mark.asyncio
    async def test_completed_first_opens_second(self, db_pool, create_user):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="completed",
        )
        second_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["second"],
        )
        async with db_pool.acquire() as conn:
            queue = await coach_session.list_student_queue(conn, student_id, coach_id)
        assert queue[0]["queue_state"] == "completed"
        assert queue[1]["id"] == second_id
        assert queue[1]["queue_state"] == "current"

    @pytest.mark.asyncio
    async def test_set_checkbox_and_mark_in_progress(self, db_pool, create_user):
        course = await _make_c_course(db_pool, n_blocks=1)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        lesson_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
        )
        async with db_pool.acquire() as conn:
            updated = await coach_session.set_checkbox(conn, lesson_id, "q1", True)
            assert updated["status"] == "in_progress"
            assert updated["open_checkboxes"] == ["q1"]
            updated = await coach_session.set_checkbox(conn, lesson_id, "q2", True)
            assert updated["open_checkboxes"] == ["q1", "q2"]
            updated = await coach_session.set_checkbox(conn, lesson_id, "q1", False)
            assert updated["open_checkboxes"] == ["q2"]
            started = await coach_session.mark_in_progress(conn, lesson_id)
            assert started["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_advance_c_cursor_moves_only_forward(self, db_pool, create_user):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student")
        async with db_pool.acquire() as conn:
            await coach_session.advance_student_c_cursor(
                conn, student_id, course["first"]
            )
            raw = await conn.fetchval(
                "SELECT intensive_progress FROM users WHERE id = $1", student_id
            )
        data = raw if isinstance(raw, dict) else json.loads(raw)
        assert data["BGRU"]["C"] == course["second"]

        async with db_pool.acquire() as conn:
            await coach_session.advance_student_c_cursor(
                conn, student_id, course["first"]
            )
            raw = await conn.fetchval(
                "SELECT intensive_progress FROM users WHERE id = $1", student_id
            )
        data = raw if isinstance(raw, dict) else json.loads(raw)
        assert data["BGRU"]["C"] == course["second"]


# ---------------------------------------------------------------------------
# HTTP: плеер студента /api/classroom/coach/{block}
# ---------------------------------------------------------------------------

class TestStudentCoachApi:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client, db_pool):
        course = await _make_c_course(db_pool, n_blocks=1)
        res = await client.get(f"/api/classroom/coach/{course['first']}")
        assert res.status_code == 401

    @pytest.mark.asyncio
    async def test_coach_is_sent_to_hub(self, client, db_pool, create_user):
        course = await _make_c_course(db_pool, n_blocks=1)
        coach_id = await create_user(role="coach")
        await _auth_as(client, db_pool, coach_id)
        res = await client.get(f"/api/classroom/coach/{course['first']}")
        assert res.status_code == 409
        body = res.json()
        assert body["error"] == "open_coach_page"
        assert "/coach/" in body["hub"]

    @pytest.mark.asyncio
    async def test_no_lesson_assigned(self, auth_client, db_pool):
        client, _ = auth_client
        course = await _make_c_course(db_pool, n_blocks=1)
        res = await client.get(f"/api/classroom/coach/{course['first']}")
        assert res.status_code == 404
        assert res.json()["error"] == "no_lesson"

    @pytest.mark.asyncio
    async def test_second_block_locked_until_first_completed(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="scheduled",
        )
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["second"],
            status="scheduled",
        )
        await _auth_as(client, db_pool, student_id)
        res = await client.get(f"/api/classroom/coach/{course['second']}")
        assert res.status_code == 403
        assert res.json()["error"] == "locked"

    @pytest.mark.asyncio
    async def test_current_block_returns_projector_payload(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(
            db_pool, n_blocks=1, file_stems=[PROJECTOR_STEM]
        )
        student_id = await create_user(role="student", first_name="Ира")
        coach_id = await create_user(role="coach", first_name="Коуч")
        item_id = await _paid_item(db_pool, student_id)
        lesson_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="in_progress",
            open_checkboxes=["1"],
        )
        await _auth_as(client, db_pool, student_id)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            res = await client.get(f"/api/classroom/coach/{course['first']}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["lesson_id"] == lesson_id
        assert body["lesson_name"] == course["first"]
        assert body["role"] == "student"
        assert body["status"] == "in_progress"
        assert body["all_open"] is False
        assert body["open_checkboxes"] == ["1"]
        assert body["checkbox_ids"] == ["intro", "q1", "q2"]
        assert "Coach lesson" in body["html"]
        assert body["ws_ticket"]

    @pytest.mark.asyncio
    async def test_completed_block_opens_all_checkboxes(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool, n_blocks=1)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="completed",
            open_checkboxes=["intro"],
        )
        await _auth_as(client, db_pool, student_id)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            res = await client.get(f"/api/classroom/coach/{course['first']}")
        assert res.status_code == 200
        body = res.json()
        assert body["all_open"] is True
        assert body["open_checkboxes"] == ["intro", "q1", "q2"]

    @pytest.mark.asyncio
    async def test_text_intensive_endpoint_rejects_c_block(
        self, auth_client, db_pool
    ):
        client, _ = auth_client
        course = await _make_c_course(db_pool, n_blocks=1)
        res = await client.get(f"/api/classroom/intensive/{course['first']}")
        assert res.status_code == 400
        assert "Text Intensive" in res.json()["error"]


class TestClassroomTreeC:
    @pytest.mark.asyncio
    async def test_tree_marks_c_blocks_by_completed_lessons(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student", access_level=0, days=30)
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="completed",
        )
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["second"],
            status="scheduled",
        )
        await _auth_as(client, db_pool, student_id)
        res = await client.get("/api/classroom/tree?source_lang=bg&ui_lang=ru")
        assert res.status_code == 200, res.text
        tree = res.json()["tree"]
        c_nodes = []
        for item in tree.get("courses") or []:
            for lesson in item.get("lessons") or []:
                if lesson.get("block_type") == "C" and lesson.get("name") in {
                    course["first"],
                    course["second"],
                }:
                    c_nodes.append(lesson)
        by_name = {node["name"]: node for node in c_nodes}
        assert by_name[course["first"]]["tree_status"] == TREE_STATUS_COMPLETED
        assert by_name[course["second"]]["tree_status"] == TREE_STATUS_CURRENT
        assert by_name[course["first"]]["player"] == "coach_online"


class TestCoachPages:
    @pytest.mark.asyncio
    async def test_hub_forbidden_for_student(self, auth_client):
        client, _ = auth_client
        res = await client.get("/bg/ru/coach/")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_hub_lists_student_with_open_lesson(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool, n_blocks=1)
        student_id = await create_user(
            role="student", email="queue_student@example.com", first_name="Анна"
        )
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="scheduled",
        )
        await _auth_as(client, db_pool, coach_id)
        res = await client.get("/bg/ru/coach/")
        assert res.status_code == 200
        assert "queue_student@example.com" in res.text or "Анна" in res.text

    @pytest.mark.asyncio
    async def test_other_coach_cannot_open_lesson(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool, n_blocks=1)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        other_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        lesson_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
        )
        await _auth_as(client, db_pool, other_id)
        res = await client.get(f"/bg/ru/coach/lesson/{lesson_id}")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_complete_lesson_opens_next_in_queue(
        self, client, db_pool, create_user
    ):
        course = await _make_c_course(db_pool)
        student_id = await create_user(role="student")
        coach_id = await create_user(role="coach")
        item_id = await _paid_item(db_pool, student_id)
        lesson_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["first"],
            status="in_progress",
        )
        second_id = await _add_lesson(
            db_pool,
            student_id=student_id,
            coach_id=coach_id,
            order_item_id=item_id,
            lesson_name=course["second"],
        )
        await _auth_as(client, db_pool, coach_id)
        with patch("app.routers.coach.load_html_block", return_value=FAKE_HTML):
            res = await post_form(
                client,
                f"/bg/ru/coach/lesson/{lesson_id}/complete",
            )
        assert res.status_code == 303
        status = await db_pool.fetchval(
            "SELECT status FROM coach_lessons WHERE id = $1", lesson_id
        )
        assert status == "completed"
        async with db_pool.acquire() as conn:
            queue = await coach_session.list_student_queue(conn, student_id, coach_id)
        assert queue[0]["queue_state"] == "completed"
        assert queue[1]["id"] == second_id
        assert queue[1]["queue_state"] == "current"
