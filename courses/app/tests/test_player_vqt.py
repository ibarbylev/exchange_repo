import uuid
from datetime import datetime

import pytest

from app.core.security import create_access_token
from app.repositories.classroom import (
    current_exercise_for_prefix,
    parse_current_exercise_map,
)


def _ensure_csrf(client):
    """Ставит csrf_token в куки (если нет) и возвращает заголовки для JSON-запросов."""
    token = client.cookies.get("csrf_token")
    if not token:
        token = "test-csrf-token-value"
        client.cookies.set("csrf_token", token)
    return {"X-CSRF-Token": token}


async def _create_theme_with_exercises(db_pool, access_levels=None):
    """Курс → урок → тема + стартеры V/Q/T с разной видимостью."""
    unique_id = f"{int(datetime.now().timestamp() * 1000000)}_{uuid.uuid4().hex[:8]}"
    course_name = f"TEST_COURSE_{unique_id}"
    lesson_name = f"TEST_LESSON_{unique_id}"
    theme_name = f"TEST_THEME_{unique_id}"
    base_pos = int(uuid.uuid4().int % 900000000) + 1

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO courses (name, title)
            VALUES ($1, 'Test Course')
        """, course_name)
        await conn.execute("""
            INSERT INTO lessons (name, course_name, title, permission, visibility)
            VALUES ($1, $2, 'Test Lesson', ARRAY[0,1,2], ARRAY[0,1,2])
        """, lesson_name, course_name)
        await conn.execute("""
            INSERT INTO themes (name, lesson_name, title, pos, permission, visibility)
            VALUES ($1, $2, 'Test Theme', $3, ARRAY[0,1,2], ARRAY[0,1,2])
        """, theme_name, lesson_name, base_pos)

        names = {
            "v001": f"{theme_name}_V001",
            "q001": f"{theme_name}_Q001",
            "q002": f"{theme_name}_Q002",
            "t001": f"{theme_name}_T001",
        }
        exercises_data = [
            (names["v001"], "Видео для всех", "V", 1, [0, 1, 2]),
            (names["q001"], "Q для всех", "Q", 2, [0, 1, 2]),
            (names["q002"], "Q только премиум", "Q", 3, [2]),
            (names["t001"], "T только премиум", "T", 4, [2]),
        ]
        for name, title, ex_type, pos_offset, visibility in exercises_data:
            await conn.execute("""
                INSERT INTO exercises
                    (name, title, exercise_type, theme_name, pos, visibility, permission)
                VALUES ($1, $2, $3, $4, $5, $6, $6)
            """, name, title, ex_type, theme_name, base_pos + pos_offset, visibility)

    return {
        "course_name": course_name,
        "lesson_name": lesson_name,
        "theme_name": theme_name,
        "base_pos": base_pos,
        **names,
    }


async def _create_two_themes_with_vqt(db_pool):
    """Две соседние темы с V/Q/T — для перехода после теста."""
    unique_id = uuid.uuid4().hex[:8]
    course_name = f"BGRUA1_{unique_id}"
    lesson_name = f"BGRUA1001_{unique_id}"
    theme_a = f"BGRUA1001_H001_{unique_id}"
    theme_b = f"BGRUA1001_H002_{unique_id}"
    base_pos = int(uuid.uuid4().int % 900000000) + 1

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO courses (name, title) VALUES ($1, 'A1')",
            course_name,
        )
        await conn.execute("""
            INSERT INTO lessons (name, course_name, title, permission, visibility)
            VALUES ($1, $2, 'L01', ARRAY[0,1,2], ARRAY[0,1,2])
        """, lesson_name, course_name)
        await conn.execute("""
            INSERT INTO themes (name, lesson_name, title, pos, permission, visibility)
            VALUES ($1, $2, 'Theme A', $3, ARRAY[0,1,2], ARRAY[0,1,2])
        """, theme_a, lesson_name, base_pos)
        await conn.execute("""
            INSERT INTO themes (name, lesson_name, title, pos, permission, visibility)
            VALUES ($1, $2, 'Theme B', $3, ARRAY[0,1,2], ARRAY[0,1,2])
        """, theme_b, lesson_name, base_pos + 10)

        rows = [
            (f"{theme_a}_V001", theme_a, "V", base_pos + 1),
            (f"{theme_a}_Q001", theme_a, "Q", base_pos + 2),
            (f"{theme_a}_T001", theme_a, "T", base_pos + 3),
            (f"{theme_b}_V001", theme_b, "V", base_pos + 11),
            (f"{theme_b}_Q001", theme_b, "Q", base_pos + 12),
            (f"{theme_b}_T001", theme_b, "T", base_pos + 13),
        ]
        for name, theme, ex_type, pos in rows:
            await conn.execute("""
                INSERT INTO exercises
                    (name, title, exercise_type, theme_name, pos, visibility, permission,
                     variants, answers)
                VALUES ($1, $2, $3, $4, $5, ARRAY[0,1,2], ARRAY[0,1,2], $6, $7)
            """, name, name, ex_type, theme, pos, "[ |да|нет]", "[да]")

    return {
        "theme_a": theme_a,
        "theme_b": theme_b,
        "first_a": f"{theme_a}_V001",
        "test_a": f"{theme_a}_T001",
        "first_b": f"{theme_b}_V001",
    }


class TestGetThemeExercises:
    """Перенесено из test_classroom_tree.py: стартеры блоков V/Q/T."""

    @pytest.mark.asyncio
    async def test_get_theme_exercises_returns_only_visible_exercises(self, auth_client, db_pool):
        client, _ = auth_client
        data = await _create_theme_with_exercises(db_pool)

        response = await client.get(f"/api/theme/{data['theme_name']}/exercises")
        assert response.status_code == 200
        exercises = response.json()
        assert exercises == [data["v001"], data["q001"]]
        assert data["t001"] not in exercises
        assert data["q002"] not in exercises

    @pytest.mark.asyncio
    async def test_get_theme_exercises_returns_sorted_by_pos(self, auth_client, db_pool):
        client, _ = auth_client
        data = await _create_theme_with_exercises(db_pool)
        response = await client.get(f"/api/theme/{data['theme_name']}/exercises")
        assert response.json() == [data["v001"], data["q001"]]

    @pytest.mark.asyncio
    async def test_premium_user_sees_more_exercises(self, client, db_pool, create_user):
        user_id = await create_user(access_level=2)
        jti = str(uuid.uuid4())
        token = create_access_token(data={"sub": str(user_id)}, jti=jti)
        await db_pool.execute("""
            INSERT INTO user_sessions (user_id, jti, ip_address, user_agent, created_at)
            VALUES ($1, $2, '127.0.0.1', 'test', NOW())
        """, user_id, jti)
        client.cookies.set("access_token", token)

        data = await _create_theme_with_exercises(db_pool)
        response = await client.get(f"/api/theme/{data['theme_name']}/exercises")
        assert response.json() == [data["v001"], data["q001"], data["t001"]]


class TestExercisePayload:
    @pytest.mark.asyncio
    async def test_video_payload_has_continue_targets(self, auth_client, db_pool):
        client, _ = auth_client
        data = await _create_two_themes_with_vqt(db_pool)
        res = await client.get(f"/api/exercise/{data['first_a']}")
        assert res.status_code == 200
        body = res.json()
        assert body["type"] == "V"
        assert body["themeName"] == data["theme_a"]
        assert body["firstQExercise"] == f"{data['theme_a']}_Q001"
        assert body["firstTestExercise"] == data["test_a"]

    @pytest.mark.asyncio
    async def test_q_payload_has_progress(self, auth_client, db_pool):
        client, _ = auth_client
        data = await _create_two_themes_with_vqt(db_pool)
        q_name = f"{data['theme_a']}_Q001"
        res = await client.get(f"/api/exercise/{q_name}")
        body = res.json()
        assert body["type"] == "Q"
        assert body["currentQIndex"] == 0
        assert [ex["name"] for ex in body["themeQExercises"]] == [q_name]
        assert body["firstTestExercise"] == data["test_a"]

    @pytest.mark.asyncio
    async def test_t_payload_points_to_next_theme_and_retry(self, auth_client, db_pool):
        """Поля, по которым плеер уходит на следующую тему или в начало текущей."""
        client, _ = auth_client
        data = await _create_two_themes_with_vqt(db_pool)
        res = await client.get(f"/api/exercise/{data['test_a']}")
        body = res.json()
        assert body["type"] == "T"
        assert body["nextThemeName"] == data["theme_b"]
        assert body["nextThemeFirstExercise"] == data["first_b"]
        assert body["currentThemeFirstExercise"] == data["first_a"]
        assert body["mistakesAllowed"] == 3


class TestCurrentExerciseCursor:
    def test_parse_legacy_string_and_jsonb_map(self):
        assert parse_current_exercise_map("BGRUA1002_Q001") == {"BGRU": "BGRUA1002_Q001"}
        assert parse_current_exercise_map({"BGRU": "BGRUA1002_T001"}) == {
            "BGRU": "BGRUA1002_T001"
        }
        assert current_exercise_for_prefix({"BGRU": "BGRUA1002_T001"}, "BGRU") == "BGRUA1002_T001"
        assert current_exercise_for_prefix({"BGRU": "BGRUA1002_T001"}, "ENRU") is None

    @pytest.mark.asyncio
    async def test_save_current_exercise_writes_jsonb_map(self, auth_client, db_pool):
        client, user_id = auth_client
        data = await _create_two_themes_with_vqt(db_pool)

        res = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": data["first_b"]},
            headers=_ensure_csrf(client),
        )
        assert res.status_code == 200
        payload = res.json()
        assert payload["success"] is True
        assert payload["current_exercise"]["BGRU"] == data["first_b"]

        raw = await db_pool.fetchval(
            "SELECT current_exercise FROM users WHERE id = $1",
            user_id,
        )
        mapping = parse_current_exercise_map(raw)
        assert mapping["BGRU"] == data["first_b"]

    @pytest.mark.asyncio
    async def test_save_current_exercise_does_not_move_backwards(self, auth_client, db_pool):
        client, user_id = auth_client
        data = await _create_two_themes_with_vqt(db_pool)

        first = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": data["first_b"]},
            headers=_ensure_csrf(client),
        )
        assert first.json()["success"] is True

        back = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": data["first_a"]},
            headers=_ensure_csrf(client),
        )
        assert back.json()["success"] is True
        assert back.json()["current_exercise"]["BGRU"] == data["first_b"]

        raw = await db_pool.fetchval(
            "SELECT current_exercise FROM users WHERE id = $1",
            user_id,
        )
        assert parse_current_exercise_map(raw)["BGRU"] == data["first_b"]


class TestFirstVideo:
    @pytest.mark.asyncio
    async def test_first_video_of_theme(self, auth_client, db_pool):
        client, _ = auth_client
        data = await _create_two_themes_with_vqt(db_pool)
        res = await client.get(f"/api/theme/{data['theme_b']}/first-video")
        assert res.status_code == 200
        assert res.json()["exercise_name"] == data["first_b"]
