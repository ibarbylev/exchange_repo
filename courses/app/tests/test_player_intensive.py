"""Text Intensive: каталог JSON + API блока.

Путь к файлам один и тот же в разработке и в Docker:
    settings.SHARED_DIR / "intensive_data"
это DATA_DIR из app.repositories.intensive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.repositories import intensive as intensive_mod
from app.repositories.intensive import (
    DATA_DIR,
    get_block,
    get_exercise_ids,
    load_catalog,
    playable_exercises,
)


EXPECTED_BLOCKS = [
    "BGRUA1_text1_to_lesson06",
    "BGRUA1_text2_to_lesson09",
    "BGRUA1_text3_to_lesson11",
    "BGRUA1_text4_to_lesson14",
    "BGRUA1_text5_to_lesson16",
    "BGRUA1_text6_to_lesson19",
    "BGRUA1_text7_to_lesson21",
    "BGRUA2_text1_to_lesson01",
    "BGRUA2_text2_to_lesson04",
    "BGRUA2_text3_to_lesson06",
    "BGRUA2_text4_to_lesson09",
    "BGRUA2_text5_to_lesson11",
    "BGRUA2_text6_to_lesson14",
    "BGRUA2_text7_to_lesson16",
    "BGRUA2_text8_to_lesson19",
    "BGRUA2_text9_to_lesson21",
]



def _ensure_csrf(client):
    """Ставит csrf_token в куки (если нет) и возвращает заголовки для JSON-запросов."""
    token = client.cookies.get("csrf_token")
    if not token:
        token = "test-csrf-token-value"
        client.cookies.set("csrf_token", token)
    return {"X-CSRF-Token": token}


def _json_path(stem: str) -> Path:
    return DATA_DIR / f"{stem}.json"


class TestIntensiveDataDir:
    def test_shared_dir_points_to_universal_location(self):
        assert DATA_DIR == settings.SHARED_DIR / "intensive_data" / "json"
        assert DATA_DIR.exists(), (
            f"Каталог интенсива не найден: {DATA_DIR}. "
            f"SHARED_DIR={settings.SHARED_DIR}. "
            "В Docker и локально это должна быть одна и та же папка intensive_data."
        )

    def test_expected_files_are_present(self):
        missing = [stem for stem in EXPECTED_BLOCKS if not _json_path(stem).is_file()]
        found = sorted(p.stem for p in DATA_DIR.glob("BGRUA*.json"))
        assert not missing, (
            f"В {DATA_DIR} нет файлов: {missing}. "
            f"Сейчас на диске: {found}"
        )


class TestCatalogAndLoader:
    def test_load_catalog_includes_every_expected_block(self):
        count = load_catalog()
        assert count >= len(EXPECTED_BLOCKS), (
            f"Каталог загрузил {count} файл(ов), ожидалось не меньше {len(EXPECTED_BLOCKS)}. "
            f"DATA_DIR={DATA_DIR}"
        )
        for stem in EXPECTED_BLOCKS:
            pack = get_block(stem)
            assert pack["exercise_ids"], (
                f"{stem}.json прочитан, но exercise_ids пуст. "
                "Так плеер рисует «Список упражнений пока пуст»."
            )
            assert pack["exercises"], f"{stem}.json: exercises пустой после playable_exercises()"

    @pytest.mark.parametrize("stem", EXPECTED_BLOCKS)
    def test_each_file_is_object_with_exercises_key(self, stem):
        path = _json_path(stem)
        assert path.is_file(), f"нет файла {path}"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), f"{stem}: корень JSON не объект, а {type(raw)}"
        exercises = raw.get("exercises")
        assert exercises, f"{stem}: в JSON нет ключа exercises или он пустой"

    def test_second_block_is_not_empty(self):
        """Регресс текущей ошибки: первый блок живой, второй — пустой список."""
        load_catalog()
        first = get_block("BGRUA1_text1_to_lesson06")
        second = get_block("BGRUA1_text2_to_lesson09")
        assert first["exercises"], "первый блок тоже пуст — каталог не читается"
        assert second["exercises"], (
            "Второй блок BGRUA1_text2_to_lesson09 отдал пустой exercises. "
            f"file={_json_path('BGRUA1_text2_to_lesson09')} "
            f"сырой тип exercises надо смотреть в JSON "
            "(массив объектов vs словарь id→объект)."
        )
        assert second["exercise_ids"]
        assert second["exercise_ids"] != first["exercise_ids"]

    def test_playable_exercises_keeps_list(self):
        items = [
            {"id": "a", "type": "gap"},
            {"id": "b", "type": "summary"},
            {"id": "c", "type": "quiz"},
        ]
        result = playable_exercises(items)
        assert [x["id"] for x in result] == ["a", "c"]

    def test_playable_exercises_keeps_dict_shaped_block(self):
        """Словарь id→объект раньше обнулял список: цикл шёл по ключам-строкам."""
        raw = {
            "ex1": {"type": "gap", "title": "Пропуск"},
            "ex2": {"id": "ex2", "type": "quiz"},
            "sum": {"type": "summary"},
        }
        result = playable_exercises(raw)
        assert [x["id"] for x in result] == ["ex1", "ex2"]


class TestGetBlockFallbacks:
    def test_get_block_accepts_json_suffix_and_path(self):
        load_catalog()
        pack = get_block("intensive_data/BGRUA1_text1_to_lesson06.json")
        assert pack["exercises"]

    def test_missing_file_raises(self):
        load_catalog()
        with pytest.raises(FileNotFoundError):
            get_block("BGRUA1_text_does_not_exist")

    def test_get_exercise_ids_empty_for_unknown_file(self):
        assert get_exercise_ids("no_such_block") == []


@pytest.fixture
async def two_intensive_blocks(db_pool):
    """Два блока серии X после одного урока, file_name = реальные JSON."""
    load_catalog()
    first = EXPECTED_BLOCKS[0]
    second = EXPECTED_BLOCKS[1]
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO courses (name, title)
            VALUES ('BGRUA1', 'A1')
            ON CONFLICT (name) DO NOTHING
        """)
        await conn.execute("""
            INSERT INTO lessons (name, course_name, title, permission, visibility)
            VALUES ('BGRUA1006', 'BGRUA1', 'Lesson 06', ARRAY[0,1,2], ARRAY[0,1,2])
            ON CONFLICT (name) DO NOTHING
        """)
        await conn.execute("DELETE FROM intensive_blocks WHERE name LIKE 'BGRUA1_text%'")
        await conn.execute("""
            INSERT INTO intensive_blocks
                (name, title, block_type, after_lesson, sort_order, file_name,
                 permission, visibility)
            VALUES
                ($1, 'Text 1', 'X', 'BGRUA1006', 1, $1, ARRAY[0,1,2], ARRAY[0,1,2]),
                ($2, 'Text 2', 'X', 'BGRUA1006', 2, $2, ARRAY[0,1,2], ARRAY[0,1,2])
        """, first, second)
    return {"first": first, "second": second}


class TestIntensiveApi:
    @pytest.mark.asyncio
    async def test_first_and_second_block_return_exercises(
        self, auth_client, two_intensive_blocks
    ):
        client, _ = auth_client
        first = two_intensive_blocks["first"]
        second = two_intensive_blocks["second"]

        res1 = await client.get(f"/api/classroom/intensive/{first}")
        assert res1.status_code == 200, res1.text
        pack1 = res1.json()
        assert pack1.get("exercises"), f"первый блок пуст: {pack1}"

        res2 = await client.get(f"/api/classroom/intensive/{second}")
        assert res2.status_code == 200, res2.text
        pack2 = res2.json()
        assert pack2.get("error") is None
        assert pack2.get("exercises"), (
            "Второй блок API вернул пустой exercises — это ровно текущий баг плеера. "
            f"file_name={pack2.get('file_name')} keys={list(pack2)}"
        )
        assert pack2["name"] == second
        assert pack2["file_name"] == second
        assert pack1["exercises"] != pack2["exercises"]

    @pytest.mark.asyncio
    async def test_first_block_next_points_to_real_exercise_of_second(
        self, auth_client, two_intensive_blocks
    ):
        client, _ = auth_client
        first = two_intensive_blocks["first"]
        second = two_intensive_blocks["second"]

        pack1 = (await client.get(f"/api/classroom/intensive/{first}")).json()
        pack2 = (await client.get(f"/api/classroom/intensive/{second}")).json()
        nxt = pack1.get("next_block") or {}
        assert nxt.get("name") == second
        second_ids = [ex.get("id") for ex in pack2.get("exercises") or []]
        assert nxt.get("first_exercise") in second_ids, (
            "next_block.first_exercise не входит в упражнения второго блока. "
            f"next={nxt} ids={second_ids[:5]}"
        )

    @pytest.mark.asyncio
    async def test_wrong_file_name_in_db_is_visible(self, auth_client, db_pool):
        """Если file_name в БД не совпал с файлом — API не должен притворяться, что список просто пуст."""
        load_catalog()
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM intensive_blocks WHERE name = 'BGRUA1_broken_file'")
            await conn.execute("""
                INSERT INTO intensive_blocks
                    (name, title, block_type, after_lesson, sort_order, file_name,
                     permission, visibility)
                VALUES
                    ('BGRUA1_broken_file', 'Broken', 'X', 'BGRUA1006', 9,
                     'no_such_intensive_file', ARRAY[0,1,2], ARRAY[0,1,2])
            """)
        client, _ = auth_client
        res = await client.get("/api/classroom/intensive/BGRUA1_broken_file")
        assert res.status_code == 404
        assert "Нет файла блока" in res.text or "error" in res.json()


class TestIntensiveProgressFormat:
    def test_lang_prefix_ignores_json_exercise_ids(self):
        from app.repositories.classroom import lang_prefix_from_name

        assert lang_prefix_from_name("BGRUA1_text1_to_lesson06") == "BGRU"
        assert lang_prefix_from_name("BGRUA1002_Q001") == "BGRU"
        assert lang_prefix_from_name("word_01_synonym_01") is None

    def test_migrate_rewrites_flat_legacy_under_language_pair(self):
        from app.repositories.classroom import migrate_intensive_progress

        # Без at плоский legacy только раскладывается по паре, курсор не затирается.
        nested = migrate_intensive_progress(
            {"X": "word_01_synonym_01"},
            "BGRU",
            "X",
            "word_02_quiz_01",
        )
        assert "X" not in nested
        assert nested["BGRU"]["X"] == "word_01_synonym_01"

        # at есть только у сдвига вперёд — тогда пишется новый курсор и время.
        stored = migrate_intensive_progress(
            {"X": "word_01_synonym_01"},
            "BGRU",
            "X",
            "word_02_quiz_01",
            at="2026-10-04T01:43:20+03:00",
        )
        assert "X" not in stored
        assert stored["BGRU"]["X"] == {
            "name": "word_02_quiz_01",
            "at": "2026-10-04T01:43:20+03:00",
        }

    @pytest.mark.asyncio
    async def test_save_progress_uses_block_name_not_word_id(
        self, auth_client, db_pool, two_intensive_blocks
    ):
        client, user_id = auth_client
        first = two_intensive_blocks["first"]
        res = await client.post(
            "/api/user/intensive-progress",
            json={
                "exercise_name": "word_01_synonym_01",
                "series": "X",
                "block_name": first,
                "lang_prefix": "BGRU",
            },
            headers=_ensure_csrf(client),
        )
        assert res.status_code == 200
        body = res.json()
        assert body.get("success") is True
        assert body.get("lang_prefix") == "BGRU"

        raw = await db_pool.fetchval(
            "SELECT intensive_progress FROM users WHERE id = $1",
            user_id,
        )
        assert raw is not None
        data = raw if isinstance(raw, dict) else json.loads(raw)
        assert "X" not in data
        assert "BGRU" in data
        # В ячейке имя упражнения и время сдвига, не голая строка и не имя блока.
        assert data["BGRU"]["X"]["name"] == "word_01_synonym_01"
        assert data["BGRU"]["X"]["at"]
