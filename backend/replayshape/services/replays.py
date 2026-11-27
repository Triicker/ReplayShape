"""Acionamento e processamento de replays.

Garantias:
* O intervalo é SEMPRE [acionamento - duração, acionamento] — nunca o horário do pagamento.
* A cobrança só pode ser criada depois que o arquivo foi gerado e validado
  (pedido em `awaiting_payment`). Falha na geração => pedido `failed`, sem cobrança.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from ..db import new_id, now
from ..edge.buffer import BufferError
from ..edge.manager import camera_config_from_row
from ..integrations.cameras.base import get_adapter
from ..security import ip_hash, random_token, short_code, token_hash
from ..video import ffmpeg as ff
from .audit import audit

log = logging.getLogger("replayshape.replays")


class TriggerError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def camera_effective(ctx, cam: dict, est: dict) -> dict:
    s = ctx.store.all()
    return {
        "price_cents": cam["price_cents"] if cam.get("price_cents") is not None else est["price_cents"],
        "currency": est["currency"],
        "replay_seconds": int(cam.get("replay_seconds") or s["replay_seconds"]),
        "quality_profile": cam.get("quality_profile") or s["quality_profile"],
        "watermark": bool(cam["watermark"]) if cam.get("watermark") is not None else bool(s["watermark_enabled"]),
    }


def _unique_short_code(ctx) -> str:
    for _ in range(10):
        c = short_code()
        if not ctx.db.one("SELECT 1 FROM orders WHERE short_code = ?", (c,)):
            return c
    raise RuntimeError("não foi possível gerar código único")


def trigger_replay(ctx, camera_id: str, source: str, device_id: str | None = None,
                   client_ip: str | None = None, claimed: bool = True) -> tuple[dict, str]:
    trigger_at = now()  # capturado ANTES de qualquer outra operação
    cam = ctx.db.one("SELECT * FROM cameras WHERE id = ?", (camera_id,))
    if not cam or not cam["active"]:
        raise TriggerError("CAMERA_NOT_FOUND", "Câmera não encontrada ou desativada.", 404)
    est = ctx.db.one("SELECT * FROM establishments WHERE id = ?", (cam["establishment_id"],))
    if not est or not est["active"]:
        raise TriggerError("ESTABLISHMENT_INACTIVE", "Estabelecimento indisponível.", 404)
    if ctx.disk_low:
        raise TriggerError("DISK_FULL", "Sistema temporariamente sem espaço. Nenhuma cobrança foi feita.", 503)
    eff = camera_effective(ctx, cam, est)
    buf = ctx.buffers.get(camera_id)
    if (buf is None or buf.state != "online") and not cam["supports_playback"]:
        raise TriggerError("CAMERA_OFFLINE", "Câmera sem sinal no momento. Nenhuma cobrança foi feita.", 503)

    duration = eff["replay_seconds"]
    token = random_token()
    req_id, order_id = new_id(), new_id()
    t = now()
    with ctx.db.tx():
        ctx.db.insert("replay_requests", dict(
            id=req_id, camera_id=camera_id, establishment_id=est["id"], source=source, device_id=device_id,
            trigger_at=trigger_at, window_start=trigger_at - duration, window_end=trigger_at,
            duration_s=duration, status="queued", claimed=1 if claimed else 0,
            client_ip_hash=ip_hash(client_ip, ctx.settings.ip_hash_salt), created_at=t, updated_at=t,
        ))
        ctx.db.insert("orders", dict(
            id=order_id, replay_request_id=req_id, establishment_id=est["id"], camera_id=camera_id,
            access_token_hash=token_hash(token), short_code=_unique_short_code(ctx),
            amount_cents=eff["price_cents"], currency=eff["currency"], status="processing",
            demo=1 if ctx.provider.is_simulated else 0, created_at=t, updated_at=t,
        ))
    audit(ctx.db, source if source != "web" else "public", "replay.triggered", "replay_request", req_id,
          actor_id=device_id, camera_id=camera_id)
    ctx.executor.submit(_safe_process, ctx, req_id)
    order = ctx.db.one("SELECT * FROM orders WHERE id = ?", (order_id,))
    return order, token


def _safe_process(ctx, req_id: str) -> None:
    try:
        process_replay(ctx, req_id)
    except Exception:  # pragma: no cover — process_replay já trata erros
        log.exception("processamento inesperado falhou")


def _fail(ctx, req: dict, code: str, detail: str, started: float) -> None:
    t = now()
    with ctx.db.tx():
        ctx.db.update("replay_requests", req["id"], dict(
            status="failed", error_code=code, error_detail=detail[:500],
            processing_ms=int((time.monotonic() - started) * 1000), updated_at=t))
        ctx.db.execute("UPDATE orders SET status = 'failed', updated_at = ? WHERE replay_request_id = ? "
                       "AND status = 'processing'", (t, req["id"]))
    audit(ctx.db, "system", "replay.failed", "replay_request", req["id"], code=code, detail=detail[:200])


def process_replay(ctx, req_id: str) -> None:
    started = time.monotonic()
    req = ctx.db.one("SELECT * FROM replay_requests WHERE id = ?", (req_id,))
    if not req or req["status"] != "queued":
        return
    cam = ctx.db.one("SELECT * FROM cameras WHERE id = ?", (req["camera_id"],))
    est = ctx.db.one("SELECT * FROM establishments WHERE id = ?", (req["establishment_id"],))
    eff = camera_effective(ctx, cam, est)
    settings = ctx.store.all()
    job_dir = ctx.settings.replays_dir / req_id
    job_dir.mkdir(parents=True, exist_ok=True)
    ctx.db.update("replay_requests", req_id, {"status": "capturing", "updated_at": now()})

    buf = ctx.buffers.get(req["camera_id"])
    segments = []
    try:
        duration = float(req["duration_s"])
        out_tmp = job_dir / "replay.tmp.mp4"
        out = job_dir / "replay.mp4"
        wm_file = None
        if eff["watermark"] and ctx.settings.font_file:
            wm_file = job_dir / "watermark.txt"
            when = datetime.fromtimestamp(req["trigger_at"]).strftime("%d/%m/%Y %H:%M")
            wm_file.write_text(f"REPLAY SHAPE  |  {est['name']}  |  {cam['area_label']}  |  {when}",
                               encoding="utf-8")

        if buf is not None:
            try:
                segments = buf.acquire_window(req["window_start"], req["window_end"],
                                              wait_timeout=ctx.settings.capture_wait_timeout)
            except BufferError as e:
                return _fail(ctx, req, e.code, e.detail, started)
            ctx.db.update("replay_requests", req_id, {"status": "processing", "updated_at": now()})
            concat = job_dir / "segments.ffconcat"
            ff.write_concat_list(concat, [s.path for s in segments])
            offset = max(0.0, req["window_start"] - segments[0].wall_start)
            cmd = ff.build_cut_command(
                ctx.settings.ffmpeg_bin, concat, out_tmp, offset, duration,
                profile=eff["quality_profile"], fast_copy=bool(settings["fast_copy"]),
                watermark_textfile=wm_file, font_file=ctx.settings.font_file,
            )
        else:
            # Estratégia B — gravação histórica do DVR/NVR (somente se validada).
            cfg = camera_config_from_row(cam, ctx.box)
            adapter = get_adapter(cfg.adapter, ctx.settings.allow_public_camera_hosts)
            url = adapter.playback_url(cfg, req["window_start"], req["window_end"])
            if not url:
                return _fail(ctx, req, "CAMERA_OFFLINE", "buffer indisponível e playback não habilitado", started)
            ctx.db.update("replay_requests", req_id, {"status": "processing", "updated_at": now()})
            q = ff.QUALITY_PROFILES.get(eff["quality_profile"], ff.QUALITY_PROFILES["balanced"])
            cmd = [ctx.settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-rtsp_transport", "tcp", "-i", url, "-t", f"{duration:.3f}",
                   "-vf", f"scale=-2:'min({q['height']},ih)',format=yuv420p", "-c:v", "libx264",
                   "-preset", "veryfast", "-b:v", f"{q['kbps']}k", "-an", "-movflags", "+faststart",
                   "-f", "mp4", str(out_tmp)]

        cp = ff.run(cmd, timeout=duration * 4 + 90, cwd=job_dir)
        if cp.returncode != 0:
            err = cp.stderr.decode(errors="replace")[-400:]
            return _fail(ctx, req, "PROCESSING_FAILED", err, started)
        info = ff.validate_output(ctx.settings.ffprobe_bin, out_tmp, duration,
                                  tolerance=2.5 if settings["fast_copy"] else 1.0)
        out_tmp.replace(out)
        for extra in ("segments.ffconcat", "watermark.txt"):
            (job_dir / extra).unlink(missing_ok=True)

        t = now()
        deadline = t + int(settings["unpaid_minutes"]) * 60
        grace = int(settings["late_payment_grace_minutes"]) * 60
        with ctx.db.tx():
            ctx.db.insert("replay_files", dict(
                id=new_id(), replay_request_id=req_id, local_path=str(out), size_bytes=out.stat().st_size,
                sha256=ff.sha256_file(out), duration_s=info.duration, width=info.width, height=info.height,
                codec=info.codec, status="private", delete_after=deadline + grace, created_at=t,
            ))
            ctx.db.update("replay_requests", req_id, dict(
                status="ready", processing_ms=int((time.monotonic() - started) * 1000), updated_at=t))
            ctx.db.execute(
                "UPDATE orders SET status = 'awaiting_payment', payment_deadline = ?, updated_at = ? "
                "WHERE replay_request_id = ? AND status = 'processing'", (deadline, t, req_id))
        audit(ctx.db, "system", "replay.ready", "replay_request", req_id,
              size=out.stat().st_size, duration=round(info.duration, 2))
    except ff.VideoError as e:
        _fail(ctx, req, e.code, e.detail, started)
    except Exception as e:
        log.exception("falha no replay %s", req_id)
        _fail(ctx, req, "INTERNAL_ERROR", type(e).__name__, started)
    finally:
        if buf is not None and segments:
            buf.release(segments)
        cur = ctx.db.one("SELECT status FROM replay_requests WHERE id = ?", (req_id,))
        if cur and cur["status"] == "failed":
            shutil.rmtree(job_dir, ignore_errors=True)


def replay_file_for_order(ctx, order: dict) -> dict | None:
    return ctx.db.one("SELECT * FROM replay_files WHERE replay_request_id = ?", (order["replay_request_id"],))


def local_file_ok(rf: dict | None) -> bool:
    return bool(rf and rf["status"] in ("private", "available") and rf["local_path"]
                and Path(rf["local_path"]).exists())
