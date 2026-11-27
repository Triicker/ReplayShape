"""Controle de acesso, IDOR, admin (sessão/CSRF/RBAC), SSRF, rate limit, produção."""
from __future__ import annotations

import pytest
from conftest import create_ready_order

from replayshape.config import load_settings
from replayshape.integrations.cameras.base import CameraConfig, CameraConfigError, IntelbrasAdapter
from replayshape.security import hash_password


def H(tok):
    return {"X-Order-Token": tok}


def test_cannot_access_other_order(demo):
    client = demo["client"]
    a_id, a_tok, _ = create_ready_order(client)
    b_id, b_tok, _ = create_ready_order(client)
    assert client.get(f"/api/v1/public/orders/{b_id}", headers=H(a_tok)).status_code == 404
    assert client.get(f"/api/v1/public/orders/{b_id}").status_code == 404
    assert client.post(f"/api/v1/public/orders/{b_id}/media-link", headers=H(a_tok)).status_code == 404
    assert client.get(f"/api/v1/public/orders/{'0' * 32}", headers=H(a_tok)).status_code == 404


def test_unauthorized_download_and_link_tampering(demo):
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, _ = create_ready_order(client)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": "approved"})
    link = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()
    bad = link["url"].replace("/media/", "/media/x")
    assert client.get(bad).status_code == 410
    # Link expirado deixa de funcionar; um novo pode ser gerado enquanto o pedido é válido.
    ctx.db.execute("UPDATE download_tokens SET expires_at = 0")
    assert client.get(link["url"]).status_code == 410
    again = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()
    assert client.get(again["url"]).status_code == 200


def test_interrupted_download_can_resume(demo):
    client = demo["client"]
    oid, tok, _ = create_ready_order(client)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers=H(tok))
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers=H(tok), json={"result": "approved"})
    url = client.post(f"/api/v1/public/orders/{oid}/media-link", headers=H(tok)).json()["url"]
    full = client.get(url).content
    part1 = client.get(url, headers={"Range": "bytes=0-9999"})
    assert part1.status_code == 206
    part2 = client.get(url, headers={"Range": "bytes=10000-"})
    assert part2.status_code == 206 and part1.content + part2.content == full
    # Vários downloads permitidos enquanto o pedido estiver válido.
    assert client.get(url).status_code == 200


def test_no_public_listing_outside_demo(make_app):
    app, client = make_app(demo_mode=False)
    assert client.get("/api/v1/public/demo/spots").status_code == 404


def test_demo_payment_endpoint_blocked_with_real_provider(make_app, monkeypatch):
    app, client = make_app()
    app.state.ctx.provider.is_simulated = False
    r = client.post(f"/api/v1/public/orders/{'a' * 32}/demo-payment", json={"result": "approved"})
    assert r.status_code == 404


def test_production_refuses_mock_and_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("RS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RS_ENV", "production")
    monkeypatch.delenv("RS_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        load_settings()


def _login(client, ctx, email="ops@x.com", role="superadmin", est=None, password="senha-forte-123"):
    from replayshape.db import new_id, now
    ctx.db.insert("admin_users", dict(id=new_id(), email=email, password_hash=hash_password(password), role=role,
                                      establishment_id=est, active=1, created_at=now()))
    r = client.post("/api/v1/admin/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def test_admin_requires_auth_and_csrf(make_app):
    app, client = make_app()
    assert client.get("/api/v1/admin/cameras").status_code == 401
    assert client.post("/api/v1/admin/auth/login", json={"email": "x@y.z", "password": "nope"}).status_code == 401
    csrf = _login(client, app.state.ctx)
    assert client.get("/api/v1/admin/cameras").status_code == 200
    est = client.get("/api/v1/admin/establishments").json()[0]["id"]
    body = {"establishment_id": est, "name": "Cam X", "area_label": "Quadra 9", "adapter": "demo"}
    assert client.post("/api/v1/admin/cameras", json=body).status_code == 403  # sem CSRF
    r = client.post("/api/v1/admin/cameras", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 201
    assert "password_enc" not in r.json()


def test_admin_camera_credentials_encrypted_and_hidden(make_app):
    app, client = make_app()
    ctx = app.state.ctx
    csrf = _login(client, ctx)
    est = client.get("/api/v1/admin/establishments").json()[0]["id"]
    r = client.post("/api/v1/admin/cameras", headers={"X-CSRF-Token": csrf}, json={
        "establishment_id": est, "name": "Intelbras 1", "area_label": "Quadra 1", "adapter": "intelbras",
        "host": "192.168.0.50", "username": "admin", "password": "SenhaSecreta!", "active": False})
    assert r.status_code == 201, r.text
    cam = r.json()
    assert cam["has_password"] and "SenhaSecreta" not in r.text
    assert "***:***@" in cam["source_url"]
    row = ctx.db.one("SELECT password_enc FROM cameras WHERE id = ?", (cam["id"],))
    assert "SenhaSecreta" not in row["password_enc"]


def test_admin_ssrf_public_host_blocked(make_app):
    app, client = make_app()
    csrf = _login(client, app.state.ctx)
    est = client.get("/api/v1/admin/establishments").json()[0]["id"]
    r = client.post("/api/v1/admin/cameras", headers={"X-CSRF-Token": csrf}, json={
        "establishment_id": est, "name": "x", "area_label": "x", "adapter": "rtsp", "rtsp_url": "rtsp://8.8.8.8/live"})
    assert r.status_code == 422
    r = client.post("/api/v1/admin/cameras", headers={"X-CSRF-Token": csrf}, json={
        "establishment_id": est, "name": "x", "area_label": "x", "adapter": "rtsp", "rtsp_url": "http://192.168.0.2/"})
    assert r.status_code == 422


def test_intelbras_url_and_host_validation():
    ad = IntelbrasAdapter()
    cfg = CameraConfig(id="1", name="c", area_label="q", adapter="intelbras", host="192.168.1.108", port=554,
                       username="admin", password="p@ss:w/rd", channel=3, subtype=1)
    url = ad.live_url(cfg)
    assert url == "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.108:554/cam/realmonitor?channel=3&subtype=1"
    assert "p%40ss" not in ad.redacted_url(cfg)
    assert ad.playback_url(cfg, 0, 60) is None  # Estratégia B desabilitada por padrão
    with pytest.raises(CameraConfigError):
        ad.live_url(CameraConfig(id="1", name="c", area_label="q", adapter="intelbras", host="example.com/x"))


def test_rbac_operator_isolated_to_own_establishment(make_app):
    app, client = make_app()
    ctx = app.state.ctx
    from replayshape.db import new_id, now
    other = new_id()
    ctx.db.insert("establishments", dict(id=other, name="Outro", slug="outro", active=1, price_cents=500,
                                         currency="BRL", brand_color="#a3e635", venue_type="padel",
                                         created_at=now(), updated_at=now()))
    csrf = _login(client, ctx, email="op@x.com", role="operator", est=other)
    cams = client.get("/api/v1/admin/cameras").json()["cameras"]
    assert cams == []  # não vê câmeras da demo (outro estabelecimento)
    demo_cam = ctx.db.one("SELECT id FROM cameras LIMIT 1")["id"]
    assert client.patch(f"/api/v1/admin/cameras/{demo_cam}", json={"name": "hack"},
                        headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.patch("/api/v1/admin/settings", json={"paid_hours": 48},
                        headers={"X-CSRF-Token": csrf}).status_code == 403


def test_settings_validation(make_app):
    app, client = make_app()
    csrf = _login(client, app.state.ctx)
    h = {"X-CSRF-Token": csrf}
    assert client.patch("/api/v1/admin/settings", json={"paid_hours": 0}, headers=h).status_code == 422
    assert client.patch("/api/v1/admin/settings", json={"unknown": 1}, headers=h).status_code == 422
    r = client.patch("/api/v1/admin/settings", json={"paid_hours": 48, "unpaid_minutes": 20}, headers=h)
    assert r.status_code == 200 and r.json()["settings"]["paid_hours"] == 48


def test_rate_limit_on_trigger(make_app):
    app, client = make_app()
    codes = [client.post("/api/v1/public/spots/demo-mesa-01/replays").status_code for _ in range(8)]
    assert 429 in codes


def test_security_headers(make_app):
    app, client = make_app()
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_csv_export_neutralizes_formulas(demo):
    ctx, client = demo["ctx"], demo["client"]
    ctx.db.execute("UPDATE establishments SET name = '=HYPERLINK(\"x\")'")
    create_ready_order(client)
    csrf = _login(client, ctx)
    r = client.get("/api/v1/admin/orders/export.csv")
    assert r.status_code == 200 and "'=HYPERLINK" in r.text
