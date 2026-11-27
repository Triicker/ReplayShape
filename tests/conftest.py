"""Fixtures compartilhadas.

Os testes usam FFmpeg real. Para não depender de relógio de parede, a maioria
injeta no buffer segmentos pré-gerados com horário conhecido (fonte de teste
cuja luminância codifica o segundo do vídeo — permite medir objetivamente a
precisão do recorte sem OCR).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from replayshape.config import load_settings  # noqa: E402
from replayshape.edge.buffer import SegmentRingBuffer  # noqa: E402
from replayshape.main import create_app  # noqa: E402

FPS = 10
SEG = 2


def lum_for_second(sec: int) -> int:
    return 16 + (sec % 50) * 4


def make_pattern_segments(out_dir: Path, seconds: int) -> list[Path]:
    """Gera segmentos .ts de 2 s com luminância = f(segundo). Rápido (não é tempo real)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"nullsrc=s=160x90:r={FPS},geq=lum='16+mod(floor(T),50)*4':cb=128:cr=128",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-g", str(FPS * SEG),
         "-keyint_min", str(FPS * SEG), "-sc_threshold", "0", "-pix_fmt", "yuv420p",
         "-f", "segment", "-segment_time", str(SEG), "-reset_timestamps", "1",
         "-segment_format", "mpegts", str(out_dir / "seg_%08d.ts")],
        check=True, capture_output=True)
    return sorted(out_dir.glob("seg_*.ts"))


_PATTERN_CACHE: dict[int, list[bytes]] = {}


def pattern_bytes(seconds: int, tmp_root: Path) -> list[bytes]:
    if seconds not in _PATTERN_CACHE:
        files = make_pattern_segments(tmp_root / f"pattern_{seconds}", seconds)
        _PATTERN_CACHE[seconds] = [f.read_bytes() for f in files]
    return _PATTERN_CACHE[seconds]


def frame_luma(path: Path, from_end: bool = False) -> int:
    args = ["ffmpeg", "-v", "error"]
    if from_end:
        args += ["-sseof", "-0.25"]
    args += ["-i", str(path), "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    out = subprocess.run(args, check=True, capture_output=True).stdout
    return out[0]


def second_from_luma(v: int) -> int:
    # A saída "gray" do FFmpeg é full range: Y(16..235) -> 0..255. Desfaz a expansão.
    y = v * 219 / 255 + 16
    return round((y - 16) / 4)


class InjectedBuffer(SegmentRingBuffer):
    """Buffer "online" alimentado com segmentos prontos (sem processo FFmpeg)."""

    def __init__(self, camera_id, directory):
        super().__init__(camera_id, directory, source_factory=lambda d: None, buffer_seconds=10_000)
        self.state = "online"
        Path(directory).mkdir(parents=True, exist_ok=True)

    def fill(self, seg_bytes: list[bytes], end_at: float | None = None, skip: set[int] | None = None,
             corrupt: set[int] | None = None) -> float:
        """Grava os segmentos terminando em `end_at`. Retorna o horário de parede do segundo 0."""
        end_at = end_at or time.time() + 6  # cobre acionamentos feitos logo em seguida
        base = end_at - len(seg_bytes) * SEG
        for i, data in enumerate(seg_bytes):
            if skip and i in skip:
                continue
            p = self.dir / f"inj_{i:05d}.ts"
            p.write_bytes(b"\x00garbage" * 50 if corrupt and i in corrupt else data)
            self.ingest_segment(p, base + i * SEG, base + (i + 1) * SEG)
        return base


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    created = []

    def _make(**overrides):
        data = tmp_path / f"data{len(created)}"
        monkeypatch.setenv("RS_DATA_DIR", str(data))
        opts = dict(start_background=False, edge_enabled=False, capture_wait_timeout=2.0)
        opts.update(overrides)
        settings = load_settings(**opts)
        app = create_app(settings)
        client = TestClient(app)
        client.__enter__()
        created.append(client)
        return app, client

    yield _make
    for c in created:
        c.__exit__(None, None, None)


@pytest.fixture
def demo(make_app, tmp_path):
    """App em modo demo com a Mesa 01 alimentada por um buffer injetado de 130 s."""
    app, client = make_app()
    ctx = app.state.ctx
    ctx.store.update({"web_trigger_cooldown_seconds": 0})
    ctx.rate.allow = lambda *a, **k: True  # rate limit é testado separadamente
    cam = ctx.db.one("SELECT * FROM cameras WHERE public_code = 'demo-mesa-01'")
    buf = InjectedBuffer(cam["id"], ctx.settings.buffers_dir / cam["id"])
    ctx.buffers.buffers[cam["id"]] = buf
    base = buf.fill(pattern_bytes(130, tmp_path.parent))
    return dict(app=app, client=client, ctx=ctx, cam=cam, buf=buf, base=base)


def wait_order(client, oid, token, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        o = client.get(f"/api/v1/public/orders/{oid}", headers={"X-Order-Token": token}).json()
        if o["status"] != "processing":
            return o
        time.sleep(0.2)
    raise AssertionError("timeout aguardando processamento")


def create_ready_order(client, code="demo-mesa-01"):
    r = client.post(f"/api/v1/public/spots/{code}/replays")
    assert r.status_code == 201, r.text
    j = r.json()
    o = wait_order(client, j["order_id"], j["access_token"])
    return j["order_id"], j["access_token"], o


@pytest.fixture(autouse=True)
def _no_rate_limit_bleed():
    yield
