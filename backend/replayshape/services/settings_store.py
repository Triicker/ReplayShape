"""Configurações operacionais editáveis pelo painel (tabela system_settings)."""
from __future__ import annotations

import json

from ..db import Database, now

DEFAULTS: dict[str, object] = {
    "buffer_seconds": 120,          # janela do buffer circular
    "replay_seconds": 60,           # duração padrão do replay
    "unpaid_minutes": 30,           # retenção de replay não pago (prazo de pagamento)
    "late_payment_grace_minutes": 10,  # mantém o arquivo após expirar para pagamentos tardios
    "paid_hours": 24,               # disponibilidade após pagamento
    "link_minutes": 15,             # validade de cada link temporário (renovável)
    "quality_profile": "balanced",  # economy | balanced | high
    "fast_copy": False,             # corte sem recodificar (alinhado a keyframes)
    "watermark_enabled": False,
    "default_price_cents": 500,
    "payment_fee_percent": 0.99,    # HIPOTÉTICO — confirmar a taxa real do seu contrato
    "min_free_disk_mb": 1024,
    "web_trigger_cooldown_seconds": 10,
    "device_claim_window_seconds": 180,
}

BOUNDS = {
    "buffer_seconds": (30, 900),
    "replay_seconds": (5, 300),
    "unpaid_minutes": (5, 24 * 60),
    "late_payment_grace_minutes": (0, 24 * 60),
    "paid_hours": (1, 24 * 30),
    "link_minutes": (1, 24 * 60),
    "default_price_cents": (0, 100_000),
    "payment_fee_percent": (0, 20),
    "min_free_disk_mb": (0, 1_000_000),
    "web_trigger_cooldown_seconds": (0, 600),
    "device_claim_window_seconds": (30, 3600),
}


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict:
        out = dict(DEFAULTS)
        for r in self.db.all("SELECT key, value FROM system_settings"):
            if r["key"] in DEFAULTS:
                out[r["key"]] = json.loads(r["value"])
        return out

    def get(self, key: str):
        r = self.db.one("SELECT value FROM system_settings WHERE key = ?", (key,))
        return json.loads(r["value"]) if r else DEFAULTS[key]

    def update(self, values: dict) -> dict:
        clean = {}
        for k, v in values.items():
            if k not in DEFAULTS:
                raise ValueError(f"configuração desconhecida: {k}")
            d = DEFAULTS[k]
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int):
                v = int(v)
            elif isinstance(d, float):
                v = float(v)
            elif k == "quality_profile" and v not in ("economy", "balanced", "high"):
                raise ValueError("perfil de qualidade inválido")
            if k in BOUNDS:
                lo, hi = BOUNDS[k]
                if not (lo <= v <= hi):
                    raise ValueError(f"{k} deve estar entre {lo} e {hi}")
            clean[k] = v
        merged = {**self.all(), **clean}
        if merged["buffer_seconds"] < merged["replay_seconds"] + 10:
            raise ValueError("o buffer deve ser pelo menos 10 s maior que a duração do replay")
        with self.db.tx():
            for k, v in clean.items():
                self.db.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (k, json.dumps(v), now()),
                )
        return merged
