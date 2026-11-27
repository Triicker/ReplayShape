"""Assistente de configuração da câmera para o PILOTO.

Funciona com:
  * DVR/NVR Intelbras (ex.: câmeras analógicas HDCVI como a VHL 1120 B) — escolhe o canal;
  * câmera IP Intelbras ou RTSP genérica.

Uso (de preferência pelo configurar_camera.bat):
    python scripts/configurar_camera.py [--ip 192.168.1.108] [--usuario admin] [--canal 3]

A senha é pedida sem aparecer na tela e fica criptografada no banco local.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("RS_DATA_DIR", str(ROOT / "data" / "piloto"))
os.environ.setdefault("RS_DEMO_MODE", "false")

from replayshape.config import load_settings  # noqa: E402
from replayshape.db import Database, new_id, now  # noqa: E402
from replayshape.integrations.cameras import discovery as dz  # noqa: E402
from replayshape.security import SecretBox  # noqa: E402

OK, WARN, BAD = "[OK]", "[!!]", "[XX]"


def ask(prompt: str, default: str | None = None) -> str:
    sfx = f" [{default}]" if default else ""
    try:
        v = input(f"{prompt}{sfx}: ").strip()
    except EOFError:
        v = ""
    return v or (default or "")


def ask_int(prompt: str, default: int) -> int:
    while True:
        v = ask(prompt, str(default))
        if v.isdigit():
            return int(v)
        print("  Digite apenas números.")


def yes(prompt: str, default: bool = True) -> bool:
    v = ask(prompt + (" (S/n)" if default else " (s/N)")).lower()
    return default if not v else v.startswith("s")


def open_folder(p: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(p))  # noqa: S606 — abre o Explorer na pasta das fotos
    except OSError:
        pass


def choose_host(args) -> str:
    if args.ip:
        return args.ip
    ips = dz.local_ipv4s()
    print(f"\nEste computador: {', '.join(ips) or 'IP não identificado'}")
    found: list[str] = []
    if ips and yes("Procurar DVR/câmeras na rede automaticamente?"):
        for ip in ips:
            print(f"  Procurando aparelhos com RTSP (porta 554) em {ip.rsplit('.', 1)[0]}.0/24 ...")
            found += dz.scan_subnet(ip)
    if found:
        print("\nAparelhos encontrados:")
        for i, h in enumerate(found, 1):
            print(f"  {i}) {h}")
        v = ask("Número da lista ou IP do DVR/câmera", "1")
        if v.isdigit() and 1 <= int(v) <= len(found):
            return found[int(v) - 1]
        return v
    if ips:
        print("  Nenhum aparelho com a porta 554 aberta foi encontrado.")
    print("  Dica: o IP do DVR aparece no menu Rede do próprio DVR (no monitor dele) ou no app iSIC.")
    return ask("IP do DVR ou da câmera")


def main() -> int:
    ap = argparse.ArgumentParser(description="Configura a câmera do piloto Replay Shape")
    ap.add_argument("--ip")
    ap.add_argument("--porta", type=int, default=554)
    ap.add_argument("--usuario")
    ap.add_argument("--canal", type=int)
    ap.add_argument("--max-canais", type=int, default=16)
    args = ap.parse_args()

    settings = load_settings(start_background=False)
    print("=" * 62)
    print(" REPLAY SHAPE — configuração da câmera do piloto")
    print("=" * 62)

    host = choose_host(args)
    if not re.fullmatch(r"[A-Za-z0-9.\-]+", host or ""):
        print(f"{BAD} IP inválido.")
        return 1
    port = args.porta
    if not dz.port_open(host, port, timeout=2):
        print(f"{BAD} {host}:{port} não respondeu. Confira se o PC está na mesma rede do DVR/câmera e o IP.")
        return 1
    print(f"{OK} {host}:{port} respondeu.")

    user = args.usuario or ask("Usuário do DVR/câmera", "admin")
    password = os.environ.get("RS_CAMERA_PASSWORD") or ""
    info = cand = None
    channel = args.canal or 1
    for attempt in range(3):
        if not password:
            password = getpass.getpass("Senha (não aparece enquanto digita): ")
        print("Testando o acesso ao vídeo...")
        info, cand, tried = dz.find_working_url(settings.ffprobe_bin, host, port, user, password, channel, 0)
        if info.ok:
            break
        if info.error_kind == "AUTH":
            print(f"{BAD} Usuário ou senha recusados. (Atenção: o aparelho pode bloquear após várias tentativas.)")
            password = ""
            if attempt < 2 and yes("Tentar outra senha?"):
                continue
            return 1
        print(f"{BAD} Não foi possível abrir o vídeo do canal {channel}.")
        for t in tried:
            print(f"     - {t.url_label}: {t.error_kind} {t.detail[:120]}")
        if args.canal is None and info.error_kind in ("NOT_FOUND", "OTHER", "TIMEOUT"):
            break  # pode ser DVR sem câmera no canal 1: a varredura abaixo resolve
        return 1

    # --- DVR? descobre os canais e tira foto de cada um -----------------------------
    snaps = settings.data_dir / "snapshots"
    if args.canal is None and yes("\nÉ um DVR/NVR (várias câmeras ligadas nele)?", default=True):
        print(f"Verificando os canais 1 a {args.max_canais} (pode levar até 1 minuto)...")
        chans = dz.scan_channels(settings.ffprobe_bin, host, port, user, password, args.max_canais)
        live = [c for c, i in sorted(chans.items()) if i.ok]
        if any(i.error_kind == "AUTH" for i in chans.values()) and not live:
            print(f"{BAD} Senha recusada nos canais.")
            return 1
        if not live:
            print(f"{BAD} Nenhum canal respondeu. Confira no DVR se o RTSP está habilitado (Rede > Conexão/RTSP).")
            return 1
        snaps.mkdir(parents=True, exist_ok=True)
        print(f"{OK} Canais com vídeo: {', '.join(map(str, live))}")
        for c in live:
            url = dz.candidate_urls(host, port, user, password, c, 1)[0][1]
            if dz.snapshot(settings.ffmpeg_bin, url, str(snaps / f"canal-{c:02d}.jpg")):
                print(f"     foto do canal {c}: {snaps / f'canal-{c:02d}.jpg'}")
        open_folder(snaps)
        print("Veja as fotos e escolha a câmera que filma a quadra/mesa.")
        while True:
            channel = ask_int("Número do canal da quadra", live[0])
            if channel in live:
                break
            print(f"  Escolha um destes: {live}")
        info, cand, _ = dz.find_working_url(settings.ffprobe_bin, host, port, user, password, channel, 0)
        if not info.ok:
            print(f"{BAD} O stream principal do canal {channel} não abriu ({info.error_kind}).")
            return 1
    elif not info or not info.ok:
        return 1

    # --- diagnóstico do stream escolhido ---------------------------------------------
    print(f"\n{OK} Vídeo encontrado: {info.url_label}")
    print(f"     {info.width}x{info.height}, {info.fps:g} fps, codec {info.codec}")
    print("Medindo o intervalo entre keyframes (8 s)...")
    gop = dz.measure_gop(settings.ffprobe_bin, cand[1])
    if gop.keyframe_interval is None:
        print(f"{WARN} Não foi possível medir: {'; '.join(gop.notes)}")
    elif gop.keyframe_interval <= 2.2:
        print(f"{OK} Keyframe a cada {gop.keyframe_interval:g} s (ideal: 1–2 s).")
    else:
        print(f"{WARN} Keyframe a cada {gop.keyframe_interval:g} s. Funciona, mas o replay demora mais para ficar pronto.")
        print("     No DVR: Configurações > Câmera/Codificação > stream principal:")
        print("     'Intervalo de I-frame' = valor do FPS e desligue H.264+/H.265+ (compressão inteligente).")
    if info.codec == "hevc":
        print(f"{WARN} O DVR envia H.265: funciona (o replay é convertido para H.264), mas usa mais CPU no recorte.")
        print("     Se puder, mude o stream principal para H.264.")
    if info.height and info.height < 480:
        print(f"{WARN} Resolução baixa ({info.height}p). Use o stream principal (subtype 0).")

    # --- cadastro -----------------------------------------------------------------------
    print("\nDados que aparecem para o cliente:")
    est_name = ask("Nome do estabelecimento", "Meu Estabelecimento")
    area = ask("Nome da quadra/mesa", "Quadra 01")
    price = ask("Preço do replay em R$", "5,00").replace(",", ".")
    try:
        price_cents = int(round(float(price) * 100))
    except ValueError:
        price_cents = 500
    duration = ask_int("Duração do replay em segundos", 60)

    db = Database(settings.db_path)
    db.migrate()
    box = SecretBox(settings.secret_key)
    t = now()
    slug = re.sub(r"[^a-z0-9]+", "-", est_name.lower()).strip("-")[:60] or "estabelecimento"
    est = db.one("SELECT * FROM establishments WHERE slug = ?", (slug,))
    with db.tx():
        if not est:
            est = dict(id=new_id(), name=est_name, slug=slug, active=1, price_cents=price_cents, currency="BRL",
                       brand_color="#a3e635", venue_type="outro",
                       privacy_notice="Este local é filmado para gerar replays sob demanda. As imagens ficam "
                                      "guardadas por cerca de 2 minutos e são apagadas automaticamente.",
                       created_at=t, updated_at=t)
            db.insert("establishments", est)
        fields = cand[2]
        vals = dict(establishment_id=est["id"], name=f"Canal {channel}", area_label=area, adapter=fields["adapter"],
                    manufacturer="Intelbras", model="DVR/câmera (piloto)", host=host, port=port, username=user,
                    password_enc=box.encrypt(password), rtsp_path=fields["rtsp_path"], channel=channel, subtype=0,
                    resolution=f"{info.width}x{info.height}" if info.width else None,
                    fps=int(round(info.fps)) if info.fps else None, replay_seconds=duration,
                    price_cents=price_cents, active=1, status="unknown", updated_at=t)
        existing = db.one("SELECT id, public_code FROM cameras WHERE host = ? AND channel = ?", (host, channel))
        if existing:
            db.update("cameras", existing["id"], vals)
            code = existing["public_code"]
        else:
            code = re.sub(r"[^a-z0-9]+", "-", area.lower()).strip("-")[:20] + "-" + new_id()[:6]
            db.insert("cameras", dict(id=new_id(), public_code=code, protocol="rtsp", created_at=t, **vals))
    print(f"\n{OK} Câmera cadastrada: {est_name} · {area} (canal {channel})")
    print(f"     Página do cliente: /m/{code}")
    print("\nPróximo passo: execute iniciar_piloto.bat")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
