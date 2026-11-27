"""Endpoints internos: webhooks de pagamento e botões físicos (ESP32)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from ..db import now
from ..integrations.payments import WebhookRejected
from ..security import consteq, device_signature, ip_hash
from ..services import orders as order_svc
from ..services.audit import audit
from ..services.replays import TriggerError, trigger_replay
from .deps import ApiError, client_ip, ctx_of, rate_limit

webhooks = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
internal = APIRouter(prefix="/api/v1/internal", tags=["interno (dispositivos)"])


@webhooks.post("/{provider}")
async def payment_webhook(provider: str, request: Request):
    ctx = ctx_of(request)
    rate_limit(request, "webhook", 300, 60)
    body = await request.body()
    if len(body) > 64 * 1024:
        raise ApiError(413, "TOO_LARGE", "corpo muito grande")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        res = order_svc.handle_notification(ctx, provider, headers, dict(request.query_params), body)
    except WebhookRejected as e:
        audit(ctx.db, "provider", "webhook.rejected", details=str(e))
        raise ApiError(401, "INVALID_SIGNATURE", "assinatura inválida") from e
    return res


async def _authenticate_device(request: Request, device_id: str) -> tuple[dict, bytes]:
    ctx = ctx_of(request)
    rate_limit(request, f"device:{device_id}", 30, 60)
    body = await request.body()
    ts = request.headers.get("x-rs-timestamp", "")
    nonce = request.headers.get("x-rs-nonce", "")
    sig = request.headers.get("x-rs-signature", "")
    dev = ctx.db.one("SELECT * FROM devices WHERE id = ?", (device_id,)) if len(device_id) == 32 else None
    if not dev or not dev["active"] or not ts.isdigit() or not (8 <= len(nonce) <= 64):
        raise ApiError(401, "DEVICE_UNAUTHORIZED", "dispositivo não autorizado")
    if abs(time.time() - int(ts)) > 120:
        raise ApiError(401, "CLOCK_SKEW", "relógio do dispositivo fora de sincronia")
    secret = ctx.box.decrypt(dev["secret_enc"]) or ""
    expected = device_signature(secret, ts, nonce, request.method, request.url.path, body)
    if not consteq(expected, sig):
        audit(ctx.db, "device", "device.bad_signature", "device", device_id)
        raise ApiError(401, "DEVICE_UNAUTHORIZED", "dispositivo não autorizado")
    if not ctx.nonces.check_and_add(f"{device_id}:{nonce}"):
        raise ApiError(409, "REPLAYED", "requisição repetida")
    return dev, body


@internal.post("/devices/{device_id}/trigger")
async def device_trigger(device_id: str, request: Request):
    ctx = ctx_of(request)
    dev, _ = await _authenticate_device(request, device_id)
    t = now()
    if dev["last_trigger_at"] and t - dev["last_trigger_at"] < dev["cooldown_seconds"]:
        # Proteção contra acionamentos duplicados (botão pressionado várias vezes).
        last = ctx.db.one("SELECT o.short_code FROM orders o JOIN replay_requests r ON r.id = o.replay_request_id "
                          "WHERE r.device_id = ? ORDER BY r.trigger_at DESC LIMIT 1", (device_id,))
        return {"status": "debounced", "short_code": last["short_code"] if last else None}
    ctx.db.update("devices", device_id, {"last_trigger_at": t, "last_seen_at": t,
                                         "last_ip_hash": ip_hash(client_ip(request), ctx.settings.ip_hash_salt)})
    try:
        order, _token = trigger_replay(ctx, dev["camera_id"], "device", device_id=device_id,
                                       client_ip=client_ip(request), claimed=False)
    except TriggerError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    return {"status": "accepted", "short_code": order["short_code"]}


@internal.post("/devices/{device_id}/heartbeat")
async def device_heartbeat(device_id: str, request: Request):
    ctx = ctx_of(request)
    dev, body = await _authenticate_device(request, device_id)
    fw = request.headers.get("x-rs-firmware", "")[:40] or None
    ctx.db.update("devices", device_id, {"last_seen_at": now(), "firmware_version": fw,
                                         "last_ip_hash": ip_hash(client_ip(request), ctx.settings.ip_hash_salt)})
    cam = ctx.db.one("SELECT status FROM cameras WHERE id = ?", (dev["camera_id"],))
    return {"ok": True, "camera_status": cam["status"] if cam else "unknown"}
