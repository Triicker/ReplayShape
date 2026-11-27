"""Dados iniciais: usuário administrador e cenário de demonstração."""
from __future__ import annotations

import logging
import secrets

from ..db import new_id, now
from ..security import hash_password
from .audit import audit

log = logging.getLogger("replayshape.seed")


def ensure_admin(ctx) -> str | None:
    """Cria o primeiro superadmin. Retorna a senha gerada (se houver) — nunca versionada."""
    if ctx.db.one("SELECT 1 FROM admin_users LIMIT 1"):
        return None
    email = (ctx.settings.admin_email or "admin@replayshape.local").lower()
    password = ctx.settings.admin_password
    generated = None
    if not password:
        if ctx.settings.is_production:
            raise RuntimeError("Defina RS_ADMIN_EMAIL e RS_ADMIN_PASSWORD para criar o primeiro administrador")
        password = generated = secrets.token_urlsafe(12)
        p = ctx.settings.data_dir / "ADMIN_INITIAL_PASSWORD.txt"
        p.write_text(f"email: {email}\nsenha: {password}\n(apague este arquivo após trocar a senha)\n",
                     encoding="utf-8")
        log.warning("Administrador inicial criado: %s — senha em %s", email, p)
    ctx.db.insert("admin_users", dict(id=new_id(), email=email, password_hash=hash_password(password),
                                      role="superadmin", active=1, created_at=now()))
    audit(ctx.db, "system", "admin.bootstrap", "admin_user", email)
    return generated


DEMO_EST_SLUG = "shape-snooker-club"


def ensure_demo_data(ctx) -> None:
    if not ctx.settings.demo_mode:
        return
    if ctx.db.one("SELECT 1 FROM establishments WHERE slug = ?", (DEMO_EST_SLUG,)):
        return
    t = now()
    est_id = new_id()
    with ctx.db.tx():
        ctx.db.insert("establishments", dict(
            id=est_id, name="Shape Snooker Club (demo)", slug=DEMO_EST_SLUG, active=1, price_cents=500,
            currency="BRL", brand_color="#a3e635", venue_type="sinuca",
            privacy_notice="Este ambiente é filmado continuamente para a geração de replays sob demanda. "
                           "As imagens ficam em um buffer de 2 minutos e são apagadas automaticamente.",
            created_at=t, updated_at=t))
        for i, (label, code, res) in enumerate([
            ("Mesa 01", "demo-mesa-01", "1280x720"),
            ("Mesa 02", "demo-mesa-02", "1280x720"),
        ], start=1):
            ctx.db.insert("cameras", dict(
                id=new_id(), establishment_id=est_id, name=f"Câmera {i:02d}", area_label=label,
                public_code=code, adapter="demo", manufacturer="Simulada", model="Mesa de sinuca sintética",
                resolution=res, fps=25, replay_seconds=60, active=1, status="unknown",
                created_at=t, updated_at=t))
        # Exemplo de cadastro Intelbras (inativo — sem equipamento real nesta demonstração).
        ctx.db.insert("cameras", dict(
            id=new_id(), establishment_id=est_id, name="Câmera Intelbras (exemplo)", area_label="Mesa 03",
            public_code="demo-mesa-03", adapter="intelbras", manufacturer="Intelbras", model="(informar)",
            host="192.168.1.108", port=554, username="admin", channel=1, subtype=0, replay_seconds=60,
            active=0, status="disabled", status_detail="Exemplo — cadastre IP/usuário/senha reais e ative.",
            created_at=t, updated_at=t))
    audit(ctx.db, "system", "demo.seeded", "establishment", est_id)
