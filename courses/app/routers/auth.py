import uuid
from fastapi import APIRouter, Response, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse

from app.db.dependencies import DBPoolDep, CurrentUser
from app.core.security import (
    check_password_for_email,
    create_access_token,
    create_email_verification_token,
    get_password_hash,
    verify_email_token,
    verify_password_reset_token,
    create_password_reset_token,
)
from app.core.email import send_verification_email, send_password_reset_email
from app.middleware.csrf import verify_csrf
from app.repositories.user import (
    get_user_by_email,
    update_last_login,
    create_user_session,
    delete_user_session,
    delete_expired_unconfirmed_users,
    can_send_verification_email
)
from app.repositories.loyalty import award_welcome
from app.routers.deps import LangDep, render_template

router = APIRouter(tags=["auth"])

# --- login / register GET ------------------------------------------------------------
@router.get("/{source_lang}/{ui_lang}/auth/login/", response_class=HTMLResponse, name="auth_login")
@router.get("/{source_lang}/{ui_lang}/auth/login",response_class=HTMLResponse)
async def login(request: Request, lang_pair: LangDep):
    return render_template(
        name="auth/login-register.html",
        request=request,
        lang_pair=lang_pair,
        context={"current_page": "courses_list"}
    )

# --- login POST ------------------------------------------------------------
@router.post("/{source_lang}/{ui_lang}/auth/login/", name="auth_login_post")
@router.post("/{source_lang}/{ui_lang}/auth/login", name="auth_login_post")
async def login(
        request: Request,
        lang_pair: LangDep,
        email: str = Form(...),
        password: str = Form(...),
        next: str | None = Form(None),
        pool: DBPoolDep = None,
        _ = Depends(verify_csrf),
    ):

    user = await get_user_by_email(pool, email)

    # --- Ошибка авторизации (пользователь не найден или неверный пароль) ---
    if not user:
        return render_template(
            name="auth/login-register.html",
            request=request,
            lang_pair=lang_pair,
            context={"error": "Неверный email или пароль"},
            status_code=401
        )

    is_valid_password = await check_password_for_email(pool, email, password)

    # === Ошибка авторизации ===
    if not user or not is_valid_password:
        return render_template(
            name="auth/login-register.html",
            request=request,
            lang_pair=lang_pair,
            context={"error": "Неверный email или пароль"},
            status_code=401
        )

    # === НОВАЯ ПРОВЕРКА: email не подтверждён ===
    if not user.get("is_confirmed", False):
        return render_template(
            name="auth/registration_success.html",   # та же страница, что и после регистрации
            request=request,
            lang_pair=lang_pair,
            context={
                "email": email,
                "not_confirmed": True,          # флаг для шаблона
                "error": "Ваш email ещё не подтверждён"
            }
        )

    # === Политика одного устройства ===
    new_jti = str(uuid.uuid4())

    # одновременной удаление старой сессии и создание новой
    await create_user_session(
        pool=pool,
        user_id=user["id"],
        jti=new_jti,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent")
    )

    access_token = create_access_token(
        data={
            "sub": str(user["id"]),
            "email": user["email"],
        },
        jti=new_jti
    )

    # === Создаём редирект ===
    from urllib.parse import unquote

    # Сначала берём из формы (скрытое поле), потом из query
    next_url = next or request.query_params.get("next")

    if next_url:
        next_url = unquote(next_url)
        if next_url.startswith("/") and not next_url.startswith("//"):
            redirect_url = next_url
        else:
            redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/"
    else:
        redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/"

    redirect_response = RedirectResponse(url=redirect_url, status_code=303)

    # Устанавливаем cookie именно в redirect_response
    redirect_response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,  # True в продакшене
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/",
    )

    await update_last_login(pool, user["id"])
    return redirect_response

# --- logout --------------------------------------------------------------------------
@router.get("/{source_lang}/{ui_lang}/logout/", response_class=RedirectResponse, name="auth_logout")
@router.get("/{source_lang}/{ui_lang}/logout", response_class=RedirectResponse)
async def logout(
    request: Request,
    lang_pair: LangDep,
    response: Response,
    pool: DBPoolDep,
    current_user: CurrentUser,
):

    # Удаляем сессию из БД
    if current_user:
        await delete_user_session(pool, current_user["user_id"])

    # Удаляем куку
    response.delete_cookie(key="access_token", path="/")

    return RedirectResponse(url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/", status_code=303)

# --- register POST -------------------------------------------------------------------
@router.post("/{source_lang}/{ui_lang}/auth/register/", name="auth_register_post")
@router.post("/{source_lang}/{ui_lang}/auth/register", name="auth_register_post")
async def register_post(
    request: Request,
    lang_pair: LangDep,
    email: str = Form(...),
    password: str = Form(...),
    first_name: str = Form(None),
    last_name: str = Form(None),
    pool: DBPoolDep = None,
    _=Depends(verify_csrf),
):

    # === Очистка просроченных неподтверждённых аккаунтов ===
    await delete_expired_unconfirmed_users(pool)
    email = email.strip().lower()

    # Проверяем существование пользователя
    existing = await get_user_by_email(pool, email)

    if existing:
        if existing.get("is_confirmed"):
            return render_template(
                name="auth/login-register.html",
                request=request,
                lang_pair=lang_pair,
                context={
                    "mode": "register",
                    "error": "Пользователь с таким email уже существует"
                },
                status_code=400
            )

        # Пользователь уже зарегистрирован, но не подтвердил email
        can_send, message = await can_send_verification_email(pool, email)

        if can_send:
            token = create_email_verification_token(email)
            await send_verification_email(email, token, lang_pair.source_lang, lang_pair.ui_lang)
            print(f"Повторное письмо подтверждения отправлено на {email}")
        else:
            # Показываем сообщение об ограничении
            return render_template(
                name="auth/registration_success.html",
                request=request,
                lang_pair=lang_pair,
                context={
                    "email": email,
                    "already_exists": True,
                    "error": message
                }
            )

        # Показываем страницу успеха
        return render_template(
            name="auth/registration_success.html",
            request=request,
            lang_pair=lang_pair,
            context={
                "email": email,
                "already_exists": True
            }
        )

    # --- НОВАЯ РЕГИСТРАЦИЯ ----------------------------------------------

    # Генерация username из email
    base_username = email.split('@')[0].replace('.', '').replace('-', '')
    username = base_username
    counter = 1

    while True:
        check_query = "SELECT id FROM users WHERE username = $1 LIMIT 1"
        exists = await pool.fetchval(check_query, username)
        if not exists:
            break
        username = f"{base_username}{counter}"
        counter += 1

    # Создаём пользователя
    hashed_password = get_password_hash(password)

    query = """
        INSERT INTO users (username, email, password_hash, first_name, last_name,
                           is_confirmed, is_active, created_at)
        VALUES ($1, $2, $3, $4, $5, FALSE, TRUE, NOW())
        RETURNING id, email, username
    """

    new_user = await pool.fetchrow(
        query, username, email, hashed_password, first_name, last_name
    )

    # Приветственные баллы лояльности
    await award_welcome(pool, new_user["id"])

    # Отправляем письмо подтверждения
    token = create_email_verification_token(email)
    await send_verification_email(
        to_email=email,
        token=token,
        source_lang=lang_pair.source_lang,
        ui_lang=lang_pair.ui_lang
    )

    # Показываем страницу успеха
    return render_template(
        name="auth/registration_success.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "email": email,
            "username": new_user["username"]
        }
    )
# --- confirm email -------------------------------------------------------------------

@router.get("/{source_lang}/{ui_lang}/auth/confirm-email/{token}/", response_class=HTMLResponse, name="confirm_email")
@router.get("/{source_lang}/{ui_lang}/auth/confirm-email/{token}", response_class=HTMLResponse)
async def confirm_email(
    request: Request,
    lang_pair: LangDep,
    token: str,
    pool: DBPoolDep = None,
):

    email = verify_email_token(token)
    if not email:
        return render_template(
            name="auth/confirm_email_invalid.html",
            request=request,
            lang_pair=lang_pair,
            context={"error": "Ссылка недействительна или истекла."},
            status_code=400
        )

    # Подтверждаем почту
    query = """
        UPDATE users 
        SET is_confirmed = TRUE, 
            last_login = NOW()
        WHERE LOWER(email) = LOWER($1)
        RETURNING id, username
    """
    user = await pool.fetchrow(query, email)

    if not user:
        return render_template(
            request=request,
            name="auth/confirm_email_invalid.html",
            lang_pair=lang_pair,
            context={"error": "Пользователь не найден"},
            status_code=404
        )

    return render_template(
        request=request,
        name="auth/confirm_email_success.html",
        lang_pair=lang_pair,
        context={
            "email": email,
            "username": user["username"]
        }
    )

# ====================== FORGOT PASSWORD ======================
@router.get("/{source_lang}/{ui_lang}/auth/forgot-password/", response_class=HTMLResponse, name="forgot_password")
@router.get("/{source_lang}/{ui_lang}/auth/forgot-password", response_class=HTMLResponse)
async def forgot_password_get(
    request: Request,
    lang_pair: LangDep,
):

    return render_template(
        request=request,
        name="auth/forgot_password.html",
        lang_pair=lang_pair,
    )


@router.post("/{source_lang}/{ui_lang}/auth/forgot-password/")
async def forgot_password_post(
    request: Request,
    lang_pair: LangDep,
    email: str = Form(...),
    pool: DBPoolDep = None,
    _ = Depends(verify_csrf),
):

    email = email.strip().lower()
    user = await get_user_by_email(pool, email)

    # Всегда отправляем письмо (даже если пользователя нет — для безопасности)
    if user:
        token = create_password_reset_token(email)
        await send_password_reset_email(email, token, lang_pair.source_lang, lang_pair.ui_lang)

    return render_template(
        request=request,
        name="auth/forgot_password_success.html",
        lang_pair=lang_pair,
        context={"email": email}
    )


# ====================== RESET PASSWORD ======================
@router.get("/{source_lang}/{ui_lang}/auth/reset-password/{token}/",
            response_class=HTMLResponse, name="reset_password")
@router.get("/{source_lang}/{ui_lang}/auth/reset-password/{token}",
            response_class=HTMLResponse)
async def reset_password_get(
    request: Request,
    lang_pair: LangDep,
    token: str,
):

    return render_template(
        request=request,
        name="auth/reset_password.html",
        lang_pair=lang_pair,
        context={"token": token }
    )


@router.post("/{source_lang}/{ui_lang}/auth/reset-password/{token}/")
async def reset_password_post(
    request: Request,
    lang_pair: LangDep,
    token: str,
    password: str = Form(...),
    password_confirm: str = Form(...),
    pool: DBPoolDep = None,
    _=Depends(verify_csrf),

):

    if password != password_confirm:
        return render_template(
            request=request,
            name="auth/reset_password.html",
            lang_pair=lang_pair,
            context={
                    "token": token,
                    "error": "Пароли не совпадают"
                },
            status_code=400
        )

    email = verify_password_reset_token(token)
    if not email:
        return render_template(
            request=request,
            name="auth/confirm_email_invalid.html",
            lang_pair=lang_pair,
            context={"error": "Ссылка недействительна или устарела"},
            status_code=400
        )

    # Меняем пароль
    hashed_password = get_password_hash(password)
    await pool.execute(
        "UPDATE users SET password_hash = $1 WHERE LOWER(email) = LOWER($2)",
        hashed_password, email
    )

    return render_template(
        request=request,
        name="auth/reset_password_success.html",
        lang_pair=lang_pair,
    )

# --- RESEND VERIFICATION ----------------------------------------------------------------
@router.get("/{source_lang}/{ui_lang}/auth/resend-verification/", response_class=HTMLResponse, name="resend_verification")
@router.get("/{source_lang}/{ui_lang}/auth/resend-verification", response_class=HTMLResponse)
async def resend_verification_get(
    request: Request,
    lang_pair: LangDep,
    email: str = None,          # передаётся через ?email=...
):

    return render_template(
        request=request,
        name="auth/resend_verification.html",
        lang_pair=lang_pair,
        context={"email": email}
    )


@router.post("/{source_lang}/{ui_lang}/auth/resend-verification/", name="resend_verification_post")
@router.post("/{source_lang}/{ui_lang}/auth/resend-verification", name="resend_verification_post")
async def resend_verification_post(
    request: Request,
    lang_pair: LangDep,
    email: str = Form(...),
    pool: DBPoolDep = None,
    _=Depends(verify_csrf),
):

    email = email.strip().lower()

    can_send, message = await can_send_verification_email(pool, email)

    if not can_send:
        return render_template(
            request=request,
            name="auth/resend_verification.html",
            lang_pair=lang_pair,                                 # ← передаём объект LangPair
            context={
                "email": email,
                "error": message
            },
            status_code=400
        )

    # Отправляем письмо повторно
    token = create_email_verification_token(email)
    await send_verification_email(email, token, lang_pair.source_lang, lang_pair.ui_lang)

    return render_template(
        request=request,
        name="auth/resend_verification_success.html",
        lang_pair=lang_pair,
        context={"email": email}
    )
