import os
from datetime import datetime
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- универсальное определение BASE_DIR ------------------------------
def get_base_dir():
    # 1. Docker — самый приоритетный
    if os.getenv("DOCKER_ENV"):
        return Path("/app")

    # 2. Локальная разработка — ищем по известным файлам
    current = Path(__file__).resolve().parent

    for _ in range(8):  # максимум 8 уровней вверх
        if (current / "docker-compose.dev.yml").exists():
            return current
        current = current.parent

    # 3. Fallback
    return Path(__file__).resolve().parent.parent.parent


BASE_DIR = get_base_dir()

print(f"[DEBUG] DOCKER_ENV = {os.getenv('DOCKER_ENV')}")
print(f"[DEBUG] BASE_DIR resolved as: {BASE_DIR}")
print(f"[DEBUG] shared/assets exists: {(BASE_DIR / 'shared/assets').exists()}")
print(f"[DEBUG] shared/assets full path: {BASE_DIR / 'shared/assets'}")

# --- Templates ---------------------------------------
templates = Jinja2Templates(
    directory=[
        BASE_DIR / "app/templates",  # добавлено для Docker
        BASE_DIR / "courses/app/templates",
        BASE_DIR / "translator/app/templates",
        BASE_DIR / "shared/templates"
    ]
)

templates.env.globals["current_year"] = lambda: datetime.now().year
templates.env.add_extension("jinja2.ext.i18n")
templates.env.cache = None
templates.env.auto_reload = True

# --- i18n Config -------------------------------------------
locale_dir = BASE_DIR / "shared" / "i18" / "locale"

SUPPORTED_UI_LANGUAGES = ["en", "ru", "bg", "pl"]
DEFAULT_SOURCE_LANGUAGE = "bg"
DEFAULT_UI_LANGUAGE = "ru"

# --- settings ----------------------------------------------
class CoursesSettings(BaseSettings):
    COURSES_POSTGRES_USER: str
    COURSES_POSTGRES_PASSWORD: str
    COURSES_POSTGRES_DB: str
    COURSES_POSTGRES_HOST: str
    COURSES_POSTGRES_PORT: int = 5432

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    SHARED_DIR: Path = BASE_DIR / "shared"  # дефолт для локальной разработки

    # --- EMAIL / SMTP ----------------------------------------
    SMTP_HOST: str = ""
    SMTP_PORT: int = 0
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    EMAIL_VERIFY_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 часа

    model_config = SettingsConfigDict(
        env_file=[
            BASE_DIR / ".env.dev",  # локальная разработка
            ".env.prod",              # docker / production
        ],
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = CoursesSettings()

import sys

# Добавляем shared в путь один раз универсально и для докера, и для locale
if str(settings.SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(settings.SHARED_DIR))


def get_sql_path(filename: str) -> Path:
    """
    Универсальный поиск SQL файлов (schema.sql, functions.sql и т.д.)
    """

    candidates = [
        BASE_DIR / "courses" / "app" / "db" / filename,  # local dev
        BASE_DIR / "app" / "db" / filename,              # docker / prod
    ]

    for candidate in candidates:
        if candidate.exists():
            print(f"[DEBUG] {filename} resolved as: {candidate}")
            return candidate

    raise FileNotFoundError(
        f"Файл {filename} не найден!\n"
        f"BASE_DIR = {BASE_DIR}\n"
        f"Проверялись пути:\n" +
        "\n".join(f"  - {c}" for c in candidates)
    )

SCHEMA_PATH = get_sql_path("schema.sql")
CURRICULUM_PATH = get_sql_path("schema_curriculum.sql")
FUNCTIONS_PATH = get_sql_path("functions.sql")
