"""Indicadores do dashboard executivo."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from ..edge.buffer import dir_size

PAID_LIKE = ("paid", "closed")


def _filters(f: dict, alias: str = "o") -> tuple[str, list]:
    where, params = ["1=1"], []
    if f.get("start"):
        where.append(f"{alias}.created_at >= ?")
        params.append(float(f["start"]))
    if f.get("end"):
        where.append(f"{alias}.created_at < ?")
        params.append(float(f["end"]))
    if f.get("establishment_id"):
        where.append(f"{alias}.establishment_id = ?")
        params.append(f["establishment_id"])
    if f.get("camera_id"):
        where.append(f"{alias}.camera_id = ?")
        params.append(f["camera_id"])
    return " AND ".join(where), params


def dashboard(ctx, f: dict) -> dict:
    db = ctx.db
    w_rr, p_rr = _filters(f, "r")
    w_o, p_o = _filters(f, "o")
    req = db.one(f"SELECT COUNT(*) n, SUM(status='ready') ready, SUM(status='failed') failed, "
                 f"AVG(CASE WHEN status='ready' THEN processing_ms END) avg_ms FROM replay_requests r WHERE {w_rr}",
                 p_rr)
    sold = db.one(
        f"SELECT COUNT(*) n, COALESCE(SUM(o.amount_cents),0) gross FROM orders o "
        f"WHERE {w_o} AND o.status IN ('paid','closed')", p_o)
    fees = db.one(
        f"SELECT COALESCE(SUM(p.fee_cents),0) fee, SUM(p.fee_cents IS NULL) unknown FROM payments p "
        f"JOIN orders o ON o.id = p.order_id WHERE {w_o} AND p.status = 'approved' AND p.duplicate = 0", p_o)
    fee_pct = float(ctx.store.get("payment_fee_percent"))
    # Taxa real quando informada pelo provedor; caso contrário, estimativa pela % configurada.
    est_fee = fees["fee"] if fees["fee"] else round(sold["gross"] * fee_pct / 100)
    by_camera = db.all(
        f"SELECT c.area_label label, e.name establishment, COUNT(*) sold, SUM(o.amount_cents) gross "
        f"FROM orders o JOIN cameras c ON c.id = o.camera_id JOIN establishments e ON e.id = o.establishment_id "
        f"WHERE {w_o} AND o.status IN ('paid','closed') GROUP BY o.camera_id ORDER BY gross DESC", p_o)
    by_est = db.all(
        f"SELECT e.name label, COUNT(*) sold, SUM(o.amount_cents) gross FROM orders o "
        f"JOIN establishments e ON e.id = o.establishment_id WHERE {w_o} AND o.status IN ('paid','closed') "
        f"GROUP BY o.establishment_id ORDER BY gross DESC", p_o)
    hours = [0] * 24
    for r in db.all(f"SELECT o.paid_at FROM orders o WHERE {w_o} AND o.status IN ('paid','closed')", p_o):
        if r["paid_at"]:
            hours[datetime.fromtimestamp(r["paid_at"]).hour] += 1
    failures = db.all(f"SELECT error_code code, COUNT(*) n FROM replay_requests r WHERE {w_rr} AND status='failed' "
                      f"GROUP BY error_code ORDER BY n DESC", p_rr)
    cams = db.one("SELECT SUM(status='online') online, SUM(status!='online' AND active=1) offline FROM cameras")
    generated = req["ready"] or 0
    return {
        "requested": req["n"] or 0,
        "generated": generated,
        "failed": req["failed"] or 0,
        "sold": sold["n"],
        "conversion": round(sold["n"] / generated, 4) if generated else 0,
        "gross_cents": sold["gross"],
        "fees_cents": est_fee,
        "fees_estimated": fees["fee"] == 0,
        "net_cents": sold["gross"] - est_fee,
        "avg_ticket_cents": round(sold["gross"] / sold["n"]) if sold["n"] else 0,
        "avg_processing_ms": int(req["avg_ms"] or 0),
        "by_camera": by_camera,
        "by_establishment": by_est,
        "by_hour": hours,
        "failures": failures,
        "cameras_online": cams["online"] or 0,
        "cameras_offline": cams["offline"] or 0,
        "storage_bytes": {
            "replays": dir_size(ctx.settings.replays_dir),
            "buffers": dir_size(ctx.settings.buffers_dir),
        },
        "disk_low": ctx.disk_low,
    }


def orders_csv(ctx, f: dict) -> str:
    w, p = _filters(f, "o")
    rows = ctx.db.all(
        f"SELECT o.short_code, o.status, o.amount_cents, o.currency, o.created_at, o.paid_at, o.available_until, "
        f"e.name establishment, c.area_label camera, r.status replay_status, r.error_code, o.demo "
        f"FROM orders o JOIN establishments e ON e.id = o.establishment_id JOIN cameras c ON c.id = o.camera_id "
        f"JOIN replay_requests r ON r.id = o.replay_request_id WHERE {w} ORDER BY o.created_at DESC", p)
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(["codigo", "status", "valor", "moeda", "criado_em", "pago_em", "disponivel_ate", "estabelecimento",
                 "camera", "status_replay", "erro", "demonstracao"])

    def fmt(ts):
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    def safe(v):  # evita injeção de fórmulas em planilhas
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    for r in rows:
        wr.writerow([r["short_code"], r["status"], f"{r['amount_cents'] / 100:.2f}".replace(".", ","),
                     r["currency"], fmt(r["created_at"]), fmt(r["paid_at"]), fmt(r["available_until"]),
                     safe(r["establishment"]), safe(r["camera"]), r["replay_status"], r["error_code"] or "",
                     "sim" if r["demo"] else "nao"])
    return buf.getvalue()
