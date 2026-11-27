"""Camada de adaptadores de câmera.

Cada fabricante/protocolo implementa `CameraAdapter`. O buffer circular só
precisa dos argumentos de entrada do FFmpeg; a Estratégia B (gravação
histórica no DVR/NVR) é opcional e só é usada quando `supports_playback`
estiver habilitado após validação no equipamento real.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from ...video import ffmpeg as ff


class CameraConfigError(ValueError):
    pass


@dataclass
class CameraConfig:
    id: str
    name: str
    area_label: str
    adapter: str
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    rtsp_path: str | None = None
    rtsp_url: str | None = None
    channel: int = 1
    subtype: int = 0
    fps: int | None = None
    resolution: str | None = None
    supports_playback: bool = False


@dataclass
class SourceSpec:
    input_args: list[str]
    filtergraph: str | None  # None => copiar o stream (sem recodificar)
    needs_encode: bool


def check_host_allowed(host: str, allow_public: bool) -> None:
    """Proteção contra SSRF: por padrão só aceita endereços de rede privada."""
    if not host or any(c in host for c in "/@?#\\ "):
        raise CameraConfigError("host inválido")
    try:
        infos = socket.getaddrinfo(host, None)
        addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    except (socket.gaierror, ValueError) as e:
        raise CameraConfigError(f"host não resolvido: {host}") from e
    if allow_public:
        return
    for a in addrs:
        if not (a.is_private or a.is_loopback) or a.is_link_local:
            raise CameraConfigError(
                "por segurança, apenas câmeras na rede local (IPs privados) são permitidas"
            )


class CameraAdapter:
    name = "base"
    label = "Base"

    def __init__(self, allow_public_hosts: bool = False):
        self.allow_public_hosts = allow_public_hosts

    def live_url(self, cam: CameraConfig) -> str:
        raise NotImplementedError

    def source(self, cam: CameraConfig, workdir: Path, font_file: str | None) -> SourceSpec:
        url = self.live_url(cam)
        return SourceSpec(
            input_args=["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url],
            filtergraph=None,
            needs_encode=False,
        )

    def redacted_url(self, cam: CameraConfig) -> str:
        try:
            u = urlsplit(self.live_url(cam))
        except Exception:
            return "(inválida)"
        netloc = u.hostname or ""
        if u.port:
            netloc += f":{u.port}"
        if u.username:
            netloc = "***:***@" + netloc
        return urlunsplit((u.scheme, netloc, u.path, u.query, ""))

    def test_connectivity(self, cam: CameraConfig, ffprobe: str, timeout: float = 15) -> dict:
        url = self.live_url(cam)
        t0 = time.monotonic()
        cp = ff.run(
            [ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
             "-print_format", "json", "-show_streams", "-select_streams", "v:0", url],
            timeout=timeout,
        )
        ok = cp.returncode == 0
        detail = "" if ok else cp.stderr.decode(errors="replace")[-300:].replace(cam.password or "\0", "***")
        return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail,
                "url": self.redacted_url(cam)}

    # Estratégia B --------------------------------------------------------
    def playback_url(self, cam: CameraConfig, start: float, end: float) -> str | None:
        return None


class DemoAdapter(CameraAdapter):
    """Câmera simulada: mesa de sinuca sintética com relógio de parede gravado."""

    name = "demo"
    label = "Câmera simulada (demonstração)"

    def live_url(self, cam: CameraConfig) -> str:
        return "lavfi://demo"

    def source(self, cam: CameraConfig, workdir: Path, font_file: str | None) -> SourceSpec:
        w, h = 1280, 720
        if cam.resolution and "x" in cam.resolution:
            try:
                w, h = (int(x) for x in cam.resolution.lower().split("x"))
            except ValueError:
                pass
        fps = cam.fps or 25
        label = workdir / "label.txt"
        label.write_text(f"{cam.area_label.upper()} - CAMERA DEMO", encoding="utf-8")
        return SourceSpec(
            input_args=ff.demo_source_input_args(fps, w, h),
            filtergraph=ff.demo_source_filtergraph(label, font_file, w, h),
            needs_encode=True,
        )

    def redacted_url(self, cam: CameraConfig) -> str:
        return "simulada://mesa-de-sinuca"

    def test_connectivity(self, cam: CameraConfig, ffprobe: str, timeout: float = 15) -> dict:
        return {"ok": True, "latency_ms": 0, "detail": "câmera simulada", "url": self.redacted_url(cam)}


class GenericRtspAdapter(CameraAdapter):
    name = "rtsp"
    label = "RTSP genérico"

    def live_url(self, cam: CameraConfig) -> str:
        if cam.rtsp_url:
            u = urlsplit(cam.rtsp_url)
            if u.scheme not in ("rtsp", "rtsps"):
                raise CameraConfigError("a URL deve começar com rtsp://")
            check_host_allowed(u.hostname or "", self.allow_public_hosts)
            return cam.rtsp_url
        if not cam.host:
            raise CameraConfigError("informe host ou URL RTSP")
        check_host_allowed(cam.host, self.allow_public_hosts)
        auth = ""
        if cam.username:
            auth = quote(cam.username, safe="") + ":" + quote(cam.password or "", safe="") + "@"
        path = cam.rtsp_path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return f"rtsp://{auth}{cam.host}:{cam.port or 554}{path}"


class IntelbrasAdapter(GenericRtspAdapter):
    """Intelbras (câmeras IP / NVR / DVR).

    Usa o padrão de URL RTSP documentado em diversos manuais Intelbras:
        rtsp://usuario:senha@IP:554/cam/realmonitor?channel=N&subtype=S
    (subtype 0 = stream principal, 1 = extra). O padrão PRECISA ser validado no
    equipamento real — modelos e firmwares diferentes podem variar.
    """

    name = "intelbras"
    label = "Intelbras (RTSP /cam/realmonitor)"

    def live_url(self, cam: CameraConfig) -> str:
        if cam.rtsp_url or cam.rtsp_path:
            return super().live_url(cam)
        if not cam.host:
            raise CameraConfigError("informe o IP do equipamento Intelbras")
        check_host_allowed(cam.host, self.allow_public_hosts)
        auth = ""
        if cam.username:
            auth = quote(cam.username, safe="") + ":" + quote(cam.password or "", safe="") + "@"
        return (f"rtsp://{auth}{cam.host}:{cam.port or 554}/cam/realmonitor"
                f"?channel={int(cam.channel)}&subtype={int(cam.subtype)}")

    def playback_url(self, cam: CameraConfig, start: float, end: float) -> str | None:
        """Estratégia B — reprodução de gravação histórica (NÃO validada).

        Padrão usado por equipamentos com firmware compatível:
            /cam/playback?channel=N&starttime=YYYY_MM_DD_HH_MM_SS&endtime=...
        Só é usada se `supports_playback` for habilitado pelo administrador após
        teste no equipamento (ver docs/INTELBRAS.md).
        """
        if not cam.supports_playback or not cam.host:
            return None
        fmt = "%Y_%m_%d_%H_%M_%S"
        s = datetime.fromtimestamp(start).strftime(fmt)
        e = datetime.fromtimestamp(end).strftime(fmt)
        auth = ""
        if cam.username:
            auth = quote(cam.username, safe="") + ":" + quote(cam.password or "", safe="") + "@"
        return (f"rtsp://{auth}{cam.host}:{cam.port or 554}/cam/playback"
                f"?channel={int(cam.channel)}&starttime={s}&endtime={e}")


ADAPTERS: dict[str, type[CameraAdapter]] = {
    a.name: a for a in (DemoAdapter, GenericRtspAdapter, IntelbrasAdapter)
}


def get_adapter(name: str, allow_public_hosts: bool = False) -> CameraAdapter:
    cls = ADAPTERS.get(name)
    if not cls:
        raise CameraConfigError(f"adaptador desconhecido: {name}")
    return cls(allow_public_hosts=allow_public_hosts)
