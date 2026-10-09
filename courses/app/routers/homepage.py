from fastapi import APIRouter, Request, HTTPException, Path
from fastapi.responses import HTMLResponse, RedirectResponse

from app.routers.deps import LangDep, get_default_lang_pair, render_template


router = APIRouter(tags=["homepage"])

# --- Редирект с корня ------------------------------------------
@router.get("/", include_in_schema=False)
async def root_redirect():
    source_lang, ui_lang = get_default_lang_pair().lang_pair
    return RedirectResponse(url=f"/{source_lang}/{ui_lang}/", status_code=307)


# --- Homepage --------------------------------------------------
@router.get("/{source_lang}/{ui_lang}/", response_class=HTMLResponse, name="home")
@router.get("/{source_lang}/{ui_lang}", response_class=HTMLResponse)
async def home_page(request: Request, lang_pair: LangDep):
    return render_template(
        name="home.html",
        request=request,
        lang_pair=lang_pair,
        context={
            "current_page": "home",
        }
    )

# --- Fallback --------------------------------------------------
@router.get("/{first}/", response_class=HTMLResponse)
@router.get("/{first}", response_class=HTMLResponse)
async def home_fallback():
    source_lang, ui_lang = get_default_lang_pair().lang_pair
    return RedirectResponse(url=f"/{source_lang}/{ui_lang}/", status_code=307)

