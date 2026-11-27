"""Pedidos, pagamentos, webhooks, conciliação e links de download."""
from __future__ import annotations

import logging
from pathlib import Path

from ..db import new_id, now
from ..integrations.payments import ProviderError, WebhookRejected
from ..security import random_token, token_hash
from .audit import audit
from .replays import local_file_ok, replay_file_for_order

log = logging.getLogger("replayshape.orders")

FINAL_PAYMENT = {"approved", "rejected", "cancelled", "expired", "refunded", "charged_back"}


class OrderError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# --- cobrança ------------------------------------------------------------
def create_or_get_payment(ctx, order_id: str) -> dict:
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        raise OrderError("ORDER_NOT_FOUND", "Pedido não encontrado.", 404)
    if order["status"] == "paid":
        raise OrderError("ALREADY_PAID", "Este pedido já foi pago.")
    if order["status"] != "awaiting_payment":
        raise OrderError("NOT_PAYABLE", "Este pedido não pode ser pago (replay não gerado ou expirado).")
    if order["payment_deadline"] and order["payment_deadline"] < now():
        raise OrderError("ORDER_EXPIRED", "O prazo para pagamento expirou.")
    rf = replay_file_for_order(ctx, order)
    if not local_file_ok(rf):
        raise OrderError("FILE_UNAVAILABLE", "O replay não está mais disponível. Nenhuma cobrança foi feita.")

    existing = ctx.db.one(
        "SELECT * FROM payments WHERE order_id = ? AND status = 'pending' AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC LIMIT 1", (order_id, now() + 30))
    if existing:
        return existing

    attempt = ctx.db.one("SELECT COUNT(*) AS n FROM payments WHERE order_id = ?", (order_id,))["n"] + 1
    idem = f"{order_id}:{attempt}"
    est = ctx.db.one("SELECT name FROM establishments WHERE id = ?", (order["establishment_id"],))
    try:
        charge = ctx.provider.create_charge(
            order_id=order_id, amount_cents=order["amount_cents"], currency=order["currency"],
            description=f"Replay Shape - {est['name']} - {order['short_code']}", idempotency_key=idem,
            expires_at=order["payment_deadline"],
            notification_url=f"{ctx.settings.public_base_url}/api/v1/webhooks/{ctx.provider.name}",
        )
    except ProviderError as e:
        audit(ctx.db, "system", "payment.create_failed", "order", order_id, error=str(e)[:200])
        raise OrderError("PROVIDER_ERROR", "Não foi possível gerar o Pix agora. Tente novamente.", 502) from e
    t = now()
    pay = dict(
        id=new_id(), order_id=order_id, provider=ctx.provider.name,
        provider_payment_id=charge.provider_payment_id, idempotency_key=idem, status=charge.status,
        amount_cents=order["amount_cents"], currency=order["currency"], pix_copy_paste=charge.pix_copy_paste,
        ticket_url=charge.ticket_url, expires_at=charge.expires_at, created_at=t, updated_at=t,
    )
    ctx.db.insert("payments", pay)
    audit(ctx.db, "system", "payment.created", "payment", pay["id"], order_id=order_id,
          provider=ctx.provider.name)
    return pay


# --- webhooks e conciliação --------------------------------------------
def handle_notification(ctx, provider_name: str, headers: dict, query: dict, body: bytes) -> dict:
    if provider_name != ctx.provider.name:
        raise WebhookRejected("provedor não configurado")
    try:
        note = ctx.provider.parse_notification(headers, query, body)
    except WebhookRejected as e:
        _record_event(ctx, provider_name, f"invalid:{new_id()}", None, "invalid", False, body[:2000], str(e))
        raise
    inserted = _record_event(ctx, provider_name, note.event_key, note.provider_payment_id, note.type, True,
                             note.payload.encode(), "received")
    if not inserted:
        return {"result": "duplicate"}
    if not note.provider_payment_id or (note.type and "payment" not in note.type):
        return {"result": "ignored"}
    result = reconcile_payment(ctx, note.provider_payment_id)
    ctx.db.execute("UPDATE payment_events SET result = ? WHERE event_key = ?", (result, note.event_key))
    return {"result": result}


def _record_event(ctx, provider, key, ppid, typ, valid, payload: bytes, result) -> bool:
    try:
        ctx.db.insert("payment_events", dict(
            id=new_id(), provider=provider, event_key=key, provider_payment_id=ppid, type=typ,
            signature_valid=1 if valid else 0, payload=payload.decode(errors="replace")[:10000],
            result=result, received_at=now()))
        return True
    except Exception as e:  # violação de UNIQUE => webhook duplicado
        if "UNIQUE" in str(e):
            return False
        raise


def reconcile_payment(ctx, provider_payment_id: str) -> str:
    """Consulta o provedor (fonte da verdade) e aplica a transição de estado."""
    local = ctx.db.one("SELECT * FROM payments WHERE provider = ? AND provider_payment_id = ?",
                       (ctx.provider.name, provider_payment_id))
    if not local:
        return "unknown_payment"
    try:
        info = ctx.provider.get_payment(provider_payment_id)
    except ProviderError as e:
        log.warning("consulta ao provedor falhou: %s", e)
        return "provider_error"
    ctx.db.update("payments", local["id"], {"last_checked_at": now()})

    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (local["order_id"],))
    # Validação independente: referência, valor e moeda precisam bater.
    if (info.external_reference != order["id"] or info.amount_cents != local["amount_cents"]
            or info.currency != local["currency"]):
        audit(ctx.db, "provider", "payment.mismatch", "payment", local["id"],
              ref_ok=info.external_reference == order["id"], amount=info.amount_cents, currency=info.currency)
        ctx.db.update("payments", local["id"], {"status_detail": "mismatch", "updated_at": now()})
        return "mismatch"

    if info.status == local["status"] and info.status != "approved":
        return "unchanged"

    if info.status == "approved":
        return _on_approved(ctx, local, order, info)
    if info.status in ("refunded", "charged_back"):
        with ctx.db.tx():
            ctx.db.update("payments", local["id"], {"status": info.status, "refund_status": "done",
                                                    "updated_at": now()})
            if order["status"] in ("paid", "refund_pending", "paid_unfulfillable") and not local["duplicate"]:
                ctx.db.update("orders", order["id"], {"status": "refunded", "updated_at": now()})
                ctx.db.execute("DELETE FROM download_tokens WHERE order_id = ?", (order["id"],))
                _schedule_file_deletion(ctx, order, now(), "refunded")
        audit(ctx.db, "provider", f"payment.{info.status}", "payment", local["id"])
        return info.status
    ctx.db.update("payments", local["id"], {"status": info.status, "status_detail": info.status_detail,
                                            "updated_at": now()})
    audit(ctx.db, "provider", f"payment.{info.status}", "payment", local["id"])
    return info.status


def _on_approved(ctx, pay: dict, order: dict, info) -> str:
    t = now()
    refund_reason = None
    with ctx.db.tx():
        pay = ctx.db.one("SELECT * FROM payments WHERE id = ?", (pay["id"],))
        order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order["id"],))
        if pay["status"] == "approved":
            return "already_approved"
        ctx.db.update("payments", pay["id"], {"status": "approved", "approved_at": t,
                                              "fee_cents": info.fee_cents, "updated_at": t})
        paid_elsewhere = ctx.db.one(
            "SELECT 1 FROM payments WHERE order_id = ? AND status = 'approved' AND id != ?",
            (order["id"], pay["id"]))
        if paid_elsewhere or order["status"] == "paid":
            ctx.db.update("payments", pay["id"], {"duplicate": 1})
            refund_reason = "duplicate"
        elif order["status"] in ("awaiting_payment", "expired"):
            rf = replay_file_for_order(ctx, order)
            if local_file_ok(rf):
                hours = int(ctx.store.get("paid_hours"))
                until = t + hours * 3600
                ctx.db.update("orders", order["id"], {"status": "paid", "paid_at": t, "available_until": until,
                                                      "updated_at": t})
                ctx.db.update("replay_files", rf["id"], {"status": "available", "delete_after": until})
                # Cancela outras cobranças pendentes do mesmo pedido.
                for other in ctx.db.all("SELECT * FROM payments WHERE order_id = ? AND status = 'pending'",
                                        (order["id"],)):
                    ctx.db.update("payments", other["id"], {"status": "cancelled", "updated_at": t})
            else:
                ctx.db.update("orders", order["id"], {"status": "paid_unfulfillable", "paid_at": t,
                                                      "updated_at": t})
                refund_reason = "file_unavailable"
        else:
            refund_reason = f"order_{order['status']}"
    audit(ctx.db, "provider", "payment.approved", "payment", pay["id"], order_id=order["id"],
          refund=refund_reason)
    if refund_reason:
        _refund(ctx, ctx.db.one("SELECT * FROM payments WHERE id = ?", (pay["id"],)), refund_reason)
        return f"approved_refund_{refund_reason}"
    if ctx.remote is not None:
        ctx.executor.submit(upload_with_retry, ctx, order["id"])
    return "approved"


def _refund(ctx, pay: dict, reason: str) -> None:
    try:
        status = ctx.provider.refund(pay["provider_payment_id"], f"refund:{pay['id']}")
        ctx.db.update("payments", pay["id"], {"refund_status": "done" if status == "done" else "requested",
                                              "updated_at": now()})
        if not pay.get("duplicate"):
            new_status = "refunded" if status == "done" else "refund_pending"
            ctx.db.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ? AND status != 'paid'",
                           (new_status, now(), pay["order_id"]))
        audit(ctx.db, "system", "payment.refund_requested", "payment", pay["id"], reason=reason)
    except ProviderError as e:
        ctx.db.update("payments", pay["id"], {"refund_status": "failed", "updated_at": now()})
        audit(ctx.db, "system", "payment.refund_failed", "payment", pay["id"], reason=reason, error=str(e)[:200])


def reconcile_pending(ctx) -> int:
    """Rede de segurança para webhooks perdidos: consulta pagamentos pendentes."""
    n = 0
    for p in ctx.db.all(
        "SELECT provider_payment_id FROM payments WHERE status = 'pending' AND provider = ? "
        "AND created_at > ? ORDER BY created_at LIMIT 100", (ctx.provider.name, now() - 2 * 86400)):
        reconcile_payment(ctx, p["provider_payment_id"])
        n += 1
    return n


def maybe_reconcile_on_poll(ctx, order_id: str) -> None:
    """Quando o comprador consulta o status, verifica o provedor (no máx. a cada 5 s)."""
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ? AND status = 'pending' ORDER BY created_at DESC "
                   "LIMIT 1", (order_id,))
    if p and (p["last_checked_at"] or 0) < now() - 5:
        reconcile_payment(ctx, p["provider_payment_id"])


# --- links temporários ----------------------------------------------------
def create_media_link(ctx, order_id: str) -> dict:
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order or order["status"] != "paid":
        raise OrderError("NOT_PAID", "O replay só é liberado após a confirmação do pagamento.", 402)
    if (order["available_until"] or 0) < now():
        raise OrderError("EXPIRED", "O prazo de disponibilidade deste replay terminou.", 410)
    rf = replay_file_for_order(ctx, order)
    if not local_file_ok(rf) and not (rf and rf["remote_status"] == "uploaded"):
        raise OrderError("FILE_UNAVAILABLE", "Arquivo indisponível. Entre em contato com o estabelecimento.", 410)
    token = random_token()
    minutes = int(ctx.store.get("link_minutes"))
    exp = min(now() + minutes * 60, order["available_until"])
    ctx.db.insert("download_tokens", dict(id=new_id(), order_id=order_id, token_hash=token_hash(token),
                                          expires_at=exp, created_at=now()))
    return {"token": token, "expires_at": exp}


def resolve_media_token(ctx, token: str) -> tuple[dict, dict]:
    row = ctx.db.one("SELECT * FROM download_tokens WHERE token_hash = ?", (token_hash(token),))
    if not row or row["expires_at"] < now():
        raise OrderError("LINK_EXPIRED", "Link expirado. Volte à página do pedido para gerar outro.", 410)
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (row["order_id"],))
    if not order or order["status"] != "paid" or (order["available_until"] or 0) < now():
        raise OrderError("EXPIRED", "Replay indisponível.", 410)
    rf = replay_file_for_order(ctx, order)
    ctx.db.execute("UPDATE download_tokens SET uses = uses + 1, last_used_at = ? WHERE id = ?", (now(), row["id"]))
    return order, rf


# --- armazenamento remoto -------------------------------------------------
def upload_with_retry(ctx, order_id: str, attempts: int = 3) -> bool:
    import time as _t
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    rf = replay_file_for_order(ctx, order)
    if not ctx.remote or not local_file_ok(rf):
        return False
    key = f"replays/{order['establishment_id']}/{rf['id']}.mp4"
    for i in range(attempts):
        try:
            ctx.db.update("replay_files", rf["id"], {"remote_status": "pending",
                                                     "upload_attempts": rf["upload_attempts"] + i + 1})
            ctx.remote.upload(Path(rf["local_path"]), key)
            ctx.db.update("replay_files", rf["id"], {"remote_status": "uploaded", "remote_key": key})
            audit(ctx.db, "system", "file.uploaded", "replay_file", rf["id"])
            return True
        except Exception as e:
            log.warning("upload falhou (tentativa %s): %s", i + 1, type(e).__name__)
            _t.sleep(2 ** i)
    ctx.db.update("replay_files", rf["id"], {"remote_status": "failed"})
    audit(ctx.db, "system", "file.upload_failed", "replay_file", rf["id"])
    return False  # o arquivo continua disponível a partir do disco local


def _schedule_file_deletion(ctx, order: dict, when: float, reason: str) -> None:
    ctx.db.execute("UPDATE replay_files SET delete_after = ?, deleted_reason = ? WHERE replay_request_id = ? "
                   "AND status IN ('private','available')", (when, reason, order["replay_request_id"]))
