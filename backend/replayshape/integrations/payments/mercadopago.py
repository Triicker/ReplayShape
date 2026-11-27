"""Mercado Pago — Pix via API de Pagamentos (/v1/payments).

Fontes consultadas (nov/2026), documentação oficial:
* Pix (Checkout API / Payments): POST https://api.mercadopago.com/v1/payments com
  `payment_method_id="pix"`, header `X-Idempotency-Key`, `date_of_expiration`
  opcional (30 min a 30 dias); resposta em
  `point_of_interaction.transaction_data.{qr_code, qr_code_base64, ticket_url}`.
* Reembolso Pix: POST /v1/payments/{id}/refunds (com X-Idempotency-Key).
* Webhooks: header `x-signature` no formato `ts=...,v1=...` e `x-request-id`.

PENDENTE DE VALIDAÇÃO EM SANDBOX: o template do manifesto da assinatura
(`id:{data.id};request-id:{x-request-id};ts:{ts};`, HMAC-SHA256) não constava
no trecho da página lido; ele segue o padrão publicado anteriormente pelo
Mercado Pago e precisa ser confirmado com credenciais de teste.
A documentação informa ainda que a MP recomenda a API de Orders para novas
integrações — avaliar migração (ver docs/MERCADOPAGO.md).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx

from .base import ChargeResult, Notification, PaymentInfo, PaymentProvider, ProviderError, WebhookRejected

_STATUS_MAP = {
    "approved": "approved",
    "authorized": "pending",
    "pending": "pending",
    "in_process": "pending",
    "in_mediation": "pending",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "charged_back": "charged_back",
}

BRT = timezone(timedelta(hours=-3))


class MercadoPagoProvider(PaymentProvider):
    name = "mercadopago"

    def __init__(self, access_token: str, webhook_secret: str, api_base: str = "https://api.mercadopago.com",
                 payer_email: str = "", client: httpx.Client | None = None):
        if not access_token:
            raise ProviderError("RS_MP_ACCESS_TOKEN não configurado")
        self.token = access_token
        self.webhook_secret = webhook_secret
        self.api = api_base.rstrip("/")
        self.payer_email = payer_email or "comprador@replayshape.invalid"
        self.client = client or httpx.Client(timeout=20)

    def _headers(self, idem: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if idem:
            h["X-Idempotency-Key"] = idem
        return h

    def create_charge(self, *, order_id, amount_cents, currency, description, idempotency_key,
                      expires_at, notification_url) -> ChargeResult:
        if currency != "BRL":
            raise ProviderError("Pix aceita apenas BRL")
        exp = datetime.fromtimestamp(expires_at, BRT).isoformat(timespec="milliseconds")
        body = {
            "transaction_amount": round(amount_cents / 100, 2),
            "description": description[:200],
            "payment_method_id": "pix",
            "payer": {"email": self.payer_email},
            "external_reference": order_id,
            "date_of_expiration": exp,
        }
        if notification_url.startswith("https://"):
            body["notification_url"] = notification_url
        r = self.client.post(f"{self.api}/v1/payments", headers=self._headers(idempotency_key), json=body)
        if r.status_code >= 300:
            raise ProviderError(f"Mercado Pago recusou a criação (HTTP {r.status_code})")
        data = r.json()
        td = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
        return ChargeResult(
            provider_payment_id=str(data["id"]),
            status=_STATUS_MAP.get(data.get("status", ""), "pending"),
            pix_copy_paste=td.get("qr_code") or "",
            qr_png_base64=td.get("qr_code_base64"),
            ticket_url=td.get("ticket_url"),
            expires_at=expires_at,
        )

    def get_payment(self, provider_payment_id: str) -> PaymentInfo:
        if not provider_payment_id.isdigit():
            raise ProviderError("id de pagamento inválido")
        r = self.client.get(f"{self.api}/v1/payments/{provider_payment_id}", headers=self._headers())
        if r.status_code == 404:
            raise ProviderError("pagamento não encontrado")
        if r.status_code >= 300:
            raise ProviderError(f"consulta falhou (HTTP {r.status_code})")
        d = r.json()
        fee = sum(int(round(float(f.get("amount", 0)) * 100)) for f in d.get("fee_details") or [])
        return PaymentInfo(
            provider_payment_id=str(d["id"]),
            status=_STATUS_MAP.get(d.get("status", ""), "pending"),
            status_detail=d.get("status_detail") or "",
            amount_cents=int(round(float(d.get("transaction_amount") or 0) * 100)),
            currency=d.get("currency_id") or "",
            external_reference=d.get("external_reference") or "",
            fee_cents=fee or None,
            raw={k: d.get(k) for k in ("id", "status", "status_detail", "date_approved")},
        )

    def parse_notification(self, headers: dict, query: dict, body: bytes) -> Notification:
        if not self.webhook_secret:
            raise WebhookRejected("segredo de webhook não configurado")
        sig = headers.get("x-signature", "")
        request_id = headers.get("x-request-id", "")
        parts = {}
        for p in sig.split(","):
            k, _, v = p.strip().partition("=")
            parts[k] = v
        ts, v1 = parts.get("ts", ""), parts.get("v1", "")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise WebhookRejected("corpo inválido") from e
        data_id = str(query.get("data.id") or (payload.get("data") or {}).get("id") or "")
        if data_id.isalnum():
            data_id = data_id.lower()
        manifest = ""
        if data_id:
            manifest += f"id:{data_id};"
        if request_id:
            manifest += f"request-id:{request_id};"
        if ts:
            manifest += f"ts:{ts};"
        expected = hmac.new(self.webhook_secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        if not (ts and v1 and hmac.compare_digest(expected, v1)):
            raise WebhookRejected("assinatura inválida")
        topic = payload.get("type") or query.get("type") or query.get("topic") or ""
        event_key = f"mp:{payload.get('id') or request_id or data_id + ':' + ts}"
        return Notification(event_key=event_key, provider_payment_id=data_id or None, type=topic,
                            payload=body.decode(errors="replace")[:10000])

    def refund(self, provider_payment_id: str, idempotency_key: str) -> str:
        r = self.client.post(
            f"{self.api}/v1/payments/{provider_payment_id}/refunds",
            headers={**self._headers(idempotency_key), "X-Render-In-Process-Refunds": "true"},
            json={},
        )
        if r.status_code >= 300:
            raise ProviderError(f"reembolso falhou (HTTP {r.status_code})")
        return "done" if r.json().get("status") == "approved" else "requested"

    def cancel(self, provider_payment_id: str) -> None:
        self.client.put(f"{self.api}/v1/payments/{provider_payment_id}", headers=self._headers(),
                        json={"status": "cancelled"})
