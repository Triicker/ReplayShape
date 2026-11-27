"""Interface modular de provedores de pagamento.

Regra de ouro: o status do pagamento NUNCA é aceito do frontend nem apenas do
corpo do webhook. O webhook só dispara uma consulta independente
(`get_payment`) à API do provedor, e o resultado é validado (valor, moeda e
referência do pedido) antes de liberar o vídeo.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    pass


class WebhookRejected(Exception):
    pass


@dataclass
class ChargeResult:
    provider_payment_id: str
    status: str
    pix_copy_paste: str
    qr_png_base64: str | None = None
    ticket_url: str | None = None
    expires_at: float | None = None


@dataclass
class PaymentInfo:
    provider_payment_id: str
    status: str  # pending|approved|rejected|cancelled|expired|refunded|charged_back
    amount_cents: int
    currency: str
    external_reference: str
    status_detail: str = ""
    fee_cents: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Notification:
    event_key: str
    provider_payment_id: str | None
    type: str
    payload: str


class PaymentProvider:
    name = "base"
    is_simulated = False

    def create_charge(self, *, order_id: str, amount_cents: int, currency: str, description: str,
                      idempotency_key: str, expires_at: float, notification_url: str) -> ChargeResult:
        raise NotImplementedError

    def get_payment(self, provider_payment_id: str) -> PaymentInfo:
        raise NotImplementedError

    def parse_notification(self, headers: dict, query: dict, body: bytes) -> Notification:
        raise NotImplementedError

    def refund(self, provider_payment_id: str, idempotency_key: str) -> str:
        raise NotImplementedError

    def cancel(self, provider_payment_id: str) -> None:
        raise NotImplementedError


def get_provider(settings, db) -> PaymentProvider:
    if settings.payment_provider == "mock":
        if settings.is_production:
            raise RuntimeError("provedor simulado bloqueado em produção")
        from .mock import MockPixProvider
        return MockPixProvider(db, settings.secret_key)
    if settings.payment_provider == "mercadopago":
        from .mercadopago import MercadoPagoProvider
        return MercadoPagoProvider(
            access_token=settings.mp_access_token,
            webhook_secret=settings.mp_webhook_secret,
            api_base=settings.mp_api_base,
            payer_email=settings.mp_payer_email,
        )
    raise RuntimeError(f"provedor de pagamento desconhecido: {settings.payment_provider}")
