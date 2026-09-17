--------------------------------------------------------------------------------
-- 1. Функция сброса просроченного доступа:
--  - запускается через CRON,
--  - проверяет и сбрасывает
--      - в 0 просроченный доступ (access_leve)
--      - в NULL дату до (access_until)

--------------------------------------------------------------------------------
-- Вариант запуска в CROM:
-- # Каждый день в 3:00 ночи сбрасывать просроченный доступ
-- 0 3 * * * psql -U courses_user -d courses_db -c "SELECT reset_expired_access();" (ежедневно в 03:00)
--------------------------------------------------------------------------------

-- ВАЖНО: точка истины - таблица user_access_history.
-- Прежде всего данные обновляем там, а
-- триггер уже сам перенесёт обновление в 2 поля users (access_level и access_until)
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reset_expired_access()
RETURNS INTEGER AS $$
DECLARE
    updated_count INTEGER;
BEGIN
    INSERT INTO user_access_history (
        user_id,
        old_access_level,
        old_access_until,
        new_access_level,
        new_access_until,
        reason
    )
    SELECT
        id,
        access_level,
        access_until,
        0,
        NULL,
        'expired'
    FROM users
    WHERE access_level > 0
      AND access_until IS NOT NULL
      AND access_until < NOW();

    GET DIAGNOSTICS updated_count = ROW_COUNT;
    RETURN updated_count;
END;
$$ LANGUAGE plpgsql;


--------------------------------------------------------------------------------
-- 2. Функция: добавляет запись в историю доступа (с правильной логикой продления/апгрейда)
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_purchase_access(
    p_user_id   INTEGER,
    p_order_id  INTEGER,
    p_slug      TEXT
)
RETURNS VOID AS $$
DECLARE
    v_new_access       SMALLINT;
    v_new_until        TIMESTAMPTZ;
    v_current_access   SMALLINT;
    v_current_until    TIMESTAMPTZ;
    v_duration         INTERVAL;
    v_now              TIMESTAMPTZ := NOW();
BEGIN
    ------------------------------------------------------------------
    -- Определяем тип подписки
    ------------------------------------------------------------------

    IF POSITION('monthly' IN p_slug) > 0 THEN
        v_duration := INTERVAL '1 month';
    ELSIF POSITION('yearly' IN p_slug) > 0 THEN
        v_duration := INTERVAL '1 year';
    ELSE
        RAISE EXCEPTION 'Не удалось определить срок подписки из slug: %', p_slug;
    END IF;

    ------------------------------------------------------------------
    -- Определяем уровень доступа
    ------------------------------------------------------------------

    IF POSITION('standard' IN p_slug) > 0 THEN
        v_new_access := 1;
    ELSIF POSITION('premium' IN p_slug) > 0 THEN
        v_new_access := 2;
    ELSE
        RAISE EXCEPTION 'Не удалось определить уровень доступа из slug: %', p_slug;
    END IF;

    ------------------------------------------------------------------
    -- Текущее состояние пользователя
    ------------------------------------------------------------------

    SELECT access_level, access_until
    INTO v_current_access, v_current_until
    FROM users
    WHERE id = p_user_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Пользователь % не найден', p_user_id;
    END IF;

    v_current_access := COALESCE(v_current_access, 0);

    ------------------------------------------------------------------
    -- Нельзя менять тип действующей подписки
    ------------------------------------------------------------------

    IF v_current_until > v_now
       AND v_new_access <> v_current_access
    THEN
        RAISE EXCEPTION
            'Нельзя изменить действующий тип доступа ДО его окончания! Текущий уровень: %, новый уровень: %',
            v_current_access,  v_new_access;
    END IF;

    ------------------------------------------------------------------
    -- Расчёт новой даты окончания
    ------------------------------------------------------------------

    IF v_current_until > v_now THEN
        v_new_until := v_current_until + v_duration;
    ELSE
        v_new_until := v_now + v_duration;
    END IF;

    ------------------------------------------------------------------
    -- Вносим изменения в таблицу user_access_history
    ------------------------------------------------------------------

    INSERT INTO user_access_history (
        user_id,
        old_access_level,
        old_access_until,
        new_access_level,
        new_access_until,
        reason,
        note
    )
    VALUES (
        p_user_id,
        v_current_access,
        v_current_until,
        v_new_access,
        v_new_until,
        'purchase',
        'Доступ изменён согласно заказа #' || p_order_id
    );

END;
$$ LANGUAGE plpgsql;


-------------------------------------------------------------------------------
-- 3. Функция: очистка старых корзин через CRON

--------------------------------------------------------------------------------
-- Вариант запуска в CROM:
-- # Каждый день в 3:00 ночи сбрасывать просроченные корзины
-- 5 3 * * * psql -U courses_user -d courses_db -c "SELECT nightly_maintenance();" (ежедневно в 03:05)
--------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION clean_old_carts(p_user_id INTEGER DEFAULT NULL)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER := 0;
BEGIN
    DELETE FROM orders
    WHERE status = 'cart'
      AND created_at < NOW() - INTERVAL '14 days'
      AND (p_user_id IS NULL OR user_id = p_user_id);

    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    IF deleted_count > 0 THEN
        RAISE NOTICE 'Очищено % старых корзин (user_id %)', deleted_count, COALESCE(p_user_id, 'ALL');
    END IF;

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;


--------------------------------------------------------------------------------
-- 4. Авто-оплата pending-заказов (основная бизнес-логика)
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION auto_payment_pending_order(p_user_id INTEGER)
RETURNS BOOLEAN AS $$
DECLARE
    v_order_id     INTEGER;
    v_order_amount DECIMAL(10,2);
    v_debited      BOOLEAN := FALSE;
BEGIN
    -- Блокируем пользователя
    PERFORM pg_advisory_xact_lock(p_user_id);

    -- Получаем pending-заказ
    SELECT id, amount
    INTO v_order_id, v_order_amount
    FROM orders
    WHERE user_id = p_user_id
      AND status = 'pending'
    LIMIT 1;

    IF v_order_id IS NULL THEN
        RETURN FALSE;
    END IF;

    -- Атомарное списание денег
    WITH current_balance AS (
        SELECT COALESCE(SUM(amount), 0) AS balance
        FROM payments
        WHERE user_id = p_user_id
    )
    INSERT INTO payments (user_id, amount, comment)
    SELECT
        p_user_id,
        -v_order_amount,
        'Автооплата заказа #' || v_order_id
    FROM current_balance
    WHERE balance >= v_order_amount;

    -- Если деньги успешно списались
    IF FOUND THEN
        v_debited := TRUE;

        -- Обновляем статус заказа
        UPDATE orders
        SET status = 'paid'
        WHERE id = v_order_id;

        -- Выдаём доступ
        PERFORM apply_purchase_access(
            p_user_id,
            v_order_id,
            (
                SELECT p.slug
                FROM products p
                JOIN order_items oi ON oi.product_id = p.id
                WHERE oi.order_id = v_order_id
                LIMIT 1
            )
        );
    END IF;

    RETURN v_debited;
END;
$$ LANGUAGE plpgsql;


--------------------------------------------------------------------------------
-- 5. Функция получения баланса пользователя
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_user_balance(p_user_id INTEGER)
RETURNS DECIMAL(10,2) AS $$
    SELECT COALESCE(SUM(amount), 0)
    FROM payments
    WHERE user_id = p_user_id;
$$ LANGUAGE sql STABLE;


--------------------------------------------------------------------------------
-- 6. Функция удаления старых корзин
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION clean_old_carts(p_user_id INTEGER DEFAULT NULL)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER := 0;
BEGIN
    DELETE FROM orders
    WHERE status = 'cart'
      AND created_at < NOW() - INTERVAL '14 days'
      AND (p_user_id IS NULL OR user_id = p_user_id);

    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    IF deleted_count > 0 THEN
        IF p_user_id IS NULL THEN
            RAISE NOTICE 'Очищено % старых корзин (для всех пользователей)', deleted_count;
        ELSE
            RAISE NOTICE 'Очищено % старых корзин (user_id %)', deleted_count, p_user_id;
        END IF;
    END IF;

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;


--------------------------------------------------------------------------------
-- 7. Функция получения или создания корзины
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_or_create_cart(p_user_id INTEGER)
RETURNS TABLE (
    id          INTEGER,
    amount      NUMERIC,
    status      VARCHAR(32),
    created_at  TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    INSERT INTO orders (user_id, status, amount)
    VALUES (p_user_id, 'cart', 0)
    ON CONFLICT (user_id) WHERE orders.status = 'cart'
    DO UPDATE SET
        status = EXCLUDED.status   -- фиктивное обновление, чтобы RETURNING сработал
    RETURNING
        orders.id,
        orders.amount,
        orders.status,
        orders.created_at;
END;
$$ LANGUAGE plpgsql;


--------------------------------------------------------------------------------
-- 8. Функция получения или создания детализации корзины
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION get_cart_details(p_user_id bigint)
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
WITH cart AS (
    SELECT id, amount, status, created_at
    FROM orders
    WHERE user_id = p_user_id
      AND status = 'cart'
),
items AS (
    SELECT
        oi.id,
        oi.quantity,
        oi.price,
        oi.discount,
        p.slug,
        p.name,
        oi.quantity * (oi.price - oi.discount) AS subtotal
    FROM order_items oi
    JOIN products p ON p.id = oi.product_id
    JOIN cart c ON c.id = oi.order_id
    ORDER BY oi.id
)
SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM cart)
        THEN NULL
    ELSE jsonb_build_object(
        'cart',
        (SELECT to_jsonb(cart) FROM cart),
        'items',
        COALESCE(
            (SELECT jsonb_agg(to_jsonb(items)) FROM items),
            '[]'::jsonb
        ),
        'total_items',
        COALESCE(
            (SELECT sum(quantity) FROM items),
            0
        )
    )
END;
$$;