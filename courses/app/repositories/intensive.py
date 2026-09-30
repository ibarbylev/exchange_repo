"""Чтение JSON-блоков Text Intensive.

Файлы:
    {SHARED_DIR}/intensive_data/json/{file_name}.json
    {SHARED_DIR}/intensive_data/html/{file_name}.html

Аудио в JSON — короткое имя без расширения и папок, например:
    "BGRUA1_text1_to_lesson06"

Публичный URL:
    /media/audio/{course}/{audio_name}.mp3

Каталог — только для блоков X. HTML-проекторы C в JSON не ищем.
Загрузка с диска — один раз в lifespan при старте воркера.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import settings

DATA_DIR = settings.SHARED_DIR / "intensive_data" / "json"
HTML_DIR = settings.SHARED_DIR / "intensive_data" / "html"
MEDIA_AUDIO_URL_PREFIX = "/media/audio"
EXCLUDED_INTENSIVE_EXERCISES = ("summary",)
SKIP_FILES = frozenset({"exercises.json"})

_BLOCKS: dict[str, dict[str, Any]] = {}


def playable_exercises(exercises: list | None) -> list:
    """Список упражнений без типов из EXCLUDED_INTENSIVE_EXERCISES.

    Поддерживает и массив объектов, и словарь id → объект.
    Словарь раньше молча превращался в пустой список: цикл шёл по ключам-строкам.
    """

    excluded = {str(item).strip().lower() for item in EXCLUDED_INTENSIVE_EXERCISES}
    if isinstance(exercises, dict):
        items = []
        for key, value in exercises.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault("id", key)
            items.append(item)
        exercises = items
    result = []

    for item in exercises or []:
        if not isinstance(item, dict):
            continue
        exercise_type = str(item.get("type") or "").strip().lower()
        if exercise_type in excluded:
            continue
        result.append(item)
    return result


def get_audio_url(course: str, audio_name: str | None) -> str | None:
    """
    Строит публичный URL аудиофайла.

    :param course:     имя курса (папка), например "BGRUA1"
    :param audio_name: короткое имя без расширения
    :return:           "/media/audio/BGRUA1/BGRUA1_text1_to_lesson06.mp3" или None
    """
    if not audio_name:
        return None
    name = audio_name.removesuffix(".mp3").removesuffix(".wav").removesuffix(".ogg")
    return f"{MEDIA_AUDIO_URL_PREFIX}/{course}/{name}.mp3"


def _catalog_key(file_name: str | None) -> str:
    """Stem имени блока без пути и суффикса .json/.html."""
    name = (file_name or "").strip()
    name = name.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.endswith(".json"):
        name = name[:-5]
    elif lower.endswith(".html"):
        name = name[:-5]
    return name.strip()


def _normalize_block(file_name: str, data: dict[str, Any]) -> dict[str, Any]:
    name = file_name.removesuffix(".json").strip()
    data.setdefault("course", name.split("_")[0] if "_" in name else "")
    data.setdefault("text_block", name)
    data.setdefault("title", name)
    data.setdefault("audio", None)
    data.setdefault("exercises", [])
    data.setdefault("transcript", "")
    data["exercises"] = playable_exercises(data.get("exercises") or [])
    data["audio_url"] = get_audio_url(data.get("course") or "", data.get("audio"))
    data["exercise_ids"] = [
        str(item.get("id"))
        for item in data["exercises"]
        if isinstance(item, dict) and item.get("id")
    ]
    return data


def load_catalog() -> int:
    """Читает JSON блоков в память. Вызывается из lifespan при старте."""
    _BLOCKS.clear()
    if not DATA_DIR.exists():
        print(f"→ Intensive catalog: directory not found: {DATA_DIR}")
        return 0

    count = 0
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name in SKIP_FILES:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            print(f"→ Intensive catalog: skip {path.name}: {exc}")
            continue
        if not isinstance(raw, dict):
            print(f"→ Intensive catalog: skip {path.name}: not an object")
            continue
        key = path.stem
        _BLOCKS[key] = _normalize_block(key, raw)
        count += 1

    print(f"→ Intensive catalog loaded: {count} block(s)")
    return count


def get_block(file_name: str) -> dict[str, Any]:
    """Пакет блока из каталога в памяти. Копия, чтобы не менять кэш."""
    name = _catalog_key(file_name)
    if not name:
        raise FileNotFoundError("file_name пустой")
    pack = _BLOCKS.get(name)
    if pack is None:
        raise FileNotFoundError(f"Нет файла блока: {name}.json")
    result = dict(pack)
    result["exercises"] = list(pack.get("exercises") or [])
    result["exercise_ids"] = list(pack.get("exercise_ids") or [])
    return result


def get_exercise_ids(file_name: str) -> list[str]:
    """Id упражнений блока X. Пустой список, если ключа нет в каталоге."""
    name = _catalog_key(file_name)
    if not name:
        return []
    pack = _BLOCKS.get(name)
    if pack is None:
        return []
    return list(pack.get("exercise_ids") or [])


def html_file_path(file_name: str):
    """Путь к HTML-проектору коуча."""
    name = (file_name or "").strip().rsplit("/", 1)[-1]
    if not name:
        raise FileNotFoundError("file_name пустой")
    if not name.lower().endswith(".html"):
        name = f"{name}.html"
    return HTML_DIR / name


def load_html_block(file_name: str) -> str:
    """Содержимое HTML-проектора без обёртки страницы."""
    path = html_file_path(file_name)
    if not path.exists():
        raise FileNotFoundError(f"Нет HTML-блока: {path.name}")
    return path.read_text(encoding="utf-8")


def checkbox_ids(html: str) -> list[str]:
    """id из data-checkbox в порядке появления, без повторов."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in re.findall(r'data-checkbox="([^"]+)"', html or ""):
        checkbox_id = raw.strip()
        if not checkbox_id or checkbox_id in seen:
            continue
        seen.add(checkbox_id)
        result.append(checkbox_id)
    return result
