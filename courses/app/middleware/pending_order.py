from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.db.dependencies import get_current_pending_order


class PendingOrderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        pending_order = None

        # Берём пользователя, которого уже положил AuthMiddleware
        user = getattr(request.state, "user", None)

        if user:
            try:
                pending_order = await get_current_pending_order(
                    pool=request.app.state.db_pool,
                    current_user=user
                )
            except Exception as e:
                print(f"[PendingOrderMiddleware] Ошибка при получении pending-заказа: {e}")

        # Сохраняем в state, чтобы было доступно в шаблонах
        request.state.pending_order = pending_order

        response = await call_next(request)
        return response