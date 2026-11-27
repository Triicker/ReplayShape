"""API administrativa (autenticação por sessão + CSRF + RBAC)."""
from __future__ import annotations

import io
import re
import secrets
import time
from datetime import datetime

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..db import new_id, now
from ..edge.manager import camera_config_from_row
from ..integrations.cameras.base import ADAPTERS, CameraConfigError, get_adapter
from ..security import hash_password, random_token, token_hash, verify_password
from ..services import metrics
from ..services import orders as order_svc
from ..services.audit import audit
from ..services.replays import TriggerError, trigger_replay
from ..video.ffmpeg import QUALITY_PROFILES
from .deps import (SESSION_COOKIE, ApiError, admin_session, check_establishment_access, client_ip, ctx_of,
                   rate_limit, require_superadmin)

router = APIRouter(prefix="/api/v1/admin", tags=["administrativo"])
SESSION_HOURS = 12
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


# --- autenticação ---------------------------------------------------------
class LoginBody(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    ctx = ctx_of(request)
    email = body.email.strip().lower()
    rate_limit(request, "login", 5, 300, key=f"{client_ip(request)}:{email}")
    rate_limit(request, "login-ip", 20, 300)
    u = ctx.db.one("SELECT * FROM admin_users WHERE email = ?", (email,))
    ok = verify_password(body.password, u["password_hash"]) if u else verify_password(body.password, _DUMMY)
    if not u or not ok or not u["active"]:
        audit(ctx.db, "admin", "auth.failed", details_email=email)
        time.sleep(0.3)
        raise ApiError(401, "INVALID_CREDENTIALS", "E-mail ou senha inválidos.")
    token, csrf = random_token(), random_token(24)
    ctx.db.insert("admin_sessions", dict(id=new_id(), user_id=u["id"], token_hash=token_hash(token),
                                         csrf_token=csrf, expires_at=now() + SESSION_HOURS * 3600,
                                         created_at=now()))
    ctx.db.update("admin_users", u["id"], {"last_login_at": now()})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=ctx.settings.cookie_secure,
                        max_age=SESSION_HOURS * 3600, path="/")
    audit(ctx.db, "admin", "auth.login", "admin_user", u["id"], actor_id=u["id"])
    return {"email": u["email"], "role": u["role"], "csrf_token": csrf}


_DUMMY = hash_password(secrets.token_hex(8))  # tempo constante para e-mails inexistentes


@router.post("/auth/logout")
def logout(request: Request, response: Response, user=Depends(admin_session)):
    ctx_of(request).db.execute("DELETE FROM admin_sessions WHERE id = ?", (user["sid"],))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    return {"email": user["email"], "role": user["role"], "establishment_id": user["establishment_id"],
            "csrf_token": user["csrf_token"], "demo_mode": ctx.settings.demo_mode,
            "simulated_payments": ctx.provider.is_simulated, "payment_provider": ctx.provider.name,
            "storage_backend": ctx.settings.storage_backend, "font_available": bool(ctx.settings.font_file)}


class PasswordBody(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=10, max_length=200)


@router.post("/me/password")
def change_password(body: PasswordBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    if not verify_password(body.current, user["password_hash"]):
        raise ApiError(400, "INVALID_PASSWORD", "Senha atual incorreta.")
    ctx.db.update("admin_users", user["id"], {"password_hash": hash_password(body.new)})
    ctx.db.execute("DELETE FROM admin_sessions WHERE user_id = ? AND id != ?", (user["id"], user["sid"]))
    (ctx.settings.data_dir / "ADMIN_INITIAL_PASSWORD.txt").unlink(missing_ok=True)
    audit(ctx.db, "admin", "auth.password_changed", "admin_user", user["id"], actor_id=user["id"])
    return {"ok": True}


# --- estabelecimentos -----------------------------------------------------
class EstablishmentBody(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    slug: str | None = Field(default=None, max_length=63)
    active: bool | None = None
    price_cents: int | None = Field(default=None, ge=0, le=100_000)
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    venue_type: str | None = Field(default=None, max_length=40)
    privacy_notice: str | None = Field(default=None, max_length=2000)


@router.get("/establishments")
def list_establishments(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    if user["role"] == "superadmin":
        return ctx.db.all("SELECT * FROM establishments ORDER BY name")
    return ctx.db.all("SELECT * FROM establishments WHERE id = ?", (user["establishment_id"],))


@router.post("/establishments", status_code=201)
def create_establishment(body: EstablishmentBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    require_superadmin(user)
    if not body.name:
        raise ApiError(422, "VALIDATION", "Informe o nome.")
    slug = (body.slug or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-"))[:63]
    if not _SLUG.match(slug):
        raise ApiError(422, "VALIDATION", "Identificador inválido.")
    if ctx.db.one("SELECT 1 FROM establishments WHERE slug = ?", (slug,)):
        raise ApiError(409, "DUPLICATE", "Já existe um estabelecimento com este identificador.")
    eid, t = new_id(), now()
    ctx.db.insert("establishments", dict(
        id=eid, name=body.name, slug=slug, active=1 if body.active is not False else 0,
        price_cents=body.price_cents if body.price_cents is not None else int(ctx.store.get("default_price_cents")),
        currency="BRL", brand_color=body.brand_color or "#a3e635", venue_type=body.venue_type or "sinuca",
        privacy_notice=body.privacy_notice, created_at=t, updated_at=t))
    audit(ctx.db, "admin", "establishment.created", "establishment", eid, actor_id=user["id"])
    return ctx.db.one("SELECT * FROM establishments WHERE id = ?", (eid,))


@router.patch("/establishments/{eid}")
def update_establishment(eid: str, body: EstablishmentBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    check_establishment_access(user, eid)
    if not ctx.db.one("SELECT 1 FROM establishments WHERE id = ?", (eid,)):
        raise ApiError(404, "NOT_FOUND", "Estabelecimento não encontrado.")
    vals = body.model_dump(exclude_none=True)
    if "slug" in vals:
        require_superadmin(user)
        if not _SLUG.match(vals["slug"]):
            raise ApiError(422, "VALIDATION", "Identificador inválido.")
    if "active" in vals:
        vals["active"] = 1 if vals["active"] else 0
    if vals:
        vals["updated_at"] = now()
        ctx.db.update("establishments", eid, vals)
        ctx.buffers.sync()
    audit(ctx.db, "admin", "establishment.updated", "establishment", eid, actor_id=user["id"], fields=list(vals))
    return ctx.db.one("SELECT * FROM establishments WHERE id = ?", (eid,))


@router.delete("/establishments/{eid}")
def delete_establishment(eid: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    require_superadmin(user)
    if ctx.db.one("SELECT 1 FROM orders WHERE establishment_id = ? LIMIT 1", (eid,)):
        ctx.db.update("establishments", eid, {"active": 0, "updated_at": now()})
        ctx.buffers.sync()
        audit(ctx.db, "admin", "establishment.deactivated", "establishment", eid, actor_id=user["id"])
        return {"ok": True, "deactivated": True,
                "message": "Há pedidos vinculados; o estabelecimento foi desativado em vez de removido."}
    ctx.db.execute("DELETE FROM establishments WHERE id = ?", (eid,))
    ctx.buffers.sync()
    audit(ctx.db, "admin", "establishment.deleted", "establishment", eid, actor_id=user["id"])
    return {"ok": True, "deactivated": False}


# --- câmeras --------------------------------------------------------------
class CameraBody(BaseModel):
    establishment_id: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    area_label: str | None = Field(default=None, min_length=1, max_length=60)
    adapter: str | None = Field(default=None, max_length=20)
    manufacturer: str | None = Field(default=None, max_length=60)
    model: str | None = Field(default=None, max_length=60)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, max_length=200)
    rtsp_path: str | None = Field(default=None, max_length=300)
    rtsp_url: str | None = Field(default=None, max_length=500)
    channel: int | None = Field(default=None, ge=1, le=256)
    subtype: int | None = Field(default=None, ge=0, le=3)
    resolution: str | None = Field(default=None, pattern=r"^\d{3,4}x\d{3,4}$")
    fps: int | None = Field(default=None, ge=1, le=60)
    replay_seconds: int | None = Field(default=None, ge=5, le=300)
    price_cents: int | None = Field(default=None, ge=0, le=100_000)
    clear_price: bool | None = None
    quality_profile: str | None = Field(default=None, pattern="^(economy|balanced|high)$")
    watermark: bool | None = None
    supports_playback: bool | None = None
    active: bool | None = None


def _camera_public(ctx, row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in ("password_enc", "rtsp_url_enc")}
    out["has_password"] = bool(row.get("password_enc"))
    out["has_rtsp_url"] = bool(row.get("rtsp_url_enc"))
    try:
        cfg = camera_config_from_row(row, ctx.box)
        out["source_url"] = get_adapter(cfg.adapter, True).redacted_url(cfg)
    except Exception:
        out["source_url"] = None
    buf = ctx.buffers.get(row["id"])
    out["buffer"] = buf.snapshot() if buf else None
    out["public_url"] = f"{ctx.settings.public_base_url}/m/{row['public_code']}"
    return out


def _validate_camera(ctx, row: dict) -> None:
    if row["adapter"] not in ADAPTERS:
        raise ApiError(422, "VALIDATION", "Adaptador inválido.")
    if row["adapter"] == "demo":
        return
    try:
        cfg = camera_config_from_row(row, ctx.box)
        get_adapter(cfg.adapter, ctx.settings.allow_public_camera_hosts).live_url(cfg)
    except CameraConfigError as e:
        raise ApiError(422, "CAMERA_CONFIG", str(e)) from e


@router.get("/cameras")
def list_cameras(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    if user["role"] == "superadmin":
        rows = ctx.db.all("SELECT * FROM cameras ORDER BY area_label")
    else:
        rows = ctx.db.all("SELECT * FROM cameras WHERE establishment_id = ? ORDER BY area_label",
                          (user["establishment_id"],))
    return {"cameras": [_camera_public(ctx, r) for r in rows], "adapters": {k: v.label for k, v in ADAPTERS.items()},
            "quality_profiles": {k: v["label"] for k, v in QUALITY_PROFILES.items()}}


def _camera_values(ctx, body: CameraBody) -> dict:
    vals = body.model_dump(exclude_none=True)
    for k in ("password", "rtsp_url"):
        if k in vals:
            v = vals.pop(k)
            vals[f"{k}_enc"] = ctx.box.encrypt(v) if v else None
    if vals.pop("clear_price", None):
        vals["price_cents"] = None
    for k in ("watermark", "supports_playback", "active"):
        if k in vals:
            vals[k] = 1 if vals[k] else 0
    return vals


@router.post("/cameras", status_code=201)
def create_camera(body: CameraBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    if not (body.establishment_id and body.name and body.area_label):
        raise ApiError(422, "VALIDATION", "Informe estabelecimento, nome e identificação da quadra/mesa.")
    check_establishment_access(user, body.establishment_id)
    if not ctx.db.one("SELECT 1 FROM establishments WHERE id = ?", (body.establishment_id,)):
        raise ApiError(404, "NOT_FOUND", "Estabelecimento não encontrado.")
    vals = _camera_values(ctx, body)
    t = now()
    row = dict(id=new_id(), adapter="rtsp", public_code=secrets.token_urlsafe(9).replace("_", "x").replace("-", "y"),
               channel=1, subtype=0, protocol="rtsp", replay_seconds=int(ctx.store.get("replay_seconds")), active=1,
               status="unknown", created_at=t, updated_at=t)
    row.update(vals)
    _validate_camera(ctx, row)
    ctx.db.insert("cameras", row)
    ctx.buffers.sync()
    audit(ctx.db, "admin", "camera.created", "camera", row["id"], actor_id=user["id"], adapter=row["adapter"])
    return _camera_public(ctx, ctx.db.one("SELECT * FROM cameras WHERE id = ?", (row["id"],)))


def _get_camera(ctx, user, cid: str) -> dict:
    cam = ctx.db.one("SELECT * FROM cameras WHERE id = ?", (cid,))
    if not cam:
        raise ApiError(404, "NOT_FOUND", "Câmera não encontrada.")
    check_establishment_access(user, cam["establishment_id"])
    return cam


@router.patch("/cameras/{cid}")
def update_camera(cid: str, body: CameraBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    cam = _get_camera(ctx, user, cid)
    vals = _camera_values(ctx, body)
    if "establishment_id" in vals:
        check_establishment_access(user, vals["establishment_id"])
    merged = {**cam, **vals}
    _validate_camera(ctx, merged)
    if vals:
        vals["updated_at"] = now()
        ctx.db.update("cameras", cid, vals)
        ctx.buffers.sync()
    audit(ctx.db, "admin", "camera.updated", "camera", cid, actor_id=user["id"],
          fields=[k for k in vals if k != "updated_at"])
    return _camera_public(ctx, ctx.db.one("SELECT * FROM cameras WHERE id = ?", (cid,)))


@router.delete("/cameras/{cid}")
def delete_camera(cid: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    _get_camera(ctx, user, cid)
    if ctx.db.one("SELECT 1 FROM replay_requests WHERE camera_id = ? LIMIT 1", (cid,)):
        ctx.db.update("cameras", cid, {"active": 0, "updated_at": now()})
        msg = "Há replays vinculados; a câmera foi desativada em vez de removida."
    else:
        ctx.db.execute("DELETE FROM cameras WHERE id = ?", (cid,))
        msg = "Câmera removida."
    ctx.buffers.sync()
    audit(ctx.db, "admin", "camera.deleted", "camera", cid, actor_id=user["id"])
    return {"ok": True, "message": msg}


@router.post("/cameras/{cid}/test")
def test_camera(cid: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    cam = _get_camera(ctx, user, cid)
    rate_limit(request, "cam-test", 10, 60)
    try:
        cfg = camera_config_from_row(cam, ctx.box)
        res = get_adapter(cfg.adapter, ctx.settings.allow_public_camera_hosts).test_connectivity(
            cfg, ctx.settings.ffprobe_bin)
    except CameraConfigError as e:
        res = {"ok": False, "detail": str(e)}
    except Exception as e:
        res = {"ok": False, "detail": type(e).__name__}
    audit(ctx.db, "admin", "camera.tested", "camera", cid, actor_id=user["id"], ok=res.get("ok"))
    return res


@router.get("/cameras/{cid}/qr.svg")
def camera_qr(cid: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    cam = _get_camera(ctx, user, cid)
    url = f"{ctx.settings.public_base_url}/m/{cam['public_code']}"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Content-Disposition": f'inline; filename="qr-{cam["public_code"]}.svg"'})


@router.post("/cameras/{cid}/trigger")
def admin_trigger(cid: str, request: Request, user=Depends(admin_session)):
    """Gera um replay de teste (não é cobrado se ninguém pagar)."""
    ctx = ctx_of(request)
    _get_camera(ctx, user, cid)
    try:
        order, token = trigger_replay(ctx, cid, "admin", client_ip=client_ip(request))
    except TriggerError as e:
        raise ApiError(e.http_status, e.code, e.message) from e
    return {"order_id": order["id"], "short_code": order["short_code"], "access_token": token}


# --- dispositivos (botões físicos) -----------------------------------------
class DeviceBody(BaseModel):
    camera_id: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    active: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=2, le=600)


@router.get("/devices")
def list_devices(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    rows = ctx.db.all("SELECT d.id, d.camera_id, d.name, d.active, d.cooldown_seconds, d.firmware_version, "
                      "d.last_seen_at, d.last_trigger_at, d.created_at, c.area_label, c.establishment_id "
                      "FROM devices d JOIN cameras c ON c.id = d.camera_id ORDER BY d.name")
    if user["role"] != "superadmin":
        rows = [r for r in rows if r["establishment_id"] == user["establishment_id"]]
    for r in rows:
        r["online"] = bool(r["last_seen_at"] and r["last_seen_at"] > now() - 180)
    return rows


@router.post("/devices", status_code=201)
def create_device(body: DeviceBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    if not (body.camera_id and body.name):
        raise ApiError(422, "VALIDATION", "Informe câmera e nome.")
    _get_camera(ctx, user, body.camera_id)
    secret = secrets.token_hex(32)
    did = new_id()
    ctx.db.insert("devices", dict(id=did, camera_id=body.camera_id, name=body.name, secret_enc=ctx.box.encrypt(secret),
                                  active=1, cooldown_seconds=body.cooldown_seconds or 15, created_at=now()))
    audit(ctx.db, "admin", "device.created", "device", did, actor_id=user["id"])
    return {"id": did, "secret": secret, "api_base": ctx.settings.public_base_url,
            "message": "Copie o segredo agora: ele não será exibido novamente."}


def _get_device(ctx, user, did):
    d = ctx.db.one("SELECT d.*, c.establishment_id FROM devices d JOIN cameras c ON c.id = d.camera_id "
                   "WHERE d.id = ?", (did,))
    if not d:
        raise ApiError(404, "NOT_FOUND", "Dispositivo não encontrado.")
    check_establishment_access(user, d["establishment_id"])
    return d


@router.patch("/devices/{did}")
def update_device(did: str, body: DeviceBody, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    _get_device(ctx, user, did)
    vals = body.model_dump(exclude_none=True)
    if "camera_id" in vals:
        _get_camera(ctx, user, vals["camera_id"])
    if "active" in vals:
        vals["active"] = 1 if vals["active"] else 0
    if vals:
        ctx.db.update("devices", did, vals)
    audit(ctx.db, "admin", "device.updated", "device", did, actor_id=user["id"], fields=list(vals))
    return {"ok": True}


@router.post("/devices/{did}/rotate-secret")
def rotate_device_secret(did: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    _get_device(ctx, user, did)
    secret = secrets.token_hex(32)
    ctx.db.update("devices", did, {"secret_enc": ctx.box.encrypt(secret)})
    audit(ctx.db, "admin", "device.secret_rotated", "device", did, actor_id=user["id"])
    return {"id": did, "secret": secret}


@router.delete("/devices/{did}")
def delete_device(did: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    _get_device(ctx, user, did)
    ctx.db.execute("DELETE FROM devices WHERE id = ?", (did,))
    audit(ctx.db, "admin", "device.deleted", "device", did, actor_id=user["id"])
    return {"ok": True}


# --- pedidos, dashboard, exportação ---------------------------------------
def _filter_args(request: Request, user) -> dict:
    q = request.query_params
    f = {k: q.get(k) for k in ("start", "end", "establishment_id", "camera_id") if q.get(k)}
    if user["role"] != "superadmin":
        f["establishment_id"] = user["establishment_id"]
    return f


@router.get("/orders")
def list_orders(request: Request, user=Depends(admin_session), status: str | None = None,
                limit: int = 50, offset: int = 0):
    ctx = ctx_of(request)
    f = _filter_args(request, user)
    where, params = metrics._filters(f, "o")
    if status:
        where += " AND o.status = ?"
        params.append(status)
    limit = max(1, min(limit, 200))
    rows = ctx.db.all(
        f"SELECT o.id, o.short_code, o.status, o.amount_cents, o.currency, o.created_at, o.paid_at, "
        f"o.payment_deadline, o.available_until, o.demo, r.status replay_status, r.error_code, r.source, "
        f"r.processing_ms, c.area_label, e.name establishment, "
        f"(SELECT p.status FROM payments p WHERE p.order_id = o.id ORDER BY p.created_at DESC LIMIT 1) payment_status, "
        f"(SELECT p.refund_status FROM payments p WHERE p.order_id = o.id AND p.refund_status IS NOT NULL LIMIT 1) "
        f"refund_status FROM orders o JOIN replay_requests r ON r.id = o.replay_request_id "
        f"JOIN cameras c ON c.id = o.camera_id JOIN establishments e ON e.id = o.establishment_id "
        f"WHERE {where} ORDER BY o.created_at DESC LIMIT ? OFFSET ?", [*params, limit, max(0, offset)])
    total = ctx.db.one(f"SELECT COUNT(*) n FROM orders o WHERE {where}", params)["n"]
    return {"orders": rows, "total": total}


@router.get("/orders/export.csv")
def export_orders(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    data = metrics.orders_csv(ctx, _filter_args(request, user))
    audit(ctx.db, "admin", "orders.exported", actor_id=user["id"])
    name = f"pedidos-replay-shape-{datetime.now():%Y%m%d-%H%M}.csv"
    return PlainTextResponse("﻿" + data, media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/orders/{oid}/refund")
def refund_order(oid: str, request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    require_superadmin(user)
    pay = ctx.db.one("SELECT * FROM payments WHERE order_id = ? AND status = 'approved' AND duplicate = 0", (oid,))
    if not pay:
        raise ApiError(409, "NO_APPROVED_PAYMENT", "Pedido sem pagamento aprovado.")
    order_svc._refund(ctx, pay, "admin")
    order_svc.reconcile_payment(ctx, pay["provider_payment_id"])
    audit(ctx.db, "admin", "order.refund", "order", oid, actor_id=user["id"])
    return ctx.db.one("SELECT id, status FROM orders WHERE id = ?", (oid,))


@router.get("/dashboard")
def dashboard(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    return metrics.dashboard(ctx, _filter_args(request, user))


@router.get("/settings")
def get_settings(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    return {"settings": ctx.store.all(),
            "quality_profiles": {k: v["label"] for k, v in QUALITY_PROFILES.items()}}


@router.patch("/settings")
async def update_settings(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    require_superadmin(user)
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError(422, "VALIDATION", "Formato inválido.")
    try:
        merged = ctx.store.update(body)
    except ValueError as e:
        raise ApiError(422, "VALIDATION", str(e)) from e
    audit(ctx.db, "admin", "settings.updated", actor_id=user["id"], fields=list(body))
    return {"settings": merged}


@router.get("/audit")
def audit_log(request: Request, user=Depends(admin_session), limit: int = 100):
    ctx = ctx_of(request)
    require_superadmin(user)
    return ctx.db.all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))


@router.get("/privacy-requests")
def list_privacy(request: Request, user=Depends(admin_session)):
    ctx = ctx_of(request)
    require_superadmin(user)
    return ctx.db.all("SELECT * FROM privacy_requests ORDER BY created_at DESC LIMIT 200")


@router.get("/buffers")
def buffers(request: Request, user=Depends(admin_session)):
    return ctx_of(request).buffers.snapshot()

