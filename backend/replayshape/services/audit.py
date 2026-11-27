from __future__ import annotations

import logging

from ..db import Database, dumps, now

log = logging.getLogger("replayshape.audit")


def audit(db: Database, actor_type: str, action: str, entity: str | None = None, entity_id: str | None = None,
          actor_id: str | None = None, **details) -> None:
    """Registra um evento de auditoria. Nunca inclua senhas, tokens ou URLs com credenciais."""
    try:
        db.execute(
            "INSERT INTO audit_logs(at, actor_type, actor_id, action, entity, entity_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), actor_type, actor_id, action, entity, entity_id, dumps(details) if details else None),
        )
    except Exception:  # auditoria nunca deve derrubar o fluxo principal
        log.exception("falha ao gravar auditoria %s", action)
