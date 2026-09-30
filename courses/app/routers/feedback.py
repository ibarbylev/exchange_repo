from html import escape
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import settings
from app.core.email import send_email
from app.db.dependencies import CurrentUserOptional, DBPoolDep
from app.middleware.csrf import verify_csrf


router = APIRouter(tags=["feedback"])

FEEDBACK_TOPICS = (
    "Вопрос по программе обучения",
    "Вопрос к службе технической поддержки",
    "Ошибка в тексте (звуковом файле, видео)",
    "Прочие вопросы",
)


def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"


def _login_url(source_lang: str, ui_lang: str, next_url: str) -> str:
    return (
        f"/{source_lang}/{ui_lang}/auth/login/"
        f"?next={quote(next_url)}"
        f"&msg=feedback_auth"
    )


def _as_bool(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes", "da"}


def _notify_email() -> str:
    return (
        getattr(settings, "FEEDBACK_NOTIFY_EMAIL", "")
        or getattr(settings, "SMTP_FROM_EMAIL", "")
        or getattr(settings, "SMTP_USER", "")
    )


async def _notify_admin(
    *,
    user: dict,
    topic: str,
    message: str,
    page_url: str,
    exercise_name: str,
    want_reply: bool,
    feedback_id: int,
) -> None:
    to_email = _notify_email()
    if not to_email:
        print("[feedback] Нет адреса для уведомления (SMTP_FROM_EMAIL / FEEDBACK_NOTIFY_EMAIL)")
        return

    user_email = escape(str(user.get("email") or "—"))
    user_id = user.get("user_id") or user.get("id") or "—"
    username = escape(str(user.get("username") or "—"))
    topic_safe = escape(topic)
    message_safe = escape(message).replace("\n", "<br>")
    page_safe = escape(page_url or "—")
    exercise_safe = escape(exercise_name or "—")
    reply_text = "да" if want_reply else "нет"

    html_content = f"""
    <h2>Новое сообщение обратной связи #{feedback_id}</h2>
    <p><strong>Пользователь:</strong> {username} (id={user_id}, {user_email})</p>
    <p><strong>Тема:</strong> {topic_safe}</p>
    <p><strong>Страница:</strong> {page_safe}</p>
    <p><strong>Упражнение:</strong> {exercise_safe}</p>
    <p><strong>Нужен ответ:</strong> {reply_text}</p>
    <hr>
    <p>{message_safe}</p>
    """

    await send_email(
        to_email=to_email,
        subject=f"[Feedback] {topic}",
        html_content=html_content,
    )


@router.post("/{source_lang}/{ui_lang}/feedback/", name="feedback_submit")
async def submit_feedback(
    request: Request,
    pool: DBPoolDep,
    source_lang: str,
    ui_lang: str,
    current_user: CurrentUserOptional,
    topic: str = Form(""),
    message: str = Form(""),
    page_url: str = Form(""),
    exercise_name: str = Form(""),
    want_reply: str | None = Form(None),
    _csrf=Depends(verify_csrf),
):
    next_url = page_url or (request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    login_url = _login_url(source_lang, ui_lang, next_url)

    if not current_user:
        if _is_ajax(request):
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "detail": "Чтобы отправить сообщение, необходимо войти в аккаунт.",
                    "login_url": login_url,
                },
            )
        response = RedirectResponse(url=login_url, status_code=303)
        response.set_cookie(
            key="flash",
            value="feedback_need_login",
            max_age=60,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return response

    topic = (topic or "").strip()
    message = (message or "").strip()
    page_url = (page_url or "").strip()
    exercise_name = (exercise_name or "").strip() or None
    want_reply_flag = _as_bool(want_reply)

    if topic not in FEEDBACK_TOPICS or not message:
        detail = "Заполните тему и текст сообщения."
        if _is_ajax(request):
            return JSONResponse(status_code=400, content={"ok": False, "detail": detail})
        response = RedirectResponse(url=next_url or f"/{source_lang}/{ui_lang}/", status_code=303)
        response.set_cookie(
            key="flash",
            value="feedback_error",
            max_age=60,
            httponly=False,
            samesite="lax",
            path="/",
        )
        return response

    user_id = current_user.get("user_id") or current_user.get("id")

    row = await pool.fetchrow(
        """
        INSERT INTO feedback (
            user_id, topic, message, page_url, exercise_name,
            want_reply, question_quality, is_resolved
        )
        VALUES ($1, $2, $3, $4, $5, $6, 0, FALSE)
        RETURNING id, created_at
        """,
        user_id,
        topic,
        message,
        page_url or None,
        exercise_name,
        want_reply_flag,
    )

    try:
        await _notify_admin(
            user=current_user,
            topic=topic,
            message=message,
            page_url=page_url,
            exercise_name=exercise_name or "",
            want_reply=want_reply_flag,
            feedback_id=row["id"],
        )
    except Exception as exc:
        print(f"[feedback] Не удалось отправить уведомление: {exc}")

    if _is_ajax(request):
        return JSONResponse(
            content={
                "ok": True,
                "id": row["id"],
                "detail": "Спасибо! Ваше сообщение отправлено.",
            }
        )

    response = RedirectResponse(url=next_url or f"/{source_lang}/{ui_lang}/", status_code=303)
    response.set_cookie(
        key="flash",
        value="feedback_sent",
        max_age=60,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return response
