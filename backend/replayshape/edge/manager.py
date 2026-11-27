"""Gerencia um buffer circular por câmera ativa (Replay Shape Edge)."""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from ..db import now
from ..integrations.cameras.base import CameraConfig, CameraConfigError, get_adapter
from .buffer import SegmentRingBuffer

log = logging.getLogger("replayshape.edge")


def camera_config_from_row(row: dict, box) -> CameraConfig:
    return CameraConfig(
        id=row["id"], name=row["name"], area_label=row["area_label"], adapter=row["adapter"],
        host=row.get("host"), port=row.get("port"), username=row.get("username"),
        password=box.decrypt(row.get("password_enc")), rtsp_path=row.get("rtsp_path"),
        rtsp_url=box.decrypt(row.get("rtsp_url_enc")), channel=row.get("channel") or 1,
        subtype=row.get("subtype") or 0, fps=row.get("fps"), resolution=row.get("resolution"),
        supports_playback=bool(row.get("supports_playback")),
    )


class BufferManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.buffers: dict[str, SegmentRingBuffer] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.Lock()

    def _fingerprint(self, row: dict) -> str:
        keys = ("adapter", "host", "port", "username", "password_enc", "rtsp_path", "rtsp_url_enc",
                "channel", "subtype", "fps", "resolution", "area_label")
        return hashlib.sha256(json.dumps([row.get(k) for k in keys]).encode()).hexdigest()

    def _on_status(self, camera_id: str, state: str, detail: str) -> None:
        status = {"online": "online", "starting": "starting", "reconnecting": "offline",
                  "offline": "offline", "error": "error", "stopped": "disabled"}.get(state, state)
        vals = {"status": status, "status_detail": detail[:500] if detail else None, "updated_at": now()}
        if status == "online":
            vals["last_seen_at"] = now()
        try:
            self.ctx.db.update("cameras", camera_id, vals)
        except Exception:
            log.exception("falha ao atualizar status da câmera")

    def sync(self) -> None:
        """Inicia/para/reinicia buffers conforme o cadastro de câmeras."""
        if not self.ctx.settings.edge_enabled:
            return
        rows = self.ctx.db.all(
            "SELECT c.* FROM cameras c JOIN establishments e ON e.id = c.establishment_id "
            "WHERE c.active = 1 AND e.active = 1"
        )
        wanted = {r["id"]: r for r in rows}
        with self._lock:
            for cid in list(self.buffers):
                if cid not in wanted or self._fingerprints.get(cid) != self._fingerprint(wanted[cid]):
                    self.buffers.pop(cid).stop()
                    self._fingerprints.pop(cid, None)
                    if cid not in wanted:
                        self.ctx.db.update("cameras", cid, {"status": "disabled", "updated_at": now()})
            for cid, row in wanted.items():
                if cid in self.buffers:
                    continue
                try:
                    cfg = camera_config_from_row(row, self.ctx.box)
                    adapter = get_adapter(cfg.adapter, self.ctx.settings.allow_public_camera_hosts)
                    if cfg.adapter != "demo":
                        adapter.live_url(cfg)  # valida host/SSRF antes de iniciar
                except CameraConfigError as e:
                    self._on_status(cid, "error", str(e))
                    continue
                font = self.ctx.settings.font_file
                buf = SegmentRingBuffer(
                    camera_id=cid,
                    directory=self.ctx.settings.buffers_dir / cid,
                    source_factory=lambda wd, a=adapter, c=cfg, f=font: a.source(c, wd, f),
                    ffmpeg_bin=self.ctx.settings.ffmpeg_bin,
                    segment_seconds=self.ctx.settings.segment_seconds,
                    buffer_seconds=lambda: self.ctx.store.get("buffer_seconds"),
                    on_status=self._on_status,
                    fps_hint=cfg.fps or 25,
                )
                self.buffers[cid] = buf
                self._fingerprints[cid] = self._fingerprint(row)
                buf.start()

    def get(self, camera_id: str) -> SegmentRingBuffer | None:
        return self.buffers.get(camera_id)

    def snapshot(self) -> dict[str, dict]:
        return {cid: b.snapshot() for cid, b in list(self.buffers.items())}

    def persist_snapshots(self) -> None:
        for cid, s in self.snapshot().items():
            self.ctx.db.execute(
                "INSERT INTO camera_buffers(camera_id, state, segments, seconds_buffered, oldest_at, newest_at, "
                "restarts, last_error, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                "state=excluded.state, segments=excluded.segments, seconds_buffered=excluded.seconds_buffered, "
                "oldest_at=excluded.oldest_at, newest_at=excluded.newest_at, restarts=excluded.restarts, "
                "last_error=excluded.last_error, updated_at=excluded.updated_at",
                (cid, s["state"], s["segments"], s["seconds_buffered"], s["oldest_at"], s["newest_at"],
                 s["restarts"], (s["last_error"] or "")[:500], now()),
            )

    def stop_all(self) -> None:
        with self._lock:
            for b in self.buffers.values():
                b.stop()
            self.buffers.clear()
            self._fingerprints.clear()
