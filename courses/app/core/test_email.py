import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 1. Делаем окружение одинаковым для pytest и прямого запуска.
#    Это нужно ДО импорта app.core.config, т.к. settings создаётся при импорте.
os.chdir(PROJECT_ROOT)                 # чтобы относительный .env.prod искался в одном месте
os.environ.pop("DOCKER_ENV", None)     # чтобы BASE_DIR не стал /app
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values
from babel.support import Translations

from app.core.config import BASE_DIR, CoursesSettings, settings, templates, locale_dir
from app.core.email import send_email, send_verification_email
from app.core.security import create_email_verification_token

ENV_DEV = BASE_DIR / ".env.dev"
SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "KEY")


def _mask(name: str, value) -> str:
    if any(m in name.upper() for m in SECRET_MARKERS):
        return f"<set, len={len(str(value))}>" if value else "<EMPTY>"
    return repr(value)


def load_dev_settings() -> CoursesSettings:
    """Явно читаем .env.dev и применяем его к уже созданному settings."""
    print(f"cwd:      {Path.cwd()}")
    print(f"BASE_DIR: {BASE_DIR}")
    print(f".env.dev: {ENV_DEV} exists={ENV_DEV.exists()}")
    print(f".env.prod (relative) exists={Path('.env.prod').exists()}")

    assert ENV_DEV.exists(), f"Не найден {ENV_DEV}"

    # что лежит в самом файле (ключи, без значений секретов)
    raw = dotenv_values(ENV_DEV)
    print("\nКлючи в .env.dev:")
    for k, v in raw.items():
        print(f"  {k} = {_mask(k, v)}")

    # только .env.dev, без .env.prod
    dev = CoursesSettings(_env_file=ENV_DEV)

    print("\nSMTP из .env.dev (то, что реально считалось):")
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"):
        print(f"  {name} = {_mask(name, getattr(dev, name))}")

    # что в глобальном settings (после импорта) - для сравнения
    diff = {
        n: (_mask(n, getattr(settings, n)), _mask(n, getattr(dev, n)))
        for n in CoursesSettings.model_fields
        if getattr(settings, n) != getattr(dev, n)
    }
    print("\nОтличия global settings vs .env.dev:", diff or "нет")

    # подменяем значения в глобальном settings, которым пользуется app.core.email
    for name in CoursesSettings.model_fields:
        setattr(settings, name, getattr(dev, name))

    assert settings.SMTP_USER, "SMTP_USER пустой: .env.dev не подхватился"
    assert settings.SMTP_PASSWORD, "SMTP_PASSWORD пустой: .env.dev не подхватился"
    return dev


async def test_email():
    load_dev_settings()
    email = "ibarbylev@gmail.com"
    ok = await send_email(
        to_email=email,
        subject="Тестовое письмо от Courses",
        html_content="<h2>Тест</h2><p>SMTP работает ✅</p>",
    )
    assert ok, "send_email вернул False"


async def test_verification_email():
    load_dev_settings()
    email = "ibarbylev@gmail.com"
    ui_lang = "ru"

    translations = Translations.load(locale_dir, [ui_lang], domain="messages")
    templates.env.install_gettext_translations(translations, newstyle=True)

    ok = await send_verification_email(
        to_email=email,
        token=create_email_verification_token(email),
        ui_lang=ui_lang,
    )
    assert ok, "send_verification_email вернул False"


if __name__ == "__main__":
    asyncio.run(test_verification_email())
