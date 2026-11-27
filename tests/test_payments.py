"""Pagamento: pendente, aprovado, recusado, webhooks, expiração, duplicidade, estornos."""
from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import create_ready_order

from replayshape.services import cleanup
from replayshape.services import orders as order_svc


def H(tok):
    return {"X-Order-Token": tok}


def pay(client, oid, tok):
    r = client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    assert r.status_code == 200, r.text
    return r.json()


def demo_pay(client, oid, tok, result):
    return client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": result})


def test_pending_payment_blocks_download(demo):
    client = demo["client"]
    oid, tok, _ = create_ready_order(client)
    o = pay(client, oid, tok)
    assert o["payment"]["status"] == "pending"
    assert o["payment"]["pix_copy_paste"].startswith("DEMO-") and o["payment"]["qr_data_uri"].startswith("data:image/svg")
    r = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok))
    assert r.status_code == 402


def test_approved_releases_correct_file(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    oid2, tok2, _ = create_ready_order(client)
    pay(client, oid, tok)
    o = demo_pay(client, oid, tok, "approved").json()
    assert o["status"] == "paid" and o["available_until"] > time.time() + 23 * 3600
    link = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()
    body = client.get(link["url"]).content
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (oid,))
    rf = ctx.db.one("SELECT * FROM replay_files WHERE replay_request_id = ?", (order["replay_request_id"],))
    assert body == Path(rf["local_path"]).read_bytes()
    # O outro pedido (não pago) continua bloqueado.
    assert client.post(f"/api/v1/public/orders/{oid2}/media-link", headers=H(tok2)).status_code == 402
    dl = client.get(link["download_url"])
    assert "attachment" in dl.headers["content-disposition"]


def test_rejected_keeps_order_payable(demo):
    client = demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    o = demo_pay(client, oid, tok, "rejected").json()
    assert o["status"] == "awaiting_payment" and o["payment"]["status"] == "rejected"
    o2 = pay(client, oid, tok)  # nova tentativa gera nova cobrança
    assert o2["payment"]["status"] == "pending"


def test_payment_reuses_pending_charge(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    a = pay(client, oid, tok)["payment"]["pix_copy_paste"]
    b = pay(client, oid, tok)["payment"]["pix_copy_paste"]
    assert a == b
    assert ctx.db.one("SELECT COUNT(*) n FROM payments WHERE order_id = ?", (oid,))["n"] == 1


def test_duplicate_webhook_is_idempotent(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    headers, body = ctx.provider.simulate(p["provider_payment_id"], "approved")
    r1 = client.post("/api/v1/webhooks/mock", headers=headers, content=body)
    r2 = client.post("/api/v1/webhooks/mock", headers=headers, content=body)
    assert r1.json()["result"] == "approved"
    assert r2.json()["result"] == "duplicate"
    assert ctx.db.one("SELECT COUNT(*) n FROM payment_events WHERE signature_valid = 1")["n"] == 1


def test_invalid_webhook_signature_rejected(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    ctx.db.execute("UPDATE mock_pix_charges SET status = 'approved' WHERE id = ?", (p["provider_payment_id"],))
    fake = json.dumps({"id": "x1", "type": "payment", "data": {"id": p["provider_payment_id"]}}).encode()
    r = client.post("/api/v1/webhooks/mock", headers={"x-mock-signature": "ts=1,v1=deadbeef"}, content=fake)
    assert r.status_code == 401
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "awaiting_payment"


def test_frontend_cannot_fake_approval(demo):
    """Corpo do webhook dizendo 'approved' não basta: o status vem da consulta ao provedor."""
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    # Assinatura válida, mas o provedor ainda diz "pending".
    headers, body = ctx.provider.simulate(p["provider_payment_id"], "approved")
    ctx.db.execute("UPDATE mock_pix_charges SET status = 'pending' WHERE id = ?", (p["provider_payment_id"],))
    client.post("/api/v1/webhooks/mock", headers=headers, content=body)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "awaiting_payment"


def test_amount_mismatch_does_not_release(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    ctx.db.execute("UPDATE mock_pix_charges SET amount_cents = 1 WHERE id = ?", (p["provider_payment_id"],))
    headers, body = ctx.provider.simulate(p["provider_payment_id"], "approved")
    res = client.post("/api/v1/webhooks/mock", headers=headers, content=body).json()
    assert res["result"] == "mismatch"
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "awaiting_payment"


def test_payment_after_expiration_with_file_still_present(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    ctx.db.execute("UPDATE orders SET payment_deadline = ? WHERE id = ?", (time.time() - 5, oid))
    cleanup.run_cleanup(ctx)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "expired"
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    ctx.provider.simulate(p["provider_payment_id"], "approved")
    # Mesmo com o pagamento local marcado como expirado, a consulta ao provedor prevalece.
    order_svc.reconcile_payment(ctx, p["provider_payment_id"])
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "paid"


def test_payment_after_file_deleted_triggers_refund(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    ctx.db.execute("UPDATE orders SET payment_deadline = ? WHERE id = ?", (time.time() - 5, oid))
    ctx.db.execute("UPDATE replay_files SET delete_after = ?", (time.time() - 1,))
    cleanup.run_cleanup(ctx)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    ctx.provider.simulate(p["provider_payment_id"], "approved")
    res = order_svc.reconcile_payment(ctx, p["provider_payment_id"])
    assert res == "approved_refund_file_unavailable"
    o = ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))
    assert o["status"] == "refunded"
    assert ctx.db.one("SELECT refund_status FROM payments WHERE id = ?", (p["id"],))["refund_status"] == "done"


def test_duplicate_payment_is_refunded(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    demo_pay(client, oid, tok, "rejected")
    pay(client, oid, tok)
    p1, p2 = ctx.db.all("SELECT * FROM payments WHERE order_id = ? ORDER BY created_at", (oid,))
    for p in (p2, p1):  # ambos acabam aprovados no provedor
        ctx.provider.simulate(p["provider_payment_id"], "approved")
        order_svc.reconcile_payment(ctx, p["provider_payment_id"])
    rows = ctx.db.all("SELECT duplicate, refund_status FROM payments WHERE order_id = ? ORDER BY created_at", (oid,))
    assert rows[0]["duplicate"] == 1 and rows[0]["refund_status"] == "done"
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "paid"


def test_refund_revokes_access(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    demo_pay(client, oid, tok, "approved")
    link = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    headers, body = ctx.provider.simulate(p["provider_payment_id"], "refunded")
    client.post("/api/v1/webhooks/mock", headers=headers, content=body)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "refunded"
    assert client.get(link["url"]).status_code == 410


def test_reconciliation_recovers_missed_webhook(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    pay(client, oid, tok)
    p = ctx.db.one("SELECT * FROM payments WHERE order_id = ?", (oid,))
    ctx.db.execute("UPDATE mock_pix_charges SET status = 'approved' WHERE id = ?", (p["provider_payment_id"],))
    assert order_svc.reconcile_pending(ctx) == 1
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "paid"


def test_no_payment_when_replay_failed(demo):
    ctx, client = demo["ctx"], demo["client"]
    ctx.buffers.buffers.clear()
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays")
    assert r.status_code == 503
    assert ctx.db.one("SELECT COUNT(*) n FROM payments")["n"] == 0
