"""Retenção, exclusão automática, espaço em disco e recuperação após reinício."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..db import now
from .audit import audit

log = logging.getLogger("replayshape.cleanup")


def delete_replay_file(ctx, rf: dict, reason: str) -> None:
    if rf.get("local_path"):
        d = Path(rf["local_path"]).parent
        if d.is_dir() and d.parent == ctx.settings.replays_dir:
            shutil.rmtree(d, ignore_errors=True)
        else:
            Path(rf["local_path"]).unlink(missing_ok=True)
    if rf.get("remote_key") and rf.get("remote_status") == "uploaded" and ctx.remote:
        try:
            ctx.remote.delete(rf["remote_key"])
        except Exception as e:
            log.warning("exclusão remota falhou: %s", type(e).__name__)
            return  # tenta novamente na próxima rodada
    ctx.db.update("replay_files", rf["id"], {
        "status": "deleted", "deleted_at": now(), "deleted_reason": rf.get("deleted_reason") or reason,
        "remote_status": "deleted" if rf.get("remote_key") else rf.get("remote_status", "none"),
    })
    audit(ctx.db, "system", "file.deleted", "replay_file", rf["id"], reason=reason)


def run_cleanup(ctx) -> dict:
    t = now()
    stats = {"orders_expired": 0, "orders_closed": 0, "files_deleted": 0, "tokens_deleted": 0}

    # 1. Pedidos não pagos cujo prazo terminou.
    for o in ctx.db.all("SELECT * FROM orders WHERE status = 'awaiting_payment' AND payment_deadline < ?", (t,)):
        ctx.db.update("orders", o["id"], {"status": "expired", "updated_at": t})
        for p in ctx.db.all("SELECT * FROM payments WHERE order_id = ? AND status = 'pending'", (o["id"],)):
            try:
                ctx.provider.cancel(p["provider_payment_id"])
            except Exception:
                pass
            ctx.db.update("payments", p["id"], {"status": "expired", "updated_at": t})
        audit(ctx.db, "system", "order.expired", "order", o["id"])
        stats["orders_expired"] += 1

    # 2. Pedidos pagos cujo prazo de disponibilidade terminou.
    for o in ctx.db.all("SELECT * FROM orders WHERE status = 'paid' AND available_until < ?", (t,)):
        ctx.db.update("orders", o["id"], {"status": "closed", "closed_at": t, "updated_at": t})
        ctx.db.execute("DELETE FROM download_tokens WHERE order_id = ?", (o["id"],))
        stats["orders_closed"] += 1

    # 3. Arquivos vencidos (não pagos após prazo + tolerância; pagos após 24 h).
    for rf in ctx.db.all("SELECT * FROM replay_files WHERE status IN ('private','available') AND delete_after < ?",
                         (t,)):
        order = ctx.db.one("SELECT status FROM orders WHERE replay_request_id = ?", (rf["replay_request_id"],))
        if order and order["status"] == "paid":
            continue  # segurança: nunca apagar um pedido pago ainda vigente
        delete_replay_file(ctx, rf, "retention")
        stats["files_deleted"] += 1

    # 4. Tokens e sessões expirados.
    stats["tokens_deleted"] = ctx.db.execute("DELETE FROM download_tokens WHERE expires_at < ?", (t,)).rowcount
    ctx.db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (t,))

    # 5. Espaço em disco.
    check_disk(ctx)
    return stats


def check_disk(ctx) -> bool:
    try:
        free_mb = shutil.disk_usage(ctx.settings.data_dir).free / (1024 * 1024)
    except OSError:
        return ctx.disk_low
    low = free_mb < float(ctx.store.get("min_free_disk_mb"))
    if low != ctx.disk_low:
        audit(ctx.db, "system", "disk.low" if low else "disk.ok", details_free_mb=int(free_mb))
    ctx.disk_low = low
    return low


def startup_recovery(ctx) -> dict:
    """Executado na inicialização: deixa o banco e o disco consistentes."""
    t = now()
    stats = {"requests_failed": 0, "files_missing": 0, "orphans_removed": 0}
    # Solicitações interrompidas pelo reinício nunca chegaram a ser cobradas.
    for r in ctx.db.all("SELECT id FROM replay_requests WHERE status IN ('queued','capturing','processing')"):
        ctx.db.update("replay_requests", r["id"], {"status": "failed", "error_code": "SYSTEM_RESTART",
                                                   "updated_at": t})
        ctx.db.execute("UPDATE orders SET status = 'failed', updated_at = ? WHERE replay_request_id = ? "
                       "AND status = 'processing'", (t, r["id"]))
        stats["requests_failed"] += 1
    # Arquivos registrados que sumiram do disco.
    for rf in ctx.db.all("SELECT * FROM replay_files WHERE status IN ('private','available')"):
        if not rf["local_path"] or not Path(rf["local_path"]).exists():
            if rf["remote_status"] == "uploaded":
                continue
            ctx.db.update("replay_files", rf["id"], {"status": "missing"})
            o = ctx.db.one("SELECT * FROM orders WHERE replay_request_id = ?", (rf["replay_request_id"],))
            if o and o["status"] == "awaiting_payment":
                ctx.db.update("orders", o["id"], {"status": "failed", "updated_at": t})
            elif o and o["status"] == "paid":
                # Pago, mas o arquivo se perdeu: marca e solicita estorno.
                from .orders import _refund
                ctx.db.update("orders", o["id"], {"status": "paid_unfulfillable", "updated_at": t})
                p = ctx.db.one("SELECT * FROM payments WHERE order_id = ? AND status = 'approved' "
                               "AND duplicate = 0", (o["id"],))
                if p:
                    _refund(ctx, p, "file_lost")
            audit(ctx.db, "system", "file.missing", "replay_file", rf["id"])
            stats["files_missing"] += 1
    # Diretórios órfãos (sem registro) e buffers antigos.
    known = {r["replay_request_id"] for r in ctx.db.all(
        "SELECT replay_request_id FROM replay_files WHERE status IN ('private','available')")}
    for d in ctx.settings.replays_dir.iterdir():
        if d.is_dir() and d.name not in known:
            shutil.rmtree(d, ignore_errors=True)
            stats["orphans_removed"] += 1
    shutil.rmtree(ctx.settings.buffers_dir, ignore_errors=True)
    ctx.settings.buffers_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(ctx.settings.tmp_dir, ignore_errors=True)
    ctx.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    if any(stats.values()):
        audit(ctx.db, "system", "startup.recovery", **stats)
    return stats
