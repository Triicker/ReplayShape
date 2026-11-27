"""Gera a versão HTML estática navegável (Cloudflare Pages) em dist/pages.

    python scripts/build_static_demo.py [--sample caminho.mp4]

* Copia o frontend, troca config.js para RS_STATIC_DEMO = true (usa mock-api.js).
* Inclui um replay de exemplo GERADO PELO PIPELINE REAL do projeto
  (demo/static/assets/sample-replay.mp4) e um QR ilustrativo.
* Não contém backend: recorte real, Pix, webhooks e admin exigem o servidor.
"""
from __future__ import annotations

import argparse
import io
import shutil
from pathlib import Path

import qrcode
import qrcode.image.svg

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "pages"
SAMPLE = ROOT / "demo" / "static" / "assets" / "sample-replay.mp4"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=SAMPLE)
    args = ap.parse_args()
    if not args.sample.exists():
        raise SystemExit(f"Replay de exemplo não encontrado: {args.sample}. Gere com scripts/make_sample_replay.py")
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(ROOT / "frontend", DIST)
    (DIST / "assets" / "config.js").write_text(
        "// Build estático (Cloudflare Pages): sem backend, tudo simulado no navegador.\n"
        "window.RS_STATIC_DEMO = true;\n", encoding="utf-8")
    shutil.copy2(args.sample, DIST / "assets" / "sample-replay.mp4")
    img = qrcode.make("DEMO-REPLAYSHAPE-PIX-NAO-PAGAVEL", image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    (DIST / "assets" / "demo-qr.svg").write_bytes(buf.getvalue())
    # SPA: rotas profundas servem o index.html.
    (DIST / "_redirects").write_text("/m/*  /index.html  200\n/o/*  /index.html  200\n"
                                     "/recuperar  /index.html  200\n/privacidade  /index.html  200\n",
                                     encoding="utf-8")
    (DIST / "_headers").write_text(
        "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  X-Frame-Options: DENY\n"
        "  Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self';"
        " script-src 'self'; connect-src 'self'; frame-ancestors 'none'\n", encoding="utf-8")
    print(f"OK: {DIST}")


if __name__ == "__main__":
    main()
