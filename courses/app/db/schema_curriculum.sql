-- ============================================================
-- Схема учебного плана curriculum (как часть общей схемы Public)
-- ============================================================

-- ============================================================
--  Таблица COURSES
-- ============================================================
CREATE TABLE IF NOT EXISTS courses (
    name        VARCHAR(255) PRIMARY KEY,
    title       VARCHAR(255) NOT NULL
);

-- ============================================================
--  Таблица LESSONS
-- ============================================================
CREATE TABLE IF NOT EXISTS lessons (
    name        	 VARCHAR(255) PRIMARY KEY,
    course_name   	 VARCHAR(255) NOT NULL REFERENCES courses(name) ON DELETE CASCADE,
    title       	 VARCHAR(255) NOT NULL,

    -- Массивы видимости и права доступа к уроку
    visibility       INTEGER[]    NOT NULL DEFAULT '{}',
    permission       INTEGER[]    NOT NULL DEFAULT '{}',
    duration         TEXT[]       NOT NULL DEFAULT '{}',
    duration_limit   TEXT[]       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_lessons_name         ON lessons(name);
CREATE INDEX IF NOT EXISTS idx_lessons_visibility   ON lessons USING GIN(visibility);
CREATE INDEX IF NOT EXISTS idx_lessons_permission   ON lessons USING GIN(permission);


-- ============================================================
--  Таблица THEMES
-- ============================================================
CREATE TABLE IF NOT EXISTS themes (
    name         VARCHAR(255) PRIMARY KEY,
    lesson_name  VARCHAR(255) NOT NULL REFERENCES lessons(name) ON DELETE CASCADE,
    title        VARCHAR(255) NOT NULL,
    view_pos     INTEGER      NOT NULL DEFAULT 0,
    pos          INTEGER      NOT NULL UNIQUE,

    -- Массивы видимости и права доступа к теме
    visibility   INTEGER[]    NOT NULL DEFAULT '{}',
    permission   INTEGER[]    NOT NULL DEFAULT '{}',
    duration     TEXT[]       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_themes_name         ON themes(name);
CREATE INDEX IF NOT EXISTS idx_themes_visibility   ON themes USING GIN(visibility);
CREATE INDEX IF NOT EXISTS idx_themes_permission   ON themes USING GIN(permission);

-- ============================================================
--  Таблица EXERCISES
-- ============================================================
CREATE TABLE IF NOT EXISTS exercises (
    name            VARCHAR(255) PRIMARY KEY,
    exercise_type   VARCHAR(10)  NOT NULL DEFAULT 'Q' CHECK (exercise_type IN ('Q', 'T', 'V')),
    theme_name      VARCHAR(255) NOT NULL REFERENCES themes(name) ON DELETE CASCADE,
    title           VARCHAR(255),
    youtube_video   VARCHAR(255),
    theory          TEXT,
    additional_image VARCHAR(255),
    additional_audio VARCHAR(255),
    question        TEXT,
    audio_question  TEXT,
    variants        TEXT,
    answers         TEXT,
    audio_answers   TEXT,
    view_pos        INTEGER      NOT NULL DEFAULT 0,
    duration        INTEGER      NOT NULL DEFAULT 0,
    duration_limit  INTEGER      NOT NULL DEFAULT 0,
    pos             INTEGER      NOT NULL UNIQUE,

    -- Массивы видимости и права доступа к упражнению
    visibility   INTEGER[] NOT NULL DEFAULT '{}',
    permission   INTEGER[] NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_exercises_name         ON exercises(name);
CREATE INDEX IF NOT EXISTS idx_exercises_visibility   ON exercises USING GIN(visibility);
CREATE INDEX IF NOT EXISTS idx_exercises_permission   ON exercises USING GIN(permission);


-- Заполняем таблицу COURSES, если она пуста
INSERT INTO courses (name, title)
SELECT *
FROM (
    VALUES
        ('BGRUA1', 'Курс болгарского языка уровень A1'),
        ('BGRUA2', 'Курс болгарского языка уровень A2'),
        ('BGRUB1', 'Курс болгарского языка уровень B1'),
        ('BGRUB2', 'Курс болгарского языка уровень B2'),
        ('ENRUA1', 'Курс английского языка уровень A1')
) AS v(name, title)
WHERE NOT EXISTS (SELECT 1 FROM courses);



-- ============================================================
--  Таблица INTENSIVE_BLOCKS
--  Узел дерева курса. Задания блока, их порядок и тела —
--  в JSON, который загружается в память при старте.
--
--  ⚠️ TODO комментарий ниже исправить после добавления полей в таблицу USER (там будут изменения!)
--  Прогресс (таблица users, другая схема):
--    intensive_cursor       JSONB  -- {"block":"text_BGRUA1_04","exercise":"word_04_mc_01"}
--    intensive_block_scores JSONB  -- {"text_BGRUA1_04":100,"text_BGRUA1_05":80}
-- ============================================================
CREATE TABLE IF NOT EXISTS intensive_blocks (
    name            VARCHAR(255) PRIMARY KEY,
    title           VARCHAR(255) NOT NULL,
    block_type      VARCHAR(1)   NOT NULL DEFAULT 'X' CHECK (block_type IN ('X', 'C')),    -- X (text); C (coach)
    after_lesson    VARCHAR(255) NOT NULL REFERENCES lessons(name) ON DELETE CASCADE,
    sort_order      INTEGER      NOT NULL DEFAULT 0,
    file_name       VARCHAR(255) NOT NULL UNIQUE,

    visibility      INTEGER[]    NOT NULL DEFAULT '{}',
    permission      INTEGER[]    NOT NULL DEFAULT '{}',

    UNIQUE (after_lesson, sort_order)
);
