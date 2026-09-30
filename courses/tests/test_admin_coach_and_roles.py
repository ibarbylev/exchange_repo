import pytest

from .conftest import post_form


async def _paid_private_lesson_item(db_pool, user_id: int, slug: str = "private-lesson-single") -> int:
    """Оплаченный заказ с позицией private-lesson-* — условие списка студентов."""
    product_id = await db_pool.fetchval(
        "SELECT id FROM products WHERE slug = $1", slug
    )
    assert product_id, f"В тестовой БД нет продукта {slug}"

    order_id = await db_pool.fetchval(
        """
        INSERT INTO orders (user_id, status, amount)
        VALUES ($1, 'paid', 25)
        RETURNING id
        """,
        user_id,
    )
    item_id = await db_pool.fetchval(
        """
        INSERT INTO order_items (order_id, product_id, quantity, price, discount)
        VALUES ($1, $2, 1, 25, 0)
        RETURNING id
        """,
        order_id,
        product_id,
    )
    return item_id


async def _ensure_coach_blocks(db_pool, names: list[str]) -> None:
    """Минимальные C-блоки + урок, на который они ссылаются."""
    await db_pool.execute("""
        INSERT INTO courses (name, title) VALUES ('BGRUA1', 'Test Course')
        ON CONFLICT (name) DO NOTHING
    """)
    await db_pool.execute("""
        INSERT INTO lessons (name, course_name, title, permission, visibility)
        VALUES ('BGRUA1001', 'BGRUA1', 'Test Lesson', ARRAY[0,1,2], ARRAY[0,1,2])
        ON CONFLICT (name) DO NOTHING
    """)
    for index, name in enumerate(names, start=1):
        await db_pool.execute(
            """
            INSERT INTO intensive_blocks
                (name, title, block_type, after_lesson, sort_order, file_name)
            VALUES ($1, $2, 'C', 'BGRUA1001', $3, $4)
            ON CONFLICT (name) DO NOTHING
            """,
            name,
            f"Coach block {name}",
            index,
            f"{name}.json",
        )


# ====================== ИЗМЕНЕНИЕ РОЛИ ======================

@pytest.mark.asyncio
async def test_user_role_change_page_finds_user_by_email(admin_client, create_user, db_pool):
    """GET /admin/user-role-change/?q= показывает найденного пользователя."""
    user_id = await create_user(email="role_search@example.com", role="student")
    response = await admin_client.get("/admin/user-role-change/?q=role_search@example.com")
    assert response.status_code == 200
    assert "role_search@example.com" in response.text
    assert str(user_id) in response.text


@pytest.mark.asyncio
async def test_user_role_change_success(admin_client, create_user, db_pool):
    """Админ успешно меняет роль student → coach."""
    user_id = await create_user(email="to_coach@example.com", role="student")
    response = await post_form(
        admin_client,
        "/admin/user-role-change/",
        data={"user_id": user_id, "role": "coach"},
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert "success=1" in location
    assert "to_coach@example.com" in location

    role = await db_pool.fetchval("SELECT role FROM users WHERE id = $1", user_id)
    assert role == "coach"


@pytest.mark.asyncio
async def test_user_role_change_invalid_role(admin_client, create_user):
    """Неизвестная роль отклоняется."""
    user_id = await create_user(role="student")
    response = await post_form(
        admin_client,
        "/admin/user-role-change/",
        data={"user_id": user_id, "role": "superhero"},
    )
    assert response.status_code == 303
    assert "error=invalid_role" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_user_role_change_user_not_found(admin_client):
    """Несуществующий user_id → error=user_not_found."""
    response = await post_form(
        admin_client,
        "/admin/user-role-change/",
        data={"user_id": 999_999, "role": "coach"},
    )
    assert response.status_code == 303
    assert "error=user_not_found" in response.headers.get("location", "")


# ====================== СТРАНИЦА РАСПИСАНИЯ ======================

@pytest.mark.asyncio
async def test_coach_schedule_page_lists_new_paid_student(admin_client, create_user, db_pool):
    """Студент с оплаченным private-lesson виден в фильтре new."""
    student_id = await create_user(
        email="paid_student@example.com",
        first_name="Анна",
        last_name="Студентова",
        role="student",
    )
    await _paid_private_lesson_item(db_pool, student_id)

    response = await admin_client.get(
        "/admin/coach-student-schedule/?mode=assign&assignment=new"
    )
    assert response.status_code == 200
    assert "paid_student@example.com" in response.text
    assert str(student_id) in response.text


@pytest.mark.asyncio
async def test_coach_schedule_page_assigned_filter_hides_new_student(
    admin_client, create_user, db_pool
):
    """Пока слотов нет, студент не попадает в фильтр assigned."""
    student_id = await create_user(email="only_new@example.com", role="student")
    await _paid_private_lesson_item(db_pool, student_id)

    response = await admin_client.get(
        "/admin/coach-student-schedule/?mode=assign&assignment=assigned"
    )
    assert response.status_code == 200
    assert "only_new@example.com" not in response.text


# ====================== ПОДСКАЗКИ И ПОЗИЦИИ ЗАКАЗА ======================

@pytest.mark.asyncio
async def test_coach_lesson_hints_short_prefix_is_empty(admin_client):
    """Префикс короче 4 символов не даёт подсказок."""
    response = await admin_client.get(
        "/admin/coach-student-schedule/lesson-hints/?prefix=BG"
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_coach_lesson_hints_returns_c_blocks(admin_client, db_pool):
    """Подсказки возвращают C-блоки по префиксу курса."""
    await _ensure_coach_blocks(db_pool, ["BGRUA1_C01", "BGRUA1_C02"])
    response = await admin_client.get(
        "/admin/coach-student-schedule/lesson-hints/?prefix=BGRUA1"
    )
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "BGRUA1_C01" in names
    assert "BGRUA1_C02" in names


@pytest.mark.asyncio
async def test_coach_order_items_for_paid_student(admin_client, create_user, db_pool):
    """GET order-items отдаёт оплаченные private-lesson позиции студента."""
    student_id = await create_user(role="student")
    item_id = await _paid_private_lesson_item(db_pool, student_id)

    response = await admin_client.get(
        f"/admin/coach-student-schedule/order-items/?student_id={student_id}"
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["id"] == item_id
    assert payload[0]["slug"].startswith("private-lesson-")
    assert payload[0]["slots_created"] == 0


# ====================== СОЗДАНИЕ СЛОТОВ ======================

@pytest.mark.asyncio
async def test_coach_create_lessons_success(admin_client, create_user, db_pool):
    """Создание слотов без дат: N пустых строк + редирект created:N."""
    student_id = await create_user(email="slot_student@example.com", role="student")
    coach_id = await create_user(email="slot_coach@example.com", role="coach")
    item_id = await _paid_private_lesson_item(db_pool, student_id)
    await _ensure_coach_blocks(db_pool, ["BGRUA1_C01", "BGRUA1_C02"])

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/create/",
        data={
            "student_id": student_id,
            "coach_id": coach_id,
            "order_item_id": item_id,
            "lessons_count": 2,
            "first_lesson_name": "BGRUA1_C01",
            "start_date": "",
            "times_per_week": 1,
        },
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert "success=created%3A2" in location or "success=created:2" in location
    assert "mode=schedule" in location

    rows = await db_pool.fetch(
        """
        SELECT lesson_name, coach_id, status, scheduled_at
        FROM coach_lessons
        WHERE student_id = $1
        ORDER BY id
        """,
        student_id,
    )
    assert len(rows) == 2
    assert [row["lesson_name"] for row in rows] == ["BGRUA1_C01", "BGRUA1_C02"]
    assert all(row["coach_id"] == coach_id for row in rows)
    assert all(row["scheduled_at"] is None for row in rows)


@pytest.mark.asyncio
async def test_coach_create_lessons_with_schedule(admin_client, create_user, db_pool):
    """Создание слотов с датой старта и одним днём недели."""
    student_id = await create_user(role="student")
    coach_id = await create_user(role="coach")
    item_id = await _paid_private_lesson_item(db_pool, student_id)
    await _ensure_coach_blocks(db_pool, ["BGRUA1_C01", "BGRUA1_C02"])

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/create/",
        data={
            "student_id": student_id,
            "coach_id": coach_id,
            "order_item_id": item_id,
            "lessons_count": 2,
            "first_lesson_name": "BGRUA1_C01",
            "start_date": "2026-09-28",  # понедельник
            "times_per_week": 1,
            "weekday_1": "1",
            "time_1": "10:30",
        },
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert "created" in location

    rows = await db_pool.fetch(
        """
        SELECT scheduled_at, status
        FROM coach_lessons
        WHERE student_id = $1
        ORDER BY scheduled_at
        """,
        student_id,
    )
    assert len(rows) == 2
    assert all(row["scheduled_at"] is not None for row in rows)
    assert rows[0]["scheduled_at"].hour == 10
    assert rows[0]["scheduled_at"].minute == 30
    delta_days = (rows[1]["scheduled_at"].date() - rows[0]["scheduled_at"].date()).days
    assert delta_days == 7


@pytest.mark.asyncio
async def test_coach_create_lessons_rejects_student_as_coach(
    admin_client, create_user, db_pool
):
    """coach_id с ролью student не принимается."""
    student_id = await create_user(role="student")
    fake_coach_id = await create_user(role="student")
    item_id = await _paid_private_lesson_item(db_pool, student_id)

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/create/",
        data={
            "student_id": student_id,
            "coach_id": fake_coach_id,
            "order_item_id": item_id,
            "lessons_count": 1,
        },
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert "error=" in location
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM coach_lessons WHERE student_id = $1", student_id
    )
    assert count == 0


@pytest.mark.asyncio
async def test_coach_create_lessons_invalid_frequency(admin_client, create_user, db_pool):
    """times_per_week не 1 и не 2 → ошибка, слоты не создаются."""
    student_id = await create_user(role="student")
    coach_id = await create_user(role="coach")
    item_id = await _paid_private_lesson_item(db_pool, student_id)

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/create/",
        data={
            "student_id": student_id,
            "coach_id": coach_id,
            "order_item_id": item_id,
            "lessons_count": 1,
            "times_per_week": 3,
        },
    )
    assert response.status_code == 303
    assert "error=" in response.headers.get("location", "")
    count = await db_pool.fetchval("SELECT COUNT(*) FROM coach_lessons")
    assert count == 0


# ====================== ОБНОВЛЕНИЕ ЗАНЯТИЯ ======================

@pytest.mark.asyncio
async def test_coach_update_lesson_success(admin_client, create_user, db_pool):
    """Обновление статуса, звёзд и комментария."""
    student_id = await create_user(role="student")
    coach_id = await create_user(role="coach")
    item_id = await _paid_private_lesson_item(db_pool, student_id)
    await _ensure_coach_blocks(db_pool, ["BGRUA1_C01"])
    lesson_id = await db_pool.fetchval(
        """
        INSERT INTO coach_lessons (student_id, coach_id, order_item_id, lesson_name)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        student_id,
        coach_id,
        item_id,
        "BGRUA1_C01",
    )

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/update/",
        data={
            "lesson_id": lesson_id,
            "student_id": student_id,
            "filter_coach_id": coach_id,
            "coach_id": coach_id,
            "lesson_name": "BGRUA1_C01",
            "status": "completed",
            "stars": "3",
            "coach_comment": "Отлично",
            "scheduled_at": "2026-10-01T11:00",
        },
    )
    assert response.status_code == 303
    assert "success=updated" in response.headers.get("location", "")

    row = await db_pool.fetchrow(
        "SELECT status, stars, coach_comment FROM coach_lessons WHERE id = $1",
        lesson_id,
    )
    assert row["status"] == "completed"
    assert row["stars"] == 3
    assert row["coach_comment"] == "Отлично"


@pytest.mark.asyncio
async def test_coach_update_lesson_not_found(admin_client):
    """Несуществующее занятие → error в редиректе."""
    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/update/",
        data={"lesson_id": 999_999, "status": "completed"},
    )
    assert response.status_code == 303
    assert "error=" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_coach_update_lesson_unknown_block(admin_client, create_user, db_pool):
    """Неизвестный C-блок при обновлении отклоняется."""
    student_id = await create_user(role="student")
    coach_id = await create_user(role="coach")
    item_id = await _paid_private_lesson_item(db_pool, student_id)
    await _ensure_coach_blocks(db_pool, ["BGRUA1_C01"])
    lesson_id = await db_pool.fetchval(
        """
        INSERT INTO coach_lessons (student_id, coach_id, order_item_id, lesson_name)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        student_id,
        coach_id,
        item_id,
        "BGRUA1_C01",
    )

    response = await post_form(
        admin_client,
        "/admin/coach-student-schedule/update/",
        data={
            "lesson_id": lesson_id,
            "student_id": student_id,
            "coach_id": coach_id,
            "lesson_name": "NO_SUCH_BLOCK",
        },
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert "error=" in location
    name = await db_pool.fetchval(
        "SELECT lesson_name FROM coach_lessons WHERE id = $1", lesson_id
    )
    assert name == "BGRUA1_C01"
