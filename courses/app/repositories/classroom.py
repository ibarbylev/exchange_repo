import json
import asyncpg

from collections import defaultdict
from typing import Any

from app.repositories.intensive import get_exercise_ids


BLOCK_LABEL = {
    "X": "Text Intensive",
    "C": "Coach Online",
}

# Имена тарифов для подсказки у заблокированных узлов.
# 2: «Интенсив» — новое имя бывшего «Премиум».
ACCESS_LEVEL_NAMES = {
    0: "Базовый",
    1: "Стандарт",
    2: "Интенсив",
}

LOCK_REASON_TARIFF = "tariff"
LOCK_REASON_SEQUENCE = "sequence"
LOCK_HINT_SEQUENCE = "сначала пройдите предыдущий блок"

TREE_STATUS_COMPLETED = "completed"
TREE_STATUS_CURRENT = "current"
TREE_STATUS_LOCKED = "locked"

PLAYER_BY_BLOCK_TYPE = {
    "X": "text_intensive",
    "C": "coach_online",
}


async def get_theme_exercise_counts(conn, theme_name: str) -> dict:
    """
    Подсчитывает количество упражнений каждого типа по уровням доступа (permission).
    Возвращает только те типы упражнений, где хотя бы на одном уровне есть упражнения.
    Возвращает словарь вида:
        counts = {
        "V": [1, 1, 1],
        "Q": [2, 21, 21],
        "T": [2, 9, 9]
    }
    """
    rows = await conn.fetch("""
        SELECT exercise_type, unnest(permission) as permission_level, COUNT(*) as count
        FROM exercises
        WHERE theme_name = $1
        GROUP BY exercise_type, permission_level
    """, theme_name)

    counts = {
        "V": [0, 0, 0],
        "Q": [0, 0, 0],
        "T": [0, 0, 0],
    }

    for row in rows:
        ex_type = row["exercise_type"]
        perm = row["permission_level"]
        count = row["count"]

        if ex_type in counts and 0 <= perm <= 2:
            counts[ex_type][perm] = count

    return {
        ex_type: count_list
        for ex_type, count_list in counts.items()
        if any(count_list)
    }


def _parse_json_map(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def lang_prefix_from_name(name: str | None) -> str | None:
    """Языковая пара из имени курса/блока/упражнения: BGRUA1_... → BGRU.

    Id упражнений интенсива (word_01_synonym_01) пары не содержат.
    """
    import re

    text = str(name or "").strip().upper()
    match = re.match(r"^([A-Z]{4})[A-Z]\d", text)
    if match:
        return match.group(1)
    return None


def _series_cursor_value(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("exercise") or raw.get("block") or raw.get("name")
    text = str(raw or "").strip()
    return text or None


def _legacy_flat_cursors(data: dict) -> dict[str, str]:
    """Старый формат {"X": "..."} без ключа языковой пары."""
    result: dict[str, str] = {}
    text = _series_cursor_value(data.get("X"))
    if text is None:
        text = _series_cursor_value(data.get("x"))
    if text:
        result["X"] = text
    return result


def _pair_without_c(value: Any) -> dict[str, Any]:
    """Копия прогресса пары без ключа C — он больше не источник правды."""
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if str(key or "").strip().upper() == "C":
            continue
        cleaned[key] = item
    return cleaned


def _intensive_cursors(
        progress: dict | None,
        lang_prefix: str | None = None,
) -> dict[str, str | None]:
    """
    intensive_progress по языковой паре — только курсор Text Intensive (X):

        {"BGRU": {"X": "BGRUA1_text2_…"}}

    Прогресс C живёт в coach_lessons, ключ C в JSON не читаем.
    Пара появляется только после первого сохранения прогресса по ней.
    Старый плоский вид {"X": "..."} читается как legacy:
    значение учитывается, только если имя курсора принадлежит этой паре.
    """
    data = _parse_json_map(progress)
    result: dict[str, str | None] = {"X": None}
    prefix = (lang_prefix or "").strip().upper() or None

    pair = None
    if prefix:
        raw_pair = data.get(prefix)
        if raw_pair is None:
            raw_pair = data.get(prefix.lower())
        if isinstance(raw_pair, dict):
            pair = raw_pair

    if pair is not None:
        source = pair
    else:
        source = _legacy_flat_cursors(data)

    text = _series_cursor_value(source.get("X"))
    if text is None:
        text = _series_cursor_value(source.get("x"))
    if text and prefix and not text.upper().startswith(prefix):
        if pair is None and lang_prefix_from_name(text):
            text = None
    result["X"] = text or None
    return result


def migrate_intensive_progress(
        progress: dict | None,
        lang_prefix: str,
        series: str,
        cursor: str,
) -> dict:
    """
    Пишет курсор Text Intensive (X) в прогресс пары. Другие пары не создаёт.
    Плоский legacy {"X": "..."} при первой записи раскладывает по префиксу
    имени курсора, чтобы не потерять уже сохранённый прогресс X.
    """
    data = _parse_json_map(progress)
    stored: dict[str, Any] = {}
    for key, value in data.items():
        key_text = str(key or "").strip().upper()
        if key_text in {"X", "C"} or not isinstance(value, dict):
            continue
        stored[key_text] = _pair_without_c(value)

    prefix = (lang_prefix or "").strip().upper()
    for legacy_series, legacy_cursor in _legacy_flat_cursors(data).items():
        if legacy_series != "X":
            continue
        legacy_prefix = lang_prefix_from_name(legacy_cursor) or prefix
        if not legacy_prefix:
            continue
        pair = stored.setdefault(legacy_prefix, {})
        pair.setdefault(legacy_series, legacy_cursor)

    series = (series or "X").upper()
    if series != "X" or not prefix:
        return stored
    pair = stored.setdefault(prefix, _pair_without_c(stored.get(prefix)))
    pair["X"] = cursor
    stored[prefix] = _pair_without_c(pair)
    return stored


def parse_current_exercise_map(value: Any) -> dict[str, str]:
    """
    current_exercise:
      - устаревшая строка "BGRUA1002_Q001"
      - карта {"BGRU": "BGRUA1002_Q001"}
    Пара появляется только после первого сохранения по ней.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            data = _parse_json_map(text)
        else:
            prefix = lang_prefix_from_name(text)
            return {prefix: text} if prefix else {}
    else:
        data = _parse_json_map(value)

    result: dict[str, str] = {}
    for key, raw in data.items():
        prefix = str(key or "").strip().upper()
        name = str(raw or "").strip()
        if len(prefix) == 4 and prefix.isalpha() and name:
            result[prefix] = name
    return result


def current_exercise_for_prefix(value: Any, lang_prefix: str | None) -> str | None:
    prefix = (lang_prefix or "").strip().upper()
    mapping = parse_current_exercise_map(value)
    if prefix:
        return mapping.get(prefix)
    if len(mapping) == 1:
        return next(iter(mapping.values()))
    return None


def _star_value(raw: Any) -> int | None:
    try:
        stars = int(raw)
    except (TypeError, ValueError):
        return None
    return stars if stars in (1, 2, 3) else None


def aggregate_stars(exercise_names: list[str] | None, stars_map: dict | None) -> int | None:
    """
    Звезда уровня = min по всем ключам.
    Нет оценки хотя бы у одного ключа → звёзд у уровня нет.
    """
    names = [str(name) for name in (exercise_names or []) if name]
    if not names:
        return None
    values = []
    mapping = stars_map or {}
    for name in names:
        stars = _star_value(mapping.get(name))
        if stars is None:
            return None
        values.append(stars)
    return min(values) if values else None


def interactive_theme_stars(
        theme_name: str,
        test_names: list[str] | None,
        stars_map: dict | None,
) -> int | None:
    """
    В интерактиве звезда ставится теме и считается только по тесту (T).
    Сначала смотрим ключ темы, затем fallback на оценки T-упражнений.
    """
    mapping = stars_map or {}
    direct = _star_value(mapping.get(theme_name))
    if direct is not None:
        return direct
    return aggregate_stars(test_names, mapping)


def _block_exercise_ids(file_name: str | None, block_type: str | None = None) -> list[str]:
    """Id упражнений из JSON-каталога. Для C каталог не спрашиваем."""
    if not file_name:
        return []
    if (block_type or "").upper() == "C":
        return []
    return get_exercise_ids(file_name)


def _permission_levels(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    levels: list[int] = []
    for item in raw:
        try:
            levels.append(int(item))
        except (TypeError, ValueError):
            continue
    return levels


def _required_plan_name(permission: Any) -> str:
    levels = _permission_levels(permission)
    if not levels:
        return ACCESS_LEVEL_NAMES[2]
    return ACCESS_LEVEL_NAMES.get(min(levels), ACCESS_LEVEL_NAMES[2])


def _lock_hint(reason: str | None, permission: Any = None) -> str:
    if reason == LOCK_REASON_SEQUENCE:
        return LOCK_HINT_SEQUENCE
    if reason == LOCK_REASON_TARIFF:
        return f"Доступно для {_required_plan_name(permission)}"
    return ""


def _cursor_series_index(blocks: list[dict], cursor: str | None) -> int:
    """Индекс текущего блока в серии. Нет курсора — открыт первый блок (0)."""
    if not blocks:
        return 0
    if not cursor:
        return 0
    for index, block in enumerate(blocks):
        if block.get("name") == cursor:
            return index
        if cursor in (block.get("exercise_ids") or []):
            return index
    return 0


def _series_tree_status(block: dict, series_index: int, cursor_index: int) -> tuple[str, str]:
    tariff_ok = bool(block.get("is_clickable", True))
    if series_index < cursor_index:
        progress_status = TREE_STATUS_COMPLETED
    elif series_index == cursor_index:
        progress_status = TREE_STATUS_CURRENT
    else:
        progress_status = TREE_STATUS_LOCKED

    if not tariff_ok:
        return TREE_STATUS_LOCKED, LOCK_REASON_TARIFF
    if progress_status == TREE_STATUS_LOCKED:
        return TREE_STATUS_LOCKED, LOCK_REASON_SEQUENCE
    return progress_status, ""


def _intensive_lesson_node(
        block: dict,
        tree_status: str,
        stars: int | None = None,
        series_index: int = 0,
        lock_reason: str = "",
) -> dict:
    """Узел на уровне урока."""
    block_type = (block.get("block_type") or "X").upper()
    extra = ["intensive-block", f"intensive-{block_type.lower()}", tree_status]

    if block_type == "C":
        icon = "fa-solid fa-person-chalkboard"
    else:
        icon = "fa-solid fa-chalkboard-user"

    title_text = block.get("title") or block.get("name")
    label = BLOCK_LABEL.get(block_type, "Intensive")
    series_label = f"{label} {series_index + 1:02d}"
    tariff_ok = bool(block.get("is_clickable", True))
    lock_hint = _lock_hint(lock_reason, block.get("permission"))
    if tree_status == TREE_STATUS_LOCKED and lock_reason:
        extra.append(f"locked-{lock_reason}")

    return {
        "name": block["name"],
        "title": f"{series_label}: {title_text}",
        "series_label": series_label,
        "title_text": title_text,
        "kind": "intensive",
        "block_type": block_type,
        "player": PLAYER_BY_BLOCK_TYPE.get(block_type, "text_intensive"),
        "file_name": block.get("file_name"),
        "after_lesson": block.get("after_lesson"),
        "exercise_ids": list(block.get("exercise_ids") or []),
        "stars": stars,
        "open_class": "open" if tree_status == TREE_STATUS_CURRENT else "",
        "is_clickable": tariff_ok and tree_status != TREE_STATUS_LOCKED,
        "is_completed": tree_status == TREE_STATUS_COMPLETED,
        "is_current": tree_status == TREE_STATUS_CURRENT,
        "is_available": tree_status == TREE_STATUS_CURRENT,
        "tree_status": tree_status,
        "lock_reason": lock_reason,
        "lock_hint": lock_hint,
        "icon_class": icon,
        "extra_classes": " ".join(extra),
        "themes": [],
    }


def _first_open_c_index(blocks: list[dict], completed_names: set[str] | None) -> int:
    """Индекс первого незакрытого C-блока по coach_lessons. Нет записей — открыт первый."""
    completed = completed_names or set()
    for index, block in enumerate(blocks):
        name = block.get("name")
        if name and name not in completed:
            return index
    return len(blocks)


def _insert_intensive_blocks(
        lesson_nodes: list[dict],
        blocks: list,
        progress: dict | None = None,
        stars_map: dict | None = None,
        lang_prefix: str | None = None,
        coach_completed_names: set[str] | None = None,
) -> list[dict]:
    """Вставляет блоки intensive после after_lesson.

    X — курсор users.intensive_progress.
    C — только статусы coach_lessons. Курсор C в JSON не используем.
    Нет закрытых занятий — на дереве открыт первый C-блок.
    """
    cursors = _intensive_cursors(progress, lang_prefix)
    by_after = defaultdict(list)
    prepared = []
    for raw in blocks:
        block = dict(raw)
        block_type = (block.get("block_type") or "X").upper()
        block["block_type"] = block_type
        if block_type == "C":
            block["exercise_ids"] = []
        else:
            block["exercise_ids"] = _block_exercise_ids(block.get("file_name"), block_type)
        prepared.append(block)
        by_after[block["after_lesson"]].append(block)
    for group in by_after.values():
        group.sort(key=lambda b: (b.get("sort_order") or 0, b.get("name") or ""))

    ordered = {"X": [], "C": []}
    for lesson in lesson_nodes:
        for block in by_after.get(lesson["name"], []):
            ordered[block["block_type"]].append(block)

    x_cursor_index = _cursor_series_index(ordered["X"], cursors.get("X"))
    first_open_c = _first_open_c_index(ordered["C"], coach_completed_names)
    seen = {"X": 0, "C": 0}

    inserted = []
    for lesson in lesson_nodes:
        inserted.append(lesson)
        for block in by_after.get(lesson["name"], []):
            block_type = block["block_type"]
            series_index = seen[block_type]
            seen[block_type] += 1
            if block_type == "C":
                status, lock_reason = _series_tree_status(block, series_index, first_open_c)
            else:
                status, lock_reason = _series_tree_status(block, series_index, x_cursor_index)
            stars = aggregate_stars(block.get("exercise_ids"), stars_map)
            inserted.append(_intensive_lesson_node(
                block, status, stars, series_index=series_index, lock_reason=lock_reason
            ))
    return inserted


def series_exercise_order(blocks: list[dict]) -> list[tuple[str, str]]:
    """
    Плоский порядок упражнений серии: (block_name, exercise_id).
    Если у блока нет упражнений, в порядок попадает сам block_name.
    """
    order: list[tuple[str, str]] = []
    for block in blocks:
        name = block.get("name")
        ids = block.get("exercise_ids") or _block_exercise_ids(
            block.get("file_name"),
            block.get("block_type"),
        )
        if ids:
            for exercise_id in ids:
                order.append((name, exercise_id))
        elif name:
            order.append((name, name))
    return order


def cursor_rank(order: list[tuple[str, str]], cursor: str | None) -> int:
    """Позиция курсора в плоском порядке. Нет курсора = -1 (можно записать первый)."""
    if not cursor:
        return -1
    for index, (block_name, exercise_id) in enumerate(order):
        if cursor == exercise_id or cursor == block_name:
            return index
    return -1


async def load_user_json_field(conn, user_id: int | None, column: str) -> dict:
    if not user_id or column not in {"intensive_progress", "exercise_stars"}:
        return {}
    try:
        row = await conn.fetchval(
            f"SELECT {column} FROM users WHERE id = $1",
            user_id,
        )
    except Exception:
        return {}
    return _parse_json_map(row)


async def _load_theme_exercise_names(conn, theme_names: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {name: [] for name in theme_names}
    if not theme_names:
        return result
    rows = await conn.fetch(
        """
        SELECT theme_name, name
        FROM exercises
        WHERE theme_name = ANY($1::varchar[])
        ORDER BY pos
        """,
        theme_names,
    )
    for row in rows:
        result.setdefault(row["theme_name"], []).append(row["name"])
    return result


async def _load_theme_test_names(conn, theme_names: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {name: [] for name in theme_names}
    if not theme_names:
        return result
    rows = await conn.fetch(
        """
        SELECT theme_name, name
        FROM exercises
        WHERE theme_name = ANY($1::varchar[])
          AND exercise_type = 'T'
        ORDER BY pos
        """,
        theme_names,
    )
    for row in rows:
        result.setdefault(row["theme_name"], []).append(row["name"])
    return result


async def build_classroom_tree(
        courses: list,
        lessons: list,
        themes: list,
        conn,
        current_theme_pos: int | None = None,
        current_theme_name: str | None = None,
        intensive_blocks: list | None = None,
        intensive_progress: dict | None = None,
        exercise_stars: dict | None = None,
        theme_exercise_names: dict[str, list[str]] | None = None,
        theme_test_names: dict[str, list[str]] | None = None,
        lang_prefix: str | None = None,
        coach_completed_names: set[str] | None = None,
) -> dict:
    """
    Вспомогательная функция для функции get_classroom_tree:
    формирует из списков курсов, уроков и тем удобное дерево.
        Подробнее в README/04_classroom.md

    Структура дерева:
    {
        "courses": [
            {
                "name": str,
                "title": str,
                "open_class": str,
                "lessons": [
                    {
                        "name": str,
                        "title": str,
                        "open_class": str,
                        "is_clickable": bool,
                        "is_completed": bool,
                        "is_current": bool,
                        "is_available": bool,
                        "icon_class": str,
                        "extra_classes": str,      # "completed locked current"
                        "themes": [
                            {
                                "name": str,
                                "title": str,
                                "pos": int,
                                "open_class": str,
                                "is_clickable": bool,
                                "is_completed": bool,
                                "is_current": bool,
                                "is_available": bool,
                                "icon_class": str,
                                "extra_classes": str     # "completed locked current"
                                "exercise_counts": dict     # статистика упражнений темы
                            }
                        ]
                    }
                ]
            }
        ]
    }
    """

    lessons_by_course = defaultdict(list)
    for lesson in lessons:
        lessons_by_course[lesson["course_name"]].append(dict(lesson))

    themes_by_lesson = defaultdict(list)
    for theme in themes:
        themes_by_lesson[theme["lesson_name"]].append(dict(theme))

    stars_map = _parse_json_map(exercise_stars)
    names_by_theme = theme_exercise_names or {}
    tests_by_theme = theme_test_names or {}

    result = []

    for course in courses:
        course_dict = {
            "name": course["name"],
            "title": course["title"],
            "open_class": "",
            "lessons": []
        }

        for lesson in lessons_by_course.get(course["name"], []):
            lesson_themes = themes_by_lesson.get(lesson["name"], [])

            # === Вычисляем статус урока ===
            lesson_clickable = lesson.get("is_clickable", True)

            lesson_completed = lesson_current = False
            if current_theme_pos is not None and current_theme_name is not None:
                lesson_completed = all(t["pos"] < current_theme_pos for t in lesson_themes)
                lesson_current = any(t["name"] == current_theme_name for t in lesson_themes)

            # === Определяем, нужно ли раскрывать курс и урок ===
            lesson_open_class = "open" if lesson_current else ""
            if lesson_current:
                course_dict["open_class"] = "open"

            # Формируем классы и иконку для урока
            lesson_extra_classes = []
            if lesson_completed:
                lesson_extra_classes.append("completed")
            if lesson_current:
                lesson_extra_classes.append("current")
            lesson_lock_reason = LOCK_REASON_TARIFF if not lesson_clickable else ""
            lesson_lock_hint = _lock_hint(lesson_lock_reason, lesson.get("permission"))
            if lesson_lock_reason:
                lesson_extra_classes.append("locked")
                lesson_extra_classes.append(f"locked-{lesson_lock_reason}")

            if not lesson_clickable:
                lesson_icon = "zmdi zmdi-lock text-muted"
            elif lesson_completed:
                lesson_icon = "zmdi zmdi-folder text-success"
            else:
                lesson_icon = "zmdi zmdi-folder"

            lesson_theme_stars: list[int] = []
            lesson_stars_ready = bool(lesson_themes)
            for theme in lesson_themes:
                theme_star = interactive_theme_stars(
                    theme["name"],
                    tests_by_theme.get(theme["name"], []),
                    stars_map,
                )
                if theme_star is None:
                    lesson_stars_ready = False
                    break
                lesson_theme_stars.append(theme_star)

            lesson_dict = {
                "name": lesson["name"],
                "title": lesson["title"],
                "kind": "lesson",
                "open_class": lesson_open_class,
                "is_clickable": lesson_clickable,
                "is_completed": lesson_completed,
                "is_current": lesson_current,
                "is_available": lesson_clickable,
                "stars": min(lesson_theme_stars) if lesson_stars_ready and lesson_theme_stars else None,
                "lock_reason": lesson_lock_reason,
                "lock_hint": lesson_lock_hint,
                "icon_class": lesson_icon,
                "extra_classes": " ".join(lesson_extra_classes),
                "themes": []
            }

            for theme in lesson_themes:
                theme_pos = theme["pos"]
                theme_clickable = theme.get("is_clickable", True)

                is_completed = bool(current_theme_pos) and theme_pos < current_theme_pos
                is_current = theme["name"] == current_theme_name

                theme_lock_reason = LOCK_REASON_TARIFF if not theme_clickable else ""
                theme_lock_hint = _lock_hint(theme_lock_reason, theme.get("permission"))
                theme_extra_classes = []
                if is_completed:
                    theme_extra_classes.append("completed")
                if theme_lock_reason:
                    theme_extra_classes.append("locked")
                    theme_extra_classes.append(f"locked-{theme_lock_reason}")
                if is_current:
                    theme_extra_classes.append("current")

                if is_completed:
                    theme_icon = "zmdi zmdi-folder-outline text-success"
                elif not theme_clickable:
                    theme_icon = "zmdi zmdi-lock text-muted"
                else:
                    theme_icon = "zmdi zmdi-folder-outline"

                # === Подсчёт статистики упражнений по уровням доступа ===
                exercise_counts = await get_theme_exercise_counts(conn, theme["name"])

                theme_dict = {
                    "name": theme["name"],
                    "title": theme["title"],
                    "pos": theme_pos,
                    "player": "vqt",
                    "is_clickable": theme_clickable,
                    "is_completed": is_completed,
                    "is_current": is_current,
                    "is_available": theme_clickable,
                    "stars": interactive_theme_stars(
                        theme["name"],
                        tests_by_theme.get(theme["name"], []),
                        stars_map,
                    ),
                    "lock_reason": theme_lock_reason,
                    "lock_hint": theme_lock_hint,
                    "icon_class": theme_icon,
                    "extra_classes": " ".join(theme_extra_classes),
                    "exercise_counts": exercise_counts,
                }
                lesson_dict["themes"].append(theme_dict)

            course_dict["lessons"].append(lesson_dict)

        course_blocks = [
            b for b in (intensive_blocks or [])
            if str(b.get("after_lesson") or "").startswith(course["name"])
        ]
        course_dict["lessons"] = _insert_intensive_blocks(
            course_dict["lessons"],
            course_blocks,
            intensive_progress,
            stars_map,
            lang_prefix,
            coach_completed_names,
        )

        result.append(course_dict)

    return {"courses": result}


async def get_classroom_tree(
        pool: asyncpg.Pool,
        lang_prefix: str,
        user_access_level: int,
        target_exercise: str | None = None,
        user_id: int | None = None,
        intensive_progress: dict | None = None,
        exercise_stars: dict | None = None,
) -> dict:
    """
    Получает из БД списки курсов, уроков, тем и формирует из них дерево,
    с помощью функции  build_classroom_tree.
    """
    async with pool.acquire() as conn:

        # Курсы (без фильтрации)
        courses = await conn.fetch(
            "SELECT name, title FROM courses WHERE name LIKE $1 || '%' ORDER BY name",
            lang_prefix
        )

        # Уроки
        lessons = await conn.fetch(
            """
            SELECT 
                name, 
                course_name, 
                title,
                permission,
                ($2 = ANY(permission)) AS is_clickable
            FROM lessons
            WHERE course_name LIKE $1 || '%'
              AND $2 = ANY(visibility)
            ORDER BY course_name, name
            """,
            lang_prefix, user_access_level
        )

        # Темы
        themes = await conn.fetch(
            """
            SELECT 
                name, 
                lesson_name, 
                title, 
                pos,
                permission,
                ($2 = ANY(permission)) AS is_clickable
            FROM themes
            WHERE lesson_name LIKE $1 || '%'
              AND $2 = ANY(visibility)
            ORDER BY lesson_name, pos
            """,
            lang_prefix, user_access_level
        )

        intensive_blocks = await conn.fetch(
            """
            SELECT
                name,
                title,
                block_type,
                after_lesson,
                sort_order,
                file_name,
                permission,
                ($2 = ANY(permission)) AS is_clickable
            FROM intensive_blocks
            WHERE after_lesson LIKE $1 || '%'
              AND $2 = ANY(visibility)
            ORDER BY after_lesson, sort_order, name
            """,
            lang_prefix, user_access_level
        )

        # === Определяем pos текущей темы ===
        if target_exercise is None:

            # Находим самое первое Q-упражнение для текущей языковой пары:
            target_exercise = await conn.fetchval(
                "SELECT name FROM exercises WHERE name LIKE $1 ORDER BY pos LIMIT 1",
                f"%{lang_prefix.upper()}%Q%"
            )

        current_theme = await conn.fetchrow("""
                SELECT t.pos, t.name
                FROM exercises e
                         JOIN themes t ON t.name = e.theme_name
                WHERE e.name = $1
                """, target_exercise)

        current_theme_pos = current_theme["pos"] if current_theme else None
        current_theme_name = current_theme["name"] if current_theme else None

        if intensive_progress is None:
            intensive_progress = await load_user_json_field(conn, user_id, "intensive_progress")
        else:
            intensive_progress = _parse_json_map(intensive_progress)

        if exercise_stars is None:
            exercise_stars = await load_user_json_field(conn, user_id, "exercise_stars")
        else:
            exercise_stars = _parse_json_map(exercise_stars)

        all_theme_names = [theme["name"] for theme in themes]
        theme_exercise_names = await _load_theme_exercise_names(conn, all_theme_names)
        theme_test_names = await _load_theme_test_names(conn, all_theme_names)

        coach_completed_names: set[str] = set()
        if user_id:
            coach_rows = await conn.fetch(
                """
                SELECT lesson_name
                FROM coach_lessons
                WHERE student_id = $1
                  AND status = 'completed'
                """,
                user_id,
            )
            coach_completed_names = {row["lesson_name"] for row in coach_rows}

        tree = await build_classroom_tree(
            courses=courses,
            lessons=lessons,
            themes=themes,
            conn=conn,
            current_theme_pos=current_theme_pos,
            current_theme_name=current_theme_name,
            intensive_blocks=intensive_blocks,
            intensive_progress=intensive_progress,
            exercise_stars=exercise_stars,
            theme_exercise_names=theme_exercise_names,
            theme_test_names=theme_test_names,
            lang_prefix=lang_prefix,
            coach_completed_names=coach_completed_names,
        )

    return tree


async def list_series_blocks(conn, series: str, lang_prefix: str | None = None) -> list[dict]:
    series = (series or "X").upper()
    if series not in {"X", "C"}:
        series = "X"
    if lang_prefix:
        rows = await conn.fetch(
            """
            SELECT name, title, block_type, after_lesson, sort_order, file_name
            FROM intensive_blocks
            WHERE block_type = $1
              AND after_lesson LIKE $2 || '%'
            ORDER BY after_lesson, sort_order, name
            """,
            series, lang_prefix,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT name, title, block_type, after_lesson, sort_order, file_name
            FROM intensive_blocks
            WHERE block_type = $1
            ORDER BY after_lesson, sort_order, name
            """,
            series,
        )
    blocks = []
    for row in rows:
        item = dict(row)
        item["exercise_ids"] = _block_exercise_ids(item.get("file_name"), series)
        blocks.append(item)
    return blocks
