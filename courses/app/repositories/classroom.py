import json
import asyncpg

from collections import defaultdict
from typing import Any


BLOCK_LABEL = {
    "X": "Text Intensive",
    "C": "Coach Online",
}

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


def _star_value(raw: Any) -> int | None:
    try:
        stars = int(raw)
    except (TypeError, ValueError):
        return None
    return stars if stars in (1, 2, 3) else None


def aggregate_stars(exercise_names: list[str] | None, stars_map: dict | None) -> int | None:
    """
    Звезда уровня = min по всем упражнениям уровня.
    Нет оценки хотя бы у одного упражнения → звёзд у уровня нет.
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


def _intensive_cursors(progress: dict | None) -> dict[str, str | None]:
    """
    intensive_progress:

        {"X": "word_01_mc_01", "C": "BGRUA1_C03"}

    X и C — параллельные курсоры, не статусы блоков.
    """
    data = _parse_json_map(progress)
    result = {"X": None, "C": None}
    for series in ("X", "C"):
        raw = data.get(series)
        if raw is None:
            raw = data.get(series.lower())
        if isinstance(raw, dict):
            raw = raw.get("exercise") or raw.get("block") or raw.get("name")
        text = str(raw or "").strip()
        result[series] = text or None
    return result


def _block_exercise_ids(file_name: str | None) -> list[str]:
    if not file_name:
        return []
    try:
        from app.text_lab import storage as text_lab_storage
        pack = text_lab_storage.load_block_file(file_name)
    except Exception:
        return []
    ids = []
    for item in pack.get("exercises") or []:
        exercise_id = item.get("id") if isinstance(item, dict) else None
        if exercise_id:
            ids.append(str(exercise_id))
    return ids


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


def _series_tree_status(block: dict, series_index: int, cursor_index: int) -> str:
    tariff_ok = bool(block.get("is_clickable", True))
    if not tariff_ok:
        return TREE_STATUS_LOCKED
    if series_index < cursor_index:
        return TREE_STATUS_COMPLETED
    if series_index == cursor_index:
        return TREE_STATUS_CURRENT
    return TREE_STATUS_LOCKED


def _intensive_lesson_node(block: dict, tree_status: str, stars: int | None = None) -> dict:
    """Узел на уровне урока."""
    block_type = (block.get("block_type") or "X").upper()
    extra = ["intensive-block", f"intensive-{block_type.lower()}", tree_status]

    if block_type == "C":
        icon = "fa-solid fa-person-chalkboard"
    else:
        icon = "fa-solid fa-chalkboard-user"

    title = block.get("title") or block.get("name")
    label = BLOCK_LABEL.get(block_type, "Intensive")
    tariff_ok = bool(block.get("is_clickable", True))

    return {
        "name": block["name"],
        "title": f"{label}: {title}",
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
        "icon_class": icon,
        "extra_classes": " ".join(extra),
        "themes": [],
    }


def _insert_intensive_blocks(
        lesson_nodes: list[dict],
        blocks: list,
        progress: dict | None = None,
        stars_map: dict | None = None,
) -> list[dict]:
    """Вставляет блоки intensive после after_lesson. Цвет — от курсора серии X/C."""
    cursors = _intensive_cursors(progress)
    by_after = defaultdict(list)
    prepared = []
    for raw in blocks:
        block = dict(raw)
        block_type = (block.get("block_type") or "X").upper()
        block["block_type"] = block_type
        block["exercise_ids"] = _block_exercise_ids(block.get("file_name"))
        prepared.append(block)
        by_after[block["after_lesson"]].append(block)
    for group in by_after.values():
        group.sort(key=lambda b: (b.get("sort_order") or 0, b.get("name") or ""))

    ordered = {"X": [], "C": []}
    for lesson in lesson_nodes:
        for block in by_after.get(lesson["name"], []):
            ordered[block["block_type"]].append(block)

    cursor_index = {
        series: _cursor_series_index(items, cursors.get(series))
        for series, items in ordered.items()
    }
    seen = {"X": 0, "C": 0}

    inserted = []
    for lesson in lesson_nodes:
        inserted.append(lesson)
        for block in by_after.get(lesson["name"], []):
            block_type = block["block_type"]
            series_index = seen[block_type]
            seen[block_type] += 1
            status = _series_tree_status(block, series_index, cursor_index[block_type])
            stars = aggregate_stars(block.get("exercise_ids"), stars_map)
            inserted.append(_intensive_lesson_node(block, status, stars))
    return inserted


def series_exercise_order(blocks: list[dict]) -> list[tuple[str, str]]:
    """
    Плоский порядок упражнений серии: (block_name, exercise_id).
    Если у блока нет упражнений, в порядок попадает сам block_name.
    """
    order: list[tuple[str, str]] = []
    for block in blocks:
        name = block.get("name")
        ids = block.get("exercise_ids") or _block_exercise_ids(block.get("file_name"))
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
            if not lesson_clickable:
                lesson_extra_classes.append("locked")

            if not lesson_clickable:
                lesson_icon = "zmdi zmdi-lock text-muted"
            elif lesson_completed:
                lesson_icon = "zmdi zmdi-folder text-success"
            else:
                lesson_icon = "zmdi zmdi-folder"

            lesson_exercise_names: list[str] = []
            for theme in lesson_themes:
                lesson_exercise_names.extend(names_by_theme.get(theme["name"], []))

            lesson_dict = {
                "name": lesson["name"],
                "title": lesson["title"],
                "kind": "lesson",
                "open_class": lesson_open_class,
                "is_clickable": lesson_clickable,
                "is_completed": lesson_completed,
                "is_current": lesson_current,
                "is_available": lesson_clickable,
                "stars": aggregate_stars(lesson_exercise_names, stars_map),
                "icon_class": lesson_icon,
                "extra_classes": " ".join(lesson_extra_classes),
                "themes": []
            }

            for theme in lesson_themes:
                theme_pos = theme["pos"]
                theme_clickable = theme.get("is_clickable", True)

                is_completed = bool(current_theme_pos) and theme_pos < current_theme_pos
                is_current = theme["name"] == current_theme_name

                theme_extra_classes = []
                if is_completed:
                    theme_extra_classes.append("completed")
                if not theme_clickable:
                    theme_extra_classes.append("locked")
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
                theme_names = names_by_theme.get(theme["name"], [])

                theme_dict = {
                    "name": theme["name"],
                    "title": theme["title"],
                    "pos": theme_pos,
                    "player": "vqt",
                    "is_clickable": theme_clickable,
                    "is_completed": is_completed,
                    "is_current": is_current,
                    "is_available": theme_clickable,
                    "stars": aggregate_stars(theme_names, stars_map),
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

        theme_exercise_names = await _load_theme_exercise_names(
            conn,
            [theme["name"] for theme in themes],
        )

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
        item["exercise_ids"] = _block_exercise_ids(item.get("file_name"))
        blocks.append(item)
    return blocks
