import asyncio
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib

from app.core.config import settings, templates


async def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Базовая асинхронная отправка письма"""
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(html_content, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            use_tls=settings.SMTP_PORT == 465,    # True если 465, иначе False
            start_tls=settings.SMTP_PORT == 587,  # True если 567, иначе False
        )
        print(f"✅ Email успешно отправлен на {to_email}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки email на {to_email}: {e}")
        return False


async def send_verification_email(
        to_email: str,
        token: str,
        source_lang: str = None,
        ui_lang: str = None
) -> bool:
    """Отправка письма с подтверждением почты"""

    # Используем дефолтные языки проекта, если не передали
    if source_lang is None:
        from app.core.config import DEFAULT_SOURCE_LANGUAGE
        source_lang = DEFAULT_SOURCE_LANGUAGE
    if ui_lang is None:
        from app.core.config import DEFAULT_UI_LANGUAGE
        ui_lang = DEFAULT_UI_LANGUAGE

    verify_url = f"http://127.0.0.1:8001/{source_lang}/{ui_lang}/auth/confirm-email/{token}/"

    # Контекст для шаблона
    context = {
        "verify_url": verify_url,
        "source_lang": source_lang,
        "ui_lang": ui_lang,
    }

    # Рендерим шаблон
    html_response = templates.TemplateResponse(
        name="auth/email_verification.html",
        request=None,  # None, потому что это не HTTP-запрос
        context=context,
    )
    html_content = html_response.body.decode("utf-8")

    subject = "Подтвердите вашу электронную почту" if ui_lang == "ru" else "Verify your email"

    return await send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content
    )


async def send_password_reset_email(
    to_email: str,
    token: str,
    source_lang: str = None,
    ui_lang: str = None
) -> bool:
    """Отправка письма для сброса пароля"""

    # Используем дефолтные языки проекта, если не передали
    if source_lang is None:
        from app.core.config import DEFAULT_SOURCE_LANGUAGE
        source_lang = DEFAULT_SOURCE_LANGUAGE
    if ui_lang is None:
        from app.core.config import DEFAULT_UI_LANGUAGE
        ui_lang = DEFAULT_UI_LANGUAGE

    if source_lang is None:
        source_lang = DEFAULT_SOURCE_LANGUAGE
    if ui_lang is None:
        ui_lang = DEFAULT_UI_LANGUAGE

    reset_url = f"http://127.0.0.1:8001/{source_lang}/{ui_lang}/auth/reset-password/{token}/"

    context = {
        "reset_url": reset_url,
        "source_lang": source_lang,
        "ui_lang": ui_lang,
    }

    html_response = templates.TemplateResponse(
        name="auth/password_reset_email.html",
        request=None,
        context=context,
    )
    html_content = html_response.body.decode("utf-8")

    subject = "Сброс пароля" if ui_lang == "ru" else "Password Reset"

    return await send_email(
        to_email=to_email,
        subject=subject,
        html_content=html_content
    )



if __name__ == "__main__":
    asyncio.run(
        send_verification_email(
            to_email="test@example.com",
            token="test-token-123",
            source_lang="bg",
            ui_lang="ru",
        )
    )