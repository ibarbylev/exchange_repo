import uuid
from fastapi import APIRouter, Request, HTTPException, Form, Query, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.config import templates, DEFAULT_SOURCE_LANGUAGE, DEFAULT_UI_LANGUAGE
from app.core.security import create_access_token
from app.db.dependencies import DBPoolDep, CurrentSuperUser
from app.middleware.csrf import verify_csrf
from app.repositories import coach_schedule
from app.repositories.user import create_user_session
from app.repositories.shop import pay_order, replenish_balance_and_pay_order


router = APIRouter(prefix="/admin", tags=["admin"])


async def get_all_tables(pool) -> list[str]:
    """Получаем все пользовательские таблицы БД"""
    query = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name ASC;
    """
    rows = await pool.fetch(query)
    return [row["table_name"] for row in rows]


async def get_table_columns(pool, table_name: str):
    """Получаем список колонок таблицы"""
    query = """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position;
    """
    return await pool.fetch(query, table_name)


async def get_table_data(pool, table_name: str, page: int = 1, per_page: int = 100):
    """Пагинированные данные + общее количество строк"""
    offset = (page - 1) * per_page

    # Общее количество строк
    count_query = f'SELECT COUNT(*) FROM "{table_name}"'
    total = await pool.fetchval(count_query)

    # Данные с попыткой сортировки по id
    try:
        data_query = f'SELECT * FROM "{table_name}" ORDER BY 1 LIMIT $1 OFFSET $2'
        data = await pool.fetch(data_query, per_page, offset)
    except Exception:
        # Fallback без сортировки
        data_query = f'SELECT * FROM "{table_name}" LIMIT $1 OFFSET $2'
        data = await pool.fetch(data_query, per_page, offset)

    return data, total


async def get_table_data_with_filters(pool, table_name: str, page: int = 1, per_page: int = 100, filters: list = None):
    """Пагинация + фильтрация по нескольким полям"""
    offset = (page - 1) * per_page
    filters = filters or []

    # Считаем общее количество с фильтрами
    count_query = f'SELECT COUNT(*) FROM "{table_name}"'
    params = []
    where_clauses = []

    for i, (field, value) in enumerate(filters):
        if field and value:
            where_clauses.append(f'"{field}" = ${len(params) + 1}')
            params.append(value)

    if where_clauses:
        count_query += " WHERE " + " AND ".join(where_clauses)

    total = await pool.fetchval(count_query, *params)

    # Основной запрос
    data_query = f'SELECT * FROM "{table_name}"'
    if where_clauses:
        data_query += " WHERE " + " AND ".join(where_clauses)

    try:
        data_query += ' ORDER BY 1'
    except:
        pass

    data_query += ' LIMIT $' + str(len(params) + 1) + ' OFFSET $' + str(len(params) + 2)
    params.extend([per_page, offset])

    data = await pool.fetch(data_query, *params)

    return data, total

@router.get("/", response_class=HTMLResponse)
async def admin_root(request: Request, pool: DBPoolDep, current_superuser: CurrentSuperUser = None):
    """Главная страница админки"""
    return templates.TemplateResponse(
        name="admin/admin_base_page.html",
        request=request,
        context={}
    )


@router.get("/tables/", response_class=HTMLResponse)
async def admin_tables_list(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
):
    """Страница со списком всех таблиц"""
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    tables = await get_all_tables(pool)

    context = {
        "request": request,
        "tables": tables,
        "user": current_superuser,
        "total_pages": 0,
    }

    return templates.TemplateResponse(
        name="admin/tables_list.html",
        request=request,
        context=context
    )


@router.get("/access/", response_class=HTMLResponse)
async def admin_access_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    q: str = Query(None),                    # параметр поиска
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    user = None
    if q:
        user = await pool.fetchrow("""
            SELECT id, username, email, access_level, access_until
            FROM users
            WHERE email ILIKE $1 OR username ILIKE $1
            LIMIT 1
        """, f"%{q}%")

    context = {
        "request": request,
        "user": current_superuser,
        "query": q,
        "found_user": user,                    # найденный пользователь
        "csrf_token": getattr(request.state, "csrf_token", None),
    }

    return templates.TemplateResponse(
        request=request,
        name="admin/access.html",
        context=context
    )


@router.post("/access/search/", response_class=HTMLResponse)
async def admin_access_search(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    query: str = Form(...),
    _ = Depends(verify_csrf),
):
    """Поиск пользователя по email или username"""
    if not current_superuser:
        raise HTTPException(status_code=401)

    user = await pool.fetchrow("""
        SELECT id, username, email, access_level, access_until
        FROM users
        WHERE email ILIKE $1 OR username ILIKE $1
        LIMIT 1
    """, f"%{query}%")

    if not user:
        return HTMLResponse("<div class='alert alert-warning'>Пользователь не найден</div>")

    context = {"user": user}
    return templates.TemplateResponse(
        request=request,
        name="admin/access_user_card.html",
        context=context
    )


@router.post("/access/update/", response_class=RedirectResponse)
async def admin_access_update(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    user_id: int = Form(...),
    new_access: int = Form(...),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        raise HTTPException(status_code=401)

    # Получаем текущие данные пользователя
    current = await pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    if not current:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    current_level = current["access_level"] or 0
    current_until = current["access_until"]
    now = datetime.now(timezone.utc)

    # === Проверки  ===
    if current_level == 0:
        return RedirectResponse("/admin/access/?error=level_zero", status_code=303)

    if current_until and current_until < now:
        return RedirectResponse("/admin/access/?error=expired", status_code=303)

    if new_access == current_level:
        return RedirectResponse("/admin/access/?error=same_level", status_code=303)

    # === Логика изменения доступа (ваша) ===
    new_until = None

    if new_access == 0:
        await pool.execute("""
            UPDATE users 
            SET access_level = 0, access_until = NULL 
            WHERE id = $1
        """, user_id)

    elif new_access == 1:
        if current_until and current_until > now:
            delta = (current_until - now).total_seconds()
            new_until = now + timedelta(seconds=delta * 1.5)
        else:
            new_until = now + timedelta(days=30)

        await pool.execute("""
            UPDATE users SET access_level = 1, access_until = $1 WHERE id = $2
        """, new_until, user_id)

    elif new_access == 2:
        if current_until and current_until > now:
            delta = (current_until - now).total_seconds()
            new_until = now + timedelta(seconds=delta * (2/3))
        else:
            new_until = now + timedelta(days=30)

        await pool.execute("""
            UPDATE users SET access_level = 2, access_until = $1 WHERE id = $2
        """, new_until, user_id)

    # Записываем в историю
    await pool.execute("""
        INSERT INTO user_access_history 
        (user_id, old_access_level, old_access_until, new_access_level, new_access_until, reason, note)
        VALUES ($1, $2, $3, $4, $5, 'admin', 'Изменение доступа админом')
    """, user_id, current_level, current_until, new_access, new_until)

    # Редирект обратно с сообщением успеха
    response = RedirectResponse("/admin/access/?success=1", status_code=303)
    return response


#  --- Подарок доступа от админа -------------------------------------------
@router.get("/gift-access/", response_class=HTMLResponse)
async def admin_gift_access_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    q: str = Query(None),                    # <-- добавили
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    found_user = None
    if q:
        found_user = await pool.fetchrow("""
            SELECT id, username, email, access_level, access_until
            FROM users
            WHERE email ILIKE $1 OR username ILIKE $1
            LIMIT 1
        """, f"%{q}%")

    context = {
        "request": request,
        "user": current_superuser,
        "query": q,
        "found_user": found_user,
        "csrf_token": getattr(request.state, "csrf_token", None),
    }
    return templates.TemplateResponse(
        request=request,
        name="admin/gift_access.html",
        context=context
    )


@router.post("/gift-access/", response_class=RedirectResponse)
async def admin_gift_access(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    user_id: int = Form(...),
    access_level: int = Form(...),
    duration_months: int = Form(...),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        raise HTTPException(status_code=401)

    if access_level not in (1, 2):
        return RedirectResponse("/admin/gift-access/?error=invalid_level", status_code=303)

    if duration_months not in (1, 12):
        return RedirectResponse("/admin/gift-access/?error=invalid_duration", status_code=303)

    now = datetime.now(timezone.utc)
    duration = timedelta(days=30 * duration_months)

    current = await pool.fetchrow(
        "SELECT access_level, access_until FROM users WHERE id = $1", user_id
    )
    if not current:
        return RedirectResponse("/admin/gift-access/?error=user_not_found", status_code=303)  # ← исправлено

    current_level = current["access_level"] or 0
    current_until = current["access_until"]

    # === Главное исправление ===
    if current_level != 0 and current_level != access_level:
        return RedirectResponse("/admin/gift-access/?error=different_level", status_code=303)

    # === Расчёт новой даты доступа ===
    if current_level == 0 or (current_until and current_until < now):
        new_until = now + duration
    else:
        new_until = current_until + duration

    await pool.execute("""
        UPDATE users 
        SET access_level = $1, access_until = $2 
        WHERE id = $3
    """, access_level, new_until, user_id)

    await pool.execute("""
        INSERT INTO user_access_history 
            (user_id, old_access_level, old_access_until, new_access_level, new_access_until, reason, note)
        VALUES ($1, $2, $3, $4, $5, 'gift', 'Подарок доступа от администратора')
    """, user_id, current_level, current_until, access_level, new_until)

    return RedirectResponse("/admin/gift-access/?success=1", status_code=303)


async def _impersonate_page_response(
    request: Request,
    pool,
    current_superuser,
    query: str | None,
):
    """Общая отрисовка страницы impersonate."""
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    users = None
    if query:
        users = await pool.fetch("""
            SELECT id, username, email, first_name, last_name, access_level, is_confirmed
            FROM users
            WHERE email ILIKE $1 OR username ILIKE $1
            ORDER BY created_at DESC
            LIMIT 30
        """, f"%{query}%")

    context = {
        "request": request,
        "user": current_superuser,
        "users": users,
        "query": query,
        "csrf_token": getattr(request.state, "csrf_token", None),
    }

    return templates.TemplateResponse(
        request=request,
        name="admin/impersonate.html",
        context=context
    )


@router.get("/impersonate/", response_class=HTMLResponse)
async def impersonate_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser,
    query: str | None = Query(None),
):
    """Страница входа под пользователем."""
    return await _impersonate_page_response(request, pool, current_superuser, query)


@router.post("/impersonate/", response_class=HTMLResponse)
async def impersonate_search(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser,
    query: str = Form(None),
    _=Depends(verify_csrf),
):
    """Поиск пользователя для входа под ним."""
    return await _impersonate_page_response(request, pool, current_superuser, query)


@router.post("/impersonate/login-as/", response_class=RedirectResponse)
async def login_as_user(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser,
    user_id: int = Form(...),
    _=Depends(verify_csrf),
):
    """Войти под выбранным пользователем"""
    if not current_superuser:
        raise HTTPException(status_code=401)

    target = await pool.fetchrow(
        "SELECT id, email, username FROM users WHERE id = $1",
        user_id
    )
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Создаём новую сессию
    new_jti = str(uuid.uuid4())
    await create_user_session(
        pool=pool,
        user_id=target["id"],
        jti=new_jti,
        ip_address=request.client.host,
        user_agent=f"Impersonated by admin (id={current_superuser['user_id']})"
    )

    # Создаём JWT
    access_token = create_access_token(
        data={
            "sub": str(target["id"]),
            "email": target["email"],
        },
        jti=new_jti
    )

    # Редирект на главную
    redirect_url = f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/"
    response = RedirectResponse(url=redirect_url, status_code=303)

    # Устанавливаем токен
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 60 * 60,
        path="/",
    )

    # === Flash-сообщение ===
    display_name = target["username"] or target["email"]
    response.set_cookie(
        key="flash",
        value=f"impersonate_success:{display_name}",
        max_age=8,           # 8 секунд
        httponly=False,
        samesite="lax",
    )
    return response


# @router.post("/gift-access/", response_class=JSONResponse)
# async def admin_gift_access(
#     request: Request,
#     pool: DBPoolDep,
#     current_superuser: CurrentSuperUser = None,
#     user_id: int = Form(...),
#     access_level: int = Form(...),   # 1 или 2
#     duration_months: int = Form(...), # 1 или 12
#     _=Depends(verify_csrf),
# ):
#     if not current_superuser:
#         raise HTTPException(status_code=401)
#
#     if access_level not in (1, 2):
#         return JSONResponse(
#             status_code=400,
#             content={"success": False, "message": "Некорректный уровень доступа"}
#         )
#
#     if duration_months not in (1, 12):
#         return JSONResponse(
#             status_code=400,
#             content={"success": False, "message": "Некорректный срок"}
#         )
#
#     now = datetime.now(timezone.utc)
#     duration = timedelta(days=30 * duration_months)
#
#     # Получаем текущий доступ пользователя
#     current = await pool.fetchrow(
#         "SELECT access_level, access_until FROM users WHERE id = $1", user_id
#     )
#     if not current:
#         return JSONResponse(
#             status_code=404,
#             content={"success": False, "message": "Пользователь не найден"}
#         )
#
#     current_level = current["access_level"] or 0
#     current_until = current["access_until"]
#
#     # === ПРОВЕРКИ ===
#     if current_level != 0 and current_level != access_level:
#         return JSONResponse(
#             status_code=400,
#             content={
#                 "success": False,
#                 "message": "У пользователя уже есть другой уровень доступа. "
#                            "Используйте страницу «Изменение доступа»."
#             }
#         )
#
#     # === Расчёт новой даты ===
#     if current_level == 0 or (current_until and current_until < now):
#         # Нет доступа или подписка истекла
#         new_until = now + duration
#     else:
#         # Есть активный доступ того же уровня — продлеваем
#         new_until = current_until + duration
#
#     # Обновляем пользователя
#     await pool.execute("""
#         UPDATE users
#         SET access_level = $1, access_until = $2
#         WHERE id = $3
#     """, access_level, new_until, user_id)
#
#     # Записываем в историю
#     await pool.execute("""
#         INSERT INTO user_access_history
#             (user_id, old_access_level, old_access_until, new_access_level, new_access_until, reason, note)
#         VALUES ($1, $2, $3, $4, $5, 'gift', 'Подарок доступа от администратора')
#     """, user_id, current_level, current_until, access_level, new_until)
#
#     return {
#         "success": True,
#         "message": f"Доступ успешно подарен (уровень {access_level}, срок {duration_months} мес.)"
#     }


# ==================== РУЧНАЯ ОПЛАТА ЗАКАЗОВ ====================

@router.get("/manual-payment/", response_class=HTMLResponse)
async def manual_payment_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser,
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    # Получаем все pending заказы
    pending_orders = await pool.fetch("""
        SELECT 
            o.id,
            o.amount,
            o.created_at,
            u.id as user_id,
            u.email,
            u.username
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.status = 'pending'
        ORDER BY o.created_at DESC
    """)

    return templates.TemplateResponse(
        name="admin/manual_payment.html",
        request=request,
        context={
            "user": current_superuser,
            "pending_orders": [dict(o) for o in pending_orders],
            "csrf_token": getattr(request.state, "csrf_token", None),        },
    )


@router.post("/manual-payment/confirm/", response_class=RedirectResponse)
async def manual_payment_confirm(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser,
    order_id: int = Form(...),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        raise HTTPException(status_code=401)

    try:
        result = await replenish_balance_and_pay_order(
            pool=pool,
            order_id=order_id,
            comment=f"Ручная оплата администратором (admin_id={current_superuser['user_id']})"
        )

        response = RedirectResponse("/admin/manual-payment/", status_code=303)
        response.set_cookie(
            key="flash",
            value="payment_success",
            max_age=8,
            httponly=False,
            samesite="lax"
        )
        return response

    except HTTPException as e:
        raise e


# --- Изменение роли пользователя ------------------------------------------

@router.get("/user-role-change/", response_class=HTMLResponse)
async def admin_user_role_change_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    q: str = Query(None),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    found_user = None

    if q:
        found_user = await pool.fetchrow("""
            SELECT
                id,
                username,
                email,
                role,
                first_name,
                last_name,
                patronymic
            FROM users
            WHERE email ILIKE $1
            LIMIT 1
        """, f"%{q.strip()}%")

    context = {
        "request": request,
        "user": current_superuser,
        "query": q,
        "found_user": found_user,
        "csrf_token": getattr(request.state, "csrf_token", None),
    }

    return templates.TemplateResponse(
        request=request,
        name="admin/user_role_change.html",
        context=context
    )


@router.post("/user-role-change/")
async def admin_user_role_change(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    user_id: int = Form(...),
    role: str = Form(...),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    allowed_roles = {
        "admin",
        "student",
        "manager",
        "coach",
        "account_manager",
    }

    if role not in allowed_roles:
        return RedirectResponse(
            url="/admin/user-role-change/?error=invalid_role",
            status_code=303
        )

    target_user = await pool.fetchrow("""
        SELECT id, email
        FROM users
        WHERE id = $1
    """, user_id)

    if not target_user:
        return RedirectResponse(
            url="/admin/user-role-change/?error=user_not_found",
            status_code=303
        )

    await pool.execute("""
        UPDATE users
        SET
            role = $1,
            updated_at = now()
        WHERE id = $2
    """, role, user_id)

    return RedirectResponse(
        url=f"/admin/user-role-change/?q={target_user['email']}&success=1",
        status_code=303
    )


# --- Изменение / создание расписания Студент-Коуч ------------------------------------------

def _page_url(**params) -> str:
    from urllib.parse import urlencode
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    query = urlencode(clean)
    return "/admin/coach-student-schedule/" + (f"?{query}" if query else "")


def _optional_int(value: Any) -> int | None:
    """Пустая строка из <select value=""> не является int — это None."""
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    return int(raw)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Некорректная дата занятия") from exc


@router.get("/coach-student-schedule/", response_class=HTMLResponse)
async def coach_student_schedule_page(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    mode: str = Query("assign"),
    assignment: str = Query("new"),
    student_id: str | None = Query(None),
    coach_id: str | None = Query(None),
    success: str | None = Query(None),
    error: str | None = Query(None),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    if mode not in {"assign", "schedule"}:
        mode = "assign"
    if assignment not in {"new", "assigned", "all"}:
        assignment = "new"

    try:
        student_id = _optional_int(student_id)
        coach_id = _optional_int(coach_id)
    except ValueError:
        student_id = None
        coach_id = None

    async with pool.acquire() as conn:
        students = await coach_schedule.list_students(conn, assignment=assignment)
        coaches = await coach_schedule.list_coaches(conn)
        order_items = (
            await coach_schedule.list_order_items(conn, student_id) if student_id else []
        )
        lessons = []
        if mode == "schedule":
            lessons = await coach_schedule.list_lessons(
                conn, student_id=student_id, coach_id=coach_id
            )

    context = {
        "request": request,
        "user": current_superuser,
        "mode": mode,
        "assignment": assignment,
        "student_id": student_id,
        "coach_id": coach_id,
        "students": students,
        "coaches": coaches,
        "order_items": order_items,
        "lessons": lessons,
        "success": success,
        "error": error,
        "csrf_token": getattr(request.state, "csrf_token", None),
        "weekday_names": coach_schedule.WEEKDAY_NAMES,
    }
    return templates.TemplateResponse(
        request=request,
        name="admin/coach_student_schedule.html",
        context=context,
    )


@router.get("/coach-student-schedule/lesson-hints/")
async def coach_lesson_hints(
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    prefix: str = Query(""),
    student_id: str | None = Query(None),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )
    async with pool.acquire() as conn:
        hints = await coach_schedule.list_block_hints(
            conn, prefix, student_id=_optional_int(student_id)
        )
    return hints


@router.get("/coach-student-schedule/order-items/")
async def coach_order_items(
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    student_id: int = Query(...),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )
    async with pool.acquire() as conn:
        items = await coach_schedule.list_order_items(conn, student_id)
    return items


@router.post("/coach-student-schedule/create/")
async def coach_create_lessons(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    student_id: int = Form(...),
    coach_id: int = Form(...),
    order_item_id: int = Form(...),
    lessons_count: int = Form(...),
    first_lesson_name: str = Form(""),
    start_date: str = Form(""),
    times_per_week: int = Form(1),
    weekday_1: str = Form(""),
    time_1: str = Form(""),
    weekday_2: str = Form(""),
    time_2: str = Form(""),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    weekday_items: list[dict] = []
    try:
        if not (first_lesson_name or "").strip():
            raise ValueError("Укажите имя первого урока")
        if times_per_week not in (1, 2):
            raise ValueError("Периодичность — 1 или 2 раза в неделю")
        if weekday_1 and time_1:
            weekday_items.append({"weekday": int(weekday_1), "time": time_1})
        if times_per_week == 2 and weekday_2 and time_2:
            weekday_items.append({"weekday": int(weekday_2), "time": time_2})
        weekdays = coach_schedule.parse_weekdays(weekday_items) if weekday_items else []
        start_on = date.fromisoformat(start_date) if start_date else None
        async with pool.acquire() as conn:
            created = await coach_schedule.create_lessons(
                conn,
                student_id=student_id,
                coach_id=coach_id,
                order_item_id=order_item_id,
                lessons_count=lessons_count,
                first_lesson_name=first_lesson_name,
                start_on=start_on,
                weekdays=weekdays,
            )
        return RedirectResponse(
            url=_page_url(
                mode="schedule",
                assignment="assigned",
                student_id=student_id,
                success=f"created:{len(created)}",
            ),
            status_code=303,
        )
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            url=_page_url(
                mode="assign",
                assignment="new",
                student_id=student_id,
                error=str(exc),
            ),
            status_code=303,
        )


@router.post("/coach-student-schedule/update/")
async def coach_update_lesson(
    request: Request,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    lesson_id: int = Form(...),
    student_id: str | None = Form(None),
    filter_coach_id: str | None = Form(None),
    coach_id: str = Form(""),
    lesson_name: str = Form(""),
    scheduled_at: str = Form(""),
    status: str = Form(""),
    stars: str = Form(""),
    coach_comment: str = Form(""),
    _=Depends(verify_csrf),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    student_id = _optional_int(student_id)
    filter_coach_id = _optional_int(filter_coach_id)

    try:
        payload: dict[str, Any] = {
            "coach_id": int(coach_id) if coach_id else None,
            "lesson_name": (lesson_name or "").strip(),
            "scheduled_at": _parse_optional_datetime(scheduled_at) if scheduled_at is not None else None,
            "status": status or None,
            "stars": int(stars) if stars else None,
            "coach_comment": coach_comment.strip() or None,
        }
        if not payload["lesson_name"]:
            raise ValueError("Имя урока обязательно")
        async with pool.acquire() as conn:
            await coach_schedule.update_lesson(conn, lesson_id, payload)
        return RedirectResponse(
            url=_page_url(
                mode="schedule",
                assignment="assigned",
                student_id=student_id,
                coach_id=filter_coach_id,
                success="updated",
            ),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=_page_url(
                mode="schedule",
                assignment="assigned",
                student_id=student_id,
                coach_id=filter_coach_id,
                error=str(exc),
            ),
            status_code=303,
        )




@router.get("/{table_name}/", response_class=HTMLResponse)
async def admin_table_list(
    request: Request,
    table_name: str,
    pool: DBPoolDep,
    current_superuser: CurrentSuperUser = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=10, le=500),
):
    if not current_superuser:
        return RedirectResponse(
            url=f"/{DEFAULT_SOURCE_LANGUAGE}/{DEFAULT_UI_LANGUAGE}/auth/login/",
            status_code=303
        )

    tables = await get_all_tables(pool)

    if table_name not in tables:
        raise HTTPException(status_code=404, detail="Таблица не найдена")

    columns = await get_table_columns(pool, table_name)

    # === Обработка фильтров ===
    filters = []
    i = 0
    while True:
        field = request.query_params.get(f"f{i}_field")
        value = request.query_params.get(f"f{i}_value")
        if not field or not value:
            break
        filters.append((field, value))
        i += 1

    data, total = await get_table_data_with_filters(pool, table_name, page, per_page, filters)
    total_pages = (total + per_page - 1) // per_page

    context = {
        "request": request,
        "tables": tables,
        "current_table": table_name,
        "columns": columns,
        "data": data,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "user": current_superuser,
        "filters": filters,
    }

    return templates.TemplateResponse(
        name="admin/tables_list.html",
        request=request,
        context=context
    )
