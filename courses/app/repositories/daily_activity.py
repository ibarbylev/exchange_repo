from datetime import datetime, timedelta
from asyncpg import Pool
from typing import Any
from .loyalty import award_daily_task

# ---------------------------------------------------------------------------
# Дневное задание (lazy Q)
# ---------------------------------------------------------------------------

def activity_day(now: datetime | None = None) -> str:
    """Календарный день активности. Новый день начинается в 07:00 времени сервера."""
    dt = now or datetime.now()
    return (dt - timedelta(hours=7)).date().isoformat()


async def _load_user_progress(pool: Pool, user_id: int) -> tuple[dict, dict, dict]:
    """Возвращает (current_exercise, intensive_progress, daily_activity)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT current_exercise, intensive_progress, daily_activity
            FROM users WHERE id = $1
            """,
            user_id,
        )
    if not row:
        return {}, {}, {}
    def as_dict(v):
        if v is None:
            return {}
        if isinstance(v, str):
            import json
            try:
                v = json.loads(v)
            except Exception:
                return {}
        return dict(v) if isinstance(v, dict) else {}
    return as_dict(row["current_exercise"]), as_dict(row["intensive_progress"]), as_dict(row["daily_activity"])


def _best_at_from_progress(current_exercise: dict, intensive_progress: dict) -> tuple[str | None, str | None]:
    """
    Ищет самый свежий курсор (по ключу at) среди VQT и Text Intensive.
    Возвращает (exercise_or_block_name, at_iso) или (None, None).
    """
    best_name = None
    best_at = None

    def consider(name: str | None, at: str | None):
        nonlocal best_name, best_at
        if not name or not at:
            return
        if best_at is None or at > best_at:
            best_at = at
            best_name = name

    # current_exercise: {"BGRU": {"name": "...", "at": "..."} } или старые форматы
    for prefix, raw in (current_exercise or {}).items():
        if isinstance(raw, dict):
            consider(raw.get("name") or raw.get("exercise"), raw.get("at"))
        elif isinstance(raw, str):
            # старый формат без даты — игнорируем как «самый свежий»
            pass

    # intensive_progress: {"BGRU": {"X": {"name": "...", "at": "..."}}}
    for prefix, pair in (intensive_progress or {}).items():
        if not isinstance(pair, dict):
            continue
        x = pair.get("X") or pair.get("x")
        if isinstance(x, dict):
            consider(x.get("name") or x.get("exercise"), x.get("at"))
        elif isinstance(x, str):
            pass

    return best_name, best_at


async def course_of_name(pool: Pool, name: str | None) -> str | None:
    """Определяет курс по имени упражнения/блока (BGRUA1002_Q001 → BGRUA1)."""
    if not name:
        return None
    async with pool.acquire() as conn:
        courses = await conn.fetch("SELECT name FROM courses")
    name_u = name.upper()
    # Самый длинный префикс
    best = None
    for row in courses:
        c = row["name"]
        if name_u.startswith(c.upper()) and (best is None or len(c) > len(best)):
            best = c
    return best


async def pick_random_q_from_course(pool: Pool, course: str) -> str | None:
    """Любое Q-упражнение курса, без учёта доступности и прогресса."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT name FROM exercises
            WHERE exercise_type = 'Q' AND name LIKE $1 || '%'
            ORDER BY RANDOM()
            LIMIT 1
            """,
            course,
        )


async def assign_or_get_daily_exercise(pool: Pool, user_id: int) -> str | None:
    """
    Возвращает exercise_current на сегодня.
    Если дневная активность уже была — None (или существующий, если ещё не пройден).
    Если exercise_current пуст или устарел — выбирает новое Q и записывает.
    """
    current, intensive, daily = await _load_user_progress(pool, user_id)
    today = activity_day()
    last_on = str(daily.get("last_on") or "")[:10]
    exercise_current = daily.get("exercise_current")

    # Уже засчитан сегодня
    if last_on == today:
        return None

    # Уже выбран и ещё не пройден
    if exercise_current:
        return str(exercise_current)

    # Выбираем новое
    name, _at = _best_at_from_progress(current, intensive)
    course = await course_of_name(pool, name)
    if not course:
        # запасной вариант — любой курс с Q
        async with pool.acquire() as conn:
            course = await conn.fetchval(
                """
                SELECT c.name FROM courses c
                WHERE EXISTS (
                    SELECT 1 FROM exercises e
                    WHERE e.exercise_type = 'Q' AND e.name LIKE c.name || '%'
                )
                ORDER BY c.name
                LIMIT 1
                """
            )
    if not course:
        return None

    exercise = await pick_random_q_from_course(pool, course)
    if not exercise:
        return None

    daily["exercise_current"] = exercise
    # не трогаем streak / last_on до прохождения
    import json
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET daily_activity = $1::jsonb WHERE id = $2",
            json.dumps(daily, ensure_ascii=False),
            user_id,
        )
    return exercise


async def _update_streak(daily: dict, today: str) -> dict:
    """Обновляет streak и last_on в словаре daily. Не трогает exercise_current."""
    try:
        streak = int(daily.get("streak") or 0)
    except (TypeError, ValueError):
        streak = 0

    last_on = str(daily.get("last_on") or "")[:10]
    from datetime import date
    if last_on:
        try:
            last_date = date.fromisoformat(last_on)
            today_date = date.fromisoformat(today)
            if (today_date - last_date).days == 1:
                streak += 1
            else:
                streak = 1
        except ValueError:
            streak = 1
    else:
        streak = 1

    daily["streak"] = streak
    daily["last_on"] = today
    return daily


async def record_daily_activity(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """
    Автоматически засчитывает дневную активность при прохождении темы или интерактивного упражнения
    (первый раз или повтор). Обновляет last_on и streak, если ещё не было сегодня.
    Баллы не начисляет — они только за специальное дневное задание.
    """
    _, _, daily = await _load_user_progress(pool, user_id)
    today = activity_day()
    last_on = str(daily.get("last_on") or "")[:10]
    if last_on == today:
        return None  # уже засчитано сегодня

    daily = await _update_streak(daily, today)

    import json
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET daily_activity = $1::jsonb WHERE id = $2",
            json.dumps(daily, ensure_ascii=False),
            user_id,
        )

    return {"streak": daily["streak"], "last_on": today}


async def complete_daily_task_if_matches(
    pool: Pool, user_id: int, exercise_name: str
) -> dict[str, Any] | None:
    """
    Если exercise_name совпадает с exercise_current и день ещё не засчитан —
    начисляет баллы, двигает last_on и streak, стирает exercise_current.
    """
    if not exercise_name:
        return None
    current, intensive, daily = await _load_user_progress(pool, user_id)
    today = activity_day()
    last_on = str(daily.get("last_on") or "")[:10]
    exercise_current = daily.get("exercise_current")

    if last_on == today:
        return None  # уже было
    if not exercise_current or str(exercise_current) != exercise_name:
        return None

    # Начисляем
    award = await award_daily_task(pool, user_id)

    # Обновляем стрик
    daily = await _update_streak(daily, today)
    daily["exercise_current"] = None  # стираем

    import json
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET daily_activity = $1::jsonb WHERE id = $2",
            json.dumps(daily, ensure_ascii=False),
            user_id,
        )

    return {"award": award, "streak": daily["streak"], "last_on": today}
