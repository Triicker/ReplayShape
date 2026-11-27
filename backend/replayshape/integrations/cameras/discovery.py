"""Descoberta e diagnóstico de câmeras/DVRs na rede local.

Usado pelo assistente `scripts/configurar_camera.py`. Funciona tanto para
câmeras IP quanto para DVRs/NVRs (câmeras analógicas HDCVI, como a Intelbras
VHL 1120 B, só são acessíveis pelo canal do DVR).

Nada aqui é exposto ao público: só roda por quem está no computador do Edge.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

from ...video.ffmpeg import CREATE_NO_WINDOW


@dataclass
class StreamInfo:
    ok: bool
    url_label: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    error_kind: str = ""  # AUTH | NOT_FOUND | UNREACHABLE | TIMEOUT | OTHER
    detail: str = ""


@dataclass
class GopInfo:
    keyframe_interval: float | None
    samples: int
    keyframes: int
    notes: list[str] = field(default_factory=list)


# --- rede ------------------------------------------------------------------
def local_ipv4s() -> list[str]:
    """IPs IPv4 privados deste computador. O da interface com rota para a internet
    (a rede Wi-Fi/cabo de verdade) vem primeiro; adaptadores virtuais (WSL,
    Hyper-V, VPN) aparecem depois."""
    primary = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # não envia pacotes; só escolhe a interface de saída
        primary = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    others: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            others.add(info[4][0])
    except OSError:
        pass
    ordered = ([primary] if primary else []) + sorted(others - {primary})
    return [ip for ip in ordered if ipaddress.ip_address(ip).is_private and not ip.startswith("127.")]


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_subnet(local_ip: str, ports=(554,), timeout: float = 0.4) -> list[str]:
    """Procura na rede /24 do computador aparelhos com a porta RTSP aberta."""
    net = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != local_ip]

    def check(h):
        return h if any(port_open(h, p, timeout) for p in ports) else None

    with ThreadPoolExecutor(max_workers=64) as ex:
        return [h for h in ex.map(check, hosts) if h]


# --- URLs candidatas ----------------------------------------------------------
def _auth(user: str, password: str) -> str:
    if not user:
        return ""
    return quote(user, safe="") + ":" + quote(password or "", safe="") + "@"


def candidate_urls(host: str, port: int, user: str, password: str, channel: int = 1,
                   subtype: int = 0) -> list[tuple[str, str, dict]]:
    """(rótulo, url, campos p/ cadastro). Ordem: o padrão Intelbras primeiro."""
    a = _auth(user, password)
    base = f"rtsp://{a}{host}:{port}"
    rm = f"/cam/realmonitor?channel={channel}&subtype={subtype}"
    return [
        ("Intelbras/Dahua (cam/realmonitor)", base + rm,
         {"adapter": "intelbras", "rtsp_path": None}),
        ("Intelbras (unicast)", base + f"/cam/realmonitor?channel={channel}&subtype={subtype}&unicast=true&proto=Onvif",
         {"adapter": "rtsp", "rtsp_path": rm + "&unicast=true&proto=Onvif"}),
        ("Genérico (/live)", base + "/live", {"adapter": "rtsp", "rtsp_path": "/live"}),
        ("Genérico (/)", base + "/", {"adapter": "rtsp", "rtsp_path": "/"}),
    ]


def redact(text: str, password: str | None) -> str:
    t = re.sub(r"(rtsps?://)[^@/\s]+@", r"\1***@", text or "")
    if password:
        t = t.replace(password, "***").replace(quote(password, safe=""), "***")
    return t


def _classify(stderr: str) -> str:
    s = stderr.lower()
    if "401" in s or "unauthorized" in s:
        return "AUTH"
    if "404" in s or "not found" in s:
        return "NOT_FOUND"
    if "refused" in s or "no route" in s or "unreachable" in s or "10061" in s:
        return "UNREACHABLE"
    if "timed out" in s or "timeout" in s:
        return "TIMEOUT"
    return "OTHER"


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None


def probe_stream(ffprobe: str, url: str, label: str = "", password: str | None = None,
                 timeout: float = 12) -> StreamInfo:
    cp = _run([ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "6000000",
               "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
               "-of", "default=nw=1", url], timeout)
    if cp is None:
        return StreamInfo(False, label, error_kind="TIMEOUT", detail="sem resposta")
    out = cp.stdout.decode(errors="replace")
    err = cp.stderr.decode(errors="replace")
    if cp.returncode != 0 or "codec_name=" not in out:
        return StreamInfo(False, label, error_kind=_classify(err), detail=redact(err.strip()[-200:], password))
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def fps(v):
        n, _, d = (v or "0/1").partition("/")
        try:
            return float(n) / float(d or 1) if float(d or 1) else 0.0
        except ValueError:
            return 0.0

    return StreamInfo(True, label, codec=kv.get("codec_name", ""), width=int(kv.get("width") or 0),
                      height=int(kv.get("height") or 0),
                      fps=round(fps(kv.get("avg_frame_rate")) or fps(kv.get("r_frame_rate")), 2))


def measure_gop(ffprobe: str, url: str, seconds: int = 8, timeout: float = 25) -> GopInfo:
    """Mede o intervalo entre keyframes (I-frames) lendo alguns segundos do stream."""
    cp = _run([ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "6000000",
               "-select_streams", "v:0", "-read_intervals", f"%+{seconds}",
               "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", url], timeout)
    if cp is None or cp.returncode != 0:
        return GopInfo(None, 0, 0, ["não foi possível medir os keyframes"])
    times = []
    total = 0
    for line in cp.stdout.decode(errors="replace").splitlines():
        parts = line.split(",")
        if len(parts) < 2:
            continue
        total += 1
        if "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    notes = []
    if len(times) < 2:
        notes.append(f"apenas {len(times)} keyframe(s) em {seconds}s: intervalo longo (H.264+/H.265+ ligado?)")
        return GopInfo(None if not times else float(seconds), total, len(times), notes)
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    interval = round(max(gaps), 2) if gaps else None
    return GopInfo(interval, total, len(times), notes)


def snapshot(ffmpeg: str, url: str, out_path: str, timeout: float = 15) -> bool:
    cp = _run([ffmpeg, "-v", "error", "-y", "-rtsp_transport", "tcp", "-timeout", "6000000", "-i", url,
               "-frames:v", "1", "-q:v", "3", out_path], timeout)
    return cp is not None and cp.returncode == 0


def find_working_url(ffprobe: str, host: str, port: int, user: str, password: str, channel: int = 1,
                     subtype: int = 0) -> tuple[StreamInfo, tuple[str, str, dict] | None, list[StreamInfo]]:
    """Testa os formatos de URL em ordem. Para cedo em erro de senha (evita bloqueio do aparelho)."""
    tried = []
    for cand in candidate_urls(host, port, user, password, channel, subtype):
        info = probe_stream(ffprobe, cand[1], cand[0], password)
        tried.append(info)
        if info.ok:
            return info, cand, tried
        if info.error_kind in ("AUTH", "UNREACHABLE"):
            break
    return (tried[-1] if tried else StreamInfo(False)), None, tried


def scan_channels(ffprobe: str, host: str, port: int, user: str, password: str, max_channels: int = 16,
                  subtype: int = 1) -> dict[int, StreamInfo]:
    """Descobre quais canais de um DVR/NVR têm vídeo (usa o stream extra, mais leve)."""
    def one(ch):
        url = candidate_urls(host, port, user, password, ch, subtype)[0][1]
        return ch, probe_stream(ffprobe, url, f"canal {ch}", password, timeout=10)

    with ThreadPoolExecutor(max_workers=4) as ex:  # poucos em paralelo: DVRs limitam conexões
        return dict(ex.map(one, range(1, max_channels + 1)))
