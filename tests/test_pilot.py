"""Partes do piloto que não dependem de equipamento: URLs, diagnóstico e buffer com GOP longo."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from conftest import InjectedBuffer

from replayshape.integrations.cameras import discovery as dz

ROOT = Path(__file__).resolve().parents[1]


def test_candidate_urls_dvr_channel_and_escaping():
    urls = dz.candidate_urls("192.168.1.50", 554, "admin", "s3nh@:/", channel=4, subtype=0)
    label, url, fields = urls[0]
    assert url == "rtsp://admin:s3nh%40%3A%2F@192.168.1.50:554/cam/realmonitor?channel=4&subtype=0"
    assert fields["adapter"] == "intelbras"
    assert all("channel=4" in u for _, u, _ in urls[:2])


def test_error_classification_and_redaction():
    assert dz._classify("method DESCRIBE failed: 401 Unauthorized") == "AUTH"
    assert dz._classify("method DESCRIBE failed: 404 Not Found") == "NOT_FOUND"
    assert dz._classify("Connection refused") == "UNREACHABLE"
    s = dz.redact("erro em rtsp://admin:segredo@10.0.0.2:554/x senha segredo", "segredo")
    assert "segredo" not in s and "***@" in s


def test_probe_unreachable_host_fails_fast():
    t0 = time.time()
    info = dz.probe_stream("ffprobe", "rtsp://127.0.0.1:9/cam/realmonitor?channel=1&subtype=0", timeout=10)
    assert not info.ok and info.error_kind in ("UNREACHABLE", "TIMEOUT", "OTHER")
    assert time.time() - t0 < 11


def test_wizard_reports_unreachable_device():
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" / "configurar_camera.py"), "--ip", "127.0.0.1",
                         "--porta", "9"], capture_output=True, text=True, timeout=60, input="")
    assert cp.returncode == 1
    assert "não respondeu" in cp.stdout


def test_buffer_waits_longer_with_long_gop(tmp_path):
    """DVR com keyframe a cada 6 s: a espera pelo segmento final se amplia automaticamente."""
    buf = InjectedBuffer("cam", tmp_path / "b")
    buf._recent_durations.extend([6.0, 6.0])
    assert buf.longest_recent_segment == 6.0
    # Último segmento termina 3 s no futuro: a espera padrão (1 s) seria insuficiente.
    seg = tmp_path / "b" / "s.ts"
    seg.write_bytes(b"x")
    now = time.time()
    buf.ingest_segment(seg, now - 60, now - 6)

    import threading

    def late():
        time.sleep(3)
        seg2 = tmp_path / "b" / "s2.ts"
        seg2.write_bytes(b"x")
        buf.ingest_segment(seg2, now - 6, now + 0.5)

    threading.Thread(target=late).start()
    segs = buf.acquire_window(now - 50, now, wait_timeout=1.0)
    assert len(segs) == 2
