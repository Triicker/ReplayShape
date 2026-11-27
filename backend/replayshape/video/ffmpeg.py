"""Processamento com FFmpeg/FFprobe.

Regras de segurança:
* FFmpeg é sempre chamado com lista de argumentos (sem shell) — não há
  interpretação de metacaracteres.
* Nenhum valor vindo do público entra na linha de comando. Textos de marca
  d'água vão por `textfile=` (arquivo), evitando escaping de filtergraph.
* Caminhos de arquivos são gerados pelo sistema (UUIDs).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

QUALITY_PROFILES: dict[str, dict] = {
    # nome: altura, bitrate (kbps), preset x264
    "economy": {"label": "Econômico (480p)", "height": 480, "kbps": 800},
    "balanced": {"label": "Equilibrado (720p)", "height": 720, "kbps": 1500},
    "high": {"label": "Alta qualidade (1080p)", "height": 1080, "kbps": 3000},
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class VideoError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ProbeInfo:
    duration: float
    codec: str
    width: int
    height: int
    fps: float
    has_audio: bool
    format_name: str


def filter_path(p: str | Path) -> str:
    """Escapa um caminho para uso como valor de opção em filtergraph."""
    s = str(p).replace("\\", "/")
    if "'" in s:
        raise VideoError("UNSAFE_PATH", "caminho com aspas não suportado em filtros")
    return "'" + s.replace(":", "\\:") + "'"


def run(cmd: list[str], timeout: float, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        raise VideoError("FFMPEG_TIMEOUT", f"tempo esgotado após {timeout}s") from e
    except FileNotFoundError as e:
        raise VideoError("FFMPEG_NOT_FOUND", str(cmd[0])) from e


def probe(ffprobe: str, path: str | Path, timeout: float = 30) -> ProbeInfo:
    cp = run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise VideoError("PROBE_FAILED", cp.stderr.decode(errors="replace")[-500:])
    data = json.loads(cp.stdout or b"{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise VideoError("NO_VIDEO_STREAM", "arquivo sem trilha de vídeo")
    fmt = data.get("format", {})
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return ProbeInfo(
        duration=dur,
        codec=v.get("codec_name", ""),
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
        fps=fps,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        format_name=fmt.get("format_name", ""),
    )


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_cut_command(
    ffmpeg: str,
    concat_list: Path,
    out_path: Path,
    offset: float,
    duration: float,
    profile: str = "balanced",
    fast_copy: bool = False,
    watermark_textfile: Path | None = None,
    font_file: str | None = None,
) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", str(concat_list)]
    if fast_copy:
        # Sem recodificação: rápido e barato, mas o corte fica alinhado a keyframes.
        cmd += ["-ss", f"{offset:.3f}", "-t", f"{duration:.3f}", "-c", "copy", "-an",
                "-movflags", "+faststart", "-f", "mp4", str(out_path)]
        return cmd
    q = QUALITY_PROFILES.get(profile, QUALITY_PROFILES["balanced"])
    kbps = q["kbps"]
    vf = [f"scale=-2:'min({q['height']},ih)'", "format=yuv420p"]
    if watermark_textfile and font_file:
        vf.append(
            "drawtext=fontfile=" + filter_path(font_file)
            + ":textfile=" + filter_path(watermark_textfile)
            + ":fontcolor=white@0.85:fontsize=h/28:box=1:boxcolor=black@0.35:boxborderw=8"
            + ":x=w-tw-h/40:y=h/40"
        )
    cmd += [
        "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level:v", "4.1",
        "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k",
        "-g", "50", "-an", "-movflags", "+faststart", "-f", "mp4", str(out_path),
    ]
    return cmd


def write_concat_list(path: Path, files: list[Path]) -> None:
    lines = ["ffconcat version 1.0"]
    for f in files:
        s = str(Path(f).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{s}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_output(ffprobe: str, path: Path, expected_duration: float, tolerance: float = 1.0) -> ProbeInfo:
    if not path.exists() or path.stat().st_size < 1024:
        raise VideoError("OUTPUT_MISSING", "arquivo de saída vazio ou ausente")
    info = probe(ffprobe, path)
    if info.codec not in ("h264", "hevc"):
        raise VideoError("BAD_CODEC", info.codec)
    if abs(info.duration - expected_duration) > tolerance:
        raise VideoError(
            "BAD_DURATION", f"duração {info.duration:.2f}s, esperado {expected_duration:.2f}s"
        )
    return info


# --- fonte sintética "mesa de sinuca" para o modo demonstração -----------
def demo_source_filtergraph(label_textfile: Path | None, font_file: str | None, width: int, height: int) -> str:
    rail = max(24, height // 15)
    ball = max(20, height // 16)
    r = ball // 2
    inner = rail + 22
    parts = [
        f"[1]format=rgba,geq=r=245:g=245:b=238:a='if(lte(hypot(X-{r},Y-{r}),{r - 1}),255,0)'[w]",
        f"[2]format=rgba,geq=r=250:g=204:b=21:a='if(lte(hypot(X-{r},Y-{r}),{r - 1}),255,0)'[y]",
        f"[3]format=rgba,geq=r=220:g=38:b=38:a='if(lte(hypot(X-{r},Y-{r}),{r - 1}),255,0)'[rd]",
        f"[0]drawbox=x=0:y=0:w=iw:h=ih:color=0x5b3a1e:t={rail},"
        f"drawbox=x={rail}:y={rail}:w=iw-{2 * rail}:h=ih-{2 * rail}:color=0x0b3d26:t=6[t]",
        f"[t][w]overlay=x='{inner}+abs(mod(t*310,2*(W-{2 * inner}-w))-(W-{2 * inner}-w))'"
        f":y='{inner}+abs(mod(t*190,2*(H-{2 * inner}-h))-(H-{2 * inner}-h))'[a]",
        f"[a][y]overlay=x='{inner}+abs(mod(t*170+400,2*(W-{2 * inner}-w))-(W-{2 * inner}-w))'"
        f":y='{inner}+abs(mod(t*260+100,2*(H-{2 * inner}-h))-(H-{2 * inner}-h))'[b]",
    ]
    last = (
        f"[b][rd]overlay=x='{inner}+abs(mod(t*120+800,2*(W-{2 * inner}-w))-(W-{2 * inner}-w))'"
        f":y='{inner}+abs(mod(t*90+300,2*(H-{2 * inner}-h))-(H-{2 * inner}-h))'"
    )
    if font_file:
        fp = filter_path(font_file)
        fs = max(16, height // 26)
        if label_textfile:
            last += (f",drawtext=fontfile={fp}:textfile={filter_path(label_textfile)}"
                     f":fontcolor=white@0.85:fontsize={fs}:x={inner}:y={inner}")
        last += (f",drawtext=fontfile={fp}:text='%{{localtime\\:%d/%m/%Y %T}}'"
                 f":fontcolor=white:fontsize={int(fs * 1.4)}:box=1:boxcolor=black@0.55:boxborderw=10"
                 f":x=w-tw-{inner}:y=h-th-{inner}")
    parts.append(last + "[v]")
    return ";\n".join(parts)


def demo_source_input_args(fps: int, width: int, height: int) -> list[str]:
    ball = max(20, height // 16)
    return [
        "-re", "-f", "lavfi", "-i", f"color=c=0x0f5132:s={width}x{height}:r={fps}",
        "-f", "lavfi", "-i", f"color=c=white:s={ball}x{ball}:r={fps}",
        "-f", "lavfi", "-i", f"color=c=0xfacc15:s={ball}x{ball}:r={fps}",
        "-f", "lavfi", "-i", f"color=c=0xdc2626:s={ball}x{ball}:r={fps}",
    ]
