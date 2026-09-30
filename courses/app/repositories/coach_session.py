"""Очередь занятий коуча и список открытых чекбоксов."""

from __future__ import annotations

import json
from typing import Any

from app.repositories.coach_schedule import display_name, row_to_lesson


def is_completed(status: str | None) -> bool:
    return (status or "").strip().lower() == "completed"


def can_run_lessons(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_superuser") or user.get("is_staff"):
        return True
    return (user.get("role") or "") in {"coach", "admin"}


def user_pk(user: dict | None) -> int | None:
    if not user:
        return None
    value = user.get("user_id") or user.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_open_checkboxes(value: Any) -> list[str]:
    """Только список id. Лишние поля отбрасываются."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        checkbox_id = str(item).strip()
        if not checkbox_id or checkbox_id in seen:
            continue
        seen.add(checkbox_id)
        result.append(checkbox_id)
    return result


def _lesson_from_row(row: Any) -> dict:
    item = row_to_lesson(row)
    item["student_name"] = row.get("student_full") or row.get("student_username")
    item["student_email"] = row.get("student_email")
    item["coach_name"] = row.get("coach_full") or row.get("coach_username")
    item["file_name"] = row.get("file_name")
    item["block_type"] = row.get("block_type")
    item["is_completed"] = is_completed(row.get("status"))
    item["open_checkboxes"] = parse_open_checkboxes(
        row.get("open_checkboxes") if hasattr(row, "get") else item.get("open_checkboxes")
    )
    return item


LESSON_SELECT = """
    SELECT
        cl.id,
        cl.student_id,
        cl.coach_id,
        cl.order_item_id,
        cl.lesson_name,
        cl.scheduled_at,
        cl.status,
        cl.open_checkboxes,
        cl.stars,
        cl.coach_comment,
        cl.updated_at,
        trim(both ' ' FROM concat_ws(' ', s.last_name, s.first_name)) AS student_full,
        s.username AS student_username,
        s.email AS student_email,
        trim(both ' ' FROM concat_ws(' ', c.last_name, c.first_name)) AS coach_full,
        c.username AS coach_username,
        ib.title AS lesson_title,
        ib.file_name,
        ib.block_type,
        p.name AS product_name,
        p.slug AS product_slug
    FROM coach_lessons cl
    JOIN users s ON s.id = cl.student_id
    LEFT JOIN users c ON c.id = cl.coach_id
    LEFT JOIN intensive_blocks ib ON ib.name = cl.lesson_name
    LEFT JOIN order_items oi ON oi.id = cl.order_item_id
    LEFT JOIN products p ON p.id = oi.product_id
"""


async def get_lesson(conn, lesson_id: int) -> dict | None:
    row = await conn.fetchrow(LESSON_SELECT + " WHERE cl.id = $1", lesson_id)
    return _lesson_from_row(row) if row else None


async def list_student_queue(
    conn,
    student_id: int,
    coach_id: int | None = None,
) -> list[dict]:
    rows = await conn.fetch(
        LESSON_SELECT + """
        WHERE cl.student_id = $1
          AND ($2::int IS NULL OR cl.coach_id = $2)
        ORDER BY cl.scheduled_at NULLS LAST, cl.id
        """,
        student_id,
        coach_id,
    )
    items = [_lesson_from_row(row) for row in rows]
    current_locked = False
    for item in items:
        if item["is_completed"]:
            item["queue_state"] = "completed"
            item["can_open"] = False
            continue
        if current_locked:
            item["queue_state"] = "locked"
            item["can_open"] = False
            continue
        item["queue_state"] = "current"
        item["can_open"] = True
        current_locked = True
    return items


def current_lesson(queue: list[dict]) -> dict | None:
    for item in queue:
        if item.get("queue_state") == "current":
            return item
    return None


async def list_coach_students(conn, coach_id: int | None, *, admin: bool = False) -> list[dict]:
    """Студенты с хотя бы одним незакрытым занятием этого коуча."""
    rows = await conn.fetch(
        """
        SELECT
            u.id,
            u.username,
            u.email,
            u.first_name,
            u.last_name,
            COUNT(*) FILTER (WHERE cl.status IS DISTINCT FROM 'completed') AS open_count,
            COUNT(*) FILTER (WHERE cl.status = 'completed') AS done_count,
            MIN(cl.scheduled_at) FILTER (
                WHERE cl.status IS DISTINCT FROM 'completed'
            ) AS next_at
        FROM users u
        JOIN coach_lessons cl ON cl.student_id = u.id
        WHERE ($2::bool OR cl.coach_id = $1)
          AND COALESCE(u.is_active, TRUE)
        GROUP BY u.id, u.username, u.email, u.first_name, u.last_name
        HAVING COUNT(*) FILTER (WHERE cl.status IS DISTINCT FROM 'completed') > 0
        ORDER BY next_at NULLS LAST, u.last_name NULLS LAST, u.first_name NULLS LAST, u.username
        """,
        coach_id,
        admin,
    )
    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "name": display_name(row),
            "open_count": row["open_count"],
            "done_count": row["done_count"],
            "next_at": row["next_at"].isoformat(timespec="minutes") if row["next_at"] else None,
        })
    return result


def lesson_accessible(lesson: dict, user: dict | None) -> bool:
    uid = user_pk(user)
    if not uid or not lesson:
        return False
    if can_run_lessons(user):
        if user.get("is_superuser") or user.get("role") == "admin":
            return True
        return lesson.get("coach_id") == uid
    return lesson.get("student_id") == uid


def student_may_view(lesson: dict, queue: list[dict]) -> bool:
    if not lesson:
        return False
    if lesson.get("is_completed"):
        return True
    current = current_lesson(queue)
    return bool(current and current["id"] == lesson["id"])


async def set_checkbox(conn, lesson_id: int, checkbox_id: str, opened: bool) -> dict:
    lesson = await get_lesson(conn, lesson_id)
    if not lesson:
        raise ValueError("Занятие не найдено")
    checkbox_id = str(checkbox_id).strip()
    if not checkbox_id:
        raise ValueError("Пустой id чекбокса")
    ids = list(lesson.get("open_checkboxes") or [])
    if opened and checkbox_id not in ids:
        ids.append(checkbox_id)
    if not opened:
        ids = [item for item in ids if item != checkbox_id]
    await conn.execute(
        """
        UPDATE coach_lessons
        SET open_checkboxes = $2::jsonb,
            status = CASE
                WHEN status IS DISTINCT FROM 'completed' THEN 'in_progress'
                ELSE status
            END
        WHERE id = $1
        """,
        lesson_id,
        json.dumps(ids, ensure_ascii=False),
    )
    updated = await get_lesson(conn, lesson_id)
    if not updated:
        raise ValueError("Занятие не найдено")
    return updated


async def mark_in_progress(conn, lesson_id: int) -> dict:
    await conn.execute(
        """
        UPDATE coach_lessons
        SET status = 'in_progress'
        WHERE id = $1
          AND status IS DISTINCT FROM 'completed'
        """,
        lesson_id,
    )
    lesson = await get_lesson(conn, lesson_id)
    if not lesson:
        raise ValueError("Занятие не найдено")
    return lesson


async def mark_completed(
    conn,
    lesson_id: int,
    open_checkbox_ids: list[str] | None = None,
) -> dict:
    """Закрывает урок. Если переданы id — записывает их как открытые."""
    if open_checkbox_ids is None:
        await conn.execute(
            """
            UPDATE coach_lessons
            SET status = 'completed'
            WHERE id = $1
            """,
            lesson_id,
        )
    else:
        await conn.execute(
            """
            UPDATE coach_lessons
            SET status = 'completed',
                open_checkboxes = $2::jsonb
            WHERE id = $1
            """,
            lesson_id,
            json.dumps(parse_open_checkboxes(open_checkbox_ids), ensure_ascii=False),
        )
    lesson = await get_lesson(conn, lesson_id)
    if not lesson:
        raise ValueError("Занятие не найдено")
    return lesson


async def advance_student_c_cursor(conn, student_id: int | None, lesson_name: str | None) -> None:
    """Сдвигает курсор ветки C на следующий блок после закрытого урока.

    Дерево студента для Coach Online строится и по этому курсору,
    и по статусам coach_lessons. Курсор обновляем, чтобы ветки не расходились.
    """
    if not student_id or not lesson_name:
        return

    from app.repositories.classroom import (
        lang_prefix_from_name,
        list_series_blocks,
        load_user_json_field,
        migrate_intensive_progress,
        series_exercise_order,
        cursor_rank,
        _intensive_cursors,
    )

    prefix = lang_prefix_from_name(lesson_name)
    if not prefix:
        return

    blocks = await list_series_blocks(conn, "C", prefix)
    names = [block.get("name") for block in blocks if block.get("name")]
    if lesson_name not in names:
        return
    index = names.index(lesson_name)
    next_name = names[index + 1] if index + 1 < len(names) else lesson_name

    progress = await load_user_json_field(conn, student_id, "intensive_progress")
    cursors = _intensive_cursors(progress, prefix)
    order = series_exercise_order(blocks)
    old_rank = cursor_rank(order, cursors.get("C"))
    new_rank = cursor_rank(order, next_name)
    if new_rank < 0:
        return
    if new_rank <= old_rank and old_rank >= 0:
        return

    progress = migrate_intensive_progress(progress, prefix, "C", next_name)
    await conn.execute(
        "UPDATE users SET intensive_progress = $1::jsonb WHERE id = $2",
        json.dumps(progress, ensure_ascii=False),
        student_id,
    )


async def find_student_lesson_for_block(conn, student_id: int, block_name: str) -> dict | None:
    row = await conn.fetchrow(
        LESSON_SELECT + """
        WHERE cl.student_id = $1
          AND cl.lesson_name = $2
        ORDER BY
            CASE WHEN cl.status IS DISTINCT FROM 'completed' THEN 0 ELSE 1 END,
            cl.scheduled_at NULLS LAST,
            cl.id
        LIMIT 1
        """,
        student_id,
        block_name,
    )
    return _lesson_from_row(row) if row else None
