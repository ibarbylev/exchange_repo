import pytest
from unittest.mock import AsyncMock, patch

from .conftest import post_form


TOPIC_PROGRAM = "Вопрос по программе обучения"
TOPIC_SUPPORT = "Вопрос к службе технической поддержки"
TOPIC_ERROR = "Ошибка в тексте (звуковом файле, видео)"
TOPIC_OTHER = "Прочие вопросы"

FEEDBACK_URL = "/bg/ru/feedback/"
ADMIN_LIST_URL = "/admin/feedback_processing/"
ADMIN_UPDATE_URL = "/admin/feedback_processing/update/"
ADMIN_TOGGLE_URL = "/admin/feedback_processing/toggle/"
ADMIN_DELETE_URL = "/admin/feedback_processing/delete/"

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


async def insert_feedback(
    db_pool,
    user_id: int,
    *,
    topic: str = TOPIC_OTHER,
    message: str = "Текст обращения",
    page_url: str | None = "/bg/ru/classroom/",
    exercise_name: str | None = None,
    want_reply: bool = False,
    question_quality: int = 0,
    is_resolved: bool = False,
) -> int:
    return await db_pool.fetchval(
        """
        INSERT INTO feedback (
            user_id, topic, message, page_url, exercise_name,
            want_reply, question_quality, is_resolved
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        user_id,
        topic,
        message,
        page_url,
        exercise_name,
        want_reply,
        question_quality,
        is_resolved,
    )


def valid_payload(**overrides) -> dict:
    data = {
        "topic": TOPIC_PROGRAM,
        "message": "Не понял задание в уроке.",
        "page_url": "/bg/ru/classroom/BGRUA1/",
        "exercise_name": "BGRUA1001_H001",
        "want_reply": "1",
    }
    data.update(overrides)
    return data


# ====================== ОТПРАВКА ОБРАТНОЙ СВЯЗИ ======================

@pytest.mark.asyncio
async def test_submit_requires_auth_ajax(client):
    """AJAX без авторизации → 401 и ссылка на логин."""
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(),
        headers=AJAX_HEADERS,
    )
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False
    assert "войти" in body["detail"].lower()
    assert body["login_url"].startswith("/bg/ru/auth/login/")
    assert "msg=feedback_auth" in body["login_url"]


@pytest.mark.asyncio
async def test_submit_requires_auth_form(client):
    """Обычная форма без авторизации → 303 на логин и flash-cookie."""
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(page_url="/bg/ru/courses/"),
    )
    assert response.status_code == 303
    location = response.headers.get("location", "")
    assert location.startswith("/bg/ru/auth/login/")
    assert "next=" in location
    assert "msg=feedback_auth" in location
    assert response.cookies.get("flash") == "feedback_need_login"


@pytest.mark.asyncio
async def test_submit_rejects_empty_message_ajax(auth_client):
    client, _user_id = auth_client
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(message="   "),
        headers=AJAX_HEADERS,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "тему" in body["detail"].lower() or "текст" in body["detail"].lower()


@pytest.mark.asyncio
async def test_submit_rejects_unknown_topic_no_row(auth_client, db_pool):
    client, user_id = auth_client
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(topic="Несуществующая тема", message="Есть текст"),
    )
    assert response.status_code == 303
    assert response.cookies.get("flash") == "feedback_error"
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM feedback WHERE user_id = $1", user_id
    ) == 0


@pytest.mark.asyncio
async def test_submit_success_ajax_persists_row(auth_client, db_pool):
    client, user_id = auth_client
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(want_reply="on"),
        headers=AJAX_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["id"]
    assert "отправлено" in body["detail"].lower()

    row = await db_pool.fetchrow("SELECT * FROM feedback WHERE id = $1", body["id"])
    assert row["user_id"] == user_id
    assert row["topic"] == TOPIC_PROGRAM
    assert row["message"] == "Не понял задание в уроке."
    assert row["page_url"] == "/bg/ru/classroom/BGRUA1/"
    assert row["exercise_name"] == "BGRUA1001_H001"
    assert row["want_reply"] is True
    assert row["question_quality"] == 0
    assert row["is_resolved"] is False
    assert row["created_at"] is not None
    assert row["answered_at"] is None


@pytest.mark.asyncio
async def test_submit_success_form_sets_flash(auth_client, db_pool):
    client, user_id = auth_client
    next_page = "/bg/ru/classroom/"
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=valid_payload(
            topic=TOPIC_SUPPORT,
            message="Не открывается плеер",
            page_url=next_page,
            exercise_name="",
            want_reply="",
        ),
    )
    assert response.status_code == 303
    assert response.headers.get("location") == next_page
    assert response.cookies.get("flash") == "feedback_sent"

    row = await db_pool.fetchrow(
        "SELECT * FROM feedback WHERE user_id = $1", user_id
    )
    assert row["topic"] == TOPIC_SUPPORT
    assert row["exercise_name"] is None
    assert row["want_reply"] is False


@pytest.mark.asyncio
async def test_submit_want_reply_false_without_checkbox(auth_client, db_pool):
    client, user_id = auth_client
    payload = valid_payload()
    payload.pop("want_reply")
    response = await post_form(
        client,
        FEEDBACK_URL,
        data=payload,
        headers=AJAX_HEADERS,
    )
    assert response.status_code == 200
    row = await db_pool.fetchrow(
        "SELECT want_reply FROM feedback WHERE user_id = $1", user_id
    )
    assert row["want_reply"] is False


@pytest.mark.asyncio
async def test_submit_sends_admin_notification(auth_client):
    client, _user_id = auth_client
    with patch("app.routers.feedback._notify_email", return_value="ops@example.com"), \
         patch("app.routers.feedback.send_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        response = await post_form(
            client,
            FEEDBACK_URL,
            data=valid_payload(topic=TOPIC_ERROR, message="Опечатка в тексте"),
            headers=AJAX_HEADERS,
        )
    assert response.status_code == 200
    mock_send.assert_awaited_once()
    kwargs = mock_send.await_args.kwargs
    assert kwargs["to_email"] == "ops@example.com"
    assert TOPIC_ERROR in kwargs["subject"]
    assert "Опечатка в тексте" in kwargs["html_content"]


@pytest.mark.asyncio
async def test_submit_succeeds_if_notification_fails(auth_client, db_pool):
    client, user_id = auth_client
    with patch("app.routers.feedback._notify_email", return_value="ops@example.com"), \
         patch(
             "app.routers.feedback.send_email",
             new_callable=AsyncMock,
             side_effect=RuntimeError("smtp down"),
         ):
        response = await post_form(
            client,
            FEEDBACK_URL,
            data=valid_payload(message="Всё равно сохранить"),
            headers=AJAX_HEADERS,
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM feedback WHERE user_id = $1", user_id
    ) == 1


@pytest.mark.asyncio
async def test_submit_skips_email_without_recipient(auth_client):
    client, _user_id = auth_client
    with patch("app.routers.feedback._notify_email", return_value=""), \
         patch("app.routers.feedback.send_email", new_callable=AsyncMock) as mock_send:
        response = await post_form(
            client,
            FEEDBACK_URL,
            data=valid_payload(),
            headers=AJAX_HEADERS,
        )
    assert response.status_code == 200
    mock_send.assert_not_awaited()


# ====================== АДМИНКА: ДОСТУП И СПИСОК ======================

@pytest.mark.asyncio
async def test_admin_feedback_requires_auth(client):
    response = await client.get(ADMIN_LIST_URL)
    assert response.status_code in {401, 303, 403}


@pytest.mark.asyncio
async def test_admin_feedback_forbidden_for_regular_user(auth_client):
    client, _user_id = auth_client
    response = await client.get(ADMIN_LIST_URL)
    assert response.status_code in {401, 403}


@pytest.mark.asyncio
async def test_admin_feedback_list_shows_items(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=30)
    await insert_feedback(
        db_pool,
        user_id,
        topic=TOPIC_PROGRAM,
        message="Нужна консультация по программе",
        exercise_name="BGRUA1001_H001",
    )
    user = await db_pool.fetchrow("SELECT email, username FROM users WHERE id = $1", user_id)

    response = await admin_client.get(ADMIN_LIST_URL)
    assert response.status_code == 200
    assert "Обратная связь" in response.text
    assert TOPIC_PROGRAM in response.text
    assert "Нужна консультация по программе" in response.text
    assert user["email"] in response.text
    assert user["username"] in response.text
    assert "BGRUA1001_H001" in response.text


@pytest.mark.asyncio
async def test_admin_feedback_filter_open_and_resolved(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    open_id = await insert_feedback(db_pool, user_id, message="Открытый вопрос", is_resolved=False)
    done_id = await insert_feedback(db_pool, user_id, message="Уже решено", is_resolved=True)

    open_page = await admin_client.get(f"{ADMIN_LIST_URL}?status=open")
    assert open_page.status_code == 200
    assert "Открытый вопрос" in open_page.text
    assert "Уже решено" not in open_page.text
    assert str(open_id) in open_page.text

    resolved_page = await admin_client.get(f"{ADMIN_LIST_URL}?status=resolved")
    assert resolved_page.status_code == 200
    assert "Уже решено" in resolved_page.text
    assert "Открытый вопрос" not in resolved_page.text
    assert str(done_id) in resolved_page.text


@pytest.mark.asyncio
async def test_admin_feedback_filter_want_reply(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    await insert_feedback(db_pool, user_id, message="Жду ответа", want_reply=True)
    await insert_feedback(db_pool, user_id, message="Просто сообщил", want_reply=False)

    response = await admin_client.get(f"{ADMIN_LIST_URL}?status=all&want_reply=1")
    assert response.status_code == 200
    assert "Жду ответа" in response.text
    assert "Просто сообщил" not in response.text


@pytest.mark.asyncio
async def test_admin_feedback_search_by_email_and_text(admin_client, access_user, db_pool):
    first_id = await access_user(access_level=1, days=10)
    second_id = await access_user(access_level=1, days=10)
    first = await db_pool.fetchrow("SELECT email FROM users WHERE id = $1", first_id)
    await insert_feedback(db_pool, first_id, message="Ищу по email пользователя")
    await insert_feedback(db_pool, second_id, topic=TOPIC_ERROR, message="Совсем другой текст")

    by_email = await admin_client.get(f"{ADMIN_LIST_URL}?status=all&q={first['email']}")
    assert by_email.status_code == 200
    assert "Ищу по email пользователя" in by_email.text
    assert "Совсем другой текст" not in by_email.text

    by_text = await admin_client.get(f"{ADMIN_LIST_URL}?status=all&q=другой")
    assert by_text.status_code == 200
    assert "Совсем другой текст" in by_text.text
    assert "Ищу по email пользователя" not in by_text.text


# ====================== АДМИНКА: UPDATE / TOGGLE / DELETE ======================

@pytest.mark.asyncio
async def test_admin_feedback_update_success(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    feedback_id = await insert_feedback(
        db_pool,
        user_id,
        topic=TOPIC_OTHER,
        message="Старый текст",
        want_reply=False,
        is_resolved=False,
    )
    response = await post_form(
        admin_client,
        ADMIN_UPDATE_URL,
        data={
            "feedback_id": feedback_id,
            "topic": TOPIC_SUPPORT,
            "message": "Обновлённый текст",
            "page_url": "/bg/ru/help/",
            "exercise_name": "EX_1",
            "want_reply": "on",
            "question_quality": "1",
            "is_resolved": "on",
            "status": "all",
        },
    )
    assert response.status_code == 303
    assert "ok=updated" in response.headers.get("location", "")

    row = await db_pool.fetchrow("SELECT * FROM feedback WHERE id = $1", feedback_id)
    assert row["topic"] == TOPIC_SUPPORT
    assert row["message"] == "Обновлённый текст"
    assert row["page_url"] == "/bg/ru/help/"
    assert row["exercise_name"] == "EX_1"
    assert row["want_reply"] is True
    assert row["question_quality"] == 1
    assert row["is_resolved"] is True
    assert row["answered_at"] is not None


@pytest.mark.asyncio
async def test_admin_feedback_update_empty_fields(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    feedback_id = await insert_feedback(db_pool, user_id, message="Нельзя стереть")
    response = await post_form(
        admin_client,
        ADMIN_UPDATE_URL,
        data={
            "feedback_id": feedback_id,
            "topic": "   ",
            "message": "   ",
            "status": "open",
        },
    )
    assert response.status_code == 303
    assert "error=empty_fields" in response.headers.get("location", "")
    row = await db_pool.fetchrow("SELECT message FROM feedback WHERE id = $1", feedback_id)
    assert row["message"] == "Нельзя стереть"


@pytest.mark.asyncio
async def test_admin_feedback_update_not_found(admin_client):
    response = await post_form(
        admin_client,
        ADMIN_UPDATE_URL,
        data={
            "feedback_id": 999999,
            "topic": TOPIC_OTHER,
            "message": "Нет такой записи",
            "status": "all",
        },
    )
    assert response.status_code == 303
    assert "error=not_found" in response.headers.get("location", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, column, before, expected",
    [
        ("want_reply", "want_reply", False, True),
        ("is_resolved", "is_resolved", False, True),
        ("question_quality", "question_quality", 0, 1),
    ],
)
async def test_admin_feedback_toggle_fields(
    admin_client, access_user, db_pool, field, column, before, expected
):
    user_id = await access_user(access_level=1, days=10)
    kwargs = {column: before}
    feedback_id = await insert_feedback(db_pool, user_id, message=f"toggle {field}", **kwargs)

    response = await post_form(
        admin_client,
        ADMIN_TOGGLE_URL,
        data={
            "feedback_id": feedback_id,
            "field": field,
            "status": "all",
        },
    )
    assert response.status_code == 303
    assert "ok=updated" in response.headers.get("location", "")

    row = await db_pool.fetchrow(
        f"SELECT {column}, answered_at FROM feedback WHERE id = $1",
        feedback_id,
    )
    assert row[column] == expected
    assert row["answered_at"] is not None


@pytest.mark.asyncio
async def test_admin_feedback_toggle_bad_field(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    feedback_id = await insert_feedback(db_pool, user_id)
    response = await post_form(
        admin_client,
        ADMIN_TOGGLE_URL,
        data={
            "feedback_id": feedback_id,
            "field": "user_id",
            "status": "all",
        },
    )
    assert response.status_code == 303
    assert "error=bad_field" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_admin_feedback_delete(admin_client, access_user, db_pool):
    user_id = await access_user(access_level=1, days=10)
    feedback_id = await insert_feedback(db_pool, user_id, message="Удалить это")
    response = await post_form(
        admin_client,
        ADMIN_DELETE_URL,
        data={"feedback_id": feedback_id, "status": "all"},
    )
    assert response.status_code == 303
    assert "ok=deleted" in response.headers.get("location", "")
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM feedback WHERE id = $1", feedback_id
    ) == 0


@pytest.mark.asyncio
async def test_admin_mutations_require_superuser(auth_client, access_user, db_pool):
    """Обычный пользователь не может менять и удалять обращения."""
    client, _auth_id = auth_client
    owner_id = await access_user(access_level=1, days=10)
    feedback_id = await insert_feedback(db_pool, owner_id)

    update = await post_form(
        client,
        ADMIN_UPDATE_URL,
        data={
            "feedback_id": feedback_id,
            "topic": TOPIC_OTHER,
            "message": "хак",
        },
    )
    toggle = await post_form(
        client,
        ADMIN_TOGGLE_URL,
        data={"feedback_id": feedback_id, "field": "is_resolved"},
    )
    delete = await post_form(
        client,
        ADMIN_DELETE_URL,
        data={"feedback_id": feedback_id},
    )
    assert update.status_code in {401, 403}
    assert toggle.status_code in {401, 403}
    assert delete.status_code in {401, 403}
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM feedback WHERE id = $1", feedback_id
    ) == 1
