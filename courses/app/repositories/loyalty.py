"""
Высокоуровневые функции начисления/списания баллов лояльности.
Все правила живут здесь.
Низкоуровневая вставка — через SQL-функцию insert_into_loyalty.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from asyncpg import Pool


async def insert_into_loyalty(
    pool: Pool,
    user_id: int,
    points: int,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """
    Низкоуровневый вызов SQL-функции insert_into_loyalty.
    Возвращает строку loyalty или None при ошибке.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM insert_into_loyalty($1, $2, $3)",
            user_id,
            points,
            reason,
        )
    return dict(row) if row else None


def weighted_daily_points() -> int:
    """1–5 баллов. Меньшие значения встречаются чаще."""
    # 1: 40%, 2: 30%, 3: 15%, 4: 10%, 5: 5%
    return random.choices([1, 2, 3, 4, 5], weights=[40, 30, 15, 10, 5])[0]


async def award_daily_task(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """1–5 баллов за решение задания дня (меньшие чаще)."""
    points = weighted_daily_points()
    return await insert_into_loyalty(
        pool, user_id, points, "Решение задания дня"
    )


async def award_useful_feedback(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """100 баллов за полезный отзыв (question_quality = 1)."""
    return await insert_into_loyalty(
        pool, user_id, 100, "Полезный отзыв по улучшению сайта"
    )


async def award_cashback(
    pool: Pool, user_id: int, amount_euro: float
) -> dict[str, Any] | None:
    """
    Кэшбэк: 1000 * 1% от суммы покупки в евро.
    Пример: 50€ → 500 баллов.
    """
    points = int(amount_euro * 10)  # 1% от суммы * 1000
    if points <= 0:
        return None
    return await insert_into_loyalty(
        pool, user_id, points, f"Кэшбэк с покупки {amount_euro:.2f} €"
    )


async def award_welcome(pool: Pool, user_id: int) -> dict[str, Any] | None:
    """1000 приветственных баллов при регистрации."""
    return await insert_into_loyalty(
        pool, user_id, 1000, "Приветственные баллы"
    )


async def get_loyalty_balance(pool: Pool, user_id: int) -> int:
    """Текущий баланс из users.loyalty_points (быстро) или fallback на SUM."""
    async with pool.acquire() as conn:
        balance = await conn.fetchval(
            "SELECT loyalty_points FROM users WHERE id = $1",
            user_id,
        )
        if balance is None:
            balance = await conn.fetchval(
                "SELECT COALESCE(SUM(points), 0) FROM loyalty WHERE user_id = $1",
                user_id,
            )
    return int(balance or 0)


async def get_loyalty_transactions(pool: Pool, user_id: int) -> list[dict]:
    """Список транзакций (новые сверху)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT points, reason, created_at
            FROM loyalty
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )

    transactions = []
    for row in rows:
        created_at = row["created_at"]
        if isinstance(created_at, datetime):
            created_at_str = created_at.strftime("%d.%m.%Y %H:%M")
        else:
            created_at_str = str(created_at)[:16]
        transactions.append({
            "points": int(row["points"] or 0),
            "reason": row["reason"] or "Операция",
            "created_at_str": created_at_str,
        })
    return transactions


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
    try:
        streak = int(daily.get("streak") or 0)
    except (TypeError, ValueError):
        streak = 0

    # Если last_on — вчера (с учётом 07:00), то +1, иначе сброс в 1
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
    daily["exercise_current"] = None  # стираем

    import json
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET daily_activity = $1::jsonb WHERE id = $2",
            json.dumps(daily, ensure_ascii=False),
            user_id,
        )

    return {"award": award, "streak": streak, "last_on": today}
