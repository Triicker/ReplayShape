"""Adaptadores de integração (sem credenciais reais) e buffer ao vivo com FFmpeg."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import subprocess
import time

import httpx
import pytest

from replayshape.edge.buffer import BufferError, SegmentRingBuffer
from replayshape.integrations.cameras.base import SourceSpec
from replayshape.integrations.payments.base import WebhookRejected
from replayshape.integrations.payments.mercadopago import MercadoPagoProvider
from replayshape.integrations.storage.base import S3Storage


# --- S3 / R2 ---------------------------------------------------------------------
def test_s3_presign_matches_aws_reference_vector():
    """Vetor oficial da documentação AWS SigV4 (query string auth, exemplo 'test.txt')."""
    s3 = S3Storage("https://s3.amazonaws.com", "us-east-1", "examplebucket", "AKIAIOSFODNN7EXAMPLE",
                   "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", path_style=False)
    url = s3.presign("GET", "test.txt", 86400, now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
    assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?")
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


# --- Mercado Pago (API simulada com httpx.MockTransport) ----------------------------
def _mp(handler, secret="whsec"):
    return MercadoPagoProvider("TEST-token", secret, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_mp_create_pix_charge_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": 123, "status": "pending", "point_of_interaction": {
            "transaction_data": {"qr_code": "00020101...", "qr_code_base64": "iVBOR", "ticket_url": "https://x"}}})

    ch = _mp(handler).create_charge(order_id="o1", amount_cents=500, currency="BRL", description="d",
                                    idempotency_key="o1:1", expires_at=time.time() + 1800,
                                    notification_url="https://exemplo.com/api/v1/webhooks/mercadopago")
    assert seen["headers"]["X-Idempotency-Key"] == "o1:1"
    assert seen["headers"]["Authorization"] == "Bearer TEST-token"
    assert seen["body"]["payment_method_id"] == "pix" and seen["body"]["transaction_amount"] == 5.0
    assert seen["body"]["external_reference"] == "o1"
    assert ch.provider_payment_id == "123" and ch.pix_copy_paste == "00020101..."


def test_mp_get_payment_maps_fields():
    def handler(req):
        return httpx.Response(200, json={"id": 123, "status": "approved", "transaction_amount": 5,
                                         "currency_id": "BRL", "external_reference": "o1",
                                         "fee_details": [{"amount": 0.05}]})
    info = _mp(handler).get_payment("123")
    assert (info.status, info.amount_cents, info.currency, info.external_reference, info.fee_cents) == \
        ("approved", 500, "BRL", "o1", 5)


def test_mp_webhook_signature():
    prov = _mp(lambda r: httpx.Response(500))
    ts, rid = "1704908010", "req-abc"
    manifest = f"id:123;request-id:{rid};ts:{ts};"
    v1 = hmac.new(b"whsec", manifest.encode(), hashlib.sha256).hexdigest()
    body = json.dumps({"id": 999, "type": "payment", "data": {"id": "123"}}).encode()
    n = prov.parse_notification({"x-signature": f"ts={ts},v1={v1}", "x-request-id": rid}, {"data.id": "123"}, body)
    assert n.provider_payment_id == "123" and n.event_key == "mp:999"
    with pytest.raises(WebhookRejected):
        prov.parse_notification({"x-signature": f"ts={ts},v1={'0' * 64}", "x-request-id": rid}, {}, body)


# --- buffer ao vivo (FFmpeg real em tempo real) ---------------------------------------
def _counter_source(_wd):
    # Luminância = segundo do stream (pts): permite medir o mapeamento stream->relógio.
    return SourceSpec(
        input_args=["-re", "-f", "lavfi", "-i", "nullsrc=s=160x90:r=10,geq=lum='16+mod(floor(T),50)*4':cb=128:cr=128"],
        filtergraph=None, needs_encode=True)


@pytest.mark.slow
def test_live_buffer_timing_and_reconnect(tmp_path):
    states = []
    started = time.time()
    buf = SegmentRingBuffer("live", tmp_path / "buf", _counter_source, segment_seconds=2, buffer_seconds=12,
                            on_status=lambda c, s, d: states.append(s), fps_hint=10)
    buf.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and buf.snapshot()["seconds_buffered"] < 10:
            time.sleep(0.2)
        snap = buf.snapshot()
        assert snap["state"] == "online" and snap["seconds_buffered"] >= 10
        # Precisão do mapeamento: segundo 0 do stream deve coincidir com o início do processo (±1 s).
        first = min(buf._segments, key=lambda s: s.wall_start)
        assert abs((first.wall_start - first.rel_start) - started) < 1.5
        # Pedido de janela recente com espera pelo segmento final.
        end = time.time()
        segs = buf.acquire_window(end - 6, end, wait_timeout=6)
        assert segs[0].wall_start <= end - 6 + 1 and segs[-1].wall_end >= end - 1
        buf.release(segs)
        # Exclusão automática de segmentos antigos.
        time.sleep(3)
        assert buf.snapshot()["oldest_at"] >= time.time() - 12 - 3
        # Reconexão: derruba o FFmpeg e espera o buffer voltar sozinho.
        buf._run.proc.kill()
        deadline = time.time() + 25
        recovered = False
        while time.time() < deadline:
            if buf.restarts >= 1 and buf.state == "online":
                recovered = True
                break
            time.sleep(0.3)
        assert recovered, states
        assert "offline" in states
    finally:
        buf.stop()


def test_buffer_reports_offline_camera(tmp_path):
    def bad_source(_wd):
        return SourceSpec(input_args=["-rtsp_transport", "tcp", "-timeout", "1000000",
                                      "-i", "rtsp://127.0.0.1:9/inexistente"], filtergraph=None, needs_encode=False)
    buf = SegmentRingBuffer("off", tmp_path / "b", bad_source, buffer_seconds=10)
    buf.start()
    try:
        time.sleep(4)
        assert buf.state in ("offline", "reconnecting", "starting")
        with pytest.raises(BufferError) as e:
            buf.acquire_window(time.time() - 5, time.time(), wait_timeout=1)
        assert e.value.code == "CAMERA_OFFLINE"
    finally:
        buf.stop()


def test_ffmpeg_available():
    assert subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
