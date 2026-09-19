--------------------------------------------------------------------------------
-- 1. Таблица пользователей
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username            VARCHAR(255) NOT NULL UNIQUE,
    email               VARCHAR(255) NOT NULL UNIQUE,    -- триггер автоматом переводит в lowercase
    password_hash       TEXT NOT NULL,
    role                VARCHAR(30) NOT NULL DEFAULT 'student',

    date_of_birth       DATE,
    first_name          VARCHAR(150),
    last_name           VARCHAR(150),
    patronymic          VARCHAR(150),
    phone               VARCHAR(32),

    loyalty_points      INTEGER DEFAULT 0 CHECK (loyalty_points >= 0),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login          TIMESTAMPTZ,
    current_exercise    VARCHAR(150),
    intensive_progress  JSONB NOT NULL DEFAULT '{}'::jsonb,

    is_active           BOOLEAN DEFAULT true NOT NULL,
    is_superuser        BOOLEAN DEFAULT false NOT NULL,
    is_staff            BOOLEAN DEFAULT false NOT NULL,
    is_confirmed        BOOLEAN NOT NULL DEFAULT false,
    is_loyalty          BOOLEAN NOT NULL DEFAULT false,

    -- Поля для политики одного устройства
    current_jti             VARCHAR(255),
    last_session_ip         INET,
    last_session_user_agent TEXT,
    last_session_at         TIMESTAMPTZ,

    -- Информация по доступу клиента
    access_level     SMALLINT NOT NULL DEFAULT 0 CHECK (access_level IN (0, 1, 2)),
    access_until     TIMESTAMPTZ,

    CONSTRAINT users_role_check CHECK (role IN ('admin', 'student', 'manager', 'coach', 'account_manager'))
);

CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_current_jti ON users(current_jti);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

--------------------------------------------------------------------------------
-- 1.1. Функция и триггер для автоматического обновления поля updated_at
-- при любом изменении строки в users и user_sessions
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Для таблицы user
DROP TRIGGER IF EXISTS update_users_updated_at ON users;

CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

--------------------------------------------------------------------------------
-- 1.2. Функция и триггер для автоматического перевода email в нижний регистр в таблицах users
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION lowercase_user_email()
RETURNS TRIGGER AS $$
BEGIN
    NEW.email = LOWER(NEW.email);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_lowercase_user_email ON users;

CREATE TRIGGER tr_lowercase_user_email
BEFORE INSERT OR UPDATE OF email ON users
FOR EACH ROW
EXECUTE FUNCTION lowercase_user_email();


--------------------------------------------------------------------------------
-- 2. Таблица активных сессий (одна на пользователя)
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_sessions (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    jti                 VARCHAR(255) NOT NULL UNIQUE,     -- JWT ID
    ip_address          INET,
    user_agent          TEXT,

    created_at          TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at          TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_jti ON user_sessions(jti);

--------------------------------------------------------------------------------
-- 2.1. Функция и триггеры для updated_at для таблиц user_sessions
--------------------------------------------------------------------------------
DROP TRIGGER IF EXISTS update_user_sessions_updated_at ON user_sessions;

CREATE TRIGGER update_user_sessions_updated_at
    BEFORE UPDATE ON user_sessions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


--------------------------------------------------------------------------------
-- 3. Таблица продуктов
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug                VARCHAR(255) NOT NULL UNIQUE,

    langs               VARCHAR(5) NOT NULL,   -- 'bg_ru', 'en_ru'
    name                VARCHAR(255) NOT NULL UNIQUE,
    price               DECIMAL(8, 2) CHECK (price >= 0),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE
);


--------------------------------------------------------------------------------
-- 4. Таблица оплат
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    amount              DECIMAL(10, 2) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment             TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);

--------------------------------------------------------------------------------
-- F. Функция и триггер автооплаты при добавлении платежа в payments
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_payments_after_insert()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.amount > 0 THEN
        PERFORM auto_payment_pending_order(NEW.user_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_payments_auto_pay ON payments;

CREATE TRIGGER tr_payments_auto_pay
    AFTER INSERT ON payments
    FOR EACH ROW
    EXECUTE FUNCTION trg_payments_after_insert();


--------------------------------------------------------------------------------
-- 5. Таблица заказов
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    payment_id          INTEGER NULL REFERENCES payments(id) ON DELETE RESTRICT,

    status              VARCHAR(32) NOT NULL DEFAULT 'cart',
    amount              DECIMAL(10,2) NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    comment             TEXT NULL,

    CONSTRAINT chk_order_status CHECK (status IN ('cart', 'pending', 'paid', 'canceled'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
-- Только одна активная корзина на пользователя
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_cart_per_user ON orders (user_id) WHERE status = 'cart';
-- Только один pending-заказ на пользователя
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_pending_per_user ON orders (user_id) WHERE status = 'pending';

--------------------------------------------------------------------------------
-- 5.1. Функция и триггер автоматического создания инвойса после оплаты заказа
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION create_invoice_on_payment()
RETURNS TRIGGER AS $$
DECLARE
    v_number BIGINT;
BEGIN
    -- глобальный lock на генерацию номера
    PERFORM pg_advisory_xact_lock(1);

    -- защита от параллельного MAX()
    LOCK TABLE invoices IN SHARE ROW EXCLUSIVE MODE;
    SELECT COALESCE(MAX(number), 0) + 1
    INTO v_number
    FROM invoices;

    INSERT INTO invoices (order_id, number, amount)
    VALUES (
        NEW.id,
        v_number,
        COALESCE(NEW.amount, 0)
    );

    RAISE NOTICE 'Создан инвойс №% для заказа №%', v_number, NEW.id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_create_invoice ON orders;

CREATE TRIGGER tr_create_invoice
    AFTER UPDATE ON orders
    FOR EACH ROW
    WHEN (OLD.status = 'pending' AND NEW.status = 'paid')
    EXECUTE FUNCTION create_invoice_on_payment();


--------------------------------------------------------------------------------
-- 6. Таблица элементов заказа
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_items (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id            INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,

    quantity            INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    price               DECIMAL(10, 2) NOT NULL CHECK (price >= 0),
    discount            DECIMAL(10, 2) NOT NULL DEFAULT 0 CHECK (discount >= 0),

    CONSTRAINT chk_item_price CHECK (price >= discount)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product_id ON order_items(product_id);

--------------------------------------------------------------------------------
-- 6.1. Автоматический пересчёт суммы заказа (amount) при любом изменении связанных с ним item_order_id
--------------------------------------------------------------------------------
-- 1. Функция пересчёта суммы заказа
CREATE OR REPLACE FUNCTION recalculate_order_amount()
RETURNS TRIGGER AS $$
DECLARE
    v_order_id INTEGER;
BEGIN
    -- Определяем ID заказа в зависимости от события
    IF TG_OP = 'DELETE' THEN
        v_order_id := OLD.order_id;
    ELSE
        v_order_id := NEW.order_id;
    END IF;

    -- Пересчитываем сумму заказа: quantity * (price - discount)
    UPDATE orders o
    SET amount = x.total
    FROM (
        SELECT COALESCE(SUM(quantity * (price - discount)), 0) AS total
        FROM order_items
        WHERE order_id = v_order_id
    ) x
    WHERE o.id = v_order_id
      AND o.amount IS DISTINCT FROM x.total;  -- изменяем только если изменилась предыдущая сумма

    RAISE NOTICE 'Пересчитана сумма заказа ID % → %', v_order_id,
                 (SELECT amount FROM orders WHERE id = v_order_id);

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;


-- 2. Триггеры на таблицу order_items
DROP TRIGGER IF EXISTS tr_recalculate_order_amount ON order_items;

CREATE TRIGGER tr_recalculate_order_amount
    AFTER INSERT OR UPDATE OR DELETE ON order_items
    FOR EACH ROW
    EXECUTE FUNCTION recalculate_order_amount();


--------------------------------------------------------------------------------
-- 7. Таблица счетов-фактур
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoices (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id            INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,

    number              BIGINT NOT NULL UNIQUE,
    amount              DECIMAL(10, 2) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


--------------------------------------------------------------------------------
-- 8. Таблица курсов
--------------------------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS courses (
--     id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
--     product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
--
--     code              VARCHAR(10) NOT NULL,     -- BGRUA1; ENRUA1 и так далее...
--     name              VARCHAR(255) NOT NULL,    -- Болгарский язык уровень A1
--     source_lang       VARCHAR(2) NOT NULL DEFAULT 'bg',
--
--     CONSTRAINT courses_code_check CHECK (code = UPPER(code)),
--     CONSTRAINT courses_source_lang_check CHECK (source_lang IN ('bg', 'en', 'pl', 'ru'))
-- );

--------------------------------------------------------------------------------
-- 9. История изменения доступа
--------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_access_history (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    old_access_level    SMALLINT NOT NULL DEFAULT 0 CHECK (old_access_level IN (0, 1, 2)),
    old_access_until    TIMESTAMPTZ,
    new_access_level    SMALLINT NOT NULL DEFAULT 0 CHECK (new_access_level IN (0, 1, 2)),
    new_access_until    TIMESTAMPTZ,
    reason              VARCHAR(30),
    note                TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()

    CONSTRAINT chk_access_history_reason CHECK (reason IN (
       'purchase', 'loyalty', 'admin', 'refund', 'gift', 'expired', 'other'
    ))
);

--------------------------------------------------------------------------------
-- 9.1 Триггер: после INSERT в историю → обновляем users
--------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_access_from_history()
RETURNS TRIGGER AS $$
BEGIN
    -- Обновляем актуальное состояние в users
    UPDATE users
    SET
        access_level = NEW.new_access_level,
        access_until = NEW.new_access_until
    WHERE id = NEW.user_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_apply_access_from_history ON user_access_history;

CREATE TRIGGER tr_apply_access_from_history
    AFTER INSERT ON user_access_history
    FOR EACH ROW
    EXECUTE FUNCTION apply_access_from_history();







--------------------------------------------------------------------------------
-- Загрузка базовых данных, если таблица products пустая
--------------------------------------------------------------------------------

INSERT INTO products (slug, langs, name, price, is_active)
SELECT
    v.slug, v.langs, v.name, v.price, v.is_active
FROM (VALUES
    ('access-standard-monthly',          'bg_ru', 'Доступ СТАНДАРТ на 1 месяц',          20.00, TRUE),
    ('access-premium-monthly',           'bg_ru', 'Доступ ПРЕМИУМ на 1 месяц',          25.00, TRUE),
    ('access-standard-yearly',           'bg_ru', 'Доступ СТАНДАРТ на 1 год',          120.00, TRUE),
    ('access-premium-yearly',            'bg_ru', 'Доступ ПРЕМИУМ на 1 год',          180.00, TRUE),
    ('single-private-lesson',            'bg_ru', 'Индивидуальное занятие с преподавателем', 25.00, TRUE),
    ('single-private-lesson-with-native-speaker', 'bg_ru',
         'Индивидуальное занятие с преподавателем-носителем языка', 35.00, TRUE),
    ('private-lesson-pack-a1',           'bg_ru', 'Курс А1 занятий с преподавателем (7 занятий)', 133.00, TRUE),
    ('private-lesson-pack-a2',           'bg_ru', 'Курс А2 занятий с преподавателем (9 занятий)', 171.00, TRUE),
    ('private-lesson-pack-with-native-speaker-a1', 'bg_ru',
         'Курс А1 занятий с преподавателем-носителем языка (7 занятий)', 175.00, TRUE),
    ('private-lesson-pack-with-native-speaker-a2', 'bg_ru',
         'Курс А2 занятий с преподавателем-носителем языка (9 занятий)', 225.00, TRUE)
) AS v(slug, langs, name, price, is_active)
WHERE NOT EXISTS (SELECT 1 FROM products LIMIT 1);



