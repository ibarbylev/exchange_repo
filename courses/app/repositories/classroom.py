import asyncpg

from collections import defaultdict


BLOCK_LABEL = {
    "X": "Text Intensive",
    "C": "Coach Online",
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

    # Инициализация всех возможных типов
    counts = {
        "V": [0, 0, 0],
        "Q": [0, 0, 0],
        "T": [0, 0, 0],
        # "P": [0, 0, 0],
        # "X": [0, 0, 0],
        # "E": [0, 0, 0],
    }

    for row in rows:
        ex_type = row["exercise_type"]
        perm = row["permission_level"]
        count = row["count"]

        if ex_type in counts and 0 <= perm <= 2:
            counts[ex_type][perm] = count

    # Убираем типы упражнений, где везде 0
    filtered = {
        ex_type: count_list
        for ex_type, count_list in counts.items()
        if any(count_list)
    }

    return filtered


def _intensive_lesson_node(block: dict) -> dict:
    """Только отображение в дереве. Без клика и без плеера."""
    block_type = (block.get("block_type") or "X").upper()
    extra = ["intensive-block", f"intensive-{block_type.lower()}"]
    if block_type == "C":
        icon = "zmdi zmdi-account-box text-info"
    else:
        icon = "zmdi zmdi-collection-text text-primary"

    title = block.get("title") or block.get("name")
    label = BLOCK_LABEL.get(block_type, "Intensive")

    return {
        "name": block["name"],
        "title": f"{label}: {title}",
        "kind": "intensive",
        "block_type": block_type,
        "after_lesson": block.get("after_lesson"),
        "open_class": "",
        "is_completed": False,
        "is_current": False,
        "icon_class": icon,
        "extra_classes": " ".join(extra),
        "themes": [],
    }


def _insert_intensive_blocks(lesson_nodes: list[dict], blocks: list) -> list[dict]:
    """Вставляет блоки intensive сразу после урока after_lesson."""
    by_after = defaultdict(list)
    for block in blocks:
        by_after[block["after_lesson"]].append(dict(block))
    for group in by_after.values():
        group.sort(key=lambda b: (b.get("sort_order") or 0, b.get("name") or ""))

    inserted = []
    for lesson in lesson_nodes:
        inserted.append(lesson)
        for block in by_after.get(lesson["name"], []):
            inserted.append(_intensive_lesson_node(block))
    return inserted


async def build_classroom_tree(
        courses: list,
        lessons: list,
        themes: list,
        conn,
        current_theme_pos: int | None = None,
        current_theme_name: str | None = None,
        intensive_blocks: list | None = None,
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

            lesson_dict = {
                "name": lesson["name"],
                "title": lesson["title"],
                # "is_open": False,
                "open_class": lesson_open_class,
                "is_clickable": lesson_clickable,
                "is_completed": lesson_completed,
                "is_current": lesson_current,
                "is_available": lesson_clickable,
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

                theme_dict = {
                    "name": theme["name"],
                    "title": theme["title"],
                    "pos": theme_pos,
                    # "is_open": False,
                    "is_clickable": theme_clickable,
                    "is_completed": is_completed,
                    "is_current": is_current,
                    "is_available": theme_clickable,
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
            course_dict["lessons"], course_blocks
        )

        result.append(course_dict)

    return {"courses": result}


async def get_classroom_tree(
    pool: asyncpg.Pool,
    lang_prefix: str,
    user_access_level: int,
    target_exercise: str | None = None
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

        tree = await build_classroom_tree(
            courses=courses,
            lessons=lessons,
            themes=themes,
            conn=conn,
            current_theme_pos=current_theme_pos,
            current_theme_name=current_theme_name,
            intensive_blocks=intensive_blocks,
        )

    return tree
