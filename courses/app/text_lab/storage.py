"""
storage.py — работа с JSON-хранилищем текстовых упражнений (лаборатория).

Путь к файлу данных: courses/app/text_lab/data/exercises.json

Аудио:
    В JSON хранится только короткое имя файла без расширения и папок,
    например: "BGRUA1_text1_to_lesson06"

    Реальный файл лежит в общем media-хранилище:
    media/audio/{course_name}/{audio_name}.mp3

    Пример: media/audio/BGRUA1/BGRUA1_text1_to_lesson06.mp3
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "exercises.json"

# Базовый URL, по которому фронтэнд будет запрашивать аудио.
# В реальном проекте этот префикс обычно отдаёт nginx / FastAPI StaticFiles
# из общего volume media/.
MEDIA_AUDIO_URL_PREFIX = "/media/audio"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _default_data() -> dict[str, Any]:
    """Стартовая структура, если файла ещё нет."""
    return {
        "course": "BGRUA1",
        "text_block": "word_01",
        "title": "Текстовый блок Word 01",
        "audio": "BGRUA1_text1_to_lesson06",  # без расширения и папок
        "exercises": [],
    }


def get_audio_url(course: str, audio_name: str | None) -> str | None:
    """
    Строит публичный URL аудиофайла.

    :param course:     имя курса (папка), например "BGRUA1"
    :param audio_name: короткое имя без расширения, например "BGRUA1_text1_to_lesson06"
    :return:           "/media/audio/BGRUA1/BGRUA1_text1_to_lesson06.mp3" или None
    """
    if not audio_name:
        return None
    # На всякий случай убираем возможное расширение, если кто-то сохранил с ним
    name = audio_name.removesuffix(".mp3").removesuffix(".wav").removesuffix(".ogg")
    return f"{MEDIA_AUDIO_URL_PREFIX}/{course}/{name}.mp3"


# ---------------------------------------------------------------------------
# Основное API
# ---------------------------------------------------------------------------

def load() -> dict[str, Any]:
    """
    Загружает весь JSON.
    Если файла нет — создаёт его со стартовой структурой.
    """
    _ensure_data_dir()

    if not DATA_FILE.exists():
        data = _default_data()
        save(data)
        return data

    with DATA_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Гарантируем наличие обязательных ключей
    data.setdefault("course", "BGRUA1")
    data.setdefault("text_block", "word_01")
    data.setdefault("title", "Текстовый блок")
    data.setdefault("audio", None)
    data.setdefault("exercises", [])
    data.setdefault("transcript", "")

    return data


def save(data: dict[str, Any]) -> None:
    """Атомарно сохраняет данные в JSON."""
    _ensure_data_dir()

    # Пишем во временный файл, потом заменяем — защита от обрыва записи
    tmp_file = DATA_FILE.with_suffix(".tmp")
    with tmp_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_file.replace(DATA_FILE)


def get_block_info() -> dict[str, Any]:
    """
    Возвращает метаинформацию о текущем текстовом блоке
    (без списка упражнений) + готовый URL аудио.
    """
    data = load()
    return {
        "course": data["course"],
        "text_block": data["text_block"],
        "title": data["title"],
        "audio": data.get("audio"),
        "audio_url": get_audio_url(data["course"], data.get("audio")),
        "transcript": data.get("transcript") or "",
    }


def list_exercises() -> list[dict[str, Any]]:
    """Возвращает список всех упражнений текущего блока."""
    return load()["exercises"]


def get_exercise(exercise_id: str) -> dict[str, Any] | None:
    """Находит упражнение по id. Возвращает None, если не найдено."""
    for ex in load()["exercises"]:
        if ex.get("id") == exercise_id:
            return ex
    return None


def add_exercise(exercise: dict[str, Any]) -> dict[str, Any]:
    """
    Добавляет новое упражнение.
    Если id не передан — генерирует его.
    Возвращает созданное упражнение.
    """
    data = load()

    if not exercise.get("id"):
        # Генерируем короткий читаемый id
        prefix = data["text_block"]
        short_uuid = uuid.uuid4().hex[:6]
        exercise["id"] = f"{prefix}_{exercise.get('type', 'ex')}_{short_uuid}"

    # Простая защита от дублей
    existing_ids = {ex["id"] for ex in data["exercises"]}
    if exercise["id"] in existing_ids:
        raise ValueError(f"Упражнение с id={exercise['id']} уже существует")

    data["exercises"].append(exercise)
    save(data)
    return exercise


def update_exercise(exercise_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Обновляет существующее упражнение. Возвращает обновлённое или None."""
    data = load()
    for i, ex in enumerate(data["exercises"]):
        if ex.get("id") == exercise_id:
            # id менять нельзя
            updates.pop("id", None)
            data["exercises"][i] = {**ex, **updates}
            save(data)
            return data["exercises"][i]
    return None


def delete_exercise(exercise_id: str) -> bool:
    """Удаляет упражнение. Возвращает True, если удалили."""
    data = load()
    original_len = len(data["exercises"])
    data["exercises"] = [ex for ex in data["exercises"] if ex.get("id") != exercise_id]
    if len(data["exercises"]) < original_len:
        save(data)
        return True
    return False


def set_block_audio(audio_name: str) -> None:
    """
    Устанавливает короткое имя аудиофайла для всего блока.
    Пример: set_block_audio("BGRUA1_text1_to_lesson06")
    """
    data = load()
    data["audio"] = audio_name.removesuffix(".mp3").removesuffix(".wav")
    save(data)


def set_block_meta(
    *,
    course: str | None = None,
    text_block: str | None = None,
    title: str | None = None,
    audio: str | None = None,
) -> dict[str, Any]:
    """Обновляет метаданные блока."""
    data = load()
    if course is not None:
        data["course"] = course
    if text_block is not None:
        data["text_block"] = text_block
    if title is not None:
        data["title"] = title
    if audio is not None:
        data["audio"] = audio.removesuffix(".mp3").removesuffix(".wav")
    save(data)
    return get_block_info()
