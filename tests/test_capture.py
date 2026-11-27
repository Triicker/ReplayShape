"""Captura e processamento: precisão do recorte, integridade do MP4 e falhas."""
from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path

from conftest import (InjectedBuffer, create_ready_order, frame_luma, pattern_bytes, second_from_luma,
                      wait_order)

from replayshape.video import ffmpeg as ff


def _replay_path(ctx, order_id):
    o = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    rf = ctx.db.one("SELECT * FROM replay_files WHERE replay_request_id = ?", (o["replay_request_id"],))
    return Path(rf["local_path"]), rf, ctx.db.one("SELECT * FROM replay_requests WHERE id = ?",
                                                    (o["replay_request_id"],))


def test_cuts_exactly_the_last_60_seconds(demo):
    """Recorte real de 60 s: o 1º e o último quadro correspondem ao intervalo do acionamento."""
    ctx, client, base = demo["ctx"], demo["client"], demo["base"]
    oid, tok, o = create_ready_order(client)
    assert o["status"] == "awaiting_payment", o
    path, rf, req = _replay_path(ctx, oid)
    assert req["window_end"] == req["trigger_at"]
    assert req["window_end"] - req["window_start"] == 60

    info = ff.probe(ctx.settings.ffprobe_bin, path)
    assert info.codec == "h264"
    assert abs(info.duration - 60) <= 0.25
    assert not info.has_audio
    assert rf["sha256"] == ff.sha256_file(path)

    exp_first = int(req["window_start"] - base)
    exp_last = int(req["window_end"] - base - 0.3)
    got_first = second_from_luma(frame_luma(path))
    got_last = second_from_luma(frame_luma(path, from_end=True))
    # Tolerância de 1 s (fronteira de segundo + quantização do encoder).
    assert abs(got_first - (exp_first % 50)) <= 1, (got_first, exp_first)
    assert abs(got_last - (exp_last % 50)) <= 1, (got_last, exp_last)


def test_mp4_is_web_compatible_faststart(demo):
    oid, tok, o = create_ready_order(demo["client"])
    path, _, _ = _replay_path(demo["ctx"], oid)
    head = path.read_bytes()[:4096]
    # faststart: o átomo 'moov' vem antes de 'mdat' (reprodução progressiva no Safari/iOS).
    assert head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))
    info = ff.probe(demo["ctx"].settings.ffprobe_bin, path)
    assert info.format_name.startswith("mov,mp4")


def test_insufficient_buffer_fails_without_charge(make_app, tmp_path):
    app, client = make_app()
    ctx = app.state.ctx
    ctx.rate.allow = lambda *a, **k: True
    cam = ctx.db.one("SELECT * FROM cameras WHERE public_code = 'demo-mesa-01'")
    buf = InjectedBuffer(cam["id"], ctx.settings.buffers_dir / cam["id"])
    ctx.buffers.buffers[cam["id"]] = buf
    buf.fill(pattern_bytes(130, tmp_path.parent)[:15])  # só 30 s gravados
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays").json()
    o = wait_order(client, r["order_id"], r["access_token"])
    assert o["status"] == "failed"
    assert o["replay"]["error_code"] == "INSUFFICIENT_BUFFER"
    pay = client.post(f"/api/v1/public/orders/{r['order_id']}/payment", headers={"X-Order-Token": r["access_token"]})
    assert pay.status_code == 409  # não é possível cobrar
    assert ctx.db.one("SELECT COUNT(*) n FROM payments")["n"] == 0


def test_gap_in_recording_is_detected(make_app, tmp_path):
    app, client = make_app()
    ctx = app.state.ctx
    ctx.rate.allow = lambda *a, **k: True
    cam = ctx.db.one("SELECT * FROM cameras WHERE public_code = 'demo-mesa-01'")
    buf = InjectedBuffer(cam["id"], ctx.settings.buffers_dir / cam["id"])
    ctx.buffers.buffers[cam["id"]] = buf
    buf.fill(pattern_bytes(130, tmp_path.parent), skip={60})  # falta 2 s no meio da janela
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays").json()
    o = wait_order(client, r["order_id"], r["access_token"])
    assert o["status"] == "failed" and o["replay"]["error_code"] == "BUFFER_GAP"


def test_corrupted_segment_fails_cleanly(make_app, tmp_path):
    app, client = make_app()
    ctx = app.state.ctx
    ctx.rate.allow = lambda *a, **k: True
    cam = ctx.db.one("SELECT * FROM cameras WHERE public_code = 'demo-mesa-01'")
    buf = InjectedBuffer(cam["id"], ctx.settings.buffers_dir / cam["id"])
    ctx.buffers.buffers[cam["id"]] = buf
    buf.fill(pattern_bytes(130, tmp_path.parent), corrupt=set(range(35, 65)))
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays").json()
    o = wait_order(client, r["order_id"], r["access_token"])
    assert o["status"] == "failed"
    assert o["replay"]["error_code"] in ("PROCESSING_FAILED", "BAD_DURATION", "OUTPUT_MISSING", "PROBE_FAILED")
    req = ctx.db.one("SELECT * FROM replay_requests WHERE id = (SELECT replay_request_id FROM orders WHERE id = ?)",
                     (r["order_id"],))
    assert not (ctx.settings.replays_dir / req["id"]).exists()  # arquivos temporários removidos


def test_camera_offline_rejects_without_order(make_app):
    app, client = make_app()
    r = client.post("/api/v1/public/spots/demo-mesa-01/replays")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "CAMERA_OFFLINE"
    assert app.state.ctx.db.one("SELECT COUNT(*) n FROM orders")["n"] == 0


def test_correct_camera_is_used(demo, tmp_path):
    """Cada QR/código identifica a câmera certa — o pedido fica associado à câmera acionada."""
    ctx, client = demo["ctx"], demo["client"]
    cam2 = ctx.db.one("SELECT * FROM cameras WHERE public_code = 'demo-mesa-02'")
    buf2 = InjectedBuffer(cam2["id"], ctx.settings.buffers_dir / cam2["id"])
    ctx.buffers.buffers[cam2["id"]] = buf2
    buf2.fill(pattern_bytes(130, tmp_path.parent))
    o1, _, _ = create_ready_order(client, "demo-mesa-01")
    o2, _, _ = create_ready_order(client, "demo-mesa-02")
    rows = {r["id"]: r["camera_id"] for r in ctx.db.all("SELECT id, camera_id FROM orders")}
    assert rows[o1] == demo["cam"]["id"] and rows[o2] == cam2["id"]


def test_simultaneous_requests(demo):
    client = demo["client"]
    with cf.ThreadPoolExecutor(4) as ex:
        results = list(ex.map(lambda _: client.post("/api/v1/public/spots/demo-mesa-01/replays").json(), range(4)))
    orders = [wait_order(client, r["order_id"], r["access_token"], timeout=90) for r in results]
    assert all(o["status"] == "awaiting_payment" for o in orders), [o["replay"] for o in orders]
    assert len({r["short_code"] for r in results}) == 4
    # Segmentos foram liberados ao final (locks zerados) e podem ser apagados.
    assert demo["buf"].snapshot()["locked"] == 0


def test_trigger_time_not_payment_time(demo):
    """O intervalo é fixado no acionamento; pagar depois não altera o recorte."""
    ctx, client = demo["ctx"], demo["client"]
    oid, tok, o = create_ready_order(client)
    _, rf, req = _replay_path(ctx, oid)
    client.post(f"/api/v1/public/orders/{oid}/payment", headers={"X-Order-Token": tok})
    client.post(f"/api/v1/public/orders/{oid}/demo-payment", headers={"X-Order-Token": tok}, json={"result": "approved"})
    _, rf2, req2 = _replay_path(ctx, oid)
    assert req2["window_start"] == req["window_start"] and rf2["sha256"] == rf["sha256"]


def test_watermark_with_hostile_text(demo):
    """Nome com caracteres especiais vai por arquivo (textfile) — sem injeção no filtergraph."""
    ctx, client = demo["ctx"], demo["client"]
    if not ctx.settings.font_file:
        import pytest
        pytest.skip("fonte indisponível")
    ctx.db.execute("UPDATE establishments SET name = ?", ("Bar'; rm -rf / ,drawtext=text=x:[a]",))
    ctx.db.execute("UPDATE cameras SET watermark = 1")
    oid, tok, o = create_ready_order(client)
    assert o["status"] == "awaiting_payment", o["replay"]


def test_quality_profile_economy_480p(demo, tmp_path):
    ctx, client = demo["ctx"], demo["client"]
    ctx.store.update({"quality_profile": "economy"})
    oid, _, o = create_ready_order(client)
    path, _, _ = _replay_path(ctx, oid)
    assert ff.probe(ctx.settings.ffprobe_bin, path).height <= 480
