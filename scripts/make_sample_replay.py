"""Gera o replay de exemplo da demo estática usando o pipeline REAL do projeto.

Sobe a câmera simulada (mesa de sinuca), espera o buffer encher, dispara um
recorte de 60 s pelo mesmo código de produção e copia o MP4 resultante para
demo/static/assets/sample-replay.mp4 (perfil econômico 480p para ficar leve).

    python scripts/make_sample_replay.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
OUT = ROOT / "demo" / "static" / "assets" / "sample-replay.mp4"


def main() -> None:
    os.environ["RS_DATA_DIR"] = tempfile.mkdtemp(prefix="rs-sample-")
    from fastapi.testclient import TestClient

    from replayshape.config import load_settings
    from replayshape.main import create_app

    app = create_app(load_settings(start_background=False))
    with TestClient(app) as c:
        ctx = app.state.ctx
        ctx.store.update({"quality_profile": "economy", "watermark_enabled": True})
        print("Aguardando o buffer acumular 60 s de vídeo (tempo real)…")
        while True:
            s = c.get("/api/v1/public/spots/demo-mesa-01").json()
            if s["camera_status"] == "online" and s["ready_in_s"] == 0:
                break
            print(f"  buffer: {s['seconds_buffered']:.0f}s", end="\r")
            time.sleep(2)
        r = c.post("/api/v1/public/spots/demo-mesa-01/replays").json()
        while True:
            o = c.get(f"/api/v1/public/orders/{r['order_id']}", headers={"X-Order-Token": r["access_token"]}).json()
            if o["status"] != "processing":
                break
            time.sleep(0.5)
        if o["status"] != "awaiting_payment":
            raise SystemExit(f"falhou: {o['replay']}")
        rf = ctx.db.one("SELECT local_path FROM replay_files ORDER BY created_at DESC LIMIT 1")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rf["local_path"], OUT)
        print(f"\nOK: {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
