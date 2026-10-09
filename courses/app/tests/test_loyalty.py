"""
Тесты функционала начисления / списания баллов программы лояльности.

Покрывают все публичные функции из app.repositories.loyalty
и защиту от отрицательного баланса в SQL-функции insert_into_loyalty.

Большинство этих тестов добавлено как элемент тестов других модулей:
(регистрация, shop, feedback, classroom и т.д.), когда начисление
будет вызываться из бизнес-логики.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from app.repositories.loyalty import (
    award_cashback,
    award_daily_task,
    award_useful_feedback,
    award_welcome,
    get_loyalty_balance,
    get_loyalty_transactions,
    insert_into_loyalty,
)


@pytest.mark.asyncio
async def test_award_welcome_adds_1000_points(db_pool, create_user):
    """
    Приветственные баллы: +1000 и обновление денормализованного поля.
    Дублирован в test_auth.test_register_new_user
    """
    user_id = await create_user()

    result = await award_welcome(db_pool, user_id)

    assert result is not None
    assert result["points"] == 1000
    assert "Приветственные" in (result["reason"] or "")

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 1000

    user_points = await db_pool.fetchval(
        "SELECT loyalty_points FROM users WHERE id = $1", user_id
    )
    assert user_points == 1000


@pytest.mark.asyncio
async def test_award_useful_feedback_adds_100_points(db_pool, create_user):
    """
    Полезный отзыв: +100 баллов.
    Дублирован в test_feedback.test_admin_feedback_update_success
    """
    user_id = await create_user()

    result = await award_useful_feedback(db_pool, user_id)

    assert result is not None
    assert result["points"] == 100
    assert "отзыв" in (result["reason"] or "").lower()

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 100


@pytest.mark.asyncio
async def test_award_cashback_calculation(db_pool, create_user):
    """
    Кэшбэк: 1% от суммы покупки * 1000 → int(amount_euro * 10).
    Примеры: 50€ → 500, 12.5€ → 125, 0 / отрицательная → None.
    Дублируется в test_shop.test_auto_pay_single_pending_order
    """
    user_id = await create_user()

    # 50 евро
    result = await award_cashback(db_pool, user_id, 50.0)
    assert result is not None
    assert result["points"] == 500
    assert "50.00" in (result["reason"] or "")

    # 12.5 евро
    result = await award_cashback(db_pool, user_id, 12.5)
    assert result is not None
    assert result["points"] == 125

    # Нулевая сумма
    result = await award_cashback(db_pool, user_id, 0.0)
    assert result is None

    # Отрицательная сумма
    result = await award_cashback(db_pool, user_id, -10.0)
    assert result is None

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 500 + 125


@pytest.mark.asyncio
async def test_award_daily_task_points_weighted_1_to_5(db_pool, create_user):
    """Задание дня: 1–5 баллов, меньшие встречаются чаще."""
    user_id = await create_user()

    with patch("app.repositories.loyalty.weighted_daily_points", return_value=2):
        result = await award_daily_task(db_pool, user_id)

    assert result is not None
    assert result["points"] == 2
    assert "задания дня" in (result["reason"] or "")

    # Без мока — диапазон 1..5
    result2 = await award_daily_task(db_pool, user_id)
    assert result2 is not None
    assert 1 <= result2["points"] <= 5


@pytest.mark.asyncio
async def test_multiple_awards_accumulate_correctly(db_pool, create_user):
    """Несколько начислений корректно суммируются в балансе и в users.loyalty_points."""
    user_id = await create_user()

    await award_welcome(db_pool, user_id)          # 1000
    await award_useful_feedback(db_pool, user_id)  # 100
    await award_cashback(db_pool, user_id, 25.0)   # 250

    with patch("app.repositories.loyalty.weighted_daily_points", return_value=5):
        await award_daily_task(db_pool, user_id)   # 5

    expected = 1000 + 100 + 250 + 5

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == expected

    user_points = await db_pool.fetchval(
        "SELECT loyalty_points FROM users WHERE id = $1", user_id
    )
    assert user_points == expected


@pytest.mark.asyncio
async def test_get_loyalty_transactions_order_and_format(db_pool, create_user):
    """Транзакции возвращаются в порядке от новых к старым, с корректным форматированием даты."""
    user_id = await create_user()

    await award_welcome(db_pool, user_id)
    await award_useful_feedback(db_pool, user_id)

    transactions = await get_loyalty_transactions(db_pool, user_id)

    assert len(transactions) == 2
    # Новые сверху
    assert transactions[0]["points"] == 100
    assert transactions[1]["points"] == 1000

    for tx in transactions:
        assert "points" in tx
        assert "reason" in tx
        assert "created_at_str" in tx
        # Формат dd.mm.YYYY HH:MM
        assert len(tx["created_at_str"]) >= 16


@pytest.mark.asyncio
async def test_insert_negative_points_sufficient_balance(db_pool, create_user):
    """Списание при достаточном балансе проходит успешно."""
    user_id = await create_user()
    await award_welcome(db_pool, user_id)  # 1000

    result = await insert_into_loyalty(
        db_pool, user_id, -25, "Покупка 1-дневной заморозки серии"
    )

    assert result is not None
    assert result["points"] == -25

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 975

    user_points = await db_pool.fetchval(
        "SELECT loyalty_points FROM users WHERE id = $1", user_id
    )
    assert user_points == 975


@pytest.mark.asyncio
async def test_insert_negative_points_insufficient_balance_raises(db_pool, create_user):
    """Списание, которое уводит баланс в минус, должно падать с P0002 / Insufficient points."""
    user_id = await create_user()
    # Баланс 0

    with pytest.raises(Exception) as exc:
        await insert_into_loyalty(db_pool, user_id, -10, "Слишком большое списание")

    assert "Insufficient points" in str(exc.value) or "P0002" in str(exc.value)

    # Баланс не изменился
    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 0

    user_points = await db_pool.fetchval(
        "SELECT loyalty_points FROM users WHERE id = $1", user_id
    )
    assert user_points == 0


@pytest.mark.asyncio
async def test_insert_into_loyalty_user_not_found(db_pool):
    """Несуществующий пользователь — ошибка P0001 / User not found."""
    with pytest.raises(Exception) as exc:
        await insert_into_loyalty(db_pool, 999_999_999, 100, "Тест")

    assert "User not found" in str(exc.value) or "P0001" in str(exc.value)


@pytest.mark.asyncio
async def test_get_loyalty_balance_matches_sum_and_denormalized(db_pool, create_user):
    """
    get_loyalty_balance возвращает значение из users.loyalty_points
    (поле NOT NULL). Оно должно совпадать с SUM(points) из таблицы loyalty.
    """
    user_id = await create_user()
    await award_welcome(db_pool, user_id)          # 1000
    await award_useful_feedback(db_pool, user_id)  # 100

    denormalized = await db_pool.fetchval(
        "SELECT loyalty_points FROM users WHERE id = $1", user_id
    )
    sum_points = await db_pool.fetchval(
        "SELECT COALESCE(SUM(points), 0) FROM loyalty WHERE user_id = $1",
        user_id,
    )
    balance = await get_loyalty_balance(db_pool, user_id)

    assert denormalized == 1100
    assert sum_points == 1100
    assert balance == denormalized
    assert balance == sum_points


@pytest.mark.asyncio
async def test_freeze_purchase_flow_via_repository(db_pool, create_user):
    """
    Имитация покупки заморозки (как в роутере loyalty.buy_freeze):
    списание 25 баллов + установка флага has_freeze.
    """
    user_id = await create_user()
    await award_welcome(db_pool, user_id)

    # Проверяем, что флаг ещё не стоит
    has_freeze_before = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze_before is False

    # Списываем
    await insert_into_loyalty(
        db_pool, user_id, -25, "Покупка 1-дневной заморозки серии"
    )

    # Ставим флаг (как делает роутер)
    await db_pool.execute(
        "UPDATE users SET has_freeze = true WHERE id = $1", user_id
    )

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 975

    has_freeze_after = await db_pool.fetchval(
        "SELECT has_freeze FROM users WHERE id = $1", user_id
    )
    assert has_freeze_after is True


# ---------------------------------------------------------------------------
# Дневное задание: выбор Q и завершение
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_task_assigns_q_from_freshest_course(db_pool, create_user, minimal_theme):
    """
    При отсутствии exercise_current выбирается случайное Q из курса
    самого свежего прогресса и пишется в daily_activity.
    """
    from app.repositories.loyalty import assign_or_get_daily_exercise
    import json

    user_id = await create_user()
    theme = await minimal_theme  # BGRUA1001_H001, курс BGRUA1

    # Создаём пару Q-упражнений в этом курсе
    await db_pool.execute("""
        INSERT INTO exercises (name, exercise_type, theme_name, pos, title)
        VALUES
            ('BGRUA1001_Q001', 'Q', $1, 10001, 'Q1'),
            ('BGRUA1001_Q002', 'Q', $1, 10002, 'Q2')
        ON CONFLICT (name) DO NOTHING
    """, theme)

    # Самый свежий прогресс — в BGRUA1
    progress = {
        "BGRU": {
            "name": "BGRUA1001_Q001",
            "at": "2026-10-09T12:00:00+03:00",
        }
    }
    await db_pool.execute(
        "UPDATE users SET current_exercise = $1::jsonb WHERE id = $2",
        json.dumps(progress),
        user_id,
    )

    exercise = await assign_or_get_daily_exercise(db_pool, user_id)
    assert exercise in ("BGRUA1001_Q001", "BGRUA1001_Q002")

    daily = await db_pool.fetchval(
        "SELECT daily_activity FROM users WHERE id = $1", user_id
    )
    if isinstance(daily, str):
        daily = json.loads(daily)
    assert daily.get("exercise_current") == exercise


@pytest.mark.asyncio
async def test_daily_task_complete_awards_and_updates_streak(db_pool, create_user, minimal_theme):
    """Завершение назначенного Q начисляет баллы и двигает стрик."""
    from app.repositories.loyalty import (
        assign_or_get_daily_exercise,
        complete_daily_task_if_matches,
    )
    import json

    user_id = await create_user()
    theme = await minimal_theme

    await db_pool.execute("""
        INSERT INTO exercises (name, exercise_type, theme_name, pos, title)
        VALUES ('BGRUA1001_Q010', 'Q', $1, 10010, 'Daily Q')
        ON CONFLICT (name) DO NOTHING
    """, theme)

    progress = {"BGRU": {"name": "BGRUA1001_Q010", "at": "2026-10-09T15:00:00+03:00"}}
    await db_pool.execute(
        "UPDATE users SET current_exercise = $1::jsonb WHERE id = $2",
        json.dumps(progress), user_id,
    )

    exercise = await assign_or_get_daily_exercise(db_pool, user_id)
    assert exercise == "BGRUA1001_Q010"

    with patch("app.repositories.loyalty.weighted_daily_points", return_value=3):
        result = await complete_daily_task_if_matches(db_pool, user_id, exercise)

    assert result is not None
    assert result["award"]["points"] == 3
    assert result["streak"] == 1
    assert result["last_on"]

    # Повторное завершение ничего не даёт
    again = await complete_daily_task_if_matches(db_pool, user_id, exercise)
    assert again is None

    balance = await get_loyalty_balance(db_pool, user_id)
    assert balance == 3

    daily = await db_pool.fetchval("SELECT daily_activity FROM users WHERE id = $1", user_id)
    if isinstance(daily, str):
        daily = json.loads(daily)
    assert daily.get("exercise_current") is None
    assert daily.get("streak") == 1
