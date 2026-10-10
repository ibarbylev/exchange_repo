"""
Тесты дневной активности и стрика.

Проверяют:
- засчитывание активности при прохождении темы (VQT) и Text Intensive
  (первый раз и повтор в тот же день / на следующий день);
- логику заморозки:
  * пропуск 1 дня + есть заморозка → день не теряется, заморозка используется;
  * пропуск 1 дня без заморозки → стрик сбрасывается;
  * пропуск 2+ дней даже с заморозкой → стрик сбрасывается, заморозка НЕ используется;
- отсутствие начисления баллов за обычную активность;
- что повторное прохождение в тот же день не меняет стрик.

По возможности проверки идут через обычные эндпоинты прохождения
(/api/user/current-exercise и /api/user/intensive-progress).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.repositories.daily_activity import (
    record_daily_activity,
    activity_day,
)
from app.repositories.loyalty import get_loyalty_balance


def _ensure_csrf(client):
    """Возвращает заголовки с CSRF-токеном из куки."""
    token = client.cookies.get("csrf_token")
    if not token:
        token = "test-csrf-token-value"
        client.cookies.set("csrf_token", token)
    return {"X-CSRF-Token": token}


async def _set_daily(
    db_pool,
    user_id: int,
    streak: int = 0,
    last_on: str | None = None,
    exercise_current: str | None = None,
    has_freeze: bool = False,
):
    """Устанавливает состояние daily_activity и флаг заморозки."""
    daily = {"streak": streak}
    if last_on is not None:
        daily["last_on"] = last_on
    if exercise_current is not None:
        daily["exercise_current"] = exercise_current

    await db_pool.execute(
        "UPDATE users SET daily_activity = $1::jsonb, has_freeze = $2 WHERE id = $3",
        json.dumps(daily, ensure_ascii=False),
        has_freeze,
        user_id,
    )


async def _get_daily(db_pool, user_id: int) -> dict:
    raw = await db_pool.fetchval(
        "SELECT daily_activity FROM users WHERE id = $1", user_id
    )
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


# ---------------------------------------------------------------------------
# Прохождение темы и интенсива (через API)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activity_added_on_theme_completion_first_and_repeat(auth_client, db_pool):
    """
    Активность засчитывается при прохождении темы (current-exercise).
    Первый раз → streak=1.
    Повтор в тот же день → не меняет.
    На следующий день → streak=2.
    """
    client, user_id = auth_client
    headers = _ensure_csrf(client)

    today = "2026-10-10"
    tomorrow = "2026-10-11"

    # Первый проход
    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        resp = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": "BGRUA1001_V001"},
            headers=headers,
        )
        assert resp.status_code == 200

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 1
    assert daily.get("last_on") == today

    balance_before = await get_loyalty_balance(db_pool, user_id)

    # Повтор в тот же день
    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        resp = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": "BGRUA1001_V002"},
            headers=headers,
        )
        assert resp.status_code == 200

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 1
    assert daily.get("last_on") == today

    # Баллы за обычную активность не начисляются
    balance_after = await get_loyalty_balance(db_pool, user_id)
    assert balance_after == balance_before

    # Следующий день
    with patch("app.repositories.daily_activity.activity_day", return_value=tomorrow):
        resp = await client.post(
            "/api/user/current-exercise",
            json={"exercise_name": "BGRUA1001_V003"},
            headers=headers,
        )
        assert resp.status_code == 200

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 2
    assert daily.get("last_on") == tomorrow


@pytest.mark.asyncio
async def test_activity_added_on_intensive_completion_first_and_repeat(auth_client, db_pool):
    """
    Активность засчитывается при прохождении Text Intensive.
    Аналогично теме: первый раз, повтор в тот же день, следующий день.
    """
    client, user_id = auth_client
    headers = _ensure_csrf(client)

    today = "2026-10-10"
    tomorrow = "2026-10-11"

    payload = {
        "exercise_name": "word_01_synonym_01",
        "series": "X",
        "block_name": "BGRUA1_text1_to_lesson06",
        "lang_prefix": "BGRU",
    }

    # Первый проход
    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        resp = await client.post("/api/user/intensive-progress", json=payload, headers=headers)
        assert resp.status_code == 200

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 1
    assert daily.get("last_on") == today

    # Повтор в тот же день
    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        await client.post("/api/user/intensive-progress", json=payload, headers=headers)

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 1

    # Следующий день
    with patch("app.repositories.daily_activity.activity_day", return_value=tomorrow):
        await client.post("/api/user/intensive-progress", json=payload, headers=headers)

    daily = await _get_daily(db_pool, user_id)
    assert daily.get("streak") == 2
    assert daily.get("last_on") == tomorrow


# ---------------------------------------------------------------------------
# Логика заморозки (через репозиторий + мок даты, т.к. нужно точно контролировать last_on)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeze_covers_one_day_skip(db_pool, create_user):
    """
    Пропуск 1 дня + куплена заморозка → день не теряется, заморозка используется.
    days_diff == 2 и has_freeze=True → streak продолжается, has_freeze=False.
    """
    user_id = await create_user()
    today = "2026-10-10"
    last = "2026-10-08"  # разница 2 дня = пропуск одного

    await _set_daily(db_pool, user_id, streak=5, last_on=last, has_freeze=True)

    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        result = await record_daily_activity(db_pool, user_id)

    assert result is not None
    assert result["streak"] == 7  # 5 + покрытый день + сегодня
    assert result["last_on"] == today

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 7
    assert daily["last_on"] == today

    has_freeze = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze is False


@pytest.mark.asyncio
async def test_one_day_skip_without_freeze_resets(db_pool, create_user):
    """
    Пропуск 1 дня без заморозки → расчёт начинается сначала (streak=1).
    """
    user_id = await create_user()
    today = "2026-10-10"
    last = "2026-10-08"

    await _set_daily(db_pool, user_id, streak=7, last_on=last, has_freeze=False)

    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        result = await record_daily_activity(db_pool, user_id)

    assert result is not None
    assert result["streak"] == 1

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 1
    assert daily["last_on"] == today

    has_freeze = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze is False


@pytest.mark.asyncio
async def test_two_day_skip_with_freeze_resets_and_does_not_use_freeze(db_pool, create_user):
    """
    Пропуск 2 дней (days_diff == 3) даже при наличии заморозки →
    расчёт начинается сначала, заморозка НЕ используется.
    """
    user_id = await create_user()
    today = "2026-10-10"
    last = "2026-10-07"  # разница 3 дня = пропуск двух

    await _set_daily(db_pool, user_id, streak=4, last_on=last, has_freeze=True)

    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        result = await record_daily_activity(db_pool, user_id)

    assert result is not None
    assert result["streak"] == 1

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 1
    assert daily["last_on"] == today

    has_freeze = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze is True  # не использована


@pytest.mark.asyncio
async def test_consecutive_days_increase_streak_without_freeze(db_pool, create_user):
    """Обычная последовательность дней без пропусков увеличивает стрик."""
    user_id = await create_user()
    day1 = "2026-10-08"
    day2 = "2026-10-09"
    day3 = "2026-10-10"

    # День 1
    with patch("app.repositories.daily_activity.activity_day", return_value=day1):
        await record_daily_activity(db_pool, user_id)
    assert (await _get_daily(db_pool, user_id))["streak"] == 1

    # День 2
    with patch("app.repositories.daily_activity.activity_day", return_value=day2):
        await record_daily_activity(db_pool, user_id)
    assert (await _get_daily(db_pool, user_id))["streak"] == 2

    # День 3
    with patch("app.repositories.daily_activity.activity_day", return_value=day3):
        await record_daily_activity(db_pool, user_id)
    assert (await _get_daily(db_pool, user_id))["streak"] == 3


@pytest.mark.asyncio
async def test_same_day_multiple_calls_do_not_change_streak(db_pool, create_user):
    """Несколько прохождений в один и тот же день не увеличивают стрик."""
    user_id = await create_user()
    today = "2026-10-10"

    with patch("app.repositories.daily_activity.activity_day", return_value=today):
        r1 = await record_daily_activity(db_pool, user_id)
        r2 = await record_daily_activity(db_pool, user_id)
        r3 = await record_daily_activity(db_pool, user_id)

    assert r1 is not None
    assert r2 is None
    assert r3 is None

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 1
    assert daily["last_on"] == today


@pytest.mark.asyncio
async def test_freeze_used_then_next_skip_resets(db_pool, create_user):
    """После использования заморозки следующий пропуск сбрасывает стрик."""
    user_id = await create_user()

    # Сначала покрываем пропуск заморозкой
    await _set_daily(db_pool, user_id, streak=3, last_on="2026-10-08", has_freeze=True)
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        await record_daily_activity(db_pool, user_id)

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 5  # 3 + покрытый день + сегодня
    has_freeze = await db_pool.fetchval("SELECT has_freeze FROM users WHERE id = $1", user_id)
    assert has_freeze is False

    # Теперь ещё один пропуск без заморозки
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-12"):
        await record_daily_activity(db_pool, user_id)

    daily = await _get_daily(db_pool, user_id)
    assert daily["streak"] == 1
    assert daily["last_on"] == "2026-10-12"


@pytest.mark.asyncio
async def test_no_points_awarded_for_regular_activity(db_pool, create_user):
    """Обычная дневная активность не начисляет баллы лояльности."""
    user_id = await create_user()
    balance_before = await get_loyalty_balance(db_pool, user_id)

    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        await record_daily_activity(db_pool, user_id)

    balance_after = await get_loyalty_balance(db_pool, user_id)
    assert balance_after == balance_before


# ---------------------------------------------------------------------------
# apply_pending_freeze (применяется при загрузке пользователя / обновлении страницы)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_pending_freeze_covers_one_day_gap(db_pool, create_user):
    """
    При открытии страницы, если пропущен ровно один день и есть заморозка,
    last_on сдвигается на пропущенный день, стрик увеличивается на 1,
    заморозка списывается.
    """
    from app.repositories.daily_activity import apply_pending_freeze

    user_id = await create_user()
    await _set_daily(db_pool, user_id, streak=1907, last_on="2026-10-08", has_freeze=True)

    daily = await _get_daily(db_pool, user_id)
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        new_daily, new_has_freeze = await apply_pending_freeze(
            db_pool, user_id, daily, True
        )

    assert new_has_freeze is False
    assert new_daily["last_on"] == "2026-10-09"  # покрыта пятница
    assert new_daily["streak"] == 1908  # пятница засчитана

    # Проверяем, что изменения записаны в БД
    db_daily = await _get_daily(db_pool, user_id)
    assert db_daily["last_on"] == "2026-10-09"
    assert db_daily["streak"] == 1908
    has_freeze = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze is False


@pytest.mark.asyncio
async def test_apply_pending_freeze_does_not_cover_two_day_gap(db_pool, create_user):
    """Пропуск двух и более дней — заморозка не применяется, состояние не меняется."""
    from app.repositories.daily_activity import apply_pending_freeze

    user_id = await create_user()
    await _set_daily(db_pool, user_id, streak=10, last_on="2026-10-07", has_freeze=True)

    daily = await _get_daily(db_pool, user_id)
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        new_daily, new_has_freeze = await apply_pending_freeze(
            db_pool, user_id, daily, True
        )

    assert new_has_freeze is True
    assert new_daily["last_on"] == "2026-10-07"
    assert new_daily["streak"] == 10

    has_freeze = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze is True


@pytest.mark.asyncio
async def test_apply_pending_freeze_no_freeze_does_nothing(db_pool, create_user):
    """Без заморозки функция ничего не меняет, даже при пропуске одного дня."""
    from app.repositories.daily_activity import apply_pending_freeze

    user_id = await create_user()
    await _set_daily(db_pool, user_id, streak=5, last_on="2026-10-08", has_freeze=False)

    daily = await _get_daily(db_pool, user_id)
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        new_daily, new_has_freeze = await apply_pending_freeze(
            db_pool, user_id, daily, False
        )

    assert new_has_freeze is False
    assert new_daily["last_on"] == "2026-10-08"
    assert new_daily["streak"] == 5


@pytest.mark.asyncio
async def test_streak_view_shows_last_7_days_ending_today(db_pool, create_user):
    """
    streak_view всегда строит метки последних 7 календарных дней, заканчивая сегодня.
    Дни ≤ last_on помечаются как выполненные.
    """
    from app.core.security import streak_view
    from app.repositories.daily_activity import apply_pending_freeze

    # Фиксируем "сегодня" через мок внутри функции сложно, поэтому проверяем структуру
    # на реальных данных после apply_pending_freeze
    user_id = await create_user()
    await _set_daily(db_pool, user_id, streak=4, last_on="2026-10-08", has_freeze=True)

    daily = await _get_daily(db_pool, user_id)
    with patch("app.repositories.daily_activity.activity_day", return_value="2026-10-10"):
        daily, _ = await apply_pending_freeze(db_pool, user_id, daily, True)

    view = streak_view(daily)
    assert view["streak_days"] == 5  # 4 + покрытый день
    assert view["active_today"] is False
    assert len(view["marks"]) == 7
    # Последняя метка — сегодня
    assert view["marks"][-1]["is_today"] is True
    assert view["marks"][-1]["done"] is False
    # День last_on (после freeze — 2026-10-09) должен быть done
    done_count = sum(1 for m in view["marks"] if m["done"])
    assert done_count >= 1
