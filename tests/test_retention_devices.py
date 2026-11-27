"""Retenção/expiração, reinício, disco, upload remoto e botões físicos."""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

from conftest import create_ready_order

from replayshape.db import new_id, now
from replayshape.security import device_signature
from replayshape.services import cleanup
from replayshape.services import orders as order_svc


def H(tok):
    return {"X-Order-Token": tok}


def _file(ctx, oid):
    o = ctx.db.one("SELECT * FROM orders WHERE id = ?", (oid,))
    return ctx.db.one("SELECT * FROM replay_files WHERE replay_request_id = ?", (o["replay_request_id"],))


def test_unpaid_replay_expires_and_is_deleted(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    rf = _file(ctx, oid)
    assert Path(rf["local_path"]).exists()
    ctx.db.execute("UPDATE orders SET payment_deadline = ?", (time.time() - 1,))
    cleanup.run_cleanup(ctx)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "expired"
    assert Path(rf["local_path"]).exists()  # ainda dentro da tolerância p/ Pix tardio
    ctx.db.execute("UPDATE replay_files SET delete_after = ?", (time.time() - 1,))
    cleanup.run_cleanup(ctx)
    rf2 = _file(ctx, oid)
    assert rf2["status"] == "deleted" and not Path(rf["local_path"]).exists()
    assert ctx.db.one("SELECT COUNT(*) n FROM audit_logs WHERE action = 'file.deleted'")["n"] == 1


def test_paid_replay_available_then_deleted_after_window(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": "approved"})
    rf = _file(ctx, oid)
    cleanup.run_cleanup(ctx)
    assert Path(rf["local_path"]).exists()  # não apaga após o primeiro download/rodada
    ctx.db.execute("UPDATE orders SET available_until = ?", (time.time() - 1,))
    ctx.db.execute("UPDATE replay_files SET delete_after = ?", (time.time() - 1,))
    cleanup.run_cleanup(ctx)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] == "closed"
    assert not Path(rf["local_path"]).exists()
    assert client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).status_code in (402, 410)


def test_restart_recovery(demo, make_app):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    # Simula pedido travado e um diretório órfão.
    rid = new_id()
    cam = demo["cam"]
    ctx.db.insert("replay_requests", dict(id=rid, camera_id=cam["id"], establishment_id=cam["establishment_id"],
                                          source="web", trigger_at=now(), window_start=now() - 60, window_end=now(),
                                          duration_s=60, status="processing", created_at=now(), updated_at=now()))
    orphan = ctx.settings.replays_dir / "orfao123"
    orphan.mkdir()
    stats = cleanup.startup_recovery(ctx)
    assert stats["requests_failed"] == 1 and stats["orphans_removed"] == 1
    assert ctx.db.one("SELECT status FROM replay_requests WHERE id = ?", (rid,))["status"] == "failed"
    assert Path(_file(ctx, oid)["local_path"]).exists()  # pedido válido preservado


def test_restart_with_missing_paid_file_flags_refund(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": "approved"})
    Path(_file(ctx, oid)["local_path"]).unlink()
    cleanup.startup_recovery(ctx)
    assert ctx.db.one("SELECT status FROM orders WHERE id = ?", (oid,))["status"] in ("refunded", "refund_pending")


def test_low_disk_blocks_new_replays(demo):
    ctx, client = demo["ctx"], demo["client"]
    ctx.store.update({"min_free_disk_mb": 999_999})
    assert cleanup.check_disk(ctx) is True
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DISK_FULL"


def test_upload_failure_keeps_local_copy(demo, monkeypatch):
    ctx, client = demo["ctx"], demo["client"]

    class Broken:
        def upload(self, *a, **k):
            raise RuntimeError("rede caiu")

    oid, tok, _ = create_ready_order(client)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": "approved"})
    ctx.remote = Broken()
    monkeypatch.setattr(time, "sleep", lambda s: None)  # acelera os retries
    assert order_svc.upload_with_retry(ctx, oid, attempts=3) is False
    rf = _file(ctx, oid)
    assert rf["remote_status"] == "failed" and rf["upload_attempts"] >= 3
    # Continua disponível a partir do disco local.
    link = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()
    assert client.get(link["url"]).status_code == 200


# --- botões físicos ------------------------------------------------------------------
def _device(ctx, cam_id, cooldown=15):
    secret = secrets.token_hex(32)
    did = new_id()
    ctx.db.insert("devices", dict(id=did, camera_id=cam_id, name="Botão", secret_enc=ctx.box.encrypt(secret),
                                  active=1, cooldown_seconds=cooldown, created_at=now()))
    return did, secret


def _signed(client, did, secret, path_suffix="trigger", ts=None, nonce=None, sig=None):
    path = f"/api/v1/internal/devices/{did}/{path_suffix}"
    ts = ts or str(int(time.time()))
    nonce = nonce or secrets.token_hex(8)
    sig = sig or device_signature(secret, ts, nonce, "POST", path, b"")
    return client.post(path, headers={"X-RS-Timestamp": ts, "X-RS-Nonce": nonce, "X-RS-Signature": sig}), nonce, ts


def test_device_trigger_associates_camera_and_claim(demo):
    ctx, client = demo["ctx"], demo["client"]
    did, secret = _device(ctx, demo["cam"]["id"])
    r, _, _ = _signed(client, did, secret)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    code = r.json()["short_code"]
    o = ctx.db.one("SELECT * FROM orders WHERE short_code = ?", (code,))
    assert o["camera_id"] == demo["cam"]["id"]
    # Quem está na mesa reivindica o acionamento pela página do QR.
    c = client.post("/api/v1/public/spots/demo-mesa-01/claim", json={}).json()
    assert c["order_id"] == o["id"]
    # Reivindicado: não aparece de novo para outra pessoa.
    assert client.post("/api/v1/public/spots/demo-mesa-01/claim", json={}).status_code == 404
    # Também é possível recuperar pelo código individual.
    assert client.post("/api/v1/public/orders/recover", json={"code": code.lower()}).status_code == 200


def test_device_debounce(demo):
    ctx, client = demo["ctx"], demo["client"]
    did, secret = _device(ctx, demo["cam"]["id"], cooldown=30)
    r1, _, _ = _signed(client, did, secret)
    r2, _, _ = _signed(client, did, secret)
    assert r1.json()["status"] == "accepted" and r2.json()["status"] == "debounced"
    assert ctx.db.one("SELECT COUNT(*) n FROM replay_requests WHERE device_id = ?", (did,))["n"] == 1


def test_device_auth_rejections(demo):
    ctx, client = demo["ctx"], demo["client"]
    did, secret = _device(ctx, demo["cam"]["id"])
    r, _, _ = _signed(client, did, secret, sig="0" * 64)
    assert r.status_code == 401
    r, _, _ = _signed(client, did, secret, ts=str(int(time.time()) - 3600))
    assert r.status_code == 401
    ok, nonce, ts = _signed(client, did, secret, path_suffix="heartbeat")
    assert ok.status_code == 200
    replay, _, _ = _signed(client, did, secret, path_suffix="heartbeat", ts=ts, nonce=nonce)
    assert replay.status_code == 409
    other_did, _ = _device(ctx, demo["cam"]["id"])
    r, _, _ = _signed(client, other_did, secret)  # segredo de outro dispositivo
    assert r.status_code == 401


def test_signature_matches_firmware_format():
    """Mesmo algoritmo do firmware (firmware/esp32): HMAC-SHA256 sobre ts\\nnonce\\nMETHOD\\npath\\nsha256(body)."""
    import hmac
    body_hash = hashlib.sha256(b"").hexdigest()
    msg = f"1700000000\nabcdef12\nPOST\n/api/v1/internal/devices/x/trigger\n{body_hash}".encode()
    expected = hmac.new(b"segredo", msg, hashlib.sha256).hexdigest()
    assert device_signature("segredo", "1700000000", "abcdef12", "post",
                            "/api/v1/internal/devices/x/trigger", b"") == expected
