from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..db import now
from ..security import consteq, token_hash


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status_code=status, detail={"code": code, "message": message})


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def ctx_of(request: Request):
    return request.app.state.ctx


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window: float, key: str | None = None) -> None:
    ctx = ctx_of(request)
    k = f"{bucket}:{key or client_ip(request)}"
    if not ctx.rate.allow(k, limit, window):
        raise ApiError(429, "RATE_LIMITED", "Muitas tentativas. Aguarde um pouco e tente novamente.")


def order_for_token(request: Request, order_id: str) -> dict:
    """Autorização por pedido: exige o token secreto entregue a quem criou o pedido.

    Protege contra IDOR — conhecer/alterar o ID não dá acesso ao pedido de terceiros.
    """
    ctx = ctx_of(request)
    token = request.headers.get("x-order-token", "")
    rate_limit(request, "order-access", 120, 60)
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,)) if len(order_id) == 32 else None
    if not order or not token or not consteq(order["access_token_hash"], token_hash(token)):
        raise ApiError(404, "ORDER_NOT_FOUND", "Pedido não encontrado.")
    return order


# --- admin -----------------------------------------------------------------
SESSION_COOKIE = "rs_admin"


def admin_session(request: Request) -> dict:
    ctx = ctx_of(request)
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "UNAUTHENTICATED", "Faça login.")
    s = ctx.db.one(
        "SELECT s.id sid, s.csrf_token, s.expires_at, u.* FROM admin_sessions s JOIN admin_users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?", (token_hash(token),))
    if not s or s["expires_at"] < now() or not s["active"]:
        raise ApiError(401, "UNAUTHENTICATED", "Sessão expirada. Faça login novamente.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not consteq(csrf, s["csrf_token"]):
            raise ApiError(403, "CSRF", "Token CSRF inválido.")
    return s


def require_superadmin(user: dict) -> None:
    if user["role"] != "superadmin":
        raise ApiError(403, "FORBIDDEN", "Permissão insuficiente.")


def check_establishment_access(user: dict, establishment_id: str | None) -> None:
    """Segregação entre estabelecimentos (RBAC)."""
    if user["role"] == "superadmin":
        return
    if not establishment_id or user["establishment_id"] != establishment_id:
        raise ApiError(404, "NOT_FOUND", "Registro não encontrado.")
