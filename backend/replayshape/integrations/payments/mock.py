"""Provedor Pix SIMULADO — somente modo demonstração.

Comporta-se como um sistema externo: tem sua própria tabela, gera um código
"copia e cola" propositalmente NÃO pagável e envia notificações assinadas
(HMAC) que passam pelo mesmo fluxo de webhook do provedor real.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from .base import ChargeResult, Notification, PaymentInfo, PaymentProvider, ProviderError, WebhookRejected


class MockPixProvider(PaymentProvider):
    name = "mock"
    is_simulated = True

    def __init__(self, db, secret: str):
        self.db = db
        self._secret = hashlib.sha256(("mock-webhook:" + secret).encode()).hexdigest()

    def create_charge(self, *, order_id, amount_cents, currency, description, idempotency_key,
                      expires_at, notification_url) -> ChargeResult:
        existing = self.db.one("SELECT * FROM mock_pix_charges WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            row = existing
        else:
            now = time.time()
            row = dict(id="mock_" + uuid.uuid4().hex[:20], external_reference=order_id,
                       idempotency_key=idempotency_key, amount_cents=amount_cents, currency=currency,
                       status="pending", expires_at=expires_at, created_at=now, updated_at=now)
            self.db.insert("mock_pix_charges", row)
        code = f"DEMO-REPLAYSHAPE-PIX-NAO-PAGAVEL-{row['id']}"
        return ChargeResult(provider_payment_id=row["id"], status=row["status"], pix_copy_paste=code,
                            expires_at=row["expires_at"])

    def get_payment(self, provider_payment_id: str) -> PaymentInfo:
        row = self.db.one("SELECT * FROM mock_pix_charges WHERE id = ?", (provider_payment_id,))
        if not row:
            raise ProviderError("pagamento não encontrado no provedor simulado")
        status = row["status"]
        if status == "pending" and row["expires_at"] < time.time():
            status = "expired"
        fee = round(row["amount_cents"] * 0.0099) if status == "approved" else None
        return PaymentInfo(provider_payment_id=row["id"], status=status, amount_cents=row["amount_cents"],
                           currency=row["currency"], external_reference=row["external_reference"],
                           fee_cents=fee, raw=row)

    # --- simulação ------------------------------------------------------
    def simulate(self, provider_payment_id: str, status: str) -> tuple[dict, bytes]:
        if status not in {"approved", "rejected", "refunded", "charged_back"}:
            raise ValueError("status simulado inválido")
        self.db.execute("UPDATE mock_pix_charges SET status = ?, updated_at = ? WHERE id = ?",
                        (status, time.time(), provider_payment_id))
        body = json.dumps({"id": uuid.uuid4().hex, "type": "payment",
                           "data": {"id": provider_payment_id}}).encode()
        ts = str(int(time.time()))
        sig = hmac.new(self._secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        return {"x-mock-signature": f"ts={ts},v1={sig}"}, body

    def parse_notification(self, headers: dict, query: dict, body: bytes) -> Notification:
        sig = headers.get("x-mock-signature", "")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        ts, v1 = parts.get("ts", ""), parts.get("v1", "")
        expected = hmac.new(self._secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not ts or not hmac.compare_digest(expected, v1):
            raise WebhookRejected("assinatura inválida")
        if abs(time.time() - int(ts)) > 300:
            raise WebhookRejected("notificação expirada")
        data = json.loads(body)
        return Notification(event_key=f"mock:{data['id']}", provider_payment_id=str(data["data"]["id"]),
                            type=data.get("type", "payment"), payload=body.decode())

    def refund(self, provider_payment_id: str, idempotency_key: str) -> str:
        self.db.execute("UPDATE mock_pix_charges SET status = 'refunded', updated_at = ? WHERE id = ?",
                        (time.time(), provider_payment_id))
        return "done"

    def cancel(self, provider_payment_id: str) -> None:
        self.db.execute(
            "UPDATE mock_pix_charges SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'pending'",
            (time.time(), provider_payment_id))
