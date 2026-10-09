from fastapi import APIRouter, Request, HTTPException, Path, Form, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db.dependencies import DBPoolDep, CurrentUser, get_current_active_user, get_current_active_user_optional
from app.middleware.csrf import verify_csrf
from app.repositories.cart import add_to_cart, get_cart_details, remove_from_cart
from app.repositories.shop import get_products_by_lang
from app.routers.deps import LangDep, render_template

# --- Homepage --------------------------------------------------
router = APIRouter(tags=["shop"])


@router.get("/{source_lang}/{ui_lang}/courses/", response_class=HTMLResponse, name="courses_list")
@router.get("/{source_lang}/{ui_lang}/courses", response_class=HTMLResponse)
async def get_courses_list(request: Request, lang_pair: LangDep, pool: DBPoolDep = None):
    products = await get_products_by_lang(pool, lang_pair)
    return render_template(
        name="courses.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "courses_list",
            "products": products,
        }
    )


@router.get("/{source_lang}/{ui_lang}/cart/", response_class=HTMLResponse, name="cart")
@router.get("/{source_lang}/{ui_lang}/cart", response_class=HTMLResponse)
async def get_cart_view(request: Request, lang_pair: LangDep, pool: DBPoolDep, current_user: CurrentUser):

    cart_data = await get_cart_details(pool, current_user["user_id"]) or {"cart": {"amount": 0}, "items": []}

    return render_template(
        name="shop/cart.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "cart",
            "cart": cart_data["cart"],  # общая информация о корзине
            "items": cart_data.get("items", []),  # список товаров
            "total_amount": float(cart_data["cart"].get("amount", 0)),
        }
    )


@router.post("/{source_lang}/{ui_lang}/cart/add/", name="cart_add")
@router.post("/{source_lang}/{ui_lang}/cart/add", name="cart_add")
async def cart_add(
    request: Request,
    lang_pair: LangDep,
    product_slug: str = Form(...),
    quantity: int = Form(1, ge=1),
    pool: DBPoolDep = None,
    current_user: CurrentUser = None,
    _ = Depends(verify_csrf),
):
    """Добавление товара в корзину"""
    if not current_user:
        # Пока позволяем анонимно или возвращаем ошибку
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Пожалуйста, войдите в аккаунт"}
        )
    try:
        result = await add_to_cart(pool, current_user["user_id"], product_slug, quantity)

        # Успешно добавили → редирект на корзину
        redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/"
        return RedirectResponse(url=redirect_url, status_code=303)

    except ValueError as e:  # ← добавлено
        # Продукт не найден или неактивен
        print(f"Cart add error: {e}")
        redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/"
        return RedirectResponse(url=redirect_url, status_code=303)

    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"success": False, "error": e.detail})
    except Exception as e:
        print(f"Error in cart_add: {e}")
        redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/"
        return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/{source_lang}/{ui_lang}/cart/remove/", name="cart_remove")
@router.post("/{source_lang}/{ui_lang}/cart/remove", name="cart_remove")
async def cart_remove(
    request: Request,
    lang_pair: LangDep,
    item_id: int = Form(...),
    pool: DBPoolDep = None,
    _ = Depends(verify_csrf),
):
    """Удаление товара из корзины"""
    # Получаем пользователя вручную, чтобы избежать автоматического 401 от CurrentUser
    try:
        current_user = await get_current_active_user(request=request, pool=pool)
    except HTTPException:
        current_user = None

    if current_user is None:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/",
            status_code=303
        )

    await remove_from_cart(pool, current_user["user_id"], item_id)

    # Редирект обратно в корзину
    redirect_url = f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/{source_lang}/{ui_lang}/courses-details/", response_class=HTMLResponse,
            name="courses_details")       # ← уникальное имя
@router.get("/{source_lang}/{ui_lang}/courses-details", response_class=HTMLResponse)
async def get_courses_details(request: Request, lang_pair: LangDep):
    return render_template(
        name="shop/courses-details.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "courses_details",
        }
    )


@router.get("/{source_lang}/{ui_lang}/cart/checkout/", response_class=HTMLResponse, name="cart_checkout")
async def cart_checkout_view(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: dict = Depends(get_current_active_user_optional),
):
    # === Самое важное ===
    # Берём пользователя из AuthMiddleware (если он там есть)
    # Если нет — берём из зависимости
    user = getattr(request.state, "user", None) or current_user

    if not user:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/",
            status_code=303
        )

    user_id = user["user_id"]

    # Получаем корзину
    order = await pool.fetchrow("""
        SELECT id, created_at, amount
        FROM orders
        WHERE user_id = $1 AND status = 'cart'
        ORDER BY created_at DESC
        LIMIT 1
    """, user_id)

    if not order:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/",
            status_code=303
        )

    # Получаем товары корзины
    items = await pool.fetch("""
        SELECT 
            oi.id,
            p.name,
            p.slug,
            oi.quantity,
            oi.price,
            oi.discount,
            (oi.quantity * (oi.price - oi.discount)) as subtotal
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = $1
    """, order["id"])

    context = {
        "request": request,
        "order": order,
        "items": items,
        "total_amount": order["amount"],
        "source_lang": lang_pair.source_lang,
        "ui_lang": lang_pair.ui_lang,
    }

    return render_template(
        name="shop/checkout.html",
        request=request,
        lang_pair=lang_pair,
        context=context
    )


@router.post("/{source_lang}/{ui_lang}/cart/select_payment/", name="cart_select_payment_post")
async def cart_select_payment(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: dict = Depends(get_current_active_user_optional),
    _ = Depends(verify_csrf),
):
    if not current_user:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/",
            status_code=303
        )

    user_id = current_user["user_id"]
    cart = await pool.fetchrow("""
        SELECT id FROM orders 
        WHERE user_id = $1 AND status = 'cart'
        LIMIT 1
    """, user_id)

    if not cart:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/",
            status_code=303
        )

    await pool.execute("""
        UPDATE orders 
        SET status = 'pending' 
        WHERE id = $1
    """, cart["id"])

    return RedirectResponse(
        url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/select_payment/",
        status_code=303
    )


@router.get("/{source_lang}/{ui_lang}/cart/select_payment/", response_class=HTMLResponse, name="select_payment")
async def cart_payment_page(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: dict = Depends(get_current_active_user_optional),
):
    if not current_user:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/",
            status_code=303
        )

    user_id = current_user["user_id"]

    # Проверяем, есть ли у пользователя заказ в статусе pending
    order = await pool.fetchrow("""
        SELECT id, amount, created_at
        FROM orders
        WHERE user_id = $1 AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """, user_id)

    if not order:
        # Если pending заказа нет — возвращаем пользователя в корзину
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/cart/",
            status_code=303
        )

    context = {
        "request": request,
        "order": order,
    }

    return render_template(
        name="shop/payment_select.html",
        request=request,
        lang_pair=lang_pair,
        context=context
    )

@router.post("/{source_lang}/{ui_lang}/cart/cancel-pending/", name="cancel_pending")
async def cancel_pending_order(
    request: Request,
    lang_pair: LangDep,
    pool: DBPoolDep,
    current_user: dict = Depends(get_current_active_user_optional),
    _ = Depends(verify_csrf),
):
    if not current_user:
        return RedirectResponse(
            url=f"/{lang_pair.source_lang}/{lang_pair.ui_lang}/auth/login/",
            status_code=303
        )

    await pool.execute("""
        DELETE FROM orders 
        WHERE user_id = $1 AND status = 'pending'
    """, current_user["user_id"])

    return RedirectResponse(
        url=request.headers.get("referer", "/"),
        status_code=303
    )

"""=========================================================="""


@router.get("/{source_lang}/{ui_lang}/table/", response_class=HTMLResponse, name="table")       # ← уникальное имя
@router.get("/{source_lang}/{ui_lang}/table", response_class=HTMLResponse)
async def get_table(request: Request, lang_pair: LangDep):

    return render_template(
        name="table-compare.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "courses_details",
        }
    )

