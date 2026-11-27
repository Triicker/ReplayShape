"""Buffer circular por câmera (Replay Shape Edge).

Um processo FFmpeg por câmera recebe o stream (RTSP ou simulado) e grava
segmentos curtos (.ts, ~2 s). O FFmpeg anota no `list_<run>.csv` o início e o
fim de cada segmento em tempo do stream; o buffer converte para horário de
parede (epoch) com uma âncora por execução:

    âncora = min(horário_observado_do_registro - fim_relativo)   (janela deslizante)

Como o registro só aparece depois que o segmento termina, o mínimo converge
para o atraso real mais baixo do pipeline. Segmentos mais antigos que a janela
configurada (padrão 120 s) são apagados, exceto os protegidos ("locks") por
um recorte em andamento.

O buffer NÃO interfere na gravação da Intelbras: ele só abre uma conexão RTSP
de leitura adicional (atenção ao limite de conexões simultâneas do equipamento).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..integrations.cameras.base import SourceSpec
from ..video.ffmpeg import CREATE_NO_WINDOW

log = logging.getLogger("replayshape.edge.buffer")
_REDACT = re.compile(r"(rtsps?://)[^@/\s]+@", re.I)  # nunca registrar usuário/senha


class BufferError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Segment:
    seq: int
    path: Path
    wall_start: float
    wall_end: float
    run: int = 0
    rel_start: float = 0.0
    rel_end: float = 0.0
    locks: int = 0

    @property
    def duration(self) -> float:
        return self.wall_end - self.wall_start


@dataclass
class _Run:
    number: int
    proc: subprocess.Popen | None
    list_path: Path
    offset: int = 0
    partial: str = ""
    anchor_obs: deque = field(default_factory=lambda: deque(maxlen=30))
    started_at: float = field(default_factory=time.time)
    last_segment_at: float = field(default_factory=time.time)


class SegmentRingBuffer:
    def __init__(
        self,
        camera_id: str,
        directory: Path,
        source_factory: Callable[[Path], SourceSpec],
        ffmpeg_bin: str = "ffmpeg",
        segment_seconds: int = 2,
        buffer_seconds: Callable[[], float] | float = 120.0,
        on_status: Callable[[str, str, str], None] | None = None,
        fps_hint: int = 25,
    ):
        self.camera_id = camera_id
        self.dir = Path(directory)
        self.source_factory = source_factory
        self.ffmpeg_bin = ffmpeg_bin
        self.segment_seconds = segment_seconds
        self._buffer_seconds = buffer_seconds
        self.on_status = on_status
        self.fps_hint = fps_hint

        self._segments: list[Segment] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run: _Run | None = None
        self._next_seq = 0
        self.restarts = 0
        self.state = "stopped"
        self.last_error = ""

    # --- ciclo de vida ---------------------------------------------------
    @property
    def buffer_seconds(self) -> float:
        b = self._buffer_seconds
        return float(b() if callable(b) else b)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name=f"buffer-{self.camera_id[:8]}", daemon=True)
        self._thread.start()

    def stop(self, wipe: bool = True) -> None:
        self._stop.set()
        self._kill_proc()
        if self._thread:
            self._thread.join(timeout=10)
        self._set_state("stopped", "")
        if wipe:
            with self._cond:
                self._segments.clear()
            shutil.rmtree(self.dir, ignore_errors=True)

    def _set_state(self, state: str, detail: str) -> None:
        detail = _REDACT.sub(r"\1***@", detail or "")
        changed = state != self.state or detail != self.last_error
        self.state = state
        if detail:
            self.last_error = detail
        if changed and self.on_status:
            try:
                self.on_status(self.camera_id, state, detail)
            except Exception:  # pragma: no cover
                log.exception("on_status falhou")

    def _kill_proc(self) -> None:
        run = self._run
        if run and run.proc and run.proc.poll() is None:
            try:
                run.proc.terminate()
                run.proc.wait(timeout=5)
            except Exception:
                try:
                    run.proc.kill()
                except Exception:
                    pass

    def _build_cmd(self, spec: SourceSpec, list_path: Path) -> list[str]:
        cmd = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *spec.input_args]
        if spec.needs_encode:
            gop = str(self.fps_hint * self.segment_seconds)
            if spec.filtergraph:
                cmd += ["-filter_complex", spec.filtergraph.replace("\n", ""), "-map", "[v]"]
            else:
                cmd += ["-map", "0:v:0"]
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-b:v", "2500k"]
        else:
            cmd += ["-map", "0:v:0", "-c:v", "copy"]
        cmd += [
            "-an", "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-reset_timestamps", "1", "-segment_format", "mpegts",
            "-segment_start_number", str(self._next_seq),
            "-segment_list", str(list_path), "-segment_list_type", "csv",
            str(self.dir / "seg_%08d.ts"),
        ]
        return cmd

    def _supervise(self) -> None:
        backoff = 1.0
        run_no = 0
        while not self._stop.is_set():
            run_no += 1
            list_path = self.dir / f"list_{run_no}.csv"
            try:
                spec = self.source_factory(self.dir)
                cmd = self._build_cmd(spec, list_path)
                self._set_state("starting" if run_no == 1 else "reconnecting", "")
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as e:
                self._set_state("error", f"falha ao iniciar captura: {type(e).__name__}: {e}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue

            run = _Run(number=run_no, proc=proc, list_path=list_path)
            self._run = run
            stderr_tail: deque[str] = deque(maxlen=20)
            threading.Thread(target=_drain, args=(proc, stderr_tail), daemon=True).start()
            got_any = False
            watchdog = max(12.0, self.segment_seconds * 5.0)
            while not self._stop.is_set():
                n = self._poll_list(run)
                if n:
                    got_any = True
                    backoff = 1.0
                    if self.state != "online":
                        self._set_state("online", "")
                self.prune()
                if proc.poll() is not None:
                    self._poll_list(run)
                    break
                if time.time() - run.last_segment_at > watchdog:
                    stderr_tail.append("watchdog: nenhum segmento novo")
                    self._kill_proc()
                    break
                self._stop.wait(0.2)
            if self._stop.is_set():
                break
            self.restarts += 1
            err = " | ".join(list(stderr_tail)[-3:])
            self._set_state("offline", (err or "stream encerrado")[:500])
            log.warning("buffer %s: processo terminou (rc=%s) %s", self.camera_id, proc.returncode, err)
            self._stop.wait(backoff if got_any else min(backoff * 2, 30))
            backoff = min(backoff * 2, 30)

    def _poll_list(self, run: _Run) -> int:
        try:
            with open(run.list_path, "r", encoding="utf-8") as f:
                f.seek(run.offset)
                data = f.read()
                run.offset = f.tell()
        except FileNotFoundError:
            return 0
        observed = time.time()
        text = run.partial + data
        lines = text.split("\n")
        run.partial = lines.pop()  # última linha pode estar incompleta
        added = 0
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) < 3:
                continue
            name, rel_start, rel_end = parts[0], float(parts[1]), float(parts[2])
            path = self.dir / Path(name).name
            run.anchor_obs.append(observed - rel_end)
            anchor = min(run.anchor_obs)
            seq = _seq_from_name(path.name)
            with self._cond:
                self._next_seq = max(self._next_seq, seq + 1)
                for s in self._segments:  # refina segmentos do mesmo run
                    if s.run == run.number:
                        s.wall_start = anchor + s.rel_start
                        s.wall_end = anchor + s.rel_end
                self._segments.append(Segment(
                    seq=seq, path=path, wall_start=anchor + rel_start, wall_end=anchor + rel_end,
                    run=run.number, rel_start=rel_start, rel_end=rel_end,
                ))
                self._cond.notify_all()
            run.last_segment_at = observed
            added += 1
        return added

    # --- API usada pelo processamento -------------------------------------
    def ingest_segment(self, path: Path, wall_start: float, wall_end: float) -> None:
        """Adiciona um segmento já gravado (usado em testes e importações)."""
        with self._cond:
            seq = self._next_seq
            self._next_seq += 1
            self._segments.append(Segment(seq=seq, path=Path(path), wall_start=wall_start, wall_end=wall_end, run=-1))
            self._segments.sort(key=lambda s: s.wall_start)
            self._cond.notify_all()

    def newest_end(self) -> float | None:
        with self._cond:
            return self._segments[-1].wall_end if self._segments else None

    def acquire_window(self, start: float, end: float, wait_timeout: float = 8.0,
                       tolerance: float = 1.0, max_gap: float = 0.6) -> list[Segment]:
        """Retorna (com lock) os segmentos que cobrem [start, end].

        Espera até `wait_timeout` o segmento que contém `end` ser finalizado.
        """
        deadline = time.time() + wait_timeout
        with self._cond:
            while True:
                newest = self._segments[-1].wall_end if self._segments else None
                if newest is not None and newest >= end - 0.05:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    if not self._segments:
                        raise BufferError("CAMERA_OFFLINE", "nenhum vídeo recebido da câmera")
                    if self.state not in ("online",):
                        raise BufferError("CAMERA_OFFLINE", self.last_error or "câmera sem sinal")
                    raise BufferError("BUFFER_TIMEOUT", "o trecho final ainda não foi gravado")
                self._cond.wait(min(remaining, 0.5))
            chosen = [s for s in self._segments if s.wall_end > start + 0.01 and s.wall_start < end - 0.01]
            if not chosen:
                raise BufferError("INSUFFICIENT_BUFFER", "sem gravação para o intervalo")
            if chosen[0].wall_start > start + tolerance:
                have = end - chosen[0].wall_start
                raise BufferError(
                    "INSUFFICIENT_BUFFER",
                    f"gravação disponível: {max(0.0, have):.0f}s de {end - start:.0f}s",
                )
            if chosen[-1].wall_end < end - tolerance:
                raise BufferError("INSUFFICIENT_BUFFER", "o fim do intervalo não foi gravado")
            for a, b in zip(chosen, chosen[1:]):
                if b.wall_start - a.wall_end > max_gap:
                    raise BufferError("BUFFER_GAP", f"falha de gravação de {b.wall_start - a.wall_end:.1f}s")
            for s in chosen:
                if not s.path.exists():
                    raise BufferError("SEGMENT_MISSING", s.path.name)
                s.locks += 1
            return list(chosen)

    def release(self, segments: list[Segment]) -> None:
        with self._cond:
            for s in segments:
                s.locks = max(0, s.locks - 1)

    def prune(self) -> int:
        limit = time.time() - self.buffer_seconds
        removed = 0
        with self._cond:
            keep = []
            for s in self._segments:
                if s.wall_end < limit and s.locks == 0:
                    try:
                        s.path.unlink(missing_ok=True)
                    except OSError:
                        keep.append(s)
                        continue
                    removed += 1
                else:
                    keep.append(s)
            self._segments = keep
        return removed

    def snapshot(self) -> dict:
        with self._cond:
            segs = list(self._segments)
        oldest = segs[0].wall_start if segs else None
        newest = segs[-1].wall_end if segs else None
        return {
            "state": self.state,
            "segments": len(segs),
            "seconds_buffered": round((newest - oldest), 1) if segs else 0.0,
            "oldest_at": oldest,
            "newest_at": newest,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "locked": sum(1 for s in segs if s.locks),
        }


def _seq_from_name(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def _drain(proc: subprocess.Popen, tail: deque) -> None:
    try:
        for line in iter(proc.stderr.readline, b""):
            s = line.decode(errors="replace").strip()
            if s:
                tail.append(s[:300])
    except Exception:
        pass


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
