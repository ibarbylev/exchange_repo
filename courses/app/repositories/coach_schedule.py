"""Репозиторий страницы назначения коуча и расписания."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any


PRIVATE_LESSON_PREFIX = "private-lesson-"
WEEKDAY_NAMES = {
    1: "понедельник",
    2: "вторник",
    3: "среда",
    4: "четверг",
    5: "пятница",
    6: "суббота",
    7: "воскресенье",
}


def display_name(row: Any) -> str:
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full = " ".join(part for part in (last, first) if part)
    return full or row["username"] or row["email"]


def course_prefix(lesson_name: str | None) -> str:
    name = (lesson_name or "").strip().upper()
    if not name:
        return ""
    return name.split("_", 1)[0]


def parse_time(value: str) -> time:
    raw = (value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Некорректное время: {value!r}")


def parse_weekdays(items: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in items or []:
        weekday = int(item.get("weekday") or 0)
        if weekday < 1 or weekday > 7:
            raise ValueError("День недели должен быть в диапазоне 1–7 (пн–вс)")
        if weekday in seen:
            raise ValueError("Один и тот же день недели указан дважды")
        seen.add(weekday)
        result.append({
            "weekday": weekday,
            "time": parse_time(str(item.get("time") or "")),
        })
    result.sort(key=lambda item: item["weekday"])
    return result


def build_schedule(
    start_on: date,
    count: int,
    weekdays: list[dict],
) -> list[datetime]:
    if count < 1:
        raise ValueError("Число занятий должно быть больше нуля")
    if not weekdays:
        return []

    by_weekday = {item["weekday"]: item["time"] for item in weekdays}
    points: list[datetime] = []
    cursor = start_on
    limit = start_on + timedelta(days=370)
    while len(points) < count and cursor <= limit:
        if cursor.isoweekday() in by_weekday:
            points.append(
                datetime.combine(
                    cursor,
                    by_weekday[cursor.isoweekday()],
                    tzinfo=timezone.utc,
                )
            )
        cursor += timedelta(days=1)
    if len(points) < count:
        raise ValueError("Не удалось набрать даты расписания на год вперёд")
    return points


def row_to_lesson(row: Any) -> dict:
    scheduled = row["scheduled_at"]
    return {
        "id": row["id"],
        "student_id": row["student_id"],
        "student_name": row.get("student_name"),
        "student_email": row.get("student_email"),
        "coach_id": row["coach_id"],
        "coach_name": row.get("coach_name"),
        "order_item_id": row["order_item_id"],
        "product_name": row.get("product_name"),
        "product_slug": row.get("product_slug"),
        "lesson_name": row["lesson_name"],
        "lesson_title": row.get("lesson_title"),
        "scheduled_at": scheduled.isoformat(timespec="minutes") if scheduled else None,
        "status": row["status"],
        "open_checkboxes": list(row["open_checkboxes"]) if row.get("open_checkboxes") else [],
        "stars": row["stars"],
        "coach_comment": row["coach_comment"],
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


async def list_students(conn, assignment: str = "new") -> list[dict]:
    """Студенты с оплаченным private-lesson-*.

    assignment=new      — ещё нет ни одной строки в coach_lessons
    assignment=assigned — уже есть хотя бы одна строка
    assignment=all      — все оплатившие
    """
    filter_sql = ""
    if assignment == "new":
        # LEFT JOIN даёт NULL, если строк ещё нет. NULL = 0 → студент пропадает из «новых».
        filter_sql = "AND COALESCE(sl.lessons_created, 0) = 0"
    elif assignment == "assigned":
        filter_sql = "AND COALESCE(sl.lessons_created, 0) > 0"

    rows = await conn.fetch(
        f"""
        SELECT
            u.id,
            u.username,
            u.email,
            u.first_name,
            u.last_name,
            paid.orders_count,
            COALESCE(sl.lessons_created, 0) AS lessons_created,
            COALESCE(sl.lessons_scheduled, 0) AS lessons_scheduled,
            COALESCE(sl.lessons_completed, 0) AS lessons_completed
        FROM users u
        JOIN (
            SELECT o.user_id,
                   COUNT(DISTINCT o.id) AS orders_count
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'paid'
              AND p.slug LIKE $1
            GROUP BY o.user_id
        ) paid ON paid.user_id = u.id
        LEFT JOIN (
            SELECT student_id,
                   COUNT(*) AS lessons_created,
                   COUNT(*) FILTER (WHERE scheduled_at IS NOT NULL) AS lessons_scheduled,
                   COUNT(*) FILTER (WHERE status = 'completed') AS lessons_completed
            FROM coach_lessons
            GROUP BY student_id
        ) sl ON sl.student_id = u.id
        WHERE COALESCE(u.is_active, TRUE)
          {filter_sql}
        ORDER BY u.last_name NULLS LAST, u.first_name NULLS LAST, u.username
        """,
        f"{PRIVATE_LESSON_PREFIX}%",
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
            "orders_count": row["orders_count"],
            "lessons_created": row["lessons_created"],
            "lessons_scheduled": row["lessons_scheduled"],
            "lessons_completed": row["lessons_completed"],
        })
    return result


async def list_coaches(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id, username, email, first_name, last_name, role
        FROM users
        WHERE role IN ('coach', 'admin')
          AND COALESCE(is_active, TRUE)
        ORDER BY
            CASE WHEN role = 'coach' THEN 0 ELSE 1 END,
            last_name NULLS LAST,
            first_name NULLS LAST,
            username
        """
    )
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "role": row["role"],
            "name": display_name(row),
        }
        for row in rows
    ]


async def list_order_items(conn, student_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            oi.id,
            o.id AS order_id,
            o.created_at,
            p.slug,
            p.name AS product_name,
            oi.quantity,
            oi.price,
            (
                SELECT COUNT(*)
                FROM coach_lessons cl
                WHERE cl.order_item_id = oi.id
            ) AS slots_created
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.user_id = $1
          AND o.status = 'paid'
          AND p.slug LIKE $2
        ORDER BY o.created_at DESC, oi.id
        """,
        student_id,
        f"{PRIVATE_LESSON_PREFIX}%",
    )
    return [
        {
            "id": row["id"],
            "order_id": row["order_id"],
            "created_at": row["created_at"].date().isoformat() if row["created_at"] else None,
            "slug": row["slug"],
            "product_name": row["product_name"],
            "quantity": row["quantity"],
            "price": float(row["price"]) if row["price"] is not None else None,
            "slots_created": row["slots_created"],
        }
        for row in rows
    ]


async def list_block_hints(
    conn,
    prefix: str,
    student_id: int | None = None,
) -> list[dict]:
    prefix = (prefix or "").strip().upper()
    if len(prefix) < 4:
        return []

    rows = await conn.fetch(
        """
        SELECT
            b.name,
            b.title,
            b.after_lesson,
            b.sort_order
        FROM intensive_blocks b
        WHERE b.block_type = 'C'
          AND upper(b.name) LIKE $1
          AND (
                $2::int IS NULL
                OR b.name NOT IN (
                    SELECT cl.lesson_name
                    FROM coach_lessons cl
                    WHERE cl.student_id = $2
                      AND cl.lesson_name IS NOT NULL
                )
          )
        ORDER BY b.name
        """,
        f"{prefix}%",
        student_id,
    )
    return [
        {
            "name": row["name"],
            "title": row["title"],
            "after_lesson": row["after_lesson"],
            "sort_order": row["sort_order"],
        }
        for row in rows
    ]


async def resolve_lesson_names(
    conn,
    first_lesson_name: str,
    count: int,
    student_id: int | None = None,
) -> list[str]:
    first = (first_lesson_name or "").strip()
    if not first:
        raise ValueError("Укажите имя первого урока")

    prefix = course_prefix(first)
    hints = await list_block_hints(conn, prefix or first, student_id=student_id)
    names = [item["name"] for item in hints]

    if first in names:
        start = names.index(first)
        chosen = names[start:start + count]
    else:
        start = next((i for i, name in enumerate(names) if name >= first), len(names))
        chosen = [first] + names[start:start + count - 1]

    if len(chosen) < count:
        raise ValueError(
            f"Не хватает блоков коуча после «{first}»: нужно {count}, найдено {len(chosen)}"
        )
    if any(not name for name in chosen):
        raise ValueError("Нельзя создать занятие без имени урока")
    return chosen[:count]


async def list_lessons(
    conn,
    student_id: int | None = None,
    coach_id: int | None = None,
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT
            cl.id,
            cl.student_id,
            cl.coach_id,
            cl.order_item_id,
            cl.lesson_name,
            cl.scheduled_at,
            cl.status,
            cl.stars,
            cl.coach_comment,
            cl.updated_at,
            trim(both ' ' FROM concat_ws(' ', s.last_name, s.first_name)) AS student_full,
            s.username AS student_username,
            s.email AS student_email,
            trim(both ' ' FROM concat_ws(' ', c.last_name, c.first_name)) AS coach_full,
            c.username AS coach_username,
            ib.title AS lesson_title,
            p.name AS product_name,
            p.slug AS product_slug
        FROM coach_lessons cl
        JOIN users s ON s.id = cl.student_id
        LEFT JOIN users c ON c.id = cl.coach_id
        LEFT JOIN intensive_blocks ib ON ib.name = cl.lesson_name
        LEFT JOIN order_items oi ON oi.id = cl.order_item_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE ($1::int IS NULL OR cl.student_id = $1)
          AND ($2::int IS NULL OR cl.coach_id = $2)
        ORDER BY cl.scheduled_at NULLS LAST, cl.id
        """,
        student_id,
        coach_id,
    )
    result = []
    for row in rows:
        item = row_to_lesson(row)
        item["student_name"] = row["student_full"] or row["student_username"]
        item["student_email"] = row["student_email"]
        item["coach_name"] = row["coach_full"] or row["coach_username"]
        result.append(item)
    return result


async def create_lessons(
    conn,
    *,
    student_id: int,
    coach_id: int,
    order_item_id: int,
    lessons_count: int,
    first_lesson_name: str | None,
    start_on: date | None,
    weekdays: list[dict],
) -> list[dict]:
    if lessons_count < 1 or lessons_count > 40:
        raise ValueError("Число занятий — от 1 до 40")
    if not (first_lesson_name or "").strip():
        raise ValueError("Укажите имя первого урока")

    student = await conn.fetchrow("SELECT id FROM users WHERE id = $1", student_id)
    if not student:
        raise ValueError("Студент не найден")

    coach = await conn.fetchrow(
        """
        SELECT id FROM users
        WHERE id = $1 AND role IN ('coach', 'admin') AND COALESCE(is_active, TRUE)
        """,
        coach_id,
    )
    if not coach:
        raise ValueError("Коуч не найден")

    item = await conn.fetchrow(
        """
        SELECT oi.id
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.id = $1
          AND o.user_id = $2
          AND o.status = 'paid'
          AND p.slug LIKE $3
        """,
        order_item_id,
        student_id,
        f"{PRIVATE_LESSON_PREFIX}%",
    )
    if not item:
        raise ValueError("У студента нет оплаченной позиции private-lesson с таким id")

    lesson_names = await resolve_lesson_names(
        conn,
        first_lesson_name or "",
        lessons_count,
        student_id=student_id,
    )
    missing = [name for name in lesson_names if name]
    if missing:
        found = await conn.fetch(
            """
            SELECT name FROM intensive_blocks
            WHERE block_type = 'C' AND name = ANY($1::text[])
            """,
            missing,
        )
        known = {row["name"] for row in found}
        unknown = [name for name in missing if name not in known]
        if unknown:
            raise ValueError("Нет блоков коуча: " + ", ".join(unknown))

    schedule = (
        build_schedule(start_on, lessons_count, weekdays)
        if start_on and weekdays
        else [None] * lessons_count
    )

    created_ids = []
    async with conn.transaction():
        for index in range(lessons_count):
            scheduled_at = schedule[index]
            # Дату пишем в scheduled_at. status='scheduled' в части схем
            # не входит в chk_coach_lesson_status — оставляем NULL,
            # админ выставляет статус на корректировке.
            status = None
            row = await conn.fetchrow(
                """
                INSERT INTO coach_lessons (
                    student_id, coach_id, order_item_id,
                    lesson_name, scheduled_at, status
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                student_id,
                coach_id,
                order_item_id,
                lesson_names[index],
                scheduled_at,
                status,
            )
            created_ids.append(row["id"])

    return await list_lessons_by_ids(conn, created_ids)


async def list_lessons_by_ids(conn, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = await conn.fetch(
        """
        SELECT
            cl.id,
            cl.student_id,
            cl.coach_id,
            cl.order_item_id,
            cl.lesson_name,
            cl.scheduled_at,
            cl.status,
            cl.stars,
            cl.coach_comment,
            cl.updated_at,
            trim(both ' ' FROM concat_ws(' ', s.last_name, s.first_name)) AS student_full,
            s.username AS student_username,
            s.email AS student_email,
            trim(both ' ' FROM concat_ws(' ', c.last_name, c.first_name)) AS coach_full,
            c.username AS coach_username,
            ib.title AS lesson_title,
            p.name AS product_name,
            p.slug AS product_slug
        FROM coach_lessons cl
        JOIN users s ON s.id = cl.student_id
        LEFT JOIN users c ON c.id = cl.coach_id
        LEFT JOIN intensive_blocks ib ON ib.name = cl.lesson_name
        LEFT JOIN order_items oi ON oi.id = cl.order_item_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE cl.id = ANY($1::int[])
        ORDER BY cl.id
        """,
        ids,
    )
    result = []
    for row in rows:
        item = row_to_lesson(row)
        item["student_name"] = row["student_full"] or row["student_username"]
        item["coach_name"] = row["coach_full"] or row["coach_username"]
        result.append(item)
    return result


async def update_lesson(conn, lesson_id: int, payload: dict) -> dict:
    current = await conn.fetchrow(
        "SELECT * FROM coach_lessons WHERE id = $1",
        lesson_id,
    )
    if not current:
        raise ValueError("Занятие не найдено")

    coach_id = payload["coach_id"] if "coach_id" in payload else current["coach_id"]
    lesson_name = payload["lesson_name"] if "lesson_name" in payload else current["lesson_name"]
    scheduled_at = payload["scheduled_at"] if "scheduled_at" in payload else current["scheduled_at"]
    status = payload["status"] if "status" in payload else current["status"]
    stars = payload["stars"] if "stars" in payload else current["stars"]
    comment = payload["coach_comment"] if "coach_comment" in payload else current["coach_comment"]

    if isinstance(lesson_name, str):
        lesson_name = lesson_name.strip() or None
    if not lesson_name:
        raise ValueError("Имя урока обязательно")
    if status == "":
        status = None
    if stars in ("", 0):
        stars = None

    exists = await conn.fetchval(
        "SELECT 1 FROM intensive_blocks WHERE name = $1 AND block_type = 'C'",
        lesson_name,
    )
    if not exists:
        raise ValueError(f"Блок коуча {lesson_name} не найден")

    if coach_id is not None:
        coach_ok = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1 AND role IN ('coach', 'admin')",
            coach_id,
        )
        if not coach_ok:
            raise ValueError("Коуч не найден")

    await conn.execute(
        """
        UPDATE coach_lessons
        SET coach_id = $2,
            lesson_name = $3,
            scheduled_at = $4,
            status = $5,
            stars = $6,
            coach_comment = $7
        WHERE id = $1
        """,
        lesson_id,
        coach_id,
        lesson_name,
        scheduled_at,
        status,
        stars,
        comment,
    )
    updated = await list_lessons_by_ids(conn, [lesson_id])
    return updated[0]
