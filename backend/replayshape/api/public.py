"""Endpoints públicos (comprador). Sem cadastro obrigatório.

Nunca existe listagem pública de vídeos: cada pedido só é acessível com o
token secreto devolvido na criação (ou com o código individual do pedido).
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..db import new_id, now
from ..security import normalize_code, random_token, token_hash
from ..services import orders as order_svc
from ..services.audit import audit
from ..services.replays import TriggerError, camera_effective, trigger_replay
from .deps import ApiError, client_ip, ctx_of, order_for_token, rate_limit

router = APIRouter(prefix="/api/v1/public", tags=["público"])

ERROR_MESSAGES = {
    "INSUFFICIENT_BUFFER": "A câmera ainda não gravou tempo suficiente para este replay.",
    "BUFFER_GAP": "Houve uma falha de gravação neste intervalo.",
    "BUFFER_TIMEOUT": "A câmera não entregou o trecho final a tempo.",
    "CAMERA_OFFLINE": "A câmera está sem sinal.",
    "SEGMENT_MISSING": "Parte da gravação não está mais disponível.",
    "PROCESSING_FAILED": "Falha ao processar o vídeo.",
    "BAD_DURATION": "O vídeo gerado não passou na validação.",
    "OUTPUT_MISSING": "O vídeo gerado não passou na validação.",
    "SYSTEM_RESTART": "O sistema reiniciou durante o processamento.",
    "FFMPEG_TIMEOUT": "O processamento demorou mais que o esperado.",
}


def qr_svg_data_uri(text: str) -> str:
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def _spot(ctx, code: str) -> tuple[dict, dict]:
    cam = ctx.db.one("SELECT * FROM cameras WHERE public_code = ?", (code,)) if 3 <= len(code) <= 64 else None
    if not cam or not cam["active"]:
        raise ApiError(404, "SPOT_NOT_FOUND", "QR Code inválido ou câmera desativada.")
    est = ctx.db.one("SELECT * FROM establishments WHERE id = ?", (cam["establishment_id"],))
    if not est or not est["active"]:
        raise ApiError(404, "SPOT_NOT_FOUND", "Estabelecimento indisponível.")
    return cam, est


def spot_payload(ctx, cam: dict, est: dict) -> dict:
    eff = camera_effective(ctx, cam, est)
    buf = ctx.buffers.get(cam["id"])
    snap = buf.snapshot() if buf else {"state": "offline", "seconds_buffered": 0}
    ready_in = 0
    if snap["state"] == "online":
        ready_in = max(0, int(eff["replay_seconds"] + 1 - snap["seconds_buffered"]))
    return {
        "code": cam["public_code"],
        "establishment": est["name"],
        "venue_type": est["venue_type"],
        "area_label": cam["area_label"],
        "camera_name": cam["name"],
        "duration_s": eff["replay_seconds"],
        "price_cents": eff["price_cents"],
        "currency": eff["currency"],
        "camera_status": "online" if snap["state"] == "online" else ("starting" if snap["state"] in
                                                                       ("starting", "reconnecting") else "offline"),
        "seconds_buffered": snap["seconds_buffered"],
        "ready_in_s": ready_in,
        "privacy_notice": est["privacy_notice"],
        "simulated": cam["adapter"] == "demo",
    }


@router.get("/config")
def public_config(request: Request):
    ctx = ctx_of(request)
    return {"demo_mode": ctx.settings.demo_mode, "simulated_payments": ctx.provider.is_simulated,
            "env": ctx.settings.app_env}


@router.get("/demo/spots")
def demo_spots(request: Request):
    ctx = ctx_of(request)
    if not ctx.settings.demo_mode:
        raise ApiError(404, "NOT_FOUND", "Disponível apenas no modo demonstração.")
    rows = ctx.db.all("SELECT c.*, e.id est_id FROM cameras c JOIN establishments e ON e.id = c.establishment_id "
                      "WHERE c.active = 1 AND e.active = 1 ORDER BY c.area_label")
    out = []
    for c in rows:
        cam, est = _spot(ctx, c["public_code"])
        out.append(spot_payload(ctx, cam, est))
    return {"spots": out}


@router.get("/spots/{code}")
def get_spot(code: str, request: Request):
    ctx = ctx_of(request)
    rate_limit(request, "spot", 120, 60)
    cam, est = _spot(ctx, code)
    return spot_payload(ctx, cam, est)


@router.post("/spots/{code}/replays", status_code=201)
def create_replay(code: str, request: Request):
    ctx = ctx_of(request)
    rate_limit(request, "trigger", 6, 60)
    cam, est = _spot(ctx, code)
    cooldown = int(ctx.store.get("web_trigger_cooldown_seconds"))
    rate_limit(request, f"trigger-cam:{cam['id']}", 1, cooldown) if cooldown else None
    try:
        order, token = trigger_replay(ctx, cam["id"], "web", client_ip=client_ip(request))
    except TriggerError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    return {"order_id": order["id"], "access_token": token, "short_code": order["short_code"]}


class ClaimBody(BaseModel):
    code: str | None = Field(default=None, max_length=20)


@router.post("/spots/{code}/claim")
def claim_device_trigger(code: str, body: ClaimBody, request: Request):
    """Recupera o replay gerado pelo BOTÃO FÍSICO desta mesa/quadra.

    Sem código: entrega o acionamento mais recente ainda não reivindicado
    (janela curta, padrão 3 min — o primeiro a reivindicar fica com ele).
    Com código: entrega exatamente o pedido daquele código.
    """
    ctx = ctx_of(request)
    rate_limit(request, "claim", 10, 60)
    cam, _ = _spot(ctx, code)
    window = int(ctx.store.get("device_claim_window_seconds"))
    with ctx.db.tx():
        if body.code:
            o = ctx.db.one("SELECT o.* FROM orders o JOIN replay_requests r ON r.id = o.replay_request_id "
                           "WHERE o.short_code = ? AND o.camera_id = ?", (normalize_code(body.code), cam["id"]))
        else:
            o = ctx.db.one(
                "SELECT o.* FROM orders o JOIN replay_requests r ON r.id = o.replay_request_id "
                "WHERE o.camera_id = ? AND r.source = 'device' AND r.claimed = 0 AND r.trigger_at > ? "
                "ORDER BY r.trigger_at DESC LIMIT 1", (cam["id"], now() - window))
        if not o:
            raise ApiError(404, "NO_TRIGGER", "Nenhum acionamento do botão encontrado nos últimos minutos.")
        token = random_token()
        ctx.db.update("orders", o["id"], {"access_token_hash": token_hash(token), "updated_at": now()})
        ctx.db.execute("UPDATE replay_requests SET claimed = 1 WHERE id = ?", (o["replay_request_id"],))
    audit(ctx.db, "public", "order.claimed", "order", o["id"])
    return {"order_id": o["id"], "access_token": token, "short_code": o["short_code"]}


class RecoverBody(BaseModel):
    code: str = Field(min_length=6, max_length=20)


@router.post("/orders/recover")
def recover_order(body: RecoverBody, request: Request):
    """Recupera um pedido pelo código individual (ex.: impresso/exibido no terminal)."""
    ctx = ctx_of(request)
    rate_limit(request, "recover", 5, 300)
    o = ctx.db.one("SELECT * FROM orders WHERE short_code = ?", (normalize_code(body.code),))
    if not o or o["status"] in ("failed", "cancelled"):
        raise ApiError(404, "ORDER_NOT_FOUND", "Código não encontrado.")
    token = random_token()
    ctx.db.update("orders", o["id"], {"access_token_hash": token_hash(token), "updated_at": now()})
    ctx.db.execute("UPDATE replay_requests SET claimed = 1 WHERE id = ?", (o["replay_request_id"],))
    audit(ctx.db, "public", "order.recovered", "order", o["id"])
    return {"order_id": o["id"], "access_token": token, "short_code": o["short_code"]}


def order_payload(ctx, order: dict) -> dict:
    req = ctx.db.one("SELECT * FROM replay_requests WHERE id = ?", (order["replay_request_id"],))
    cam = ctx.db.one("SELECT * FROM cameras WHERE id = ?", (order["camera_id"],))
    est = ctx.db.one("SELECT * FROM establishments WHERE id = ?", (order["establishment_id"],))
    pay = ctx.db.one("SELECT * FROM payments WHERE order_id = ? ORDER BY created_at DESC LIMIT 1", (order["id"],))
    payment = None
    if pay:
        payment = {"status": pay["status"], "expires_at": pay["expires_at"], "ticket_url": pay["ticket_url"]}
        if pay["status"] == "pending" and order["status"] == "awaiting_payment":
            payment["pix_copy_paste"] = pay["pix_copy_paste"]
            payment["qr_data_uri"] = qr_svg_data_uri(pay["pix_copy_paste"]) if pay["pix_copy_paste"] else None
    return {
        "id": order["id"],
        "short_code": order["short_code"],
        "status": order["status"],
        "amount_cents": order["amount_cents"],
        "currency": order["currency"],
        "created_at": order["created_at"],
        "payment_deadline": order["payment_deadline"],
        "available_until": order["available_until"],
        "demo": bool(order["demo"]),
        "server_time": now(),
        "replay": {
            "status": req["status"],
            "trigger_at": req["trigger_at"],
            "duration_s": req["duration_s"],
            "processing_ms": req["processing_ms"],
            "error_code": req["error_code"],
            "error_message": ERROR_MESSAGES.get(req["error_code"] or "", "Não foi possível gerar o replay.")
            if req["status"] == "failed" else None,
        },
        "spot": {"establishment": est["name"], "area_label": cam["area_label"], "code": cam["public_code"]},
        "payment": payment,
        "simulated_payments": ctx.provider.is_simulated,
    }


@router.get("/orders/{order_id}")
def get_order(order_id: str, request: Request):
    ctx = ctx_of(request)
    order = order_for_token(request, order_id)
    if order["status"] == "awaiting_payment":
        order_svc.maybe_reconcile_on_poll(ctx, order_id)
        order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    return order_payload(ctx, order)


@router.post("/orders/{order_id}/payment")
def create_payment(order_id: str, request: Request):
    ctx = ctx_of(request)
    order_for_token(request, order_id)
    rate_limit(request, "payment", 10, 60)
    try:
        order_svc.create_or_get_payment(ctx, order_id)
    except order_svc.OrderError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    return order_payload(ctx, ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,)))


class DemoPayBody(BaseModel):
    result: str = Field(pattern="^(approved|rejected)$")


@router.post("/orders/{order_id}/demo-payment")
def demo_payment(order_id: str, body: DemoPayBody, request: Request):
    """SOMENTE DEMONSTRAÇÃO: simula a aprovação/recusa no provedor fictício.

    A liberação passa pelo mesmo caminho do provedor real: notificação
    assinada -> consulta independente -> validação -> transição de estado.
    """
    ctx = ctx_of(request)
    if not ctx.provider.is_simulated or ctx.settings.is_production:
        raise ApiError(404, "NOT_FOUND", "Indisponível.")
    order_for_token(request, order_id)
    pay = ctx.db.one("SELECT * FROM payments WHERE order_id = ? AND status = 'pending' ORDER BY created_at DESC "
                     "LIMIT 1", (order_id,))
    if not pay:
        raise ApiError(409, "NO_PENDING_PAYMENT", "Gere o Pix antes de simular o pagamento.")
    headers, raw = ctx.provider.simulate(pay["provider_payment_id"], body.result)
    order_svc.handle_notification(ctx, ctx.provider.name, headers, {}, raw)
    return order_payload(ctx, ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,)))


@router.post("/orders/{order_id}/media-link")
def media_link(order_id: str, request: Request):
    ctx = ctx_of(request)
    order = order_for_token(request, order_id)
    try:
        link = order_svc.create_media_link(ctx, order_id)
    except order_svc.OrderError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    base = f"/api/v1/public/media/{link['token']}/replay-{order['short_code']}.mp4"
    return {"url": base, "download_url": base + "?download=1", "expires_at": link["expires_at"]}


@router.get("/media/{token}/{filename}")
def media(token: str, filename: str, request: Request, download: int = 0):
    ctx = ctx_of(request)
    rate_limit(request, "media", 600, 60)
    try:
        order, rf = order_svc.resolve_media_token(ctx, token)
    except order_svc.OrderError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    name = f"replay-shape-{order['short_code']}.mp4"
    if rf and rf["local_path"] and Path(rf["local_path"]).exists():
        return FileResponse(
            rf["local_path"], media_type="video/mp4",
            filename=name if download else None,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"},
        )
    if rf and rf["remote_status"] == "uploaded" and ctx.remote:
        minutes = int(ctx.store.get("link_minutes"))
        return RedirectResponse(ctx.remote.presigned_get(rf["remote_key"], minutes * 60,
                                                         filename=name if download else None), status_code=302)
    raise ApiError(410, "FILE_UNAVAILABLE", "Arquivo indisponível.")


class PrivacyBody(BaseModel):
    order_code: str | None = Field(default=None, max_length=20)
    contact: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=5, max_length=2000)


@router.post("/privacy-requests", status_code=201)
def privacy_request(body: PrivacyBody, request: Request):
    """Canal LGPD: solicitação de exclusão/informação sobre imagens."""
    ctx = ctx_of(request)
    rate_limit(request, "privacy", 3, 600)
    pid = new_id()
    ctx.db.insert("privacy_requests", dict(id=pid, order_code=normalize_code(body.order_code or "") or None,
                                           contact=body.contact, message=body.message, created_at=now()))
    audit(ctx.db, "public", "privacy.request", "privacy_request", pid)
    return {"id": pid, "message": "Solicitação registrada. O estabelecimento responderá pelo contato informado."}
