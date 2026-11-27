"""Inicia o Replay Shape em MODO PILOTO (câmera real + Pix de teste).

    python scripts/iniciar_piloto.py              # pergunta como acessar
    python scripts/iniciar_piloto.py --local      # só rede Wi-Fi do local
    python scripts/iniciar_piloto.py --internet   # link HTTPS temporário (Cloudflare Quick Tunnel)
    python scripts/iniciar_piloto.py --repetir    # usa a última escolha (início automático)

* Dados ficam em data/piloto (separados da demonstração).
* Pagamento: Pix SIMULADO, a menos que o .env configure o Mercado Pago.
* Mantém o computador acordado enquanto estiver rodando (Windows).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
PORT = 8000


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


def keep_awake(on: bool) -> None:
    """Impede suspensão do Windows enquanto o servidor roda (a tela pode apagar)."""
    if os.name != "nt":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def find_cloudflared() -> str | None:
    p = shutil.which("cloudflared")
    if p:
        return p
    for c in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe")):
        if Path(c).exists():
            return c
    return None


def start_quick_tunnel(exe: str) -> tuple[subprocess.Popen, str | None]:
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    found: list[str] = []

    def reader():
        for raw in iter(proc.stdout.readline, b""):
            m = re.search(rb"https://[a-z0-9-]+\.trycloudflare\.com", raw)
            if m and not found:
                found.append(m.group(0).decode())

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + 40
    while time.time() < deadline and not found and proc.poll() is None:
        time.sleep(0.3)
    return proc, (found[0] if found else None)


def print_qr(url: str) -> None:
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.make(fit=True)
        q.print_ascii(invert=True)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--local", action="store_true")
    g.add_argument("--internet", action="store_true")
    g.add_argument("--repetir", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("RS_DATA_DIR", str(ROOT / "data" / "piloto"))
    os.environ.setdefault("RS_DEMO_MODE", "false")
    os.environ.setdefault("RS_ENV", "development")
    data_dir = Path(os.environ["RS_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = data_dir / "piloto.json"
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}

    print("=" * 62)
    print(" REPLAY SHAPE — MODO PILOTO")
    print("=" * 62)
    if not shutil.which("ffmpeg") and not os.environ.get("RS_FFMPEG_BIN"):
        print("[XX] FFmpeg não encontrado. Rode instalar.bat (ou: winget install Gyan.FFmpeg) e abra de novo.")
        return 1

    from replayshape.config import load_settings
    from replayshape.db import Database
    from replayshape.integrations.cameras.discovery import local_ipv4s

    pre = load_settings(start_background=False)
    db = Database(pre.db_path)
    db.migrate()
    cams = db.all("SELECT c.public_code, c.area_label, e.name est FROM cameras c JOIN establishments e "
                  "ON e.id = c.establishment_id WHERE c.active = 1 AND e.active = 1 ORDER BY c.area_label")
    db.close()
    if not cams:
        print("[XX] Nenhuma câmera cadastrada. Rode configurar_camera.bat primeiro.")
        return 1

    if args.internet:
        internet = True
    elif args.local:
        internet = False
    elif args.repetir:
        internet = bool(prefs.get("internet"))
    else:
        print("\nComo o celular vai acessar?")
        print("  1) Pelo Wi-Fi do local (mais simples)")
        print("  2) Pela internet/4G com link HTTPS temporário (permite compartilhar o vídeo direto)")
        internet = (input("Escolha [1]: ").strip() == "2")
    prefs["internet"] = internet
    prefs_file.write_text(json.dumps(prefs), encoding="utf-8")

    tunnel_proc = None
    lan = local_ipv4s()
    base = f"http://{lan[0]}:{PORT}" if lan else f"http://localhost:{PORT}"
    if internet:
        exe = find_cloudflared()
        if not exe:
            print("[XX] cloudflared não encontrado. Instale com:  winget install Cloudflare.cloudflared")
            print("     (depois feche e abra esta janela). Seguindo só com a rede local.")
        else:
            print("Abrindo link HTTPS temporário pela Cloudflare (até 40 s)...")
            tunnel_proc, url = start_quick_tunnel(exe)
            if url:
                base = url
            else:
                print("[!!] Não consegui obter o link. Seguindo só com a rede local.")
    os.environ["RS_PUBLIC_BASE_URL"] = base

    print("\n" + "-" * 62)
    for c in cams:
        url = f"{base}/m/{c['public_code']}"
        print(f" {c['est']} · {c['area_label']}\n   {url}")
        print_qr(url)
    print(f" Painel admin: {base}/admin   (neste PC: http://localhost:{PORT}/admin)")
    pw = data_dir / "ADMIN_INITIAL_PASSWORD.txt"
    print(f" Senha inicial do admin: arquivo {pw}" if pw.exists() or not (data_dir / "replayshape.db").exists()
          else " Admin: use a senha que você já definiu.")
    if not internet:
        print(" O celular precisa estar no MESMO Wi-Fi deste computador.")
        print(" Se o Windows perguntar sobre o Firewall, permita em 'Redes privadas'.")
    if base.startswith("https://") and "trycloudflare" in base:
        print(" O link muda a cada vez que o piloto é iniciado (gere o QR de novo).")
    print(" Pagamento: " + ("Mercado Pago" if os.environ.get("RS_PAYMENT_PROVIDER") == "mercadopago"
                            else "Pix de TESTE (nenhum valor é cobrado)"))
    print(" Aguarde ~60 s após iniciar: o buffer precisa acumular o primeiro minuto.")
    print(" Para encerrar: feche esta janela ou Ctrl+C.")
    print("-" * 62 + "\n")

    import logging

    import uvicorn

    from replayshape.main import create_app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    keep_awake(True)
    try:
        uvicorn.run(create_app(load_settings()), host="0.0.0.0", port=PORT, proxy_headers=True, log_level="warning")
    finally:
        keep_awake(False)
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
