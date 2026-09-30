from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.routing import NoMatchFound

from app.db import lifespan
from app.middleware import I18nMiddleware, AuthMiddleware, PendingOrderMiddleware, CSRFMiddleware
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.feedback import router as feedback_router
from app.routers.shop import router as shop_router
from app.routers.homepage import router as homepage_router
from app.routers.classroom import router as classroom_router
from app.routers.coach import router as coach_router
from app.core.config import (
    BASE_DIR,
    templates,
    DEFAULT_SOURCE_LANGUAGE,
    SUPPORTED_UI_LANGUAGES,
    DEFAULT_UI_LANGUAGE,
)

# === APP =============================================
app = FastAPI(title="Courses", lifespan=lifespan)


# === LOCALE =========================================
def get_locale(request: Request) -> str:
    path = request.url.path.lstrip("/")
    parts = path.split("/")

    # Приоритет 1: Язык из URL (второй сегмент)
    if len(parts) >= 2 and parts[1] in SUPPORTED_UI_LANGUAGES:
        return parts[1]

    # Приоритет 2: Cookie
    if request.cookies.get("lang") in SUPPORTED_UI_LANGUAGES:
        return request.cookies.get("lang")

    # Приоритет 3: Query параметр
    if request.query_params.get("lang") in SUPPORTED_UI_LANGUAGES:
        return request.query_params.get("lang")

    # Приоритет 4: Accept-Language
    accept_lang = request.headers.get("accept-language", "")
    if accept_lang:
        lang = accept_lang.split(",")[0].split("-")[0].lower()
        if lang in SUPPORTED_UI_LANGUAGES:
            return lang

    # Дефолт
    return DEFAULT_UI_LANGUAGE


# === MIDDLEWARE =====================================
app.add_middleware(CSRFMiddleware)
app.add_middleware(PendingOrderMiddleware)
app.add_middleware(I18nMiddleware)   # i18n лучше подключать раньше
app.add_middleware(AuthMiddleware)   # auth после i18n

# === static & media ==================================
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "shared/assets")), name="static")
app.mount("/media", StaticFiles(directory=str(BASE_DIR / "media")), name="media")

# ==== ROUTERS =======================================
app.include_router(admin_router)
app.include_router(feedback_router)
app.include_router(homepage_router)
app.include_router(shop_router)
app.include_router(auth_router)
app.include_router(classroom_router)
app.include_router(coach_router)


# === Safe url_for для общих шаблонов ==================
def setup_url_for_sheared_assets(app: FastAPI):

    def url_for_sheared_assets(name: str, **path_params):
        try:
            # Пытаемся использовать стандартный url_for текущего приложения
            return app.url_path_for(name, **path_params)
        except NoMatchFound:
            # === Внешние маршруты (между сервисами) ===
            source_lang = path_params.get("source_lang") or DEFAULT_SOURCE_LANGUAGE
            ui_lang = path_params.get("ui_lang") or DEFAULT_UI_LANGUAGE

            try:
                from url_chunks import URL_CHUNKS
            except ImportError as e:
                print(f"[WARNING] Не удалось импортировать URL_CHUNKS из shared: {e}")

            for url_chunk in URL_CHUNKS:
                if url_chunk == name.lower():
                    return f"/{source_lang}/{ui_lang}/{url_chunk}/"

            if name in ["home", "homepage", "index", "main"]:
                return f"/{source_lang}/{ui_lang}/"

            return "It looks like the link is from another part of the project!!!"

    templates.env.globals["url_for_sheared_assets"] = url_for_sheared_assets

setup_url_for_sheared_assets(app)


# ==== HEALTH ========================================
@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "service": "courses"}